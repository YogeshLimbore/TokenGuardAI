from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from time import perf_counter

from sqlalchemy.orm import Session

from app.cache.redis_client import (
    get_redis_client,
)
from app.core.config import get_settings
from app.database.models import (
    ProviderAttemptLog,
    ProviderCredential,
)
from app.observability.metrics import (
    FALLBACKS_TOTAL,
    PROVIDER_ATTEMPTS_TOTAL,
    PROVIDER_LATENCY_SECONDS,
)
from app.providers.anthropic import AnthropicProviderError
from app.providers.base import ProviderResponse
from app.providers.factory import ProviderFactory
from app.providers.groq import GroqProviderError
from app.providers.ollama import OllamaProviderError
from app.providers.openai import OpenAIProviderError
from app.providers.openrouter import OpenRouterProviderError
from app.schemas.chat import ChatMessage
from app.services.credential_service import CredentialService
from app.services.provider_health_service import (
    ProviderHealthService,
)
from app.services.provider_retry_service import (
    ProviderRetryService,
)
from app.services.redis_circuit_breaker_service import (
    ProviderCircuitOpenError,
    RedisCircuitBreakerError,
    RedisCircuitBreakerService,
)

logger = logging.getLogger(__name__)

# Every provider-specific error that a failed attempt can raise.
# Fallback treats all of them identically: log the failure, then
# move on to the next attempt (backup credential or local Ollama).
PROVIDER_ERRORS = (
    OpenRouterProviderError,
    OllamaProviderError,
    OpenAIProviderError,
    AnthropicProviderError,
    GroqProviderError,
)


@dataclass
class ProviderAttempt:
    provider: str
    model: str
    credential_id: int | None
    credential_source: str
    api_key: str | None


@dataclass
class FallbackResult:
    response: ProviderResponse
    provider: str
    model: str
    credential_id: int | None
    credential_source: str
    attempts_made: int
    fallback_used: bool


class AllProviderAttemptsFailedError(RuntimeError):
    def __init__(
        self,
        message: str,
        errors: list[str],
    ) -> None:
        super().__init__(message)
        self.errors = errors


