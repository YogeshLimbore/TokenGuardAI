import json
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from opentelemetry import trace
from sqlalchemy.orm import Session

from app.cache.redis_client import get_redis_client
from app.core.config import get_settings
from app.database.connection import get_database_session
from app.database.models import (
    LLMRequestLog,
    ModelRoutingLog,
    Project,
)
from app.dependencies.authentication import get_current_project
from app.observability.context import (
    get_correlation_id,
)
from app.observability.metrics import (
    BUDGET_DECISIONS_TOTAL,
    CACHE_OPERATIONS_TOTAL,
    CHAT_REQUESTS_TOTAL,
    ESTIMATED_COST_USD_TOTAL,
    GOVERNANCE_DECISIONS_TOTAL,
    PII_FINDINGS_TOTAL,
    PROMPT_INJECTION_FINDINGS_TOTAL,
    RATE_LIMIT_REJECTIONS_TOTAL,
    TOKENS_TOTAL,
)
from app.schemas.budget import BudgetDecision
from app.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    CostDetails,
    OptimizationDetails,
    TokenUsage,
)
from app.schemas.reliability import (
    ReliabilityDetails,
)
from app.schemas.routing import (
    FallbackDetails,
    RoutingDecision,
)
from app.services.budget_service import (
    BudgetConfigurationError,
    BudgetService,
)
from app.services.cache_service import CacheService
from app.services.credential_service import (
    CredentialNotFoundError,
)
from app.services.fallback_service import (
    AllProviderAttemptsFailedError,
    FallbackService,
)
from app.services.governance_service import (
    GovernanceService,
)
from app.services.model_policy_service import (
    ModelAccessDeniedError,
)
from app.services.pricing_service import PricingService
from app.services.provider_health_service import (
    ProviderHealthService,
)
from app.services.rate_limit_service import (
    RateLimitExceededError,
    RateLimitService,
)
from app.services.redis_cache_service import (
    RedisCacheError,
    RedisExactCacheService,
)
from app.services.redis_circuit_breaker_service import (
    RedisCircuitBreakerError,
    RedisCircuitBreakerService,
)
from app.services.redis_rate_limit_service import (
    RateLimitExceededError as RedisRateLimitExceededError,
)
from app.services.redis_rate_limit_service import (
    RedisRateLimitError,
    RedisRateLimitService,
)
from app.services.routing_service import (
    RoutingConfigurationError,
    RoutingService,
)
from app.services.security_audit_service import (
    SecurityAuditService,
)
from app.services.semantic_cache_service import (
    SemanticCacheService,
)
from app.services.token_service import TokenService

router = APIRouter(
    prefix="/v1/chat",
    tags=["Chat"],
)

settings = get_settings()

tracer = trace.get_tracer(__name__)


def combine_messages(
    request: ChatCompletionRequest,
) -> str:
    return "\n\n".join(
        f"{message.role}: {message.content}" for message in request.messages
    )


def create_request_log(
    *,
    database: Session,
    project_id: int,
    request_id: str,
    provider: str,
    model: str,
    prompt: str,
    response: str | None,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
    status_value: str,
    error_message: str | None = None,
) -> None:
    log = LLMRequestLog(
        project_id=project_id,
        request_id=request_id,
        provider=provider,
        model=model,
        prompt=prompt,
        response=response,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated_cost_usd,
        latency_ms=latency_ms,
        status=status_value,
        error_message=error_message,
    )

    database.add(log)
    database.commit()


def build_cache_hit_reliability(
    request_id: str,
) -> ReliabilityDetails:
    return ReliabilityDetails(
        correlation_id=(get_correlation_id() or request_id),
        provider_attempts=0,
        retry_count=0,
        circuit_breaker_state="not_applicable",
        provider_health_score=None,
        fallback_used=False,
    )


