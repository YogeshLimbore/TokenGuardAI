import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base, get_database_session
from app.dependencies.admin_authentication import require_admin
from app.main import app


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

    # These tests exercise *project*-key authentication, not admin
    # authentication, so the admin dependency is bypassed here. Admin
    # authentication itself is covered by test_admin_authentication.py.
    app.dependency_overrides[require_admin] = lambda: True

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _create_project_and_key(client: TestClient) -> str:
    create_response = client.post(
        "/v1/projects",
        json={
            "name": "Auth Test Project",
            "daily_token_budget": 100_000,
            "daily_cost_budget_usd": 1.0,
            "maximum_tokens_per_request": 10_000,
            "maximum_cost_per_request_usd": 0.05,
            "requests_per_minute": 30,
        },
    )

    assert create_response.status_code == 201
    project_id = create_response.json()["id"]

    key_response = client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={"name": "Test Key"},
    )

    assert key_response.status_code == 201

    return key_response.json()["api_key"]


def test_missing_key_returns_401(client: TestClient):
    response = client.get("/v1/credentials")

    assert response.status_code == 401
    assert response.json()["detail"] == "X-TokenGuard-Key header is required."


def test_invalid_key_returns_401(client: TestClient):
    response = client.get(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": "tg_live_invalid_key"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or inactive project API key."


def test_valid_key_authenticates_successfully(
    client: TestClient,
):
    api_key = _create_project_and_key(client)

    response = client.get(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": api_key},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_revoked_key_returns_401(client: TestClient):
    api_key = _create_project_and_key(client)

    keys_response = client.get(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": api_key},
    )
    assert keys_response.status_code == 200

    # Find the project + key we just created so we can revoke it.
    projects_response = client.get("/v1/projects")
    project_id = projects_response.json()[0]["id"]

    list_keys_response = client.get(f"/v1/projects/{project_id}/api-keys")
    key_id = list_keys_response.json()[0]["id"]

    revoke_response = client.delete(f"/v1/projects/{project_id}/api-keys/{key_id}")
    assert revoke_response.status_code == 200

    response = client.get(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": api_key},
    )

    assert response.status_code == 401


def test_two_projects_have_independent_keys(
    client: TestClient,
):
    first_key = _create_project_and_key(client)

    second_create_response = client.post(
        "/v1/projects",
        json={"name": "Second Project"},
    )
    second_project_id = second_create_response.json()["id"]

    second_key_response = client.post(
        f"/v1/projects/{second_project_id}/api-keys",
        json={"name": "Second Key"},
    )
    second_key = second_key_response.json()["api_key"]

    assert first_key != second_key

    first_response = client.get(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": first_key},
    )
    second_response = client.get(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": second_key},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