class FallbackService:
    def __init__(self) -> None:
        self.credential_service = CredentialService()
        self.settings = get_settings()

        self.retry_service = ProviderRetryService()

        self.circuit_breaker = RedisCircuitBreakerService(get_redis_client())

        self.health_service = ProviderHealthService()

    def build_attempts(
        self,
        database: Session,
        *,
        project_id: int,
        selected_provider: str,
        selected_model: str,
        explicit_credential_id: int | None,
        ollama_fallback_model: str = "llama3.2",
    ) -> list[ProviderAttempt]:
        attempts: list[ProviderAttempt] = []
        used_ids: set[int] = set()

        if selected_provider == "ollama":
            return [
                ProviderAttempt(
                    provider="ollama",
                    model=selected_model,
                    credential_id=None,
                    credential_source="not_required",
                    api_key=None,
                )
            ]

        if explicit_credential_id is not None:
            credential, api_key = self.credential_service.decrypt_api_key(
                database,
                credential_id=explicit_credential_id,
                project_id=project_id,
                require_active=True,
            )

            if credential.provider != selected_provider:
                raise ValueError(
                    "The explicit credential belongs to "
                    f"'{credential.provider}', but the selected "
                    f"provider is '{selected_provider}'."
                )

            attempts.append(
                ProviderAttempt(
                    provider=selected_provider,
                    model=selected_model,
                    credential_id=credential.id,
                    credential_source="explicit",
                    api_key=api_key,
                )
            )

            used_ids.add(credential.id)

        else:
            default_credential = self.credential_service.get_default_credential(
                database,
                provider=selected_provider,
                project_id=project_id,
            )

            if default_credential is not None:
                _, api_key = self.credential_service.decrypt_api_key(
                    database,
                    credential_id=(default_credential.id),
                    project_id=project_id,
                    require_active=True,
                )

                attempts.append(
                    ProviderAttempt(
                        provider=selected_provider,
                        model=selected_model,
                        credential_id=(default_credential.id),
                        credential_source="default",
                        api_key=api_key,
                    )
                )

                used_ids.add(default_credential.id)

        backup_credentials = self.credential_service.get_active_credentials(
            database,
            provider=selected_provider,
            project_id=project_id,
            exclude_ids=used_ids,
        )

        for credential in backup_credentials:
            _, api_key = self.credential_service.decrypt_api_key(
                database,
                credential_id=credential.id,
                project_id=project_id,
                require_active=True,
            )

            attempts.append(
                ProviderAttempt(
                    provider=selected_provider,
                    model=selected_model,
                    credential_id=credential.id,
                    credential_source="backup",
                    api_key=api_key,
                )
            )

        attempts.append(
            ProviderAttempt(
                provider="ollama",
                model=ollama_fallback_model,
                credential_id=None,
                credential_source="fallback_local",
                api_key=None,
            )
        )

        return attempts

    def _ensure_circuit_available(
        self,
        *,
        provider: str,
    ) -> None:
        if not self.settings.circuit_breaker_enabled:
            return

        try:
            self.circuit_breaker.ensure_available(provider=provider)
        except RedisCircuitBreakerError as error:
            # Redis is unavailable. Fail open rather than
            # blocking every provider attempt just because
            # circuit state cannot be read.
            logger.warning(
                "Circuit breaker state unavailable for %s: %s",
                provider,
                error,
            )

    def _record_circuit_success(
        self,
        *,
        provider: str,
    ) -> None:
        if not self.settings.circuit_breaker_enabled:
            return

        try:
            self.circuit_breaker.record_success(provider=provider)
        except RedisCircuitBreakerError as error:
            logger.warning(
                "Could not record circuit success for %s: %s",
                provider,
                error,
            )

    def _record_circuit_failure(
        self,
        *,
        provider: str,
    ) -> None:
        if not self.settings.circuit_breaker_enabled:
            return

        try:
            self.circuit_breaker.record_failure(provider=provider)
        except RedisCircuitBreakerError as error:
            logger.warning(
                "Could not record circuit failure for %s: %s",
                provider,
                error,
            )

    @staticmethod
    def _log_attempt(
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
            credential_source=(attempt.credential_source),
            succeeded=succeeded,
            error_type=(type(error).__name__ if error is not None else None),
            error_message=(str(error) if error is not None else None),
            latency_ms=latency_ms,
        )

        database.add(log)
        database.commit()

    async def execute(
        self,
        database: Session,
        *,
        project_id: int,
        request_id: str,
        attempts: list[ProviderAttempt],
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
    ) -> FallbackResult:
        errors: list[str] = []

        for attempt_number, attempt in enumerate(
            attempts,
            start=1,
        ):
            started_at = perf_counter()

            try:
                self._ensure_circuit_available(provider=attempt.provider)

                # The attempt is bound as a default argument so the
                # closure cannot pick up a later loop iteration's value.
                async def provider_operation(
                    current_attempt: ProviderAttempt = attempt,
                ) -> ProviderResponse:
                    provider = ProviderFactory.create(
                        current_attempt.provider,
                        api_key=current_attempt.api_key,
                    )

                    return await provider.generate(
                        messages=messages,
                        model=current_attempt.model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )

                response = await self.retry_service.execute(provider_operation)

                latency_ms = round(
                    (perf_counter() - started_at) * 1000,
                    2,
                )

                self._log_attempt(
                    database,
                    project_id=project_id,
                    request_id=request_id,
                    attempt_number=attempt_number,
                    attempt=attempt,
                    succeeded=True,
                    latency_ms=latency_ms,
                )

                self._record_circuit_success(provider=attempt.provider)

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

                self.health_service.record_event(
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
                        from datetime import (
                            datetime,
                        )

                        credential.last_used_at = datetime.now(UTC)

                        database.commit()

                return FallbackResult(
                    response=response,
                    provider=attempt.provider,
                    model=response.model,
                    credential_id=attempt.credential_id,
                    credential_source=(attempt.credential_source),
                    attempts_made=attempt_number,
                    fallback_used=attempt_number > 1,
                )

            except (
                *PROVIDER_ERRORS,
                ProviderCircuitOpenError,
            ) as error:
                latency_ms = round(
                    (perf_counter() - started_at) * 1000,
                    2,
                )

                self._log_attempt(
                    database,
                    project_id=project_id,
                    request_id=request_id,
                    attempt_number=attempt_number,
                    attempt=attempt,
                    succeeded=False,
                    latency_ms=latency_ms,
                    error=error,
                )

                if not isinstance(
                    error,
                    ProviderCircuitOpenError,
                ):
                    self._record_circuit_failure(provider=attempt.provider)

                PROVIDER_ATTEMPTS_TOTAL.labels(
                    provider=attempt.provider,
                    model=attempt.model,
                    result="failure",
                    error_type=type(error).__name__,
                ).inc()

                self.health_service.record_event(
                    database,
                    provider=attempt.provider,
                    model=attempt.model,
                    succeeded=False,
                    latency_ms=latency_ms,
                    error_type=type(error).__name__,
                    retry_count=0,
                )

                errors.append(f"{attempt.provider}/{attempt.model}: {error}")

        raise AllProviderAttemptsFailedError(
            "All provider attempts failed.",
            errors=errors,
        )
