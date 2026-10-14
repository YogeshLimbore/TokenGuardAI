from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert

from app.database.connection import SessionLocal
from app.database.models import (
    LLMRequestLog,
    ProjectUsageDaily,
)
from app.workers.celery_app import celery_app


@celery_app.task(
    name="tokenguard.aggregate_project_usage",
)
def aggregate_project_usage(days: int = 2) -> dict[str, int]:
    database = SessionLocal()

    try:
        today = datetime.now(UTC).date()
        days = max(1, min(days, 90))
        first_day = today - timedelta(days=days - 1)

        start = datetime.combine(
            first_day,
            datetime.min.time(),
            tzinfo=UTC,
        )

        statement = (
            select(
                LLMRequestLog.project_id,
                func.date(LLMRequestLog.created_at).label("usage_date"),
                func.count(LLMRequestLog.id).label("request_count"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "completed",
                            1,
                        ),
                        else_=0,
                    )
                ).label("successful_requests"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "failed",
                            1,
                        ),
                        else_=0,
                    )
                ).label("failed_requests"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status.in_(
                                [
                                    "cache_hit",
                                    "semantic_cache_hit",
                                ]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("cache_hits"),
                func.coalesce(
                    func.sum(LLMRequestLog.input_tokens),
                    0,
                ).label("input_tokens"),
                func.coalesce(
                    func.sum(LLMRequestLog.output_tokens),
                    0,
                ).label("output_tokens"),
                func.coalesce(
                    func.sum(LLMRequestLog.total_tokens),
                    0,
                ).label("total_tokens"),
                func.coalesce(
                    func.sum(LLMRequestLog.estimated_cost_usd),
                    0.0,
                ).label("estimated_cost_usd"),
            )
            .where(
                LLMRequestLog.created_at >= start,
                LLMRequestLog.project_id.is_not(None),
            )
            .group_by(
                LLMRequestLog.project_id,
                func.date(LLMRequestLog.created_at),
            )
        )

        rows = database.execute(statement).all()

        for row in rows:
            insert_statement = insert(ProjectUsageDaily).values(
                project_id=row.project_id,
                usage_date=row.usage_date,
                request_count=int(row.request_count or 0),
                successful_requests=int(row.successful_requests or 0),
                failed_requests=int(row.failed_requests or 0),
                cache_hits=int(row.cache_hits or 0),
                input_tokens=int(row.input_tokens or 0),
                output_tokens=int(row.output_tokens or 0),
                total_tokens=int(row.total_tokens or 0),
                estimated_cost_usd=float(row.estimated_cost_usd or 0.0),
            )

            update_statement = insert_statement.on_conflict_do_update(
                constraint="uq_project_usage_daily",
                set_={
                    "request_count": (insert_statement.excluded.request_count),
                    "successful_requests": (
                        insert_statement.excluded.successful_requests
                    ),
                    "failed_requests": (insert_statement.excluded.failed_requests),
                    "cache_hits": (insert_statement.excluded.cache_hits),
                    "input_tokens": (insert_statement.excluded.input_tokens),
                    "output_tokens": (insert_statement.excluded.output_tokens),
                    "total_tokens": (insert_statement.excluded.total_tokens),
                    "estimated_cost_usd": (
                        insert_statement.excluded.estimated_cost_usd
                    ),
                    "updated_at": datetime.now(UTC),
                },
            )

            database.execute(update_statement)

        database.commit()

        return {
            "projects_aggregated": len({row.project_id for row in rows}),
            "daily_records_aggregated": len(rows),
        }

    finally:
        database.close()
