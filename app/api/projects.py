from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy.orm import Session

from app.database.connection import (
    get_database_session,
)
from app.database.models import (
    Project,
    ProjectAPIKey,
)
from app.dependencies.admin_authentication import (
    require_admin,
)
from app.schemas.projects import (
    ProjectAPIKeyCreateRequest,
    ProjectAPIKeyCreateResponse,
    ProjectAPIKeyResponse,
    ProjectAPIKeyRevokeResponse,
    ProjectCreateRequest,
    ProjectResponse,
    ProjectUpdateRequest,
)
from app.services.project_service import (
    DuplicateProjectError,
    ProjectAPIKeyNotFoundError,
    ProjectNotFoundError,
    ProjectService,
)

router = APIRouter(
    prefix="/v1/projects",
    tags=["Projects"],
    dependencies=[
        Depends(require_admin),
    ],
)


def project_to_response(
    project: Project,
) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        description=project.description,
        is_active=project.is_active,
        daily_token_budget=(project.daily_token_budget),
        daily_cost_budget_usd=(project.daily_cost_budget_usd),
        maximum_tokens_per_request=(project.maximum_tokens_per_request),
        maximum_cost_per_request_usd=(project.maximum_cost_per_request_usd),
        requests_per_minute=(project.requests_per_minute),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def api_key_to_response(
    key_record: ProjectAPIKey,
) -> ProjectAPIKeyResponse:
    return ProjectAPIKeyResponse(
        id=key_record.id,
        project_id=key_record.project_id,
        name=key_record.name,
        masked_key=key_record.masked_key,
        is_active=key_record.is_active,
        expires_at=key_record.expires_at,
        last_used_at=key_record.last_used_at,
        created_at=key_record.created_at,
    )


@router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    request: ProjectCreateRequest,
    database: Session = Depends(get_database_session),
) -> ProjectResponse:
    try:
        service = ProjectService()

        project = service.create_project(
            database,
            **request.model_dump(),
        )

        return project_to_response(project)

    except DuplicateProjectError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.get(
    "",
    response_model=list[ProjectResponse],
)
async def list_projects(
    database: Session = Depends(get_database_session),
) -> list[ProjectResponse]:
    service = ProjectService()

    return [project_to_response(project) for project in service.list_projects(database)]


@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
)
async def get_project(
    project_id: int,
    database: Session = Depends(get_database_session),
) -> ProjectResponse:
    try:
        service = ProjectService()

        return project_to_response(
            service.get_by_id(
                database,
                project_id,
            )
        )

    except ProjectNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error


@router.patch(
    "/{project_id}",
    response_model=ProjectResponse,
)
async def update_project(
    project_id: int,
    request: ProjectUpdateRequest,
    database: Session = Depends(get_database_session),
) -> ProjectResponse:
    try:
        service = ProjectService()

        project = service.update_project(
            database,
            project_id=project_id,
            updates=request.model_dump(exclude_unset=True),
        )

        return project_to_response(project)

    except ProjectNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except DuplicateProjectError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.post(
    "/{project_id}/api-keys",
    response_model=ProjectAPIKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_api_key(
    project_id: int,
    request: ProjectAPIKeyCreateRequest,
    database: Session = Depends(get_database_session),
) -> ProjectAPIKeyCreateResponse:
    try:
        service = ProjectService()

        key_record, plaintext_key = service.create_api_key(
            database,
            project_id=project_id,
            name=request.name,
            expires_at=request.expires_at,
        )

        return ProjectAPIKeyCreateResponse(
            id=key_record.id,
            project_id=key_record.project_id,
            name=key_record.name,
            api_key=plaintext_key,
            masked_key=key_record.masked_key,
            is_active=key_record.is_active,
            expires_at=key_record.expires_at,
            created_at=key_record.created_at,
        )

    except ProjectNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error


@router.get(
    "/{project_id}/api-keys",
    response_model=list[ProjectAPIKeyResponse],
)
async def list_project_api_keys(
    project_id: int,
    database: Session = Depends(get_database_session),
) -> list[ProjectAPIKeyResponse]:
    service = ProjectService()

    service.get_by_id(
        database,
        project_id,
    )

    return [
        api_key_to_response(key_record)
        for key_record in service.list_api_keys(
            database,
            project_id=project_id,
        )
    ]


@router.delete(
    "/{project_id}/api-keys/{key_id}",
    response_model=ProjectAPIKeyRevokeResponse,
)
async def revoke_project_api_key(
    project_id: int,
    key_id: int,
    database: Session = Depends(get_database_session),
) -> ProjectAPIKeyRevokeResponse:
    try:
        service = ProjectService()

        service.revoke_api_key(
            database,
            project_id=project_id,
            key_id=key_id,
        )

        return ProjectAPIKeyRevokeResponse(
            key_id=key_id,
            revoked=True,
            message="Project API key revoked.",
        )

    except (
        ProjectNotFoundError,
        ProjectAPIKeyNotFoundError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
