from __future__ import annotations

import time
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings


class RedisRateLimitError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RedisRateLimitService:
    def __init__(
        self,
        redis_client: Redis,
    ) -> None:
        self.redis = redis_client
        self.settings = get_settings()

    def enforce(
        self,
        *,
        project_id: int,
        provider: str,
        limit: int,
    ) -> RateLimitResult:
        current_window = int(time.time() // 60)

        key = (
            f"{self.settings.redis_rate_limit_prefix}:"
            f"project:{project_id}:"
            f"provider:{provider}:"
            f"window:{current_window}"
        )

        try:
            pipeline = self.redis.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, 120)
            current_count, _ = pipeline.execute()
        except RedisError as error:
            raise RedisRateLimitError("Redis rate-limit operation failed.") from error

        current_count = int(current_count)

        remaining = max(
            limit - current_count,
            0,
        )

        seconds_into_window = int(time.time()) % 60
        retry_after_seconds = max(
            60 - seconds_into_window,
            1,
        )

        if current_count > limit:
            raise RateLimitExceededError(
                provider=provider,
                limit=limit,
                retry_after_seconds=retry_after_seconds,
            )

        return RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=remaining,
            retry_after_seconds=retry_after_seconds,
        )
