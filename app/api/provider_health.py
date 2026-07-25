from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from app.cache.redis_client import (
    get_redis_client,
)
from app.database.connection import (
    get_database_session,
)
from app.database.models import (
    ProviderHealthEvent,
)
from app.dependencies.admin_authentication import (
    require_admin,
)
from app.schemas.provider_health import (
    ProviderHealthResponse,
)
from app.services.provider_health_service import (
    ProviderHealthService,
)
from app.services.redis_circuit_breaker_service import (
    RedisCircuitBreakerError,
    RedisCircuitBreakerService,
)

router = APIRouter(
    prefix="/v1/admin/provider-health",
    tags=["Admin Provider Health"],
    dependencies=[
        Depends(require_admin),
    ],
)


@router.get(
    "",
    response_model=list[ProviderHealthResponse],
)
async def list_provider_health(
    database: Session = Depends(get_database_session),
) -> list[ProviderHealthResponse]:
    combinations = database.execute(
        select(
            distinct(ProviderHealthEvent.provider),
            ProviderHealthEvent.model,
        )
    ).all()

    health_service = ProviderHealthService()

    breaker_service = RedisCircuitBreakerService(get_redis_client())

    results = []

    for provider, model in combinations:
        health = health_service.calculate_health(
            database,
            provider=provider,
            model=model,
        )

        try:
            circuit_state = breaker_service.get_state(provider=provider).state
        except RedisCircuitBreakerError:
            circuit_state = "unknown"

        results.append(
            ProviderHealthResponse(
                provider=provider,
                model=model,
                score=health.score,
                success_rate_percent=(health.success_rate_percent),
                average_latency_ms=(health.average_latency_ms),
                attempts=health.attempts,
                failures=health.failures,
                healthy=health.healthy,
                circuit_state=circuit_state,
            )
        )

    return results
