import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.database.connection import Base, get_database_session
from app.main import app

PLAINTEXT_ADMIN_KEY = "tg_admin_test_key_value"
ADMIN_KEY_HASH = hashlib.sha256(PLAINTEXT_ADMIN_KEY.encode("utf-8")).hexdigest()


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
        yield test_client

    settings.admin_api_key_hash = original_hash
    app.dependency_overrides.clear()


def test_missing_admin_key_returns_401(client: TestClient):
    response = client.get("/v1/projects")

    assert response.status_code == 401
    assert response.json()["detail"] == "X-TokenGuard-Admin-Key header is required."


def test_invalid_admin_key_returns_401(client: TestClient):
    response = client.get(
        "/v1/projects",
        headers={"X-TokenGuard-Admin-Key": "tg_admin_wrong"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid administrator API key."


def test_valid_admin_key_authenticates_successfully(
    client: TestClient,
):
    response = client.get(
        "/v1/projects",
        headers={"X-TokenGuard-Admin-Key": PLAINTEXT_ADMIN_KEY},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_project_key_cannot_be_used_as_admin_key(
    client: TestClient,
):
    create_response = client.post(
        "/v1/projects",
        json={"name": "Admin Isolation Test"},
        headers={"X-TokenGuard-Admin-Key": PLAINTEXT_ADMIN_KEY},
    )
    assert create_response.status_code == 201
    project_id = create_response.json()["id"]

    key_response = client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={"name": "Isolation Key"},
        headers={"X-TokenGuard-Admin-Key": PLAINTEXT_ADMIN_KEY},
    )
    assert key_response.status_code == 201
    project_api_key = key_response.json()["api_key"]

    response = client.get(
        "/v1/projects",
        headers={"X-TokenGuard-Admin-Key": project_api_key},
    )

    assert response.status_code == 401
