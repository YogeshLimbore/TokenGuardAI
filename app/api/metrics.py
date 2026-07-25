from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    generate_latest,
)

from app.core.config import get_settings

router = APIRouter(
    tags=["Observability"],
)


@router.get(
    "/metrics",
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    settings = get_settings()

    if not settings.enable_prometheus_metrics:
        return Response(status_code=404)

    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
