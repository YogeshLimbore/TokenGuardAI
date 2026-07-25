from __future__ import annotations

import time
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.observability.metrics import (
    CIRCUIT_BREAKER_STATE,
)


class ProviderCircuitOpenError(RuntimeError):
    pass


class RedisCircuitBreakerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CircuitState:
    provider: str
    state: str
    failure_count: int
    opened_at: float | None


class RedisCircuitBreakerService:
    def __init__(
        self,
        redis_client: Redis,
    ) -> None:
        self.redis = redis_client
        self.settings = get_settings()

    def _failure_key(
        self,
        provider: str,
    ) -> str:
        return f"tokenguard:circuit:{provider}:failures"

    def _opened_key(
        self,
        provider: str,
    ) -> str:
        return f"tokenguard:circuit:{provider}:opened_at"

    def get_state(
        self,
        *,
        provider: str,
    ) -> CircuitState:
        try:
            failures_value = self.redis.get(self._failure_key(provider))

            opened_value = self.redis.get(self._opened_key(provider))
        except RedisError as error:
            raise RedisCircuitBreakerError("Could not read circuit state.") from error

        failure_count = int(failures_value or 0)

        opened_at = float(opened_value) if opened_value else None

        state = "closed"

        if opened_at is not None:
            elapsed = time.time() - opened_at

            if elapsed >= self.settings.circuit_breaker_reset_timeout_seconds:
                state = "half-open"
            else:
                state = "open"

        CIRCUIT_BREAKER_STATE.labels(provider=provider).set(
            {
                "closed": 0.0,
                "half-open": 0.5,
                "open": 1.0,
            }[state]
        )

        return CircuitState(
            provider=provider,
            state=state,
            failure_count=failure_count,
            opened_at=opened_at,
        )

    def ensure_available(
        self,
        *,
        provider: str,
    ) -> CircuitState:
        state = self.get_state(provider=provider)

        if state.state == "open":
            retry_after = max(
                int(
                    self.settings.circuit_breaker_reset_timeout_seconds
                    - (time.time() - (state.opened_at or 0))
                ),
                1,
            )

            raise ProviderCircuitOpenError(
                f"Circuit is open for {provider}. Retry after {retry_after} seconds."
            )

        return state

    def record_success(
        self,
        *,
        provider: str,
    ) -> None:
        try:
            self.redis.delete(
                self._failure_key(provider),
                self._opened_key(provider),
            )
        except RedisError as error:
            raise RedisCircuitBreakerError("Could not reset circuit.") from error

        CIRCUIT_BREAKER_STATE.labels(provider=provider).set(0.0)

    def record_failure(
        self,
        *,
        provider: str,
    ) -> int:
        try:
            failures = int(self.redis.incr(self._failure_key(provider)))

            self.redis.expire(
                self._failure_key(provider),
                3600,
            )

            if failures >= self.settings.circuit_breaker_failure_threshold:
                self.redis.set(
                    self._opened_key(provider),
                    str(time.time()),
                    ex=(self.settings.circuit_breaker_reset_timeout_seconds * 2),
                )
        except RedisError as error:
            raise RedisCircuitBreakerError("Could not update circuit.") from error

        return failures