@router.post(
    "/completions",
    response_model=ChatCompletionResponse,
)
async def create_chat_completion(
    request: ChatCompletionRequest,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> ChatCompletionResponse:
    request_id = str(uuid4())
    started_at = perf_counter()

    with tracer.start_as_current_span("tokenguard.chat_completion") as span:
        span.set_attribute(
            "tokenguard.project_id",
            project.id,
        )

        span.set_attribute(
            "tokenguard.request_id",
            request_id,
        )

        span.set_attribute(
            "tokenguard.auto_route",
            request.auto_route,
        )

        routing_service = RoutingService()

        try:
            route = routing_service.decide(request)
        except RoutingConfigurationError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(error),
            ) from error

        selected_provider_name = route.provider
        selected_model = route.model

        span.set_attribute(
            "llm.provider",
            selected_provider_name,
        )

        span.set_attribute(
            "llm.model",
            selected_model,
        )

        span.set_attribute(
            "tokenguard.routing_tier",
            route.tier,
        )

        # Ollama never needs a vault credential. When automatic routing
        # is what landed the request on Ollama, a supplied credential is
        # simply not applicable to this request rather than a request
        # error -- the caller did not choose Ollama themselves. Manual
        # requests that explicitly select "ollama" while also supplying a
        # credential_id still fail fast, since that combination can only
        # be a caller mistake. Either way, this decision is made explicit
        # in the routing reasons and audit log below, rather than being a
        # silent side effect.
        credential_not_applicable = (
            request.credential_id is not None
            and selected_provider_name == "ollama"
            and route.automatic_routing_used
        )

        routing_reasons = list(route.reasons)

        if credential_not_applicable:
            routing_reasons.append(
                f"credential_id {request.credential_id} was "
                "ignored because the automatically selected "
                "route uses Ollama, which does not require an "
                "API credential."
            )

        routing_decision = RoutingDecision(
            automatic_routing_used=(route.automatic_routing_used),
            tier=route.tier,
            provider=route.provider,
            model=route.model,
            complexity_score=route.complexity_score,
            confidence=route.confidence,
            reasons=routing_reasons,
            original_provider=route.original_provider,
            original_model=route.original_model,
        )

        routing_log = ModelRoutingLog(
            project_id=project.id,
            request_id=request_id,
            automatic_routing_used=(route.automatic_routing_used),
            routing_preference=(request.routing_preference),
            routing_tier=route.tier,
            original_provider=route.original_provider,
            original_model=route.original_model,
            selected_provider=route.provider,
            selected_model=route.model,
            complexity_score=route.complexity_score,
            confidence=route.confidence,
            reasons_json=json.dumps(
                routing_reasons,
                ensure_ascii=False,
            ),
        )

        database.add(routing_log)
        database.commit()

        token_service = TokenService()
        cache_service = CacheService()
        pricing_service = PricingService()
        semantic_cache_service = SemanticCacheService()
        fallback_service = FallbackService()
        budget_service = BudgetService()
        rate_limit_service = RateLimitService()
        provider_health_service = ProviderHealthService()
        governance_service = GovernanceService()
        security_audit_service = SecurityAuditService()

        redis_client = get_redis_client()
        redis_cache_service = RedisExactCacheService(redis_client)
        redis_rate_limit_service = RedisRateLimitService(redis_client)
        circuit_breaker_service = RedisCircuitBreakerService(redis_client)

        if (
            request.credential_id is not None
            and selected_provider_name == "ollama"
            and not route.automatic_routing_used
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("Ollama does not require an API credential."),
            )

        estimated_input_tokens = token_service.estimate_message_tokens(request.messages)

        effective_max_tokens = (
            min(
                request.max_tokens,
                route.maximum_output_tokens,
            )
            if (
                request.max_tokens is not None
                and route.maximum_output_tokens is not None
            )
            else (request.max_tokens or route.maximum_output_tokens)
        )

        requested_output_tokens = effective_max_tokens or 500

        try:
            budget_result = budget_service.evaluate(
                database,
                project=project,
                request_id=request_id,
                provider=selected_provider_name,
                model=selected_model,
                estimated_input_tokens=(estimated_input_tokens),
                requested_output_tokens=(requested_output_tokens),
                request_maximum_cost_usd=(request.maximum_request_cost_usd),
                request_maximum_total_tokens=(request.maximum_total_tokens),
                allow_downgrade=(request.allow_budget_downgrade),
            )
        except BudgetConfigurationError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(error),
            ) from error

        BUDGET_DECISIONS_TOTAL.labels(
            project_id=str(project.id),
            action=budget_result.action,
        ).inc()

        if not budget_result.allowed:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": ("The request exceeded TokenGuard budget limits."),
                    "reasons": budget_result.reasons,
                },
            )

        if budget_result.action == "downgrade":
            route.reasons.append(
                "Budget enforcement changed the route to "
                f"{budget_result.final_provider}/"
                f"{budget_result.final_model}."
            )

        selected_provider_name = budget_result.final_provider
        selected_model = budget_result.final_model

        if settings.use_redis_rate_limiting:
            try:
                redis_rate_limit_service.enforce(
                    project_id=project.id,
                    provider=selected_provider_name,
                    limit=budget_result.requests_per_minute,
                )
            except RedisRateLimitExceededError as error:
                RATE_LIMIT_REJECTIONS_TOTAL.labels(
                    project_id=str(project.id),
                    provider=selected_provider_name,
                ).inc()

                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "message": str(error),
                        "retry_after_seconds": (error.retry_after_seconds),
                    },
                    headers={"Retry-After": str(error.retry_after_seconds)},
                ) from error
            except RedisRateLimitError:
                # Redis is unavailable. Fall back to the
                # database-backed limiter rather than making
                # chat unavailable.
                try:
                    rate_limit_service.enforce(
                        database,
                        request_id=request_id,
                        provider=selected_provider_name,
                        requests_per_minute=(budget_result.requests_per_minute),
                        scope=f"project:{project.id}",
                        project_id=project.id,
                    )
                except RateLimitExceededError as error:
                    RATE_LIMIT_REJECTIONS_TOTAL.labels(
                        project_id=str(project.id),
                        provider=selected_provider_name,
                    ).inc()

                    raise HTTPException(
                        status_code=(status.HTTP_429_TOO_MANY_REQUESTS),
                        detail={
                            "message": str(error),
                            "retry_after_seconds": (error.retry_after_seconds),
                        },
                        headers={"Retry-After": str(error.retry_after_seconds)},
                    ) from error
        else:
            try:
                rate_limit_service.enforce(
                    database,
                    request_id=request_id,
                    provider=selected_provider_name,
                    requests_per_minute=(budget_result.requests_per_minute),
                    scope=f"project:{project.id}",
                    project_id=project.id,
                )
            except RateLimitExceededError as error:
                RATE_LIMIT_REJECTIONS_TOTAL.labels(
                    project_id=str(project.id),
                    provider=selected_provider_name,
                ).inc()

                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "message": str(error),
                        "retry_after_seconds": (error.retry_after_seconds),
                    },
                    headers={"Retry-After": str(error.retry_after_seconds)},
                ) from error

        response_budget = BudgetDecision(
            enforcement_enabled=(budget_result.enforcement_enabled),
            allowed=budget_result.allowed,
            action=budget_result.action,
            original_provider=(budget_result.original_provider),
            original_model=budget_result.original_model,
            final_provider=budget_result.final_provider,
            final_model=budget_result.final_model,
            estimated_input_tokens=(budget_result.estimated_input_tokens),
            requested_output_tokens=(budget_result.requested_output_tokens),
            estimated_total_tokens=(budget_result.estimated_total_tokens),
            estimated_cost_usd=(budget_result.estimated_cost_usd),
            daily_tokens_used=(budget_result.daily_tokens_used),
            daily_token_budget=(budget_result.daily_token_budget),
            daily_cost_used_usd=(budget_result.daily_cost_used_usd),
            daily_cost_budget_usd=(budget_result.daily_cost_budget_usd),
            reasons=budget_result.reasons,
        )

        routed_request = request.model_copy(
            update={
                "provider": selected_provider_name,
                "model": selected_model,
                "max_tokens": effective_max_tokens,
            }
        )

        try:
            governance_result = governance_service.evaluate_input(
                project_id=project.id,
                provider=selected_provider_name,
                model=selected_model,
                messages=routed_request.messages,
            )
        except ModelAccessDeniedError as error:
            GOVERNANCE_DECISIONS_TOTAL.labels(
                project_id=str(project.id),
                action="block",
                event_type="input",
            ).inc()

            security_audit_service.record(
                database,
                project_id=project.id,
                request_id=request_id,
                correlation_id=get_correlation_id(),
                event_type="model_access_denied",
                severity="medium",
                action="block",
                provider=selected_provider_name,
                model=selected_model,
                sanitized_summary=str(error),
            )

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "message": "Model access denied.",
                    "reason": str(error),
                },
            ) from error

        GOVERNANCE_DECISIONS_TOTAL.labels(
            project_id=str(project.id),
            action=governance_result.decision.action,
            event_type="input",
        ).inc()

        for injection_finding in governance_result.decision.prompt_injection_findings:
            PROMPT_INJECTION_FINDINGS_TOTAL.labels(
                category=injection_finding.category,
                action=governance_result.decision.action,
            ).inc()

        for sensitive_finding in governance_result.decision.sensitive_data_findings:
            PII_FINDINGS_TOTAL.labels(
                entity_type=sensitive_finding.entity_type,
                action=sensitive_finding.action,
            ).inc()

        if not governance_result.decision.allowed:
            security_audit_service.record(
                database,
                project_id=project.id,
                request_id=request_id,
                correlation_id=get_correlation_id(),
                event_type="governance_input_blocked",
                severity="high",
                action="block",
                provider=selected_provider_name,
                model=selected_model,
                risk_score=float(governance_result.decision.prompt_injection_score),
                detected_categories=[
                    finding.category
                    for finding in governance_result.decision.prompt_injection_findings
                ],
                sanitized_summary=" ".join(governance_result.decision.reasons),
                metadata={
                    "pii_finding_count": len(
                        governance_result.decision.sensitive_data_findings
                    ),
                    "sanitized": (governance_result.decision.sanitized),
                },
            )

            raise HTTPException(
                status_code=(status.HTTP_422_UNPROCESSABLE_ENTITY),
                detail={
                    "message": (
                        "The request was blocked by TokenGuard governance policy."
                    ),
                    "governance": (governance_result.decision.model_dump()),
                },
            )

        routed_request = routed_request.model_copy(
            update={"messages": governance_result.messages}
        )

        combined_prompt = combine_messages(routed_request)

        if governance_result.decision.sanitized:
            security_audit_service.record(
                database,
                project_id=project.id,
                request_id=request_id,
                correlation_id=get_correlation_id(),
                event_type="sensitive_data_redacted",
                severity="medium",
                action="redact",
                provider=selected_provider_name,
                model=selected_model,
                detected_categories=[
                    finding.entity_type
                    for finding in governance_result.decision.sensitive_data_findings
                ],
                metadata={
                    "sanitized_messages_count": (
                        governance_result.decision.sanitized_messages_count
                    )
                },
            )

        input_moderation_result = await governance_service.evaluate_input_moderation(
            messages=routed_request.messages
        )

        if not input_moderation_result.safe:
            security_audit_service.record(
                database,
                project_id=project.id,
                request_id=request_id,
                correlation_id=get_correlation_id(),
                event_type=(input_moderation_result.audit_event_type),
                severity=(input_moderation_result.audit_severity),
                action="block",
                provider=selected_provider_name,
                model=selected_model,
                detected_categories=(input_moderation_result.detected_categories),
            )

            GOVERNANCE_DECISIONS_TOTAL.labels(
                project_id=str(project.id),
                action="block",
                event_type="input",
            ).inc()

            raise HTTPException(
                status_code=(status.HTTP_422_UNPROCESSABLE_ENTITY),
                detail={
                    "message": (
                        "The request was blocked by TokenGuard content moderation."
                    ),
                    "reasons": (input_moderation_result.detected_categories),
                },
            )

        cache_key = cache_service.generate_cache_key(
            request=routed_request,
            selected_model=selected_model,
            project_id=project.id,
        )

        redis_cache_key = (
            redis_cache_service.generate_key(
                project_id=project.id,
                request=routed_request,
                provider=selected_provider_name,
                model=selected_model,
            )
            if settings.use_redis_exact_cache
            else None
        )

        if request.use_cache:
            lookup_started_at = perf_counter()

            redis_cache_hit = None

            if redis_cache_key is not None:
                try:
                    redis_cache_hit = redis_cache_service.get(redis_cache_key)
                except RedisCacheError:
                    # Redis failures fall through to the
                    # database-backed exact cache below, or to
                    # a fresh provider call. Chat must never
                    # become unavailable just because Redis is
                    # unreachable.
                    redis_cache_hit = None

            if redis_cache_hit is not None:
                lookup_latency_ms = round(
                    (perf_counter() - lookup_started_at) * 1000,
                    2,
                )

                avoided_cost = pricing_service.calculate_cost(
                    provider=redis_cache_hit["provider"],
                    model=redis_cache_hit["model"],
                    input_tokens=redis_cache_hit["input_tokens"],
                    output_tokens=redis_cache_hit["output_tokens"],
                )

                total_latency_ms = round(
                    (perf_counter() - started_at) * 1000,
                    2,
                )

                create_request_log(
                    database=database,
                    project_id=project.id,
                    request_id=request_id,
                    provider=redis_cache_hit["provider"],
                    model=redis_cache_hit["model"],
                    prompt=combined_prompt,
                    response=redis_cache_hit["content"],
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    estimated_cost_usd=0.0,
                    latency_ms=total_latency_ms,
                    status_value="cache_hit",
                )

                CACHE_OPERATIONS_TOTAL.labels(
                    cache_type="exact",
                    result="hit",
                ).inc()

                CHAT_REQUESTS_TOTAL.labels(
                    project_id=str(project.id),
                    provider=redis_cache_hit["provider"],
                    model=redis_cache_hit["model"],
                    status="cache_hit",
                ).inc()

                span.set_attribute(
                    "tokenguard.cache_hit",
                    True,
                )

                return ChatCompletionResponse(
                    request_id=request_id,
                    provider=redis_cache_hit["provider"],
                    model=redis_cache_hit["model"],
                    content=redis_cache_hit["content"],
                    usage=TokenUsage(
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                    ),
                    cost=CostDetails(
                        input_cost_usd=0.0,
                        output_cost_usd=0.0,
                        total_cost_usd=0.0,
                        pricing_found=(avoided_cost.pricing_found),
                    ),
                    latency_ms=total_latency_ms,
                    optimization=OptimizationDetails(
                        cache_enabled=True,
                        cache_hit=True,
                        cache_type="exact",
                        semantic_similarity_score=None,
                        tokens_avoided=(redis_cache_hit["total_tokens"]),
                        cost_avoided_usd=(avoided_cost.total_cost_usd),
                        original_latency_ms=(redis_cache_hit["latency_ms"]),
                        cache_lookup_latency_ms=(lookup_latency_ms),
                    ),
                    routing=routing_decision,
                    budget=response_budget,
                    governance=governance_result.decision,
                    reliability=build_cache_hit_reliability(request_id),
                )

            cached_response = cache_service.get_cached_response(
                database=database,
                cache_key=cache_key,
                project_id=project.id,
            )

            lookup_latency_ms = round(
                (perf_counter() - lookup_started_at) * 1000,
                2,
            )

            if cached_response is not None:
                cache_service.register_cache_hit(
                    database=database,
                    cache_entry=cached_response,
                )

                avoided_cost = pricing_service.calculate_cost(
                    provider=cached_response.provider,
                    model=cached_response.model,
                    input_tokens=cached_response.input_tokens,
                    output_tokens=cached_response.output_tokens,
                )

                total_latency_ms = round(
                    (perf_counter() - started_at) * 1000,
                    2,
                )

                create_request_log(
                    database=database,
                    project_id=project.id,
                    request_id=request_id,
                    provider=cached_response.provider,
                    model=cached_response.model,
                    prompt=combined_prompt,
                    response=cached_response.response,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    estimated_cost_usd=0.0,
                    latency_ms=total_latency_ms,
                    status_value="cache_hit",
                )

                CACHE_OPERATIONS_TOTAL.labels(
                    cache_type="exact",
                    result="hit",
                ).inc()

                CHAT_REQUESTS_TOTAL.labels(
                    project_id=str(project.id),
                    provider=cached_response.provider,
                    model=cached_response.model,
                    status="cache_hit",
                ).inc()

                span.set_attribute(
                    "tokenguard.cache_hit",
                    True,
                )

                return ChatCompletionResponse(
                    request_id=request_id,
                    provider=cached_response.provider,
                    model=cached_response.model,
                    content=cached_response.response,
                    usage=TokenUsage(
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                    ),
                    cost=CostDetails(
                        input_cost_usd=0.0,
                        output_cost_usd=0.0,
                        total_cost_usd=0.0,
                        pricing_found=(avoided_cost.pricing_found),
                    ),
                    latency_ms=total_latency_ms,
                    optimization=OptimizationDetails(
                        cache_enabled=True,
                        cache_hit=True,
                        cache_type="exact",
                        semantic_similarity_score=None,
                        tokens_avoided=(cached_response.total_tokens),
                        cost_avoided_usd=(avoided_cost.total_cost_usd),
                        original_latency_ms=(cached_response.original_latency_ms),
                        cache_lookup_latency_ms=(lookup_latency_ms),
                    ),
                    routing=routing_decision,
                    budget=response_budget,
                    governance=governance_result.decision,
                    reliability=build_cache_hit_reliability(request_id),
                )

            CACHE_OPERATIONS_TOTAL.labels(
                cache_type="exact",
                result="miss",
            ).inc()

        if request.use_cache and request.use_semantic_cache:
            semantic_lookup_started_at = perf_counter()

            semantic_match = semantic_cache_service.find_match(
                database,
                project_id=project.id,
                request=routed_request,
                selected_model=selected_model,
            )

            semantic_lookup_latency_ms = round(
                (perf_counter() - semantic_lookup_started_at) * 1000,
                2,
            )

            if semantic_match is not None:
                cached_entry = semantic_match.entry

                semantic_cache_service.register_hit(
                    database,
                    cached_entry,
                )

                total_latency_ms = round(
                    (perf_counter() - started_at) * 1000,
                    2,
                )

                create_request_log(
                    database=database,
                    project_id=project.id,
                    request_id=request_id,
                    provider=cached_entry.provider,
                    model=cached_entry.model,
                    prompt=combined_prompt,
                    response=cached_entry.response,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    estimated_cost_usd=0.0,
                    latency_ms=total_latency_ms,
                    status_value="semantic_cache_hit",
                )

                CACHE_OPERATIONS_TOTAL.labels(
                    cache_type="semantic",
                    result="hit",
                ).inc()

                CHAT_REQUESTS_TOTAL.labels(
                    project_id=str(project.id),
                    provider=cached_entry.provider,
                    model=cached_entry.model,
                    status="semantic_cache_hit",
                ).inc()

                span.set_attribute(
                    "tokenguard.cache_hit",
                    True,
                )

                return ChatCompletionResponse(
                    request_id=request_id,
                    provider=cached_entry.provider,
                    model=cached_entry.model,
                    content=cached_entry.response,
                    usage=TokenUsage(
                        input_tokens=0,
                        output_tokens=0,
                        total_tokens=0,
                    ),
                    cost=CostDetails(
                        input_cost_usd=0.0,
                        output_cost_usd=0.0,
                        total_cost_usd=0.0,
                        pricing_found=True,
                    ),
                    latency_ms=total_latency_ms,
                    optimization=OptimizationDetails(
                        cache_enabled=True,
                        cache_hit=True,
                        cache_type="semantic",
                        semantic_similarity_score=round(
                            semantic_match.similarity_score,
                            4,
                        ),
                        tokens_avoided=(cached_entry.total_tokens),
                        cost_avoided_usd=(cached_entry.original_cost_usd),
                        original_latency_ms=(cached_entry.original_latency_ms),
                        cache_lookup_latency_ms=(semantic_lookup_latency_ms),
                    ),
                    routing=routing_decision,
                    budget=response_budget,
                    governance=governance_result.decision,
                    reliability=build_cache_hit_reliability(request_id),
                )

            CACHE_OPERATIONS_TOTAL.labels(
                cache_type="semantic",
                result="miss",
            ).inc()

        span.set_attribute(
            "tokenguard.cache_hit",
            False,
        )

        try:
            attempts = fallback_service.build_attempts(
                database,
                project_id=project.id,
                selected_provider=selected_provider_name,
                selected_model=selected_model,
                explicit_credential_id=(
                    None if credential_not_applicable else routed_request.credential_id
                ),
                ollama_fallback_model="llama3.2",
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        except CredentialNotFoundError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(error),
            ) from error

        try:
            fallback_result = await fallback_service.execute(
                database,
                project_id=project.id,
                request_id=request_id,
                attempts=attempts,
                messages=routed_request.messages,
                temperature=routed_request.temperature,
                max_tokens=routed_request.max_tokens,
            )

            provider_response = fallback_result.response
            final_provider_name = fallback_result.provider
            final_model = fallback_result.model

            input_tokens = (
                provider_response.input_tokens
                if provider_response.input_tokens is not None
                else estimated_input_tokens
            )

            output_tokens = (
                provider_response.output_tokens
                if provider_response.output_tokens is not None
                else token_service.estimate_text_tokens(provider_response.content)
            )

            total_tokens = input_tokens + output_tokens

            cost = pricing_service.calculate_cost(
                provider=final_provider_name,
                model=final_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            total_latency_ms = round(
                (perf_counter() - started_at) * 1000,
                2,
            )

            span.set_attribute(
                "llm.provider",
                final_provider_name,
            )

            span.set_attribute(
                "llm.model",
                final_model,
            )

            span.set_attribute(
                "llm.usage.input_tokens",
                input_tokens,
            )

            span.set_attribute(
                "llm.usage.output_tokens",
                output_tokens,
            )

            output_governance_result = await governance_service.evaluate_output(
                text=provider_response.content
            )

            if not output_governance_result.safe:
                security_audit_service.record(
                    database,
                    project_id=project.id,
                    request_id=request_id,
                    correlation_id=get_correlation_id(),
                    event_type=(output_governance_result.audit_event_type),
                    severity=(output_governance_result.audit_severity),
                    action="block",
                    provider=final_provider_name,
                    model=final_model,
                    detected_categories=(output_governance_result.detected_categories),
                )

                # Never persist or cache output that failed a
                # security scan -- the response text itself may
                # contain the leaked secret or unsafe content.
                create_request_log(
                    database=database,
                    project_id=project.id,
                    request_id=request_id,
                    provider=final_provider_name,
                    model=final_model,
                    prompt=combined_prompt,
                    response=None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    estimated_cost_usd=cost.total_cost_usd,
                    latency_ms=total_latency_ms,
                    status_value="blocked",
                    error_message=(
                        "The provider response was blocked by "
                        "TokenGuard output security policy."
                    ),
                )

                CHAT_REQUESTS_TOTAL.labels(
                    project_id=str(project.id),
                    provider=final_provider_name,
                    model=final_model,
                    status="blocked",
                ).inc()

                GOVERNANCE_DECISIONS_TOTAL.labels(
                    project_id=str(project.id),
                    action="block",
                    event_type="output",
                ).inc()

                raise HTTPException(
                    status_code=(status.HTTP_422_UNPROCESSABLE_ENTITY),
                    detail={
                        "message": (
                            "The response was blocked by "
                            "TokenGuard output security policy."
                        ),
                        "reasons": (output_governance_result.detected_categories),
                    },
                )

            create_request_log(
                database=database,
                project_id=project.id,
                request_id=request_id,
                provider=final_provider_name,
                model=final_model,
                prompt=combined_prompt,
                response=provider_response.content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=cost.total_cost_usd,
                latency_ms=total_latency_ms,
                status_value="completed",
            )

            if request.use_cache:
                cache_service.save_response(
                    database=database,
                    cache_key=cache_key,
                    project_id=project.id,
                    provider=final_provider_name,
                    model=final_model,
                    prompt=combined_prompt,
                    response=provider_response.content,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=total_latency_ms,
                )

                if redis_cache_key is not None:
                    try:
                        redis_cache_service.set(
                            cache_key=redis_cache_key,
                            value={
                                "provider": final_provider_name,
                                "model": final_model,
                                "content": (provider_response.content),
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "total_tokens": total_tokens,
                                "latency_ms": total_latency_ms,
                                "cost_usd": cost.total_cost_usd,
                            },
                        )
                    except RedisCacheError:
                        pass

            if (
                request.use_cache
                and request.use_semantic_cache
                and not semantic_cache_service.response_already_stored(
                    database,
                    project_id=project.id,
                    request=routed_request,
                    selected_model=final_model,
                )
            ):
                semantic_cache_service.save_response(
                    database,
                    project_id=project.id,
                    request=routed_request,
                    selected_model=final_model,
                    response=provider_response.content,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=total_latency_ms,
                    cost_usd=cost.total_cost_usd,
                )

            response_routing_reasons = list(route.reasons)

            if credential_not_applicable:
                response_routing_reasons.append(
                    f"credential_id {request.credential_id} was "
                    "ignored because the automatically selected "
                    "route uses Ollama, which does not require an "
                    "API credential."
                )

            if fallback_result.fallback_used:
                response_routing_reasons.append(
                    "The original provider attempt failed, "
                    f"so TokenGuard completed the request using "
                    f"{final_provider_name}/{final_model}."
                )

            final_routing_decision = RoutingDecision(
                automatic_routing_used=(route.automatic_routing_used),
                tier=route.tier,
                provider=route.provider,
                model=route.model,
                complexity_score=route.complexity_score,
                confidence=route.confidence,
                reasons=response_routing_reasons,
                original_provider=route.original_provider,
                original_model=route.original_model,
                fallback=FallbackDetails(
                    fallback_used=(fallback_result.fallback_used),
                    attempts_made=(fallback_result.attempts_made),
                    final_credential_id=(fallback_result.credential_id),
                    credential_source=(fallback_result.credential_source),
                ),
            )

            CHAT_REQUESTS_TOTAL.labels(
                project_id=str(project.id),
                provider=final_provider_name,
                model=final_model,
                status="completed",
            ).inc()

            TOKENS_TOTAL.labels(
                project_id=str(project.id),
                provider=final_provider_name,
                model=final_model,
                token_type="input",
            ).inc(input_tokens)

            TOKENS_TOTAL.labels(
                project_id=str(project.id),
                provider=final_provider_name,
                model=final_model,
                token_type="output",
            ).inc(output_tokens)

            ESTIMATED_COST_USD_TOTAL.labels(
                project_id=str(project.id),
                provider=final_provider_name,
                model=final_model,
            ).inc(cost.total_cost_usd)

            health_result = provider_health_service.calculate_health(
                database,
                provider=final_provider_name,
                model=final_model,
            )

            try:
                circuit_state = circuit_breaker_service.get_state(
                    provider=final_provider_name
                )

                circuit_state_value = circuit_state.state
            except RedisCircuitBreakerError:
                circuit_state_value = "unknown"

            reliability = ReliabilityDetails(
                correlation_id=(get_correlation_id() or request_id),
                provider_attempts=(fallback_result.attempts_made),
                retry_count=0,
                circuit_breaker_state=(circuit_state_value),
                provider_health_score=(health_result.score),
                fallback_used=(fallback_result.fallback_used),
            )

            return ChatCompletionResponse(
                request_id=request_id,
                provider=final_provider_name,
                model=final_model,
                content=provider_response.content,
                usage=TokenUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                ),
                cost=CostDetails(
                    input_cost_usd=cost.input_cost_usd,
                    output_cost_usd=cost.output_cost_usd,
                    total_cost_usd=cost.total_cost_usd,
                    pricing_found=cost.pricing_found,
                ),
                latency_ms=total_latency_ms,
                optimization=OptimizationDetails(
                    cache_enabled=request.use_cache,
                    cache_hit=False,
                    cache_type="none",
                    semantic_similarity_score=None,
                    tokens_avoided=0,
                    cost_avoided_usd=0.0,
                ),
                routing=final_routing_decision,
                budget=response_budget,
                governance=governance_result.decision,
                reliability=reliability,
            )

        except AllProviderAttemptsFailedError as error:
            database.rollback()

            total_latency_ms = round(
                (perf_counter() - started_at) * 1000,
                2,
            )

            create_request_log(
                database=database,
                project_id=project.id,
                request_id=request_id,
                provider=selected_provider_name,
                model=selected_model,
                prompt=combined_prompt,
                response=None,
                input_tokens=estimated_input_tokens,
                output_tokens=0,
                total_tokens=estimated_input_tokens,
                estimated_cost_usd=0.0,
                latency_ms=total_latency_ms,
                status_value="failed",
                error_message=str(error),
            )

            CHAT_REQUESTS_TOTAL.labels(
                project_id=str(project.id),
                provider=selected_provider_name,
                model=selected_model,
                status="failed",
            ).inc()

            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "message": str(error),
                    "attempt_errors": error.errors,
                },
            ) from error

        except HTTPException:
            # Intentional HTTP errors raised above (e.g. the output
            # governance block) must propagate with their own status
            # code instead of being caught by the generic handler
            # below and turned into a 500.
            raise

        except Exception as error:
            database.rollback()

            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected internal error occurred.",
            ) from error
