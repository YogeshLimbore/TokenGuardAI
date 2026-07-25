from __future__ import annotations

from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)

from sqlalchemy import (
    case,
    func,
    select,
)
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import (
    ProviderHealthEvent,
)
from app.observability.metrics import (
    PROVIDER_HEALTH_SCORE,
)


@dataclass(frozen=True)
class ProviderHealthResult:
    provider: str
    model: str
    score: float
    success_rate_percent: float
    average_latency_ms: float
    attempts: int
    failures: int
    healthy: bool


class ProviderHealthService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def record_event(
        self,
        database: Session,
        *,
        provider: str,
        model: str,
        succeeded: bool,
        latency_ms: float,
        error_type: str | None,
        retry_count: int,
    ) -> None:
        database.add(
            ProviderHealthEvent(
                provider=provider,
                model=model,
                succeeded=succeeded,
                latency_ms=latency_ms,
                error_type=error_type,
                retry_count=retry_count,
            )
        )

        database.commit()

    def calculate_health(
        self,
        database: Session,
        *,
        provider: str,
        model: str,
    ) -> ProviderHealthResult:
        window_start = datetime.now(UTC) - timedelta(
            minutes=(self.settings.provider_health_window_minutes)
        )

        statement = select(
            func.count(ProviderHealthEvent.id).label("attempts"),
            func.sum(
                case(
                    (
                        ProviderHealthEvent.succeeded.is_(True),
                        1,
                    ),
                    else_=0,
                )
            ).label("successes"),
            func.avg(ProviderHealthEvent.latency_ms).label("average_latency_ms"),
        ).where(
            ProviderHealthEvent.provider == provider,
            ProviderHealthEvent.model == model,
            ProviderHealthEvent.created_at >= window_start,
        )

        row = database.execute(statement).one()

        attempts = int(row.attempts or 0)
        successes = int(row.successes or 0)
        failures = attempts - successes

        if attempts == 0:
            success_rate = 100.0
            average_latency = 0.0
        else:
            success_rate = (successes / attempts) * 100

            average_latency = float(row.average_latency_ms or 0.0)

        latency_penalty = min(
            average_latency / 200,
            25,
        )

        failure_penalty = (100 - success_rate) * 0.75

        score = max(
            100 - failure_penalty - latency_penalty,
            0,
        )

        score = round(score, 2)

        PROVIDER_HEALTH_SCORE.labels(
            provider=provider,
            model=model,
        ).set(score)

        return ProviderHealthResult(
            provider=provider,
            model=model,
            score=score,
            success_rate_percent=round(
                success_rate,
                2,
            ),
            average_latency_ms=round(
                average_latency,
                2,
            ),
            attempts=attempts,
            failures=failures,
            healthy=(score >= self.settings.provider_unhealthy_score_threshold),
        )
