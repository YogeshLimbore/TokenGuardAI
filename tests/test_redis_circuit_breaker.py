import time
import uuid

import pytest
from redis import Redis

from app.core.config import Settings, get_settings
from app.services import (
    redis_circuit_breaker_service as circuit_breaker_module,
)
from app.services.redis_circuit_breaker_service import (
    ProviderCircuitOpenError,
    RedisCircuitBreakerService,
)


@pytest.fixture
def redis_client():
    settings = get_settings()
    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
    )

    yield client

    client.close()


def _make_service(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    *,
    circuit_breaker_enabled: bool = True,
    circuit_breaker_failure_threshold: int = 2,
    circuit_breaker_reset_timeout_seconds: int = 1,
) -> RedisCircuitBreakerService:
    test_settings = Settings(
        circuit_breaker_enabled=circuit_breaker_enabled,
        circuit_breaker_failure_threshold=(circuit_breaker_failure_threshold),
        circuit_breaker_reset_timeout_seconds=(circuit_breaker_reset_timeout_seconds),
    )

    monkeypatch.setattr(
        circuit_breaker_module,
        "get_settings",
        lambda: test_settings,
    )

    return RedisCircuitBreakerService(redis_client)


@pytest.fixture
def provider_name() -> str:
    # A unique key per test avoids cross-test interference on a
    # shared Redis instance.
    return f"test-provider-{uuid.uuid4().hex[:8]}"


def _cleanup(
    redis_client: Redis,
    provider: str,
) -> None:
    redis_client.delete(
        f"tokenguard:circuit:{provider}:failures",
        f"tokenguard:circuit:{provider}:opened_at",
    )


def test_circuit_starts_closed(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    provider_name: str,
) -> None:
    service = _make_service(monkeypatch, redis_client)

    try:
        state = service.get_state(provider=provider_name)

        assert state.state == "closed"
        assert state.failure_count == 0

        # Should not raise.
        service.ensure_available(provider=provider_name)
    finally:
        _cleanup(redis_client, provider_name)


def test_circuit_opens_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    provider_name: str,
) -> None:
    service = _make_service(
        monkeypatch,
        redis_client,
        circuit_breaker_failure_threshold=2,
    )

    try:
        service.record_failure(provider=provider_name)
        state = service.get_state(provider=provider_name)
        assert state.state == "closed"

        service.record_failure(provider=provider_name)
        state = service.get_state(provider=provider_name)
        assert state.state == "open"
    finally:
        _cleanup(redis_client, provider_name)


def test_circuit_blocks_while_open(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    provider_name: str,
) -> None:
    service = _make_service(
        monkeypatch,
        redis_client,
        circuit_breaker_failure_threshold=1,
        circuit_breaker_reset_timeout_seconds=30,
    )

    try:
        service.record_failure(provider=provider_name)

        with pytest.raises(ProviderCircuitOpenError):
            service.ensure_available(provider=provider_name)
    finally:
        _cleanup(redis_client, provider_name)


def test_circuit_enters_half_open_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    provider_name: str,
) -> None:
    service = _make_service(
        monkeypatch,
        redis_client,
        circuit_breaker_failure_threshold=1,
        circuit_breaker_reset_timeout_seconds=1,
    )

    try:
        service.record_failure(provider=provider_name)

        state = service.get_state(provider=provider_name)
        assert state.state == "open"

        time.sleep(1.2)

        state = service.get_state(provider=provider_name)
        assert state.state == "half-open"

        # A half-open circuit allows a trial request through.
        service.ensure_available(provider=provider_name)
    finally:
        _cleanup(redis_client, provider_name)


def test_circuit_closes_after_successful_trial(
    monkeypatch: pytest.MonkeyPatch,
    redis_client: Redis,
    provider_name: str,
) -> None:
    service = _make_service(
        monkeypatch,
        redis_client,
        circuit_breaker_failure_threshold=1,
        circuit_breaker_reset_timeout_seconds=1,
    )

    try:
        service.record_failure(provider=provider_name)
        assert service.get_state(provider=provider_name).state == "open"

        time.sleep(1.2)

        assert service.get_state(provider=provider_name).state == "half-open"

        service.record_success(provider=provider_name)

        state = service.get_state(provider=provider_name)
        assert state.state == "closed"
        assert state.failure_count == 0
    finally:
        _cleanup(redis_client, provider_name)
