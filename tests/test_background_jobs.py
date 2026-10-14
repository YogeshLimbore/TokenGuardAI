import json
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import (
    Base,
    SessionLocal,
    get_database_session,
)
from app.database.models import BackgroundJob, Project
from app.dependencies.admin_authentication import require_admin
from app.main import app
from app.workers.tasks.evaluation_tasks import (
    run_semantic_evaluation,
)


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
    app.dependency_overrides[require_admin] = lambda: True

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def _create_project_and_key(client: TestClient, name: str) -> str:
    create_response = client.post(
        "/v1/projects",
        json={"name": name},
    )
    assert create_response.status_code == 201
    project_id = create_response.json()["id"]

    key_response = client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={
            "name": f"{name}-key",
            "scopes": ["jobs:create", "jobs:read", "jobs:cancel"],
        },
    )
    assert key_response.status_code == 201

    return key_response.json()["api_key"]


def test_job_creation_returns_202(client: TestClient):
    api_key = _create_project_and_key(client, "Job Project A")

    with patch("app.api.jobs.run_semantic_evaluation.apply_async") as mock_apply_async:
        response = client.post(
            "/v1/jobs/evaluations/semantic-cache",
            json={"dataset_path": ("evaluation/semantic_cache_dataset.json")},
            headers={"X-TokenGuard-Key": api_key},
        )

    assert response.status_code == 202

    body = response.json()
    assert body["status"] == "queued"
    assert body["progress_percent"] == 0.0
    assert body["job_type"] == "semantic_cache_evaluation"

    mock_apply_async.assert_called_once()
    _, kwargs = mock_apply_async.call_args
    assert kwargs["task_id"] == body["job_id"]
    assert kwargs["kwargs"]["dataset_path"] == (
        "evaluation/semantic_cache_dataset.json"
    )


def test_job_creation_rejects_path_traversal(
    client: TestClient,
):
    api_key = _create_project_and_key(client, "Job Project B")

    response = client.post(
        "/v1/jobs/evaluations/semantic-cache",
        json={"dataset_path": "../../etc/passwd"},
        headers={"X-TokenGuard-Key": api_key},
    )

    assert response.status_code == 422


def test_job_creation_rejects_paths_outside_evaluation_dir(
    client: TestClient,
):
    api_key = _create_project_and_key(client, "Job Project C")

    response = client.post(
        "/v1/jobs/evaluations/semantic-cache",
        json={"dataset_path": "config/model_pricing.json"},
        headers={"X-TokenGuard-Key": api_key},
    )

    assert response.status_code == 422


def test_project_can_read_its_own_job(client: TestClient):
    api_key = _create_project_and_key(client, "Job Project D")

    with patch("app.api.jobs.run_semantic_evaluation.apply_async"):
        create_response = client.post(
            "/v1/jobs/evaluations/semantic-cache",
            json={},
            headers={"X-TokenGuard-Key": api_key},
        )

    job_id = create_response.json()["job_id"]

    get_response = client.get(
        f"/v1/jobs/{job_id}",
        headers={"X-TokenGuard-Key": api_key},
    )

    assert get_response.status_code == 200
    assert get_response.json()["job_id"] == job_id


def test_project_cannot_read_another_projects_job(
    client: TestClient,
):
    owner_key = _create_project_and_key(client, "Job Owner Project")
    other_key = _create_project_and_key(client, "Job Other Project")

    with patch("app.api.jobs.run_semantic_evaluation.apply_async"):
        create_response = client.post(
            "/v1/jobs/evaluations/semantic-cache",
            json={},
            headers={"X-TokenGuard-Key": owner_key},
        )

    job_id = create_response.json()["job_id"]

    get_response = client.get(
        f"/v1/jobs/{job_id}",
        headers={"X-TokenGuard-Key": other_key},
    )

    assert get_response.status_code == 404


