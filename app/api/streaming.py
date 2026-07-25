from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.chat import combine_messages, create_request_log
from app.cache.redis_client import get_redis_client
from app.core.config import get_settings
from app.database.connection import get_database_session
from app.database.models import (
    ModelRoutingLog,
    Project,
    ProviderAttemptLog,
    ProviderCredential,
)
from app.dependencies.authentication import get_current_project
from app.observability.context import get_correlation_id
from app.observability.metrics import (
    BUDGET_DECISIONS_TOTAL,
    CACHE_OPERATIONS_TOTAL,
    CHAT_REQUESTS_TOTAL,
    ESTIMATED_COST_USD_TOTAL,
    FALLBACKS_TOTAL,
    GOVERNANCE_DECISIONS_TOTAL,
    PII_FINDINGS_TOTAL,
    PROMPT_INJECTION_FINDINGS_TOTAL,
    PROVIDER_ATTEMPTS_TOTAL,
    PROVIDER_LATENCY_SECONDS,
    RATE_LIMIT_REJECTIONS_TOTAL,
    TOKENS_TOTAL,
)
from app.providers.factory import ProviderFactory
from app.schemas.chat import ChatCompletionRequest
from app.services.budget_service import (
    BudgetConfigurationError,
    BudgetService,
)
from app.services.cache_service import CacheService
from app.services.credential_service import (
    CredentialNotFoundError,
)
from app.services.fallback_service import (
    FallbackService,
    ProviderAttempt,
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
from app.services.sse_service import SSEService
from app.services.token_service import TokenService

router = APIRouter(
    prefix="/v1/chat",
    tags=["Streaming"],
)

settings = get_settings()


async def _pump_with_heartbeat(
    stream: AsyncIterator[Any],
    *,
    heartbeat_seconds: float,
) -> AsyncIterator[tuple[str, Any]]:
    """
    Wrap a provider event stream so that a heartbeat marker is
    produced whenever no real event arrives within
    ``heartbeat_seconds``. This keeps the SSE connection alive
    through proxies that close idle connections.
    """

    aiter = stream.__aiter__()

    # NOTE: We deliberately do not use ``asyncio.wait_for`` around
    # ``aiter.__anext__()`` here. ``wait_for`` cancels its inner
    # coroutine on timeout, and cancelling an in-flight
    # ``__anext__()`` call corrupts the underlying async generator
    # (the cancellation is raised inside the generator body, which
    # closes it permanently). That would mean a single heartbeat
    # tick silently ends the provider stream forever. Instead, keep
    # the same pending task alive across timeout cycles and only
    # retrieve/replace it once it actually completes.
    pending = asyncio.ensure_future(aiter.__anext__())

    try:
        while True:
            done, _ = await asyncio.wait(
                {pending},
                timeout=heartbeat_seconds,
            )

            if pending not in done:
                yield "heartbeat", None
                continue

            try:
                event = pending.result()
            except StopAsyncIteration:
                return

            yield "event", event

            pending = asyncio.ensure_future(aiter.__anext__())

    finally:
        if not pending.done():
            pending.cancel()


def _log_provider_attempt(
    database: Session,
    *,
    project_id: int,
    request_id: str,
    attempt_number: int,
    attempt: ProviderAttempt,
    succeeded: bool,
    latency_ms: float,
    error: Exception | None = None,
) -> None:
    log = ProviderAttemptLog(
        project_id=project_id,
        request_id=request_id,
        attempt_number=attempt_number,
        provider=attempt.provider,
        model=attempt.model,
        credential_id=attempt.credential_id,
        credential_source=attempt.credential_source,
        succeeded=succeeded,
        error_type=(type(error).__name__ if error is not None else None),
        error_message=(str(error) if error is not None else None),
        latency_ms=latency_ms,
    )

    database.add(log)
    database.commit()


@router.post(
    "/completions/stream",
    response_class=StreamingResponse,
)
async def stream_chat_completion(
    request: ChatCompletionRequest,
    http_request: Request,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> StreamingResponse:
    if not settings.enable_streaming:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Streaming is disabled on this deployment.",
        )

    request_id = str(uuid4())
    correlation_id = get_correlation_id() or request_id
    started_at = perf_counter()

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

    credential_not_applicable = (
        request.credential_id is not None
        and selected_provider_name == "ollama"
        and route.automatic_routing_used
    )

    if (
        request.credential_id is not None
        and selected_provider_name == "ollama"
        and not route.automatic_routing_used
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ollama does not require an API credential.",
        )

    routing_reasons = list(route.reasons)

    if credential_not_applicable:
        routing_reasons.append(
            f"credential_id {request.credential_id} was "
            "ignored because the automatically selected "
            "route uses Ollama, which does not require an "
            "API credential."
        )

    routing_log = ModelRoutingLog(
        project_id=project.id,
        request_id=request_id,
        automatic_routing_used=route.automatic_routing_used,
        routing_preference=request.routing_preference,
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

    estimated_input_tokens = token_service.estimate_message_tokens(request.messages)

    effective_max_tokens = (
        min(
            request.max_tokens,
            route.maximum_output_tokens,
        )
        if (request.max_tokens is not None and route.maximum_output_tokens is not None)
        else (request.max_tokens or route.maximum_output_tokens)
    )

    requested_output_tokens = effective_max_tokens or 500

    # -- Budget enforcement happens before the stream opens. --
    try:
        budget_result = budget_service.evaluate(
            database,
            project=project,
            request_id=request_id,
            provider=selected_provider_name,
            model=selected_model,
            estimated_input_tokens=estimated_input_tokens,
            requested_output_tokens=requested_output_tokens,
            request_maximum_cost_usd=(request.maximum_request_cost_usd),
            request_maximum_total_tokens=(request.maximum_total_tokens),
            allow_downgrade=request.allow_budget_downgrade,
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

    selected_provider_name = budget_result.final_provider
    selected_model = budget_result.final_model

    # -- Rate limiting also happens before the stream opens. --
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

    routed_request = request.model_copy(
        update={
            "provider": selected_provider_name,
            "model": selected_model,
            "max_tokens": effective_max_tokens,
        }
    )

    # -- Governance runs before the stream opens, same as chat.py. --
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
            correlation_id=correlation_id,
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
            correlation_id=correlation_id,
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": ("The request was blocked by TokenGuard governance policy."),
                "governance": (governance_result.decision.model_dump()),
            },
        )

    routed_request = routed_request.model_copy(
        update={"messages": governance_result.messages}
    )

    if governance_result.decision.sanitized:
        security_audit_service.record(
            database,
            project_id=project.id,
            request_id=request_id,
            correlation_id=correlation_id,
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
            correlation_id=correlation_id,
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": (
                    "The request was blocked by TokenGuard content moderation."
                ),
                "reasons": (input_moderation_result.detected_categories),
            },
        )

    # combined_prompt is derived from the sanitized messages so that
    # redacted sensitive data is never persisted to the request log.
    combined_prompt = combine_messages(routed_request)

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

    # -- Resolve a cache hit (if any) before opening the stream. --
    cache_hit: dict[str, Any] | None = None
    cache_type = "none"
    similarity_score: float | None = None

    if request.use_cache:
        if redis_cache_key is not None:
            try:
                redis_hit = redis_cache_service.get(redis_cache_key)
            except RedisCacheError:
                redis_hit = None

            if redis_hit is not None:
                cache_hit = redis_hit
                cache_type = "exact"

        if cache_hit is None:
            db_hit = cache_service.get_cached_response(
                database=database,
                cache_key=cache_key,
                project_id=project.id,
            )

            if db_hit is not None:
                cache_service.register_cache_hit(
                    database=database,
                    cache_entry=db_hit,
                )

                cache_hit = {
                    "provider": db_hit.provider,
                    "model": db_hit.model,
                    "content": db_hit.response,
                }
                cache_type = "exact"

        if cache_hit is None and request.use_semantic_cache:
            semantic_match = semantic_cache_service.find_match(
                database,
                project_id=project.id,
                request=routed_request,
                selected_model=selected_model,
            )

            if semantic_match is not None:
                semantic_cache_service.register_hit(
                    database,
                    semantic_match.entry,
                )

                cache_hit = {
                    "provider": semantic_match.entry.provider,
                    "model": semantic_match.entry.model,
                    "content": semantic_match.entry.response,
                }
                cache_type = "semantic"
                similarity_score = round(
                    semantic_match.similarity_score,
                    4,
                )

        CACHE_OPERATIONS_TOTAL.labels(
            cache_type=(cache_type if cache_type != "none" else "exact"),
            result="hit" if cache_hit is not None else "miss",
        ).inc()

    # -- Build ordered provider attempts for a cache miss. --
    attempts: list[ProviderAttempt] = []

    if cache_hit is None:
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

    sse_service = SSEService()

    async def generate_events() -> AsyncIterator[str]:
        yield sse_service.encode(
            event="metadata",
            event_id=request_id,
            data={
                "request_id": request_id,
                "correlation_id": correlation_id,
                "project_id": project.id,
                "provider": selected_provider_name,
                "model": selected_model,
            },
        )

        # -- Cache hit: replay the cached content, do not re-call --
        # -- any provider and do not persist a new cache entry.  --
        if cache_hit is not None:
            content = cache_hit.get("content") or ""

            yield sse_service.encode(
                event="token",
                data={"type": "token", "content": content},
            )

            avoided_cost = pricing_service.calculate_cost(
                provider=cache_hit["provider"],
                model=cache_hit["model"],
                input_tokens=0,
                output_tokens=0,
            )

            yield sse_service.encode(
                event="usage",
                data={
                    "type": "usage",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "estimated_cost_usd": (avoided_cost.total_cost_usd),
                },
            )

            total_latency_ms = round(
                (perf_counter() - started_at) * 1000,
                2,
            )

            create_request_log(
                database=database,
                project_id=project.id,
                request_id=request_id,
                provider=cache_hit["provider"],
                model=cache_hit["model"],
                prompt=combined_prompt,
                response=content,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                estimated_cost_usd=0.0,
                latency_ms=total_latency_ms,
                status_value=(
                    "semantic_cache_hit" if cache_type == "semantic" else "cache_hit"
                ),
            )

            CHAT_REQUESTS_TOTAL.labels(
                project_id=str(project.id),
                provider=cache_hit["provider"],
                model=cache_hit["model"],
                status=(
                    "semantic_cache_hit" if cache_type == "semantic" else "cache_hit"
                ),
            ).inc()

            yield sse_service.encode(
                event="completed",
                data={
                    "type": "completed",
                    "request_id": request_id,
                    "provider": cache_hit["provider"],
                    "model": cache_hit["model"],
                    "latency_ms": total_latency_ms,
                    "cache_hit": True,
                    "cache_type": cache_type,
                    "similarity_score": similarity_score,
                },
            )

            return

        # -- Cache miss: stream tokens from the provider, --
        # -- with fallback allowed only before the first token. --
        collected_content: list[str] = []
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0

        first_token_sent = False
        disconnected = False
        succeeded = False
        final_provider_name = selected_provider_name
        final_model = selected_model
        attempt_errors: list[str] = []

        for attempt_number, attempt in enumerate(
            attempts,
            start=1,
        ):
            attempt_started_at = perf_counter()

            try:
                if settings.circuit_breaker_enabled:
                    try:
                        circuit_breaker_service.ensure_available(
                            provider=attempt.provider
                        )
                    except RedisCircuitBreakerError:
                        pass

                provider = ProviderFactory.create(
                    attempt.provider,
                    api_key=attempt.api_key,
                )

                stream_iterator = provider.stream(
                    messages=routed_request.messages,
                    model=attempt.model,
                    temperature=routed_request.temperature,
                    max_tokens=routed_request.max_tokens,
                )

                async for kind, provider_event in _pump_with_heartbeat(
                    stream_iterator,
                    heartbeat_seconds=(settings.stream_heartbeat_seconds),
                ):
                    if await http_request.is_disconnected():
                        disconnected = True
                        break

                    if kind == "heartbeat":
                        yield sse_service.heartbeat()
                        continue

                    if provider_event.event_type == "token":
                        content = provider_event.content or ""
                        collected_content.append(content)
                        first_token_sent = True

                        yield sse_service.encode(
                            event="token",
                            data={
                                "type": "token",
                                "content": content,
                            },
                        )

                    elif provider_event.event_type == "usage":
                        input_tokens = provider_event.input_tokens or 0
                        output_tokens = provider_event.output_tokens or 0
                        total_tokens = (
                            provider_event.total_tokens or input_tokens + output_tokens
                        )

                if disconnected:
                    break

                latency_ms = round(
                    (perf_counter() - attempt_started_at) * 1000,
                    2,
                )

                PROVIDER_ATTEMPTS_TOTAL.labels(
                    provider=attempt.provider,
                    model=attempt.model,
                    result="success",
                    error_type="none",
                ).inc()

                PROVIDER_LATENCY_SECONDS.labels(
                    provider=attempt.provider,
                    model=attempt.model,
                ).observe(latency_ms / 1000)

                if settings.circuit_breaker_enabled:
                    try:
                        circuit_breaker_service.record_success(
                            provider=attempt.provider
                        )
                    except RedisCircuitBreakerError:
                        pass

                provider_health_service.record_event(
                    database,
                    provider=attempt.provider,
                    model=attempt.model,
                    succeeded=True,
                    latency_ms=latency_ms,
                    error_type=None,
                    retry_count=0,
                )

                if attempt_number > 1:
                    FALLBACKS_TOTAL.labels(
                        original_provider=(attempts[0].provider),
                        final_provider=attempt.provider,
                        reason="provider_failure",
                    ).inc()

                if attempt.credential_id is not None:
                    credential = database.get(
                        ProviderCredential,
                        attempt.credential_id,
                    )

                    if credential is not None:
                        credential.last_used_at = datetime.now(UTC)
                        database.commit()

                final_provider_name = attempt.provider
                final_model = attempt.model
                succeeded = True
                break

            except asyncio.CancelledError:
                raise

            except Exception as error:
                latency_ms = round(
                    (perf_counter() - attempt_started_at) * 1000,
                    2,
                )

                _log_provider_attempt(
                    database,
                    project_id=project.id,
                    request_id=request_id,
                    attempt_number=attempt_number,
                    attempt=attempt,
                    succeeded=False,
                    latency_ms=latency_ms,
                    error=error,
                )

                if settings.circuit_breaker_enabled:
                    try:
                        circuit_breaker_service.record_failure(
                            provider=attempt.provider
                        )
                    except RedisCircuitBreakerError:
                        pass

                PROVIDER_ATTEMPTS_TOTAL.labels(
                    provider=attempt.provider,
                    model=attempt.model,
                    result="failure",
                    error_type=type(error).__name__,
                ).inc()

                provider_health_service.record_event(
                    database,
                    provider=attempt.provider,
                    model=attempt.model,
                    succeeded=False,
                    latency_ms=latency_ms,
                    error_type=type(error).__name__,
                    retry_count=0,
                )

                attempt_errors.append(f"{attempt.provider}/{attempt.model}: {error}")

                if first_token_sent:
                    # Tokens already reached the client. Switching
                    # providers now would produce inconsistent
                    # output, so the stream ends here instead.
                    yield sse_service.encode(
                        event="error",
                        data={
                            "type": "error",
                            "message": (
                                "The streaming request failed "
                                "after tokens were already sent."
                            ),
                            "error_type": (type(error).__name__),
                        },
                    )
                    break

                # No tokens sent yet: safe to fall back.
                continue

        total_latency_ms = round(
            (perf_counter() - started_at) * 1000,
            2,
        )

        full_content = "".join(collected_content)

        if disconnected:
            create_request_log(
                database=database,
                project_id=project.id,
                request_id=request_id,
                provider=final_provider_name,
                model=final_model,
                prompt=combined_prompt,
                response=full_content or None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=(total_tokens or estimated_input_tokens),
                estimated_cost_usd=0.0,
                latency_ms=total_latency_ms,
                status_value="disconnected",
                error_message=("The client disconnected before the stream completed."),
            )

            CHAT_REQUESTS_TOTAL.labels(
                project_id=str(project.id),
                provider=final_provider_name,
                model=final_model,
                status="disconnected",
            ).inc()

            return

        if not succeeded:
            create_request_log(
                database=database,
                project_id=project.id,
                request_id=request_id,
                provider=selected_provider_name,
                model=selected_model,
                prompt=combined_prompt,
                response=full_content or None,
                input_tokens=estimated_input_tokens,
                output_tokens=0,
                total_tokens=estimated_input_tokens,
                estimated_cost_usd=0.0,
                latency_ms=total_latency_ms,
                status_value="failed",
                error_message="; ".join(attempt_errors)
                or "All provider attempts failed.",
            )

            CHAT_REQUESTS_TOTAL.labels(
                project_id=str(project.id),
                provider=selected_provider_name,
                model=selected_model,
                status="failed",
            ).inc()

            if not first_token_sent:
                yield sse_service.encode(
                    event="error",
                    data={
                        "type": "error",
                        "message": ("All provider attempts failed."),
                        "error_type": ("AllProviderAttemptsFailedError"),
                    },
                )

            return

        if not input_tokens and not output_tokens:
            input_tokens = estimated_input_tokens
            output_tokens = token_service.estimate_text_tokens(full_content)
            total_tokens = input_tokens + output_tokens

        cost = pricing_service.calculate_cost(
            provider=final_provider_name,
            model=final_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        # -- Output governance runs before caching. Tokens have --
        # -- already reached the client, so an unsafe result can --
        # -- no longer be withheld -- only kept out of the cache --
        # -- and flagged via the security audit log.             --
        output_governance_result = await governance_service.evaluate_output(
            text=full_content
        )

        if not output_governance_result.safe:
            security_audit_service.record(
                database,
                project_id=project.id,
                request_id=request_id,
                correlation_id=correlation_id,
                event_type=(output_governance_result.audit_event_type),
                severity=(output_governance_result.audit_severity),
                action="block",
                provider=final_provider_name,
                model=final_model,
                detected_categories=(output_governance_result.detected_categories),
                metadata={
                    "tokens_already_streamed": True,
                },
            )

            GOVERNANCE_DECISIONS_TOTAL.labels(
                project_id=str(project.id),
                action="block",
                event_type="output",
            ).inc()

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
                status_value="completed_unsafe",
                error_message=(
                    "The provider response was flagged by "
                    "TokenGuard output security policy after "
                    "tokens had already been streamed to the "
                    "client. The response was not cached."
                ),
            )

            CHAT_REQUESTS_TOTAL.labels(
                project_id=str(project.id),
                provider=final_provider_name,
                model=final_model,
                status="completed_unsafe",
            ).inc()

            yield sse_service.encode(
                event="error",
                data={
                    "type": "error",
                    "message": (
                        "The response was flagged by "
                        "TokenGuard output security policy "
                        "after streaming completed. It was "
                        "not cached."
                    ),
                    "error_type": "output_governance_blocked",
                },
            )

            return

        yield sse_service.encode(
            event="usage",
            data={
                "type": "usage",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": cost.total_cost_usd,
            },
        )

        create_request_log(
            database=database,
            project_id=project.id,
            request_id=request_id,
            provider=final_provider_name,
            model=final_model,
            prompt=combined_prompt,
            response=full_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=cost.total_cost_usd,
            latency_ms=total_latency_ms,
            status_value="completed",
        )

        # Only a fully completed response is ever cached.
        if request.use_cache:
            cache_service.save_response(
                database=database,
                cache_key=cache_key,
                project_id=project.id,
                provider=final_provider_name,
                model=final_model,
                prompt=combined_prompt,
                response=full_content,
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
                            "content": full_content,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": total_tokens,
                            "latency_ms": total_latency_ms,
                            "cost_usd": cost.total_cost_usd,
                        },
                    )
                except RedisCacheError:
                    pass

            if request.use_semantic_cache and not (
                semantic_cache_service.response_already_stored(
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
                    response=full_content,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=total_latency_ms,
                    cost_usd=cost.total_cost_usd,
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

        yield sse_service.encode(
            event="completed",
            data={
                "type": "completed",
                "request_id": request_id,
                "provider": final_provider_name,
                "model": final_model,
                "latency_ms": total_latency_ms,
                "cache_hit": False,
                "cache_type": "none",
                "similarity_score": None,
            },
        )

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Correlation-ID": correlation_id,
        },
    )
