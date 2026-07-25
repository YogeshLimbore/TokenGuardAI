"""
Rate limiting for unauthenticated authentication endpoints.

Login and refresh happen before a session exists, so limits are keyed by
client IP address rather than by project or user ID. This slows down
credential-stuffing and brute-force attacks. Redis is best-effort: if it
is unreachable, authentication must remain available, so failures here
fail open rather than blocking the request.
"""

from __future__ import annotations

import time

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings


class AuthRateLimitExceededError(RuntimeError):
    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__("Too many authentication attempts. Please try again later.")
        self.retry_after_seconds = retry_after_seconds


class AuthRateLimitService:
    def __init__(self, redis_client: Redis) -> None:
        self.redis = redis_client
        self.settings = get_settings()

    def enforce(
        self,
        *,
        scope: str,
        identifier: str,
        limit: int,
        window_seconds: int = 60,
    ) -> None:
        """
        Raise AuthRateLimitExceededError if *identifier* has made more
        than *limit* requests to *scope* within the current window.
        """
        current_window = int(time.time() // window_seconds)

        key = (
            f"{self.settings.redis_rate_limit_prefix}:"
            f"auth:{scope}:{identifier}:{current_window}"
        )

        try:
            pipeline = self.redis.pipeline()
            pipeline.incr(key)
            pipeline.expire(key, window_seconds * 2)
            current_count, _ = pipeline.execute()
        except RedisError:
            return

        if int(current_count) > limit:
            seconds_into_window = int(time.time()) % window_seconds

            raise AuthRateLimitExceededError(
                retry_after_seconds=max(
                    window_seconds - seconds_into_window,
                    1,
                )
            )
