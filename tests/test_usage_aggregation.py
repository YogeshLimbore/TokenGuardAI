from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.database.connection import SessionLocal
from app.database.models import (
    LLMRequestLog,
    Project,
    ProjectUsageDaily,
)
from app.workers.tasks.usage_tasks import (
    aggregate_project_usage,
)


@pytest.fixture
def project_id():
    # aggregate_project_usage() runs against the real, module-level
    # SessionLocal (the test Postgres database configured by
    # tests/conftest.py), not a per-request SQLite override, so this
    # fixture creates and cleans up real rows there.
    database = SessionLocal()

    try:
        project = Project(
            name=f"usage-aggregation-{uuid4()}",
        )
        database.add(project)
        database.commit()
        database.refresh(project)
        created_project_id = project.id

    finally:
        database.close()

    yield created_project_id

    database = SessionLocal()

    try:
        database.execute(
            delete(ProjectUsageDaily).where(
                ProjectUsageDaily.project_id == created_project_id
            )
        )
        database.execute(
            delete(LLMRequestLog).where(LLMRequestLog.project_id == created_project_id)
        )
        database.execute(delete(Project).where(Project.id == created_project_id))
        database.commit()

    finally:
        database.close()


def _insert_log(
    database,
    *,
    project_id: int,
    status_value: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    database.add(
        LLMRequestLog(
            project_id=project_id,
            request_id=str(uuid4()),
            provider="ollama",
            model="llama3.2",
            prompt="test prompt",
            response="test response",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated_cost_usd=cost_usd,
            latency_ms=10.0,
            status=status_value,
            created_at=datetime.now(UTC),
        )
    )


def _get_usage_rows(project_id: int) -> list[ProjectUsageDaily]:
    database = SessionLocal()

    try:
        return list(
            database.scalars(
                select(ProjectUsageDaily).where(
                    ProjectUsageDaily.project_id == project_id
                )
            )
        )
    finally:
        database.close()


def test_aggregation_creates_one_row_per_project_per_day(
    project_id: int,
):
    database = SessionLocal()

    try:
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.01,
        )
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=5,
            output_tokens=15,
            cost_usd=0.02,
        )
        database.commit()

    finally:
        database.close()

    aggregate_project_usage()

    rows = _get_usage_rows(project_id)

    assert len(rows) == 1
    assert rows[0].request_count == 2


def test_repeated_aggregation_updates_instead_of_duplicating(
    project_id: int,
):
    database = SessionLocal()

    try:
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.01,
        )
        database.commit()

    finally:
        database.close()

    aggregate_project_usage()

    database = SessionLocal()

    try:
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=7,
            output_tokens=13,
            cost_usd=0.03,
        )
        database.commit()

    finally:
        database.close()

    aggregate_project_usage()

    rows = _get_usage_rows(project_id)

    assert len(rows) == 1
    assert rows[0].request_count == 2
    assert rows[0].total_tokens == 50


def test_cache_hits_are_counted_correctly(project_id: int):
    database = SessionLocal()

    try:
        _insert_log(
            database,
            project_id=project_id,
            status_value="cache_hit",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
        _insert_log(
            database,
            project_id=project_id,
            status_value="semantic_cache_hit",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.01,
        )
        database.commit()

    finally:
        database.close()

    aggregate_project_usage()

    rows = _get_usage_rows(project_id)
    assert len(rows) == 1

    row = rows[0]
    assert row.cache_hits == 2
    assert row.request_count == 3


def test_failed_requests_are_counted_correctly(
    project_id: int,
):
    database = SessionLocal()

    try:
        _insert_log(
            database,
            project_id=project_id,
            status_value="failed",
            input_tokens=8,
            output_tokens=0,
            cost_usd=0.0,
        )
        _insert_log(
            database,
            project_id=project_id,
            status_value="failed",
            input_tokens=3,
            output_tokens=0,
            cost_usd=0.0,
        )
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.01,
        )
        database.commit()

    finally:
        database.close()

    aggregate_project_usage()

    rows = _get_usage_rows(project_id)
    assert len(rows) == 1

    row = rows[0]
    assert row.failed_requests == 2
    assert row.successful_requests == 1


def test_token_and_cost_totals_match_request_logs(
    project_id: int,
):
    database = SessionLocal()

    try:
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.10,
        )
        _insert_log(
            database,
            project_id=project_id,
            status_value="completed",
            input_tokens=200,
            output_tokens=75,
            cost_usd=0.20,
        )
        database.commit()

    finally:
        database.close()

    aggregate_project_usage()

    rows = _get_usage_rows(project_id)
    assert len(rows) == 1

    row = rows[0]
    assert row.input_tokens == 300
    assert row.output_tokens == 125
    assert row.total_tokens == 425
    assert round(row.estimated_cost_usd, 2) == 0.30
