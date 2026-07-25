from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.database.models import (
    ExactResponseCache,
    LLMRequestLog,
    SemanticResponseCache,
)
from app.schemas.analytics import (
    AnalyticsSummaryResponse,
    CacheStatistics,
    DailyUsagePoint,
    LatencyStatistics,
    ModelUsageStatistics,
    ProviderUsageStatistics,
    RecentRequestItem,
    RequestStatistics,
    TokenStatistics,
)


class AnalyticsService:
    """
    Calculates usage and optimization statistics from TokenGuard logs.

    Every method requires a ``project_id`` so that one project's
    statistics can never include another project's requests, cache
    entries, or spend.
    """

    @staticmethod
    def _calculate_percentage(
        numerator: int | float,
        denominator: int | float,
    ) -> float:
        if denominator == 0:
            return 0.0

        return round(
            (numerator / denominator) * 100,
            2,
        )

    def get_summary(
        self,
        database: Session,
        *,
        project_id: int,
    ) -> AnalyticsSummaryResponse:
        # -----------------------------------------------------
        # Request counts
        # -----------------------------------------------------

        request_counts_statement = select(
            func.count(LLMRequestLog.id).label("total_requests"),
            func.sum(
                case(
                    (
                        LLMRequestLog.status.in_(
                            [
                                "completed",
                                "cache_hit",
                                "semantic_cache_hit",
                            ]
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("successful_requests"),
            func.sum(
                case(
                    (LLMRequestLog.status == "failed", 1),
                    else_=0,
                )
            ).label("failed_requests"),
            func.sum(
                case(
                    (LLMRequestLog.status == "completed", 1),
                    else_=0,
                )
            ).label("generated_requests"),
            func.sum(
                case(
                    (
                        LLMRequestLog.status.in_(["cache_hit", "semantic_cache_hit"]),
                        1,
                    ),
                    else_=0,
                )
            ).label("cache_hits"),
        ).where(LLMRequestLog.project_id == project_id)

        request_counts = database.execute(request_counts_statement).one()

        total_requests = int(request_counts.total_requests or 0)
        successful_requests = int(request_counts.successful_requests or 0)
        failed_requests = int(request_counts.failed_requests or 0)
        generated_requests = int(request_counts.generated_requests or 0)
        cache_hits = int(request_counts.cache_hits or 0)

        cache_hit_rate = self._calculate_percentage(
            cache_hits,
            successful_requests,
        )

        # -----------------------------------------------------
        # Actual token usage
        # -----------------------------------------------------

        actual_tokens_statement = select(
            func.coalesce(
                func.sum(LLMRequestLog.total_tokens),
                0,
            )
        ).where(
            LLMRequestLog.project_id == project_id,
            LLMRequestLog.status == "completed",
        )

        actual_tokens_consumed = int(database.scalar(actual_tokens_statement) or 0)

        # -----------------------------------------------------
        # Cache savings
        #
        # Every cache record stores the tokens used by the
        # original generation. Each hit avoids that amount.
        # Exact-match and semantic-match caches are combined
        # into a single set of savings statistics.
        # -----------------------------------------------------

        exact_cache_savings_statement = select(
            func.coalesce(
                func.sum(
                    ExactResponseCache.total_tokens * ExactResponseCache.hit_count
                ),
                0,
            ).label("tokens_avoided"),
            func.coalesce(
                func.sum(
                    ExactResponseCache.original_latency_ms
                    * ExactResponseCache.hit_count
                ),
                0.0,
            ).label("potential_generation_latency_ms"),
            func.count(ExactResponseCache.id).label("unique_cached_requests"),
            func.coalesce(
                func.sum(ExactResponseCache.hit_count),
                0,
            ).label("total_cache_hits"),
            func.coalesce(
                func.max(ExactResponseCache.hit_count),
                0,
            ).label("most_used_cache_entry_hits"),
        ).where(ExactResponseCache.project_id == project_id)

        exact_cache_savings = database.execute(exact_cache_savings_statement).one()

        semantic_cache_savings_statement = select(
            func.coalesce(
                func.sum(
                    SemanticResponseCache.total_tokens * SemanticResponseCache.hit_count
                ),
                0,
            ).label("tokens_avoided"),
            func.coalesce(
                func.sum(
                    SemanticResponseCache.original_latency_ms
                    * SemanticResponseCache.hit_count
                ),
                0.0,
            ).label("potential_generation_latency_ms"),
            func.count(SemanticResponseCache.id).label("unique_cached_requests"),
            func.coalesce(
                func.sum(SemanticResponseCache.hit_count),
                0,
            ).label("total_cache_hits"),
            func.coalesce(
                func.max(SemanticResponseCache.hit_count),
                0,
            ).label("most_used_cache_entry_hits"),
        ).where(SemanticResponseCache.project_id == project_id)

        semantic_cache_savings = database.execute(
            semantic_cache_savings_statement
        ).one()

        tokens_avoided = int(exact_cache_savings.tokens_avoided or 0) + int(
            semantic_cache_savings.tokens_avoided or 0
        )

        total_potential_tokens = actual_tokens_consumed + tokens_avoided

        token_saving_percent = self._calculate_percentage(
            tokens_avoided,
            total_potential_tokens,
        )

        unique_cached_requests = int(
            exact_cache_savings.unique_cached_requests or 0
        ) + int(semantic_cache_savings.unique_cached_requests or 0)

        total_cache_hits = int(exact_cache_savings.total_cache_hits or 0) + int(
            semantic_cache_savings.total_cache_hits or 0
        )

        most_used_cache_entry_hits = max(
            int(exact_cache_savings.most_used_cache_entry_hits or 0),
            int(semantic_cache_savings.most_used_cache_entry_hits or 0),
        )

        potential_generation_latency_ms = float(
            exact_cache_savings.potential_generation_latency_ms or 0.0
        ) + float(semantic_cache_savings.potential_generation_latency_ms or 0.0)

        # -----------------------------------------------------
        # Latency statistics
        # -----------------------------------------------------

        generated_latency_statement = select(
            func.coalesce(
                func.avg(LLMRequestLog.latency_ms),
                0.0,
            )
        ).where(
            LLMRequestLog.project_id == project_id,
            LLMRequestLog.status == "completed",
        )

        average_generated_latency_ms = round(
            float(database.scalar(generated_latency_statement) or 0.0),
            2,
        )

        cache_latency_statement = select(
            func.coalesce(
                func.avg(LLMRequestLog.latency_ms),
                0.0,
            )
        ).where(
            LLMRequestLog.project_id == project_id,
            LLMRequestLog.status.in_(["cache_hit", "semantic_cache_hit"]),
        )

        average_cache_latency_ms = round(
            float(database.scalar(cache_latency_statement) or 0.0),
            2,
        )

        total_actual_cache_latency_statement = select(
            func.coalesce(
                func.sum(LLMRequestLog.latency_ms),
                0.0,
            )
        ).where(
            LLMRequestLog.project_id == project_id,
            LLMRequestLog.status.in_(["cache_hit", "semantic_cache_hit"]),
        )

        total_actual_cache_latency_ms = float(
            database.scalar(total_actual_cache_latency_statement) or 0.0
        )

        estimated_time_saved_ms = round(
            max(
                potential_generation_latency_ms - total_actual_cache_latency_ms,
                0.0,
            ),
            2,
        )

        return AnalyticsSummaryResponse(
            requests=RequestStatistics(
                total_requests=total_requests,
                successful_requests=successful_requests,
                failed_requests=failed_requests,
                generated_requests=generated_requests,
                cache_hits=cache_hits,
                cache_hit_rate_percent=cache_hit_rate,
            ),
            tokens=TokenStatistics(
                actual_tokens_consumed=(actual_tokens_consumed),
                tokens_avoided=tokens_avoided,
                total_potential_tokens=(total_potential_tokens),
                token_saving_percent=(token_saving_percent),
            ),
            latency=LatencyStatistics(
                average_generated_latency_ms=(average_generated_latency_ms),
                average_cache_latency_ms=(average_cache_latency_ms),
                estimated_time_saved_ms=(estimated_time_saved_ms),
            ),
            cache=CacheStatistics(
                unique_cached_requests=(unique_cached_requests),
                total_cache_hits=total_cache_hits,
                most_used_cache_entry_hits=(most_used_cache_entry_hits),
            ),
            # Ollama is local, so direct API cost savings are zero.
            estimated_cost_saved_usd=0.0,
        )

    def get_provider_usage(
        self,
        database: Session,
        *,
        project_id: int,
    ) -> list[ProviderUsageStatistics]:
        statement = (
            select(
                LLMRequestLog.provider.label("provider"),
                func.count(LLMRequestLog.id).label("request_count"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "completed",
                            1,
                        ),
                        else_=0,
                    )
                ).label("generated_requests"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "cache_hit",
                            1,
                        ),
                        else_=0,
                    )
                ).label("exact_cache_hits"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "semantic_cache_hit",
                            1,
                        ),
                        else_=0,
                    )
                ).label("semantic_cache_hits"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "failed",
                            1,
                        ),
                        else_=0,
                    )
                ).label("failed_requests"),
                func.coalesce(
                    func.sum(LLMRequestLog.total_tokens),
                    0,
                ).label("total_tokens"),
                func.coalesce(
                    func.sum(LLMRequestLog.estimated_cost_usd),
                    0.0,
                ).label("total_cost_usd"),
                func.coalesce(
                    func.avg(LLMRequestLog.latency_ms),
                    0.0,
                ).label("average_latency_ms"),
            )
            .where(LLMRequestLog.project_id == project_id)
            .group_by(LLMRequestLog.provider)
            .order_by(func.count(LLMRequestLog.id).desc())
        )

        rows = database.execute(statement).all()

        return [
            ProviderUsageStatistics(
                provider=row.provider,
                request_count=int(row.request_count or 0),
                generated_requests=int(row.generated_requests or 0),
                exact_cache_hits=int(row.exact_cache_hits or 0),
                semantic_cache_hits=int(row.semantic_cache_hits or 0),
                failed_requests=int(row.failed_requests or 0),
                total_tokens=int(row.total_tokens or 0),
                total_cost_usd=round(
                    float(row.total_cost_usd or 0.0),
                    10,
                ),
                average_latency_ms=round(
                    float(row.average_latency_ms or 0.0),
                    2,
                ),
            )
            for row in rows
        ]

    def get_model_usage(
        self,
        database: Session,
        *,
        project_id: int,
    ) -> list[ModelUsageStatistics]:
        statement = (
            select(
                LLMRequestLog.provider,
                LLMRequestLog.model,
                func.count(LLMRequestLog.id).label("request_count"),
                func.coalesce(
                    func.sum(LLMRequestLog.total_tokens),
                    0,
                ).label("total_tokens"),
                func.coalesce(
                    func.sum(LLMRequestLog.estimated_cost_usd),
                    0.0,
                ).label("total_cost_usd"),
            )
            .where(
                LLMRequestLog.project_id == project_id,
            )
            .group_by(
                LLMRequestLog.provider,
                LLMRequestLog.model,
            )
            .order_by(func.count(LLMRequestLog.id).desc())
        )

        rows = database.execute(statement).all()

        return [
            ModelUsageStatistics(
                provider=row.provider,
                model=row.model,
                request_count=int(row.request_count or 0),
                total_tokens=int(row.total_tokens or 0),
                total_cost_usd=round(
                    float(row.total_cost_usd or 0.0),
                    10,
                ),
            )
            for row in rows
        ]

    def get_recent_requests(
        self,
        database: Session,
        *,
        project_id: int,
        limit: int = 50,
    ) -> list[RecentRequestItem]:
        statement = (
            select(LLMRequestLog)
            .where(LLMRequestLog.project_id == project_id)
            .order_by(LLMRequestLog.created_at.desc())
            .limit(limit)
        )

        rows = database.scalars(statement).all()

        return [
            RecentRequestItem(
                request_id=row.request_id,
                provider=row.provider,
                model=row.model,
                status=row.status,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                total_tokens=row.total_tokens,
                estimated_cost_usd=(row.estimated_cost_usd),
                latency_ms=row.latency_ms,
                created_at=row.created_at,
                error_message=row.error_message,
            )
            for row in rows
        ]

    def get_daily_usage(
        self,
        database: Session,
        *,
        project_id: int,
        days: int = 30,
    ) -> list[DailyUsagePoint]:
        start_date = datetime.now(UTC) - timedelta(days=days)

        request_date = func.date(LLMRequestLog.created_at)

        statement = (
            select(
                request_date.label("date"),
                func.count(LLMRequestLog.id).label("requests"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "completed",
                            1,
                        ),
                        else_=0,
                    )
                ).label("generated_requests"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "cache_hit",
                            1,
                        ),
                        else_=0,
                    )
                ).label("exact_cache_hits"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "semantic_cache_hit",
                            1,
                        ),
                        else_=0,
                    )
                ).label("semantic_cache_hits"),
                func.sum(
                    case(
                        (
                            LLMRequestLog.status == "failed",
                            1,
                        ),
                        else_=0,
                    )
                ).label("failed_requests"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                LLMRequestLog.status == "completed",
                                LLMRequestLog.total_tokens,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("actual_tokens"),
                func.coalesce(
                    func.sum(LLMRequestLog.estimated_cost_usd),
                    0.0,
                ).label("cost_usd"),
            )
            .where(
                LLMRequestLog.project_id == project_id,
                LLMRequestLog.created_at >= start_date,
            )
            .group_by(request_date)
            .order_by(request_date)
        )

        rows = database.execute(statement).all()

        return [
            DailyUsagePoint(
                date=str(row.date),
                requests=int(row.requests or 0),
                generated_requests=int(row.generated_requests or 0),
                exact_cache_hits=int(row.exact_cache_hits or 0),
                semantic_cache_hits=int(row.semantic_cache_hits or 0),
                failed_requests=int(row.failed_requests or 0),
                actual_tokens=int(row.actual_tokens or 0),
                cost_usd=round(
                    float(row.cost_usd or 0.0),
                    10,
                ),
            )
            for row in rows
        ]
