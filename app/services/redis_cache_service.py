from __future__ import annotations

import json
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.schemas.chat import ChatCompletionRequest


class RedisCacheError(RuntimeError):
    pass


class RedisExactCacheService:
    def __init__(
        self,
        redis_client: Redis,
    ) -> None:
        self.redis = redis_client
        self.settings = get_settings()

    def generate_key(
        self,
        *,
        project_id: int,
        request: ChatCompletionRequest,
        provider: str,
        model: str,
    ) -> str:
        from app.services.cache_service import CacheService

        request = request.model_copy(update={"provider": provider})
        digest = CacheService.generate_cache_key(request, model, project_id)
        return f"{self.settings.redis_exact_cache_prefix}:project:{project_id}:{digest}"

    def get(
        self,
        cache_key: str,
    ) -> dict[str, Any] | None:
        try:
            payload = self.redis.get(cache_key)
        except RedisError as error:
            raise RedisCacheError("Redis cache lookup failed.") from error

        if payload is None:
            return None

        try:
            result = json.loads(payload)
        except json.JSONDecodeError as error:
            self.redis.delete(cache_key)

            raise RedisCacheError("Redis cache contained invalid JSON.") from error

        if not isinstance(result, dict):
            self.redis.delete(cache_key)
            return None

        return result

    def set(
        self,
        *,
        cache_key: str,
        value: dict[str, Any],
    ) -> None:
        try:
            self.redis.set(
                cache_key,
                json.dumps(
                    value,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                ex=self.settings.redis_cache_ttl_seconds,
            )
        except RedisError as error:
            raise RedisCacheError("Redis cache write failed.") from error

    def delete_project_cache(
        self,
        *,
        project_id: int,
    ) -> int:
        pattern = f"{self.settings.redis_exact_cache_prefix}:project:{project_id}:*"

        deleted = 0

        try:
            for key in self.redis.scan_iter(
                match=pattern,
                count=500,
            ):
                deleted += int(self.redis.delete(key))
        except RedisError as error:
            raise RedisCacheError("Failed to clear project cache.") from error

        return deleted
