from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy.orm import Session

from app.database.connection import (
    get_database_session,
)
from app.database.models import Project
from app.dependencies.authentication import (
    get_current_project,
)
from app.schemas.analytics import (
    ProjectAnalyticsResponse,
    ProjectIdentity,
)
from app.services.analytics_service import (
    AnalyticsService,
)

router = APIRouter(
    prefix="/v1/analytics",
    tags=["Analytics"],
)


@router.get(
    "/summary",
    response_model=ProjectAnalyticsResponse,
)
async def get_analytics_summary(
    days: int = Query(
        default=30,
        ge=1,
        le=365,
    ),
    recent_request_limit: int = Query(
        default=50,
        ge=1,
        le=500,
    ),
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> ProjectAnalyticsResponse:
    """
    Return this project's aggregate request, token, cache, latency,
    provider, model, and daily-usage statistics.

    All figures are scoped strictly to the authenticated project's
    ``project_id`` -- one project can never see another project's data.
    """

    service = AnalyticsService()

    return ProjectAnalyticsResponse(
        project=ProjectIdentity(
            id=project.id,
            name=project.name,
        ),
        summary=service.get_summary(
            database,
            project_id=project.id,
        ),
        provider_usage=(
            service.get_provider_usage(
                database,
                project_id=project.id,
            )
        ),
        model_usage=(
            service.get_model_usage(
                database,
                project_id=project.id,
            )
        ),
        daily_usage=service.get_daily_usage(
            database,
            project_id=project.id,
            days=days,
        ),
        recent_requests=(
            service.get_recent_requests(
                database,
                project_id=project.id,
                limit=recent_request_limit,
            )
        ),
    )
