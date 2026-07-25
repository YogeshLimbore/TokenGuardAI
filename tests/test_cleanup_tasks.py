"""
Retention cleanup tasks.

These Celery tasks delete data, so the tests pin exactly which rows are in
scope: anything newer than the retention cutoff, and anything still running,
must survive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import (
    BackgroundJob,
    LLMRequestLog,
    ProviderHealthEvent,
)
from app.workers.tasks import cleanup_tasks
from tests.conftest import create_project_with_key

pytestmark = pytest.mark.integration


@pytest.fixture
def project_id(database: Session) -> int:
    project, _ = create_project_with_key(
        database,
        name="Cleanup Test Project",
    )
    return project.id


@pytest.fixture(autouse=True)
def _use_the_test_session(
    database: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point the task's own session factory at the rolled-back test session."""
    monkeypatch.setattr(
        cleanup_tasks,
        "SessionLocal",
        lambda: database,
    )


def _request_log(
    *,
    project_id: int,
    request_id: str,
    age_days: float,
) -> LLMRequestLog:
    return LLMRequestLog(
        project_id=project_id,
        request_id=request_id,
        provider="ollama",
        model="llama3.2",
        prompt="Hello.",
        response="Hi.",
        input_tokens=2,
        output_tokens=2,
        total_tokens=4,
        estimated_cost_usd=0.0,
        latency_ms=1.0,
        status="success",
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )


def test_old_request_logs_are_deleted_and_recent_ones_are_kept(
    database: Session,
    project_id: int,
) -> None:
    retention_days = get_settings().request_log_retention_days

    database.add_all(
        [
            _request_log(
                project_id=project_id,
                request_id="expired-1",
                age_days=retention_days + 5,
            ),
            _request_log(
                project_id=project_id,
                request_id="expired-2",
                age_days=retention_days + 1,
            ),
            _request_log(
                project_id=project_id,
                request_id="fresh",
                age_days=0.5,
            ),
        ]
    )
    database.commit()

    result = cleanup_tasks.cleanup_old_request_logs()

    assert result == {"deleted_request_logs": 2}

    remaining = database.scalars(
        select(LLMRequestLog.request_id).where(LLMRequestLog.project_id == project_id)
    ).all()

    assert list(remaining) == ["fresh"]


def test_request_log_cleanup_is_a_no_op_on_an_empty_table(
    database: Session,
    project_id: int,
) -> None:
    assert cleanup_tasks.cleanup_old_request_logs() == {"deleted_request_logs": 0}


def test_expired_jobs_and_old_health_events_are_deleted(
    database: Session,
    project_id: int,
) -> None:
    settings = get_settings()
    job_cutoff_days = settings.job_result_retention_days

    def job(
        job_id: str,
        *,
        completed_days_ago: float | None,
    ) -> BackgroundJob:
        completed_at = (
            None
            if completed_days_ago is None
            else datetime.now(UTC) - timedelta(days=completed_days_ago)
        )

        return BackgroundJob(
            job_id=job_id,
            project_id=project_id,
            job_type="semantic_evaluation",
            status="completed" if completed_at else "running",
            completed_at=completed_at,
        )

    def health_event(*, age_days: float) -> ProviderHealthEvent:
        return ProviderHealthEvent(
            provider="openrouter",
            model="openai/gpt-4.1-mini",
            succeeded=True,
            latency_ms=120.0,
            created_at=datetime.now(UTC) - timedelta(days=age_days),
        )

    database.add_all(
        [
            job("expired", completed_days_ago=job_cutoff_days + 2),
            job("recent", completed_days_ago=0.25),
            job("still-running", completed_days_ago=None),
            health_event(age_days=45),
            health_event(age_days=31),
            health_event(age_days=2),
        ]
    )
    database.commit()

    result = cleanup_tasks.cleanup_expired_cache()

    assert result == {
        "deleted_jobs": 1,
        "deleted_health_events": 2,
    }

    remaining_jobs = set(
        database.scalars(
            select(BackgroundJob.job_id).where(BackgroundJob.project_id == project_id)
        ).all()
    )

    assert remaining_jobs == {"recent", "still-running"}
    assert len(database.scalars(select(ProviderHealthEvent)).all()) == 1


def test_cache_cleanup_is_a_no_op_when_nothing_has_expired(
    database: Session,
    project_id: int,
) -> None:
    assert cleanup_tasks.cleanup_expired_cache() == {
        "deleted_jobs": 0,
        "deleted_health_events": 0,
    }
