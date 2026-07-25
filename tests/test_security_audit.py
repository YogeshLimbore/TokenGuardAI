import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.database.connection import Base, get_database_session
from app.database.models import SecurityAuditEvent
from app.main import app

PLAINTEXT_ADMIN_KEY = "tg_admin_security_audit_test_key"
ADMIN_KEY_HASH = hashlib.sha256(PLAINTEXT_ADMIN_KEY.encode("utf-8")).hexdigest()

ADMIN_HEADERS = {"X-TokenGuard-Admin-Key": PLAINTEXT_ADMIN_KEY}


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    def override_get_database_session():
        database = TestingSessionLocal()

        try:
            yield database
        finally:
            database.close()

    app.dependency_overrides[get_database_session] = override_get_database_session

    settings = get_settings()
    original_hash = settings.admin_api_key_hash
    settings.admin_api_key_hash = ADMIN_KEY_HASH

    with TestClient(app) as test_client:
        yield test_client, TestingSessionLocal

    settings.admin_api_key_hash = original_hash
    app.dependency_overrides.clear()


def _insert_event(
    SessionLocal,
    **overrides,
) -> None:
    defaults = dict(
        project_id=1,
        request_id="req-1",
        correlation_id="corr-1",
        event_type="governance_input_blocked",
        severity="high",
        action="block",
        provider="ollama",
        model="llama3.2",
        risk_score=90.0,
        detected_categories_json=json.dumps(["instruction_override"]),
        sanitized_summary=(
            "Prompt-injection risk exceeded the configured block threshold."
        ),
        metadata_json=None,
        created_at=datetime.now(UTC),
    )
    defaults.update(overrides)

    with SessionLocal() as database:
        database.add(SecurityAuditEvent(**defaults))
        database.commit()


def test_missing_admin_key_is_rejected(client):
    test_client, _ = client

    response = test_client.get("/v1/admin/security-events")

    assert response.status_code == 401


def test_project_api_key_cannot_access_security_events(
    client,
):
    test_client, _ = client

    create_response = test_client.post(
        "/v1/projects",
        json={"name": "Security Audit Isolation Project"},
        headers=ADMIN_HEADERS,
    )
    assert create_response.status_code == 201
    project_id = create_response.json()["id"]

    key_response = test_client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={"name": "Isolation Key"},
        headers=ADMIN_HEADERS,
    )
    assert key_response.status_code == 201
    project_api_key = key_response.json()["api_key"]

    response = test_client.get(
        "/v1/admin/security-events",
        headers={"X-TokenGuard-Admin-Key": project_api_key},
    )

    assert response.status_code == 401


def test_list_security_events_returns_recorded_events(
    client,
):
    test_client, SessionLocal = client

    _insert_event(
        SessionLocal,
        event_type="governance_input_blocked",
    )
    _insert_event(
        SessionLocal,
        event_type="sensitive_data_redacted",
        severity="medium",
        action="redact",
    )

    response = test_client.get(
        "/v1/admin/security-events",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    events = response.json()
    assert len(events) == 2
    assert {event["event_type"] for event in events} == {
        "governance_input_blocked",
        "sensitive_data_redacted",
    }


def test_filter_by_project_id(client):
    test_client, SessionLocal = client

    _insert_event(SessionLocal, project_id=1)
    _insert_event(SessionLocal, project_id=2)

    response = test_client.get(
        "/v1/admin/security-events",
        params={"project_id": 1},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]["project_id"] == 1


def test_filter_by_severity(client):
    test_client, SessionLocal = client

    _insert_event(SessionLocal, severity="high")
    _insert_event(SessionLocal, severity="medium")

    response = test_client.get(
        "/v1/admin/security-events",
        params={"severity": "medium"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]["severity"] == "medium"


def test_filter_by_event_type(client):
    test_client, SessionLocal = client

    _insert_event(SessionLocal, event_type="output_secret_detected")
    _insert_event(SessionLocal, event_type="model_access_denied")

    response = test_client.get(
        "/v1/admin/security-events",
        params={"event_type": "output_secret_detected"},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]["event_type"] == "output_secret_detected"


def test_detected_categories_are_parsed_from_json(client):
    test_client, SessionLocal = client

    _insert_event(
        SessionLocal,
        detected_categories_json=json.dumps(["CREDIT_CARD", "US_SSN"]),
    )

    response = test_client.get(
        "/v1/admin/security-events",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    events = response.json()
    assert events[0]["detected_categories"] == [
        "CREDIT_CARD",
        "US_SSN",
    ]


def test_limit_parameter_restricts_result_count(client):
    test_client, SessionLocal = client

    for index in range(5):
        _insert_event(SessionLocal, request_id=f"req-{index}")

    response = test_client.get(
        "/v1/admin/security-events",
        params={"limit": 2},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_events_are_ordered_newest_first(client):
    test_client, SessionLocal = client

    now = datetime.now(UTC)

    _insert_event(
        SessionLocal,
        request_id="older",
        created_at=now - timedelta(minutes=5),
    )
    _insert_event(
        SessionLocal,
        request_id="newer",
        created_at=now,
    )

    response = test_client.get(
        "/v1/admin/security-events",
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    events = response.json()
    assert events[0]["request_id"] == "newer"
    assert events[1]["request_id"] == "older"