def test_completed_result_is_persisted(client: TestClient):
    api_key = _create_project_and_key(client, "Job Project E")

    with patch("app.api.jobs.run_semantic_evaluation.apply_async"):
        create_response = client.post(
            "/v1/jobs/evaluations/semantic-cache",
            json={},
            headers={"X-TokenGuard-Key": api_key},
        )

    job_id = create_response.json()["job_id"]

    # Simulate the worker completing the job out-of-band, the way
    # the real Celery task updates the row when it finishes.
    override = app.dependency_overrides[get_database_session]
    database_generator = override()
    database = next(database_generator)

    try:
        job = database.scalar(
            select(BackgroundJob).where(BackgroundJob.job_id == job_id)
        )
        job.status = "completed"
        job.progress_percent = 100.0
        job.result_json = json.dumps({"dataset_size": 20})
        job.completed_at = datetime.now(UTC)
        database.commit()
    finally:
        database_generator.close()

    get_response = client.get(
        f"/v1/jobs/{job_id}",
        headers={"X-TokenGuard-Key": api_key},
    )

    assert get_response.status_code == 200
    body = get_response.json()
    assert body["status"] == "completed"
    assert body["result"] == {"dataset_size": 20}


def test_cancellation_marks_queued_job_cancelled(
    client: TestClient,
):
    api_key = _create_project_and_key(client, "Job Project F")

    with patch("app.api.jobs.run_semantic_evaluation.apply_async"):
        create_response = client.post(
            "/v1/jobs/evaluations/semantic-cache",
            json={},
            headers={"X-TokenGuard-Key": api_key},
        )

    job_id = create_response.json()["job_id"]

    with patch("app.api.jobs.celery_app.control.revoke") as mock_revoke:
        cancel_response = client.delete(
            f"/v1/jobs/{job_id}",
            headers={"X-TokenGuard-Key": api_key},
        )

    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"
    mock_revoke.assert_called_once_with(
        job_id,
        terminate=False,
    )

    # Cancelling an already-terminal job is a harmless no-op.
    second_cancel_response = client.delete(
        f"/v1/jobs/{job_id}",
        headers={"X-TokenGuard-Key": api_key},
    )
    assert second_cancel_response.status_code == 200
    assert second_cancel_response.json()["status"] == "cancelled"


def test_worker_failure_marks_job_failed():
    # The Celery task uses the module-level SessionLocal (the real
    # database configured for this process), not the per-request
    # SQLite override used by the API-level tests above. It also
    # enforces a real foreign key to `projects`, so a real project
    # row must exist first.
    database = SessionLocal()

    try:
        project = Project(name=f"job-failure-test-{uuid4()}")
        database.add(project)
        database.commit()
        database.refresh(project)
        real_project_id = project.id

        job = BackgroundJob(
            job_id=f"test-worker-failure-job-{uuid4()}",
            project_id=real_project_id,
            job_type="semantic_cache_evaluation",
            status="queued",
            progress_percent=0.0,
        )
        database.add(job)
        database.commit()
        database.refresh(job)
        database_job_id = job.id

    finally:
        database.close()

    # Celery tasks can run synchronously (without a broker or
    # worker process) via .apply(), which is exactly what a real
    # worker would execute for a dataset that does not exist.
    eager_result = run_semantic_evaluation.apply(
        kwargs={
            "database_job_id": database_job_id,
            "project_id": 1,
            "dataset_path": "evaluation/does_not_exist.json",
        },
    )

    assert eager_result.failed() or eager_result.state in {
        "FAILURE",
        "IGNORED",
    }

    verification_database = SessionLocal()

    try:
        refreshed_job = verification_database.get(
            BackgroundJob,
            database_job_id,
        )
        assert refreshed_job.status == "failed"
        assert refreshed_job.error_message is not None

    finally:
        verification_database.execute(
            delete(BackgroundJob).where(BackgroundJob.id == database_job_id)
        )
        verification_database.execute(
            delete(Project).where(Project.id == real_project_id)
        )
        verification_database.commit()
        verification_database.close()
