from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import RateLimitEvent


class RateLimitExceededError(RuntimeError):
    def __init__(
        self,
        *,
        provider: str,
        limit: int,
        retry_after_seconds: int,
    ) -> None:
        super().__init__(
            f"Rate limit exceeded for {provider}. Maximum {limit} requests per minute."
        )

        self.provider = provider
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


class RateLimitService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @staticmethod
    def _window_start() -> datetime:
        return datetime.now(UTC) - timedelta(minutes=1)

    def enforce(
        self,
        database: Session,
        *,
        request_id: str,
        provider: str,
        requests_per_minute: int,
        scope: str = "global",
        project_id: int | None = None,
    ) -> None:
        if not self.settings.rate_limiting_enabled:
            return

        window_start = self._window_start()

        cleanup_statement = delete(RateLimitEvent).where(
            RateLimitEvent.created_at < datetime.now(UTC) - timedelta(hours=1)
        )

        database.execute(cleanup_statement)

        count_statement = select(func.count(RateLimitEvent.id)).where(
            RateLimitEvent.scope == scope,
            RateLimitEvent.provider == provider,
            RateLimitEvent.created_at >= window_start,
        )

        current_count = int(database.scalar(count_statement) or 0)

        if current_count >= requests_per_minute:
            database.rollback()

            raise RateLimitExceededError(
                provider=provider,
                limit=requests_per_minute,
                retry_after_seconds=60,
            )

        database.add(
            RateLimitEvent(
                project_id=project_id,
                scope=scope,
                provider=provider,
                request_id=request_id,
            )
        )

        database.commit()
