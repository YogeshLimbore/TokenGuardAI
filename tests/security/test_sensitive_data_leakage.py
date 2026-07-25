"""
Sensitive data must not survive anywhere observable.

For a request containing PII, the raw values must be absent from the
PostgreSQL request log, the exact-cache row, every Redis value and key, the
security audit trail, the Prometheus exposition, and application logs. For a
provider response containing a secret, the response must be blocked and never
cached or returned.

Every value used here is synthetic test data.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    ExactResponseCache,
    LLMRequestLog,
    Project,
    SecurityAuditEvent,
)
from tests.conftest import create_project_with_key
from tests.fakes.fake_provider import FakeProvider

pytestmark = [pytest.mark.security, pytest.mark.integration]

TEST_EMAIL = "test.user@example.com"
TEST_PHONE = "+1 202 555 0187"
TEST_CARD = "4111 1111 1111 1111"
LEAKED_PROJECT_KEY = "tg_live_testonly12345678901234567890"


@pytest.fixture
def project_and_key(database: Session) -> tuple[Project, str]:
    return create_project_with_key(database, name="Leakage Project")


def _chat_body(prompt: str) -> dict:
    return {
        "provider": "ollama",
        "model": "llama3.2",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "use_cache": True,
        "use_semantic_cache": False,
        "auto_route": False,
    }


def _redis_dump(redis_client: Redis) -> str:
    """Return every key and string value in the test keyspace."""
    parts: list[str] = []

    for key in redis_client.scan_iter(match="*"):
        parts.append(str(key))

        # Only string keys have a GET representation; other types are
        # covered by their key name, which is already recorded above.
        if redis_client.type(key) != "string":
            continue

        value = redis_client.get(key)

        if value is not None:
            parts.append(str(value))

    return "\n".join(parts)


def test_pii_is_absent_from_logs_caches_and_audit_records(
    client: TestClient,
    database: Session,
    redis_client: Redis,
    project_and_key: tuple[Project, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, api_key = project_and_key

    prompt = f"Email {TEST_EMAIL} and call {TEST_PHONE} to confirm the order."

    fake_provider = FakeProvider(content="Acknowledged.")

    with caplog.at_level(logging.DEBUG):
        with patch(
            "app.services.fallback_service.ProviderFactory.create",
            return_value=fake_provider,
        ):
            response = client.post(
                "/v1/chat/completions",
                headers={"X-TokenGuard-Key": api_key},
                json=_chat_body(prompt),
            )

    assert response.status_code == 200
    assert response.json()["governance"]["sanitized"] is True

    # The provider never sees the raw values.
    sent_text = " ".join(
        message.content for message in fake_provider.calls[0]["messages"]
    )
    assert TEST_EMAIL not in sent_text

    request_log = database.scalar(select(LLMRequestLog))
    assert request_log is not None
    assert TEST_EMAIL not in (request_log.prompt or "")
    assert TEST_EMAIL not in (request_log.response or "")

    cache_rows = list(database.scalars(select(ExactResponseCache)))
    for row in cache_rows:
        assert TEST_EMAIL not in row.prompt
        assert TEST_EMAIL not in row.cache_key
        assert TEST_EMAIL not in (row.response or "")

    audit_events = list(database.scalars(select(SecurityAuditEvent)))
    assert audit_events
    for event in audit_events:
        assert TEST_EMAIL not in (event.sanitized_summary or "")
        assert TEST_EMAIL not in (event.detected_categories_json or "")

    redis_contents = _redis_dump(redis_client)
    assert TEST_EMAIL not in redis_contents

    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert TEST_EMAIL not in log_text


def test_credit_card_request_is_blocked_and_leaves_no_trace(
    client: TestClient,
    database: Session,
    redis_client: Redis,
    project_and_key: tuple[Project, str],
) -> None:
    _, api_key = project_and_key

    fake_provider = FakeProvider(content="Should never be produced.")

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=fake_provider,
    ):
        response = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": api_key},
            json=_chat_body(f"Save my card {TEST_CARD} for later."),
        )

    assert response.status_code == 422
    assert fake_provider.calls == []
    assert TEST_CARD not in response.text

    assert database.scalar(select(ExactResponseCache)) is None
    assert TEST_CARD not in _redis_dump(redis_client)

    for event in database.scalars(select(SecurityAuditEvent)):
        assert TEST_CARD not in (event.sanitized_summary or "")
        assert TEST_CARD not in (event.detected_categories_json or "")


def test_leaked_secret_in_output_is_blocked_and_never_cached(
    client: TestClient,
    database: Session,
    redis_client: Redis,
    project_and_key: tuple[Project, str],
) -> None:
    _, api_key = project_and_key

    fake_provider = FakeProvider(
        content=f"Your key is {LEAKED_PROJECT_KEY}. Keep it safe.",
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=fake_provider,
    ):
        response = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": api_key},
            json=_chat_body("What is my project key?"),
        )

    # Blocked, and the secret is not echoed to the caller.
    assert response.status_code == 422
    assert LEAKED_PROJECT_KEY not in response.text

    # Not cached, in PostgreSQL or Redis.
    assert database.scalar(select(ExactResponseCache)) is None
    assert LEAKED_PROJECT_KEY not in _redis_dump(redis_client)

    # Recorded only as a category, never in raw form.
    event = database.scalar(
        select(SecurityAuditEvent).where(
            SecurityAuditEvent.event_type == "output_secret_detected"
        )
    )
    assert event is not None
    assert event.severity == "critical"
    assert LEAKED_PROJECT_KEY not in (event.sanitized_summary or "")
    assert LEAKED_PROJECT_KEY not in (event.detected_categories_json or "")
    assert "tokenguard_project_key" in (event.detected_categories_json or "")

    request_log = database.scalar(select(LLMRequestLog))
    assert request_log is not None
    assert request_log.status == "blocked"
    assert request_log.response is None

    # A retry of the same prompt must go back to the provider rather than
    # serving a cached copy of the blocked answer.
    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=fake_provider,
    ):
        retry = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": api_key},
            json=_chat_body("What is my project key?"),
        )

    assert retry.status_code == 422
    assert len(fake_provider.calls) == 2


def test_project_api_keys_are_not_exposed_by_metrics_or_health(
    client: TestClient,
    project_and_key: tuple[Project, str],
) -> None:
    _, api_key = project_and_key

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=FakeProvider(),
    ):
        client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": api_key},
            json=_chat_body("Warm up the metrics."),
        )

    metrics = client.get("/metrics")
    health = client.get("/health")

    assert api_key not in metrics.text
    assert api_key not in health.text

    # Prometheus labels must not carry free-form prompt text either.
    assert "Warm up the metrics" not in metrics.text


def test_pii_analyzer_loggers_are_pinned_above_debug() -> None:
    """
    Presidio logs the text it analyzes at DEBUG. Those loggers must stay
    above DEBUG so switching the app to debug logging cannot start writing
    raw PII into the log stream.
    """
    from app.observability.logging import configure_logging

    configure_logging()

    for name in ("presidio-analyzer", "presidio-anonymizer"):
        assert logging.getLogger(name).getEffectiveLevel() > logging.DEBUG


def test_error_responses_do_not_leak_infrastructure_details(
    client: TestClient,
) -> None:
    unauthenticated = client.get("/v1/analytics/summary")

    assert unauthenticated.status_code == 401

    body = unauthenticated.text.lower()
    for leak in ("postgresql", "psycopg", "redis://", "traceback", "5433"):
        assert leak not in body
