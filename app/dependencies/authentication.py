from datetime import UTC, datetime

from fastapi import (
    Depends,
    HTTPException,
    status,
)
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.connection import (
    get_database_session,
)
from app.database.models import (
    Project,
    ProjectAPIKey,
)
from app.observability.context import (
    set_project_id,
)
from app.services.project_key_service import (
    ProjectKeyService,
)

settings = get_settings()

project_api_key_header = APIKeyHeader(
    name="X-TokenGuard-Key",
    scheme_name="TokenGuardProjectKey",
    description=("TokenGuard project API key beginning with tg_live_."),
    auto_error=False,
)


async def get_current_project(
    api_key: str | None = Depends(project_api_key_header),
    database: Session = Depends(get_database_session),
) -> Project:
    if not settings.require_project_authentication:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Project authentication is disabled, "
                "but no development project fallback "
                "has been configured."
            ),
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-TokenGuard-Key header is required.",
        )

    key_service = ProjectKeyService()

    key_hash = key_service.hash_key(api_key)

    statement = (
        select(ProjectAPIKey, Project)
        .join(
            Project,
            Project.id == ProjectAPIKey.project_id,
        )
        .where(
            ProjectAPIKey.key_hash == key_hash,
            ProjectAPIKey.is_active.is_(True),
            Project.is_active.is_(True),
        )
    )

    row = database.execute(statement).tuples().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive project API key.",
        )

    key_record, project = row

    if key_service.is_expired(key_record.expires_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Project API key has expired.",
        )

    now = datetime.now(UTC)
    key_record.last_used_at = now

    database.commit()

    set_project_id(project.id)

    return project
