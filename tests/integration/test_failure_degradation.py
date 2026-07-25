"""
Degradation behaviour when a dependency or provider fails.

These are the in-process equivalents of the manual runbook in
``docs/failure-simulation.md``: instead of stopping containers, the
dependency is replaced with one that raises the same errors the real client
would raise. The manual drills remain valuable for verifying container
restarts and Celery recovery; these tests keep the code paths honest on
every commit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database.models import (
    ExactResponseCache,
    Project,
    ProviderAttemptLog,
)
from app.providers.ollama import OllamaProviderError
from app.providers.openrouter import OpenRouterProviderError
from tests.conftest import create_project_with_key
from tests.fakes.fake_provider import FakeProvider

pytestmark = [pytest.mark.integration]

REDIS_FAILURE = RedisConnectionError(
    "Error 10061 connecting to localhost:6379. Connection refused."
)


class BrokenRedis:
    """Raises the same error a stopped Redis server produces."""

    def __getattr__(self, name: str):
        def raise_connection_error(*args, **kwargs):
            raise REDIS_FAILURE

        return raise_connection_error


class BrokenSession:
    """Stands in for a session whose connection has dropped."""

    def execute(self, *args, **kwargs):
        raise OperationalError(
            "SELECT 1",
            {},
            Exception("connection to server at localhost, port 5433 failed"),
        )

    def close(self) -> None:
        return None


@pytest.fixture
def project_and_key(database: Session) -> tuple[Project, str]:
    return create_project_with_key(database, name="Degradation Project")


def _chat_body(prompt: str, **overrides) -> dict:
    body = {
        "provider": "ollama",
        "model": "llama3.2",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "use_cache": True,
        "use_semantic_cache": False,
        "auto_route": False,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Redis outage
# ---------------------------------------------------------------------------


def test_health_reports_degraded_when_redis_is_unavailable(
    client: TestClient,
) -> None:
    with patch(
        "app.api.health.get_redis_client",
        return_value=BrokenRedis(),
    ):
        response = client.get("/health")

    assert response.status_code == 200

    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["database"]["status"] == "healthy"
    assert payload["redis"]["status"] == "unhealthy"

    # The failure must not disclose where Redis lives.
    body = response.text
    for detail in ("localhost", "6379", "10061", "redis://"):
        assert detail not in body


def test_health_reports_unhealthy_when_both_dependencies_fail(
    client: TestClient,
) -> None:
    from app.database.connection import get_database_session
    from app.main import app

    app.dependency_overrides[get_database_session] = lambda: BrokenSession()

    try:
        with patch(
            "app.api.health.get_redis_client",
            return_value=BrokenRedis(),
        ):
            response = client.get("/health")
    finally:
        app.dependency_overrides.pop(get_database_session, None)

    assert response.status_code == 200

    payload = response.json()
    assert payload["status"] == "unhealthy"
    assert payload["database"]["status"] == "unhealthy"

    for detail in ("5433", "psycopg", "Traceback", "tokenguard_password"):
        assert detail not in response.text


def test_chat_still_succeeds_when_redis_is_unavailable(
    client: TestClient,
    database: Session,
    project_and_key: tuple[Project, str],
) -> None:
    """
    Redis powers exact caching, rate limiting, and circuit-breaker state.
    All three have database-backed or fail-open fallbacks, so a Redis
    outage must degrade caching rather than reject traffic.
    """
    _, api_key = project_and_key

    fake_provider = FakeProvider()

    with (
        patch(
            "app.api.chat.get_redis_client",
            return_value=BrokenRedis(),
        ),
        patch(
            "app.services.fallback_service.get_redis_client",
            return_value=BrokenRedis(),
        ),
        patch(
            "app.services.fallback_service.ProviderFactory.create",
            return_value=fake_provider,
        ),
    ):
        first = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": api_key},
            json=_chat_body("Does the gateway survive a Redis outage?"),
        )

    assert first.status_code == 200
    assert first.json()["optimization"]["cache_hit"] is False
    assert len(fake_provider.calls) == 1

    # The PostgreSQL-backed cache still records the response, so the
    # gateway recovers cache coverage without Redis.
    assert database.scalar(select(ExactResponseCache)) is not None


def test_login_still_works_when_redis_is_unavailable(
    client: TestClient,
    database: Session,
) -> None:
    """Auth rate limiting is best effort: it must fail open, not closed."""
    from tests.conftest import TEST_PASSWORD, create_user

    user = create_user(database, email="redis-outage@example.com")

    with patch(
        "app.api.auth.get_redis_client",
        return_value=BrokenRedis(),
    ):
        response = client.post(
            "/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        )

    assert response.status_code == 200
    assert response.json()["access_token"]


# ---------------------------------------------------------------------------
# Provider outage
# ---------------------------------------------------------------------------


def _provider_router(*, openrouter_fails: bool, ollama_fails: bool):
    """Return a ProviderFactory.create replacement keyed by provider name."""
    ollama_provider = FakeProvider(provider_name="ollama")

    def create(provider_name: str, api_key: str | None = None):
        if provider_name == "openrouter":
            if openrouter_fails:
                return _FailingProvider(
                    OpenRouterProviderError("OpenRouter is unreachable.")
                )
            return FakeProvider(provider_name="openrouter")

        if ollama_fails:
            return _FailingProvider(OllamaProviderError("Ollama is unreachable."))

        return ollama_provider

    return create, ollama_provider


class _FailingProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.provider_name = "failing"

    async def generate(self, **kwargs):
        raise self.error


def test_openrouter_outage_falls_back_to_local_ollama(
    client: TestClient,
    database: Session,
    project_and_key: tuple[Project, str],
) -> None:
    project, api_key = project_and_key

    credential_response = client.post(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": api_key},
        json={
            "provider": "openrouter",
            "name": "Outage Credential",
            "api_key": "sk-or-testonly-outage-key-0123456789",
            "is_default": True,
        },
    )
    assert credential_response.status_code == 201

    create, ollama_provider = _provider_router(
        openrouter_fails=True,
        ollama_fails=False,
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        side_effect=create,
    ):
        response = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": api_key},
            json=_chat_body(
                "Fall back when the hosted provider is down.",
                provider="openrouter",
                model="openai/gpt-4.1-mini",
            ),
        )

    assert response.status_code == 200

    payload = response.json()
    assert payload["provider"] == "ollama"
    assert payload["reliability"]["fallback_used"] is True
    assert payload["reliability"]["provider_attempts"] == 2
    assert len(ollama_provider.calls) == 1

    attempts = list(
        database.scalars(
            select(ProviderAttemptLog)
            .where(ProviderAttemptLog.project_id == project.id)
            .order_by(ProviderAttemptLog.attempt_number)
        )
    )

    assert [attempt.provider for attempt in attempts] == [
        "openrouter",
        "ollama",
    ]
    assert attempts[0].succeeded is False
    assert attempts[0].error_type == "OpenRouterProviderError"
    assert attempts[1].succeeded is True

    # The failed attempt must not leak the provider credential.
    assert "sk-or-testonly-outage-key-0123456789" not in (
        attempts[0].error_message or ""
    )


def test_total_provider_outage_returns_503_and_caches_nothing(
    client: TestClient,
    database: Session,
    project_and_key: tuple[Project, str],
) -> None:
    project, api_key = project_and_key

    client.post(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": api_key},
        json={
            "provider": "openrouter",
            "name": "Outage Credential",
            "api_key": "sk-or-testonly-outage-key-0123456789",
            "is_default": True,
        },
    )

    create, _ = _provider_router(
        openrouter_fails=True,
        ollama_fails=True,
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        side_effect=create,
    ):
        response = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": api_key},
            json=_chat_body(
                "Everything is down.",
                provider="openrouter",
                model="openai/gpt-4.1-mini",
            ),
        )

    assert response.status_code == 503
    assert "Traceback" not in response.text

    # No partial or failed response may be cached.
    assert database.scalar(select(ExactResponseCache)) is None

    attempts = list(
        database.scalars(
            select(ProviderAttemptLog).where(
                ProviderAttemptLog.project_id == project.id
            )
        )
    )

    # Attempt history is preserved for post-incident review.
    assert len(attempts) == 2
    assert all(attempt.succeeded is False for attempt in attempts)
