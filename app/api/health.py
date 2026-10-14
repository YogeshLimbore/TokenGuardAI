import logging

from fastapi import APIRouter, Depends, Response, status
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.cache.redis_client import get_redis_client
from app.core.config import get_settings
from app.database.connection import get_database_session
from app.schemas.health import (
    DependencyHealth,
    DetailedHealthResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["Health"],
)

# /health is typically reachable from load balancers and uptime monitors,
# so dependency failures are reported as a fixed string. Driver errors
# embed hostnames, ports, and sometimes DSN credentials, which must stay
# in the server-side log instead.
_DATABASE_UNAVAILABLE_MESSAGE = "PostgreSQL is not reachable."
_REDIS_UNAVAILABLE_MESSAGE = "Redis is not reachable."


@router.get(
    "/health",
    response_model=DetailedHealthResponse,
)
async def health_check(
    database: Session = Depends(get_database_session),
) -> DetailedHealthResponse:
    settings = get_settings()

    database_health = DependencyHealth(status="healthy")

    redis_health = DependencyHealth(status="healthy")

    try:
        database.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        logger.error(
            "Health check could not reach PostgreSQL: %s",
            error,
        )

        database_health = DependencyHealth(
            status="unhealthy",
            message=_DATABASE_UNAVAILABLE_MESSAGE,
        )

    try:
        get_redis_client().ping()
    except RedisError as error:
        logger.warning(
            "Health check could not reach Redis: %s",
            error,
        )

        redis_health = DependencyHealth(
            status="unhealthy",
            message=_REDIS_UNAVAILABLE_MESSAGE,
        )

    dependencies = [
        database_health.status,
        redis_health.status,
    ]

    if all(status_value == "healthy" for status_value in dependencies):
        overall_status = "healthy"
    elif any(status_value == "healthy" for status_value in dependencies):
        overall_status = "degraded"
    else:
        overall_status = "unhealthy"

    return DetailedHealthResponse(
        status=overall_status,
        application=settings.app_name,
        version=settings.app_version,
        database=database_health,
        redis=redis_health,
    )


@router.get("/live")
async def liveness_check() -> dict[str, str]:
    """Process liveness; dependency state is deliberately excluded."""
    return {"status": "alive"}


@router.get("/ready", response_model=DetailedHealthResponse)
async def readiness_check(
    response: Response,
    database: Session = Depends(get_database_session),
) -> DetailedHealthResponse:
    """Return 503 until both required runtime dependencies are reachable."""
    result = await health_check(database)
    if result.status != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
