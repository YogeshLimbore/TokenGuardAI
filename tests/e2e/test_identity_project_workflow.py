"""
End-to-end workflow: platform admin to first billed chat request.

Bootstrap platform administrator -> log in -> create organization ->
create project -> create project API key -> create provider credential ->
submit chat request -> read analytics.

The provider is faked so this test never contacts OpenRouter or Ollama.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.models import User
from app.dependencies.admin_authentication import require_admin
from app.main import app
from tests.conftest import TEST_PASSWORD
from tests.fakes.fake_provider import DEFAULT_CONTENT, FakeProvider

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


@pytest.fixture
def admin_client(client: TestClient) -> TestClient:
    """
    Client with the legacy shared admin key check stubbed out.

    Project and project-key routes still authenticate with
    ``X-TokenGuard-Admin-Key`` (see docs/migration-admin-to-jwt.md). The
    workflow under test here is the identity path, so the shared-key check
    is replaced rather than reimplemented.
    """
    app.dependency_overrides[require_admin] = lambda: True
    return client


def test_complete_project_workflow(
    admin_client: TestClient,
    database: Session,
    platform_admin_user: User,
) -> None:
    login_response = admin_client.post(
        "/v1/auth/login",
        json={
            "email": platform_admin_user.email,
            "password": TEST_PASSWORD,
        },
    )

    assert login_response.status_code == 200

    tokens = login_response.json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    bearer_headers = {"Authorization": f"Bearer {access_token}"}

    profile_response = admin_client.get(
        "/v1/auth/me",
        headers=bearer_headers,
    )

    assert profile_response.status_code == 200
    assert profile_response.json()["email"] == platform_admin_user.email
    assert profile_response.json()["is_platform_admin"] is True

    organization_response = admin_client.post(
        "/v1/organizations",
        headers=bearer_headers,
        json={
            "name": "E2E Organization",
            "slug": "e2e-organization",
        },
    )

    assert organization_response.status_code == 201
    organization_id = organization_response.json()["id"]

    project_response = admin_client.post(
        f"/v1/organizations/{organization_id}/projects",
        headers=bearer_headers,
        json={
            "name": "E2E Project",
            "description": "End-to-end test project",
            "daily_token_budget": 10_000,
            "daily_cost_budget_usd": 1,
            "maximum_tokens_per_request": 2_000,
            "maximum_cost_per_request_usd": 0.1,
            "requests_per_minute": 30,
        },
    )

    assert project_response.status_code == 201
    project_id = project_response.json()["id"]

    # The project must be reachable through its organization, and only
    # through its own organization.
    scoped_project_response = admin_client.get(
        f"/v1/organizations/{organization_id}/projects/{project_id}",
        headers=bearer_headers,
    )

    assert scoped_project_response.status_code == 200

    key_response = admin_client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={"name": "E2E Key", "expires_at": None},
    )

    assert key_response.status_code == 201

    project_api_key = key_response.json()["api_key"]
    assert project_api_key.startswith("tg_live_")

    project_headers = {"X-TokenGuard-Key": project_api_key}

    credential_response = admin_client.post(
        "/v1/credentials",
        headers=project_headers,
        json={
            "provider": "openrouter",
            "name": "E2E OpenRouter Credential",
            "api_key": "sk-or-testonly-0123456789abcdef",
            "is_default": True,
        },
    )

    assert credential_response.status_code == 201

    credential = credential_response.json()
    assert credential["masked_key"]
    # The plaintext key must never be echoed back.
    assert "sk-or-testonly-0123456789abcdef" not in credential_response.text

    fake_provider = FakeProvider()

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=fake_provider,
    ):
        chat_response = admin_client.post(
            "/v1/chat/completions",
            headers=project_headers,
            json={
                "provider": "ollama",
                "model": "llama3.2",
                "messages": [{"role": "user", "content": "What is an API gateway?"}],
                "temperature": 0.2,
                "max_tokens": 100,
                "use_cache": True,
                "use_semantic_cache": False,
                "auto_route": False,
            },
        )

    assert chat_response.status_code == 200

    chat_payload = chat_response.json()
    assert chat_payload["content"] == DEFAULT_CONTENT
    assert chat_payload["optimization"]["cache_hit"] is False
    assert chat_payload["usage"]["total_tokens"] == 30
    assert len(fake_provider.calls) == 1

    analytics_response = admin_client.get(
        "/v1/analytics/summary",
        headers=project_headers,
    )

    assert analytics_response.status_code == 200

    analytics = analytics_response.json()
    assert analytics["project"]["id"] == project_id
    assert analytics["summary"]["requests"]["total_requests"] == 1
    assert analytics["summary"]["requests"]["cache_hits"] == 0
    assert analytics["summary"]["tokens"]["actual_tokens_consumed"] == 30

    refresh_response = admin_client.post(
        "/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert refresh_response.status_code == 200
    assert refresh_response.json()["refresh_token"] != refresh_token

    logout_response = admin_client.post(
        "/v1/auth/logout",
        json={"refresh_token": refresh_response.json()["refresh_token"]},
    )

    assert logout_response.status_code in {200, 204}


def test_cached_second_request_does_not_call_provider(
    admin_client: TestClient,
    platform_admin_user: User,
) -> None:
    login_response = admin_client.post(
        "/v1/auth/login",
        json={
            "email": platform_admin_user.email,
            "password": TEST_PASSWORD,
        },
    )
    bearer_headers = {
        "Authorization": f"Bearer {login_response.json()['access_token']}"
    }

    organization_id = admin_client.post(
        "/v1/organizations",
        headers=bearer_headers,
        json={"name": "Cache Org", "slug": "cache-org"},
    ).json()["id"]

    project_id = admin_client.post(
        f"/v1/organizations/{organization_id}/projects",
        headers=bearer_headers,
        json={"name": "Cache Project"},
    ).json()["id"]

    project_api_key = admin_client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={"name": "Cache Key", "expires_at": None},
    ).json()["api_key"]

    body = {
        "provider": "ollama",
        "model": "llama3.2",
        "messages": [{"role": "user", "content": "Explain caching."}],
        "temperature": 0.0,
        "use_cache": True,
        "use_semantic_cache": False,
        "auto_route": False,
    }

    fake_provider = FakeProvider()

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=fake_provider,
    ):
        first = admin_client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": project_api_key},
            json=body,
        )
        second = admin_client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": project_api_key},
            json=body,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["optimization"]["cache_hit"] is False
    assert second.json()["optimization"]["cache_hit"] is True
    assert second.json()["optimization"]["cache_type"] == "exact"
    assert len(fake_provider.calls) == 1
