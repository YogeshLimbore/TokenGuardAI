import json
from datetime import UTC, datetime

from fastapi import (
    Depends,
    HTTPException,
    Request,
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
    http_request: Request,
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

    permission = _required_scope(http_request.url.path, http_request.method)
    authorization = http_request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return await _jwt_project(http_request, authorization[7:], permission, database)

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
    if permission not in json.loads(key_record.scopes_json):
        raise HTTPException(
            status_code=403, detail=f"Project key requires scope: {permission}"
        )
    from app.database.models import Organization

    if project.organization_id is not None:
        organization = database.get(Organization, project.organization_id)
        if organization is None or not organization.is_active:
            raise HTTPException(
                status_code=401, detail="Project organization is inactive."
            )

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


def _required_scope(path: str, method: str) -> str:
    if "/providers/pricing" in path:
        return "governance:read" if method == "GET" else "governance:manage"
    if "/providers" in path:
        return "credentials:read"
    if "/credentials" in path:
        return "credentials:read" if method == "GET" else "credentials:manage"
    if "/analytics" in path:
        return "analytics:read"
    if "/jobs" in path:
        return (
            "jobs:read"
            if method == "GET"
            else ("jobs:cancel" if method == "DELETE" else "jobs:create")
        )
    if "/security" in path:
        return "security_events:read"
    return "chat"


async def _jwt_project(
    request: Request, token: str, permission: str, database: Session
) -> Project:
    from app.database.models import Organization, OrganizationMembership
    from app.dependencies.user_authentication import get_current_user
    from app.security.permissions import ROLE_PERMISSIONS

    user = await get_current_user(token, database)
    try:
        project_id = int(request.headers.get("X-TokenGuard-Project-ID", ""))
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail="X-TokenGuard-Project-ID is required with bearer authentication.",
        ) from error
    project = database.get(Project, project_id)
    if project is None or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found.")
    organization = (
        database.get(Organization, project.organization_id)
        if project.organization_id
        else None
    )
    if organization is not None and not organization.is_active:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not user.is_platform_admin:
        membership = database.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == project.organization_id,
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.is_active.is_(True),
            )
        )
        if membership is None:
            raise HTTPException(status_code=404, detail="Project not found.")
        permissions = ROLE_PERMISSIONS.get(membership.role, set())
        allowed = (
            permission in permissions
            if permission != "chat"
            else membership.role in {"owner", "admin", "developer"}
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="Permission denied.")
    set_project_id(project.id)
    return project
