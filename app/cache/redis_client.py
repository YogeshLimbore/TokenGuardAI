from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings


class RedisUnavailableError(RuntimeError):
    pass


@lru_cache
def get_redis_client() -> Redis:
    settings = get_settings()

    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
        health_check_interval=30,
    )


def verify_redis_connection() -> bool:
    client = get_redis_client()

    try:
        return bool(client.ping())
    except RedisError as error:
        raise RedisUnavailableError("Redis is unavailable.") from error
