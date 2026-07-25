from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult

from app.core.config import get_settings
from app.database.connection import SessionLocal
from app.database.models import (
    BackgroundJob,
    LLMRequestLog,
    ProviderHealthEvent,
)
from app.workers.celery_app import celery_app


@celery_app.task(
    name="tokenguard.cleanup_old_request_logs",
)
def cleanup_old_request_logs() -> dict[str, int]:
    # NOTE: Request logs may be subject to audit or compliance
    # retention requirements in some deployments. Adjust
    # REQUEST_LOG_RETENTION_DAYS (or archive instead of delete)
    # before enabling this task in a regulated environment.
    settings = get_settings()
    database = SessionLocal()

    try:
        cutoff = datetime.now(UTC) - timedelta(days=settings.request_log_retention_days)

        # Session.execute is typed as returning Result; a DELETE always
        # yields a CursorResult, which is what carries rowcount.
        result = cast(
            "CursorResult[Any]",
            database.execute(
                delete(LLMRequestLog).where(LLMRequestLog.created_at < cutoff)
            ),
        )

        database.commit()

        return {"deleted_request_logs": result.rowcount or 0}

    finally:
        database.close()


@celery_app.task(
    name="tokenguard.cleanup_expired_cache",
)
def cleanup_expired_cache() -> dict[str, int]:
    # Redis TTL already removes exact-cache entries automatically.
    # This task instead removes:
    # - expired background-job results
    # - old provider health events
    settings = get_settings()
    database = SessionLocal()

    try:
        job_cutoff = datetime.now(UTC) - timedelta(
            days=settings.job_result_retention_days
        )

        health_cutoff = datetime.now(UTC) - timedelta(days=30)

        jobs_result = cast(
            "CursorResult[Any]",
            database.execute(
                delete(BackgroundJob).where(BackgroundJob.completed_at < job_cutoff)
            ),
        )

        health_result = cast(
            "CursorResult[Any]",
            database.execute(
                delete(ProviderHealthEvent).where(
                    ProviderHealthEvent.created_at < health_cutoff
                )
            ),
        )

        database.commit()

        return {
            "deleted_jobs": jobs_result.rowcount or 0,
            "deleted_health_events": (health_result.rowcount or 0),
        }

    finally:
        database.close()
