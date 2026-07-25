"""
Cross-project isolation for project-API-key scoped resources.

A project API key authenticates exactly one project. Every resource reached
with that key — credentials, background jobs, cached responses, analytics —
must be filtered by that project's ID, so swapping a numeric ID in the URL
cannot reach another tenant's data.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    BackgroundJob,
    ExactResponseCache,
    Project,
    ProviderCredential,
)
from tests.conftest import create_project_with_key
from tests.fakes.fake_provider import FakeProvider

pytestmark = [pytest.mark.security, pytest.mark.integration]

FOREIGN_PROVIDER_KEY = "sk-or-testonly-project-b-key-0123456789"


@pytest.fixture
def two_projects(
    database: Session,
) -> tuple[tuple[Project, str], tuple[Project, str]]:
    project_a, key_a = create_project_with_key(
        database,
        name="Isolation Project A",
    )
    project_b, key_b = create_project_with_key(
        database,
        name="Isolation Project B",
    )

    return (project_a, key_a), (project_b, key_b)


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


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_project_key_cannot_read_foreign_credential(
    client: TestClient,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (_, key_a), (_, key_b) = two_projects

    created = client.post(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": key_b},
        json={
            "provider": "openrouter",
            "name": "Project B Credential",
            "api_key": FOREIGN_PROVIDER_KEY,
            "is_default": True,
        },
    )
    assert created.status_code == 201
    credential_id = created.json()["id"]

    response = client.get(
        f"/v1/credentials/{credential_id}",
        headers={"X-TokenGuard-Key": key_a},
    )

    assert response.status_code == 404
    assert "Project B Credential" not in response.text


def test_project_key_cannot_mutate_foreign_credential(
    client: TestClient,
    database: Session,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (_, key_a), (_, key_b) = two_projects

    credential_id = client.post(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": key_b},
        json={
            "provider": "openrouter",
            "name": "Project B Credential",
            "api_key": FOREIGN_PROVIDER_KEY,
        },
    ).json()["id"]

    patched = client.patch(
        f"/v1/credentials/{credential_id}",
        headers={"X-TokenGuard-Key": key_a},
        json={"name": "Renamed By Project A"},
    )
    rotated = client.post(
        f"/v1/credentials/{credential_id}/rotate",
        headers={"X-TokenGuard-Key": key_a},
        json={"api_key": "sk-or-testonly-rotated-by-attacker"},
    )
    deleted = client.delete(
        f"/v1/credentials/{credential_id}",
        headers={"X-TokenGuard-Key": key_a},
    )

    assert patched.status_code == 404
    assert rotated.status_code == 404
    assert deleted.status_code == 404

    credential = database.get(ProviderCredential, credential_id)
    assert credential is not None
    assert credential.name == "Project B Credential"
    assert credential.is_active is True


def test_credential_list_is_scoped_to_the_authenticated_project(
    client: TestClient,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (_, key_a), (_, key_b) = two_projects

    client.post(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": key_b},
        json={
            "provider": "openrouter",
            "name": "Project B Credential",
            "api_key": FOREIGN_PROVIDER_KEY,
        },
    )

    response = client.get(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": key_a},
    )

    assert response.status_code == 200
    assert response.json() == []


def test_stored_credential_is_encrypted_at_rest(
    client: TestClient,
    database: Session,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    _, (_, key_b) = two_projects

    credential_id = client.post(
        "/v1/credentials",
        headers={"X-TokenGuard-Key": key_b},
        json={
            "provider": "openrouter",
            "name": "Project B Credential",
            "api_key": FOREIGN_PROVIDER_KEY,
        },
    ).json()["id"]

    credential = database.get(ProviderCredential, credential_id)
    assert credential is not None

    stored = credential.encrypted_api_key
    assert FOREIGN_PROVIDER_KEY not in stored
    assert FOREIGN_PROVIDER_KEY not in credential.masked_key


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------


def test_project_key_cannot_read_foreign_job(
    client: TestClient,
    database: Session,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (_, key_a), (project_b, key_b) = two_projects

    # Inserted directly so the test does not require a running Celery
    # worker; the isolation logic under test lives in the read path.
    database.add(
        BackgroundJob(
            job_id="job-owned-by-project-b",
            project_id=project_b.id,
            job_type="semantic_cache_evaluation",
            status="completed",
            progress_percent=100.0,
            result_json='{"secret": "project-b-results"}',
        )
    )
    database.commit()

    foreign_read = client.get(
        "/v1/jobs/job-owned-by-project-b",
        headers={"X-TokenGuard-Key": key_a},
    )
    owner_read = client.get(
        "/v1/jobs/job-owned-by-project-b",
        headers={"X-TokenGuard-Key": key_b},
    )

    assert foreign_read.status_code == 404
    assert "project-b-results" not in foreign_read.text
    assert owner_read.status_code == 200


def test_project_key_cannot_cancel_foreign_job(
    client: TestClient,
    database: Session,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (_, key_a), (project_b, _) = two_projects

    database.add(
        BackgroundJob(
            job_id="cancellable-project-b-job",
            project_id=project_b.id,
            job_type="semantic_cache_evaluation",
            status="queued",
        )
    )
    database.commit()

    response = client.delete(
        "/v1/jobs/cancellable-project-b-job",
        headers={"X-TokenGuard-Key": key_a},
    )

    assert response.status_code == 404

    job = database.scalar(
        select(BackgroundJob).where(BackgroundJob.job_id == "cancellable-project-b-job")
    )
    assert job is not None
    assert job.status == "queued"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_exact_cache_entries_do_not_cross_projects(
    client: TestClient,
    database: Session,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (_, key_a), (_, key_b) = two_projects

    prompt = "Which project am I?"

    provider_for_a = FakeProvider(content="Answer for project A")
    provider_for_b = FakeProvider(content="Answer for project B")

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=provider_for_a,
    ):
        first_a = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": key_a},
            json=_chat_body(prompt),
        )
        second_a = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": key_a},
            json=_chat_body(prompt),
        )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=provider_for_b,
    ):
        first_b = client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": key_b},
            json=_chat_body(prompt),
        )

    assert second_a.json()["optimization"]["cache_hit"] is True
    assert second_a.json()["content"] == "Answer for project A"

    # Project B asked the identical question but must be served by the
    # provider, never from project A's cache entry.
    assert first_b.status_code == 200
    assert first_b.json()["optimization"]["cache_hit"] is False
    assert first_b.json()["content"] == "Answer for project B"
    assert len(provider_for_b.calls) == 1
    assert first_a.json()["content"] == "Answer for project A"


def test_cache_rows_and_redis_keys_are_namespaced_by_project(
    client: TestClient,
    database: Session,
    redis_client: Redis,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (project_a, key_a), (project_b, key_b) = two_projects

    prompt = "Namespace check."

    for key in (key_a, key_b):
        with patch(
            "app.services.fallback_service.ProviderFactory.create",
            return_value=FakeProvider(),
        ):
            assert (
                client.post(
                    "/v1/chat/completions",
                    headers={"X-TokenGuard-Key": key},
                    json=_chat_body(prompt),
                ).status_code
                == 200
            )

    rows = list(
        database.scalars(
            select(ExactResponseCache).where(
                ExactResponseCache.project_id.in_([project_a.id, project_b.id])
            )
        )
    )

    # Same prompt, two projects: two independent rows, and the cache key
    # of one project must not match the other's.
    assert {row.project_id for row in rows} == {
        project_a.id,
        project_b.id,
    }

    keys_by_project = {row.project_id: row.cache_key for row in rows}
    assert keys_by_project[project_a.id] != keys_by_project[project_b.id]

    redis_keys = [
        key
        for key in redis_client.scan_iter(match="*exact-cache*")
        if isinstance(key, str)
    ]

    for redis_key in redis_keys:
        assert "project:" in redis_key


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def test_analytics_never_include_another_projects_requests(
    client: TestClient,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (project_a, key_a), (project_b, key_b) = two_projects

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=FakeProvider(),
    ):
        client.post(
            "/v1/chat/completions",
            headers={"X-TokenGuard-Key": key_b},
            json=_chat_body("Only project B sends this."),
        )

    analytics_a = client.get(
        "/v1/analytics/summary",
        headers={"X-TokenGuard-Key": key_a},
    )
    analytics_b = client.get(
        "/v1/analytics/summary",
        headers={"X-TokenGuard-Key": key_b},
    )

    assert analytics_a.status_code == 200
    assert analytics_a.json()["project"]["id"] == project_a.id
    assert analytics_a.json()["summary"]["requests"]["total_requests"] == 0

    assert analytics_b.json()["project"]["id"] == project_b.id
    assert analytics_b.json()["summary"]["requests"]["total_requests"] == 1


def test_revoked_project_key_stops_working(
    client: TestClient,
    database: Session,
    two_projects: tuple[tuple[Project, str], tuple[Project, str]],
) -> None:
    (project_a, key_a), _ = two_projects

    project = database.get(Project, project_a.id)
    assert project is not None
    project.is_active = False
    database.commit()

    response = client.get(
        "/v1/analytics/summary",
        headers={"X-TokenGuard-Key": key_a},
    )

    assert response.status_code == 401
