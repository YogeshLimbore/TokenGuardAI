from datetime import UTC, datetime

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    status,
)
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.database.models import Project, ProviderCredential
from app.dependencies.authentication import get_current_project
from app.schemas.credentials import (
    CredentialAuditResponse,
    CredentialCreateRequest,
    CredentialDeleteResponse,
    CredentialResponse,
    CredentialRotateRequest,
    CredentialTestResponse,
    CredentialUpdateRequest,
)
from app.services.credential_service import (
    CredentialNotFoundError,
    CredentialService,
    DuplicateCredentialError,
)
from app.services.encryption_service import (
    CredentialDecryptionError,
    EncryptionConfigurationError,
)

router = APIRouter(
    prefix="/v1/credentials",
    tags=["Credentials"],
)


def credential_to_response(
    credential: ProviderCredential,
) -> CredentialResponse:
    return CredentialResponse(
        id=credential.id,
        provider=credential.provider,
        name=credential.name,
        masked_key=credential.masked_key,
        is_active=credential.is_active,
        is_default=credential.is_default,
        last_test_succeeded=(credential.last_test_succeeded),
        last_test_message=(credential.last_test_message),
        last_tested_at=credential.last_tested_at,
        last_used_at=credential.last_used_at,
        created_at=credential.created_at,
        updated_at=credential.updated_at,
    )


@router.post(
    "",
    response_model=CredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_credential(
    request: CredentialCreateRequest,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> CredentialResponse:
    try:
        service = CredentialService()

        credential = service.create(
            database,
            project_id=project.id,
            provider=request.provider,
            name=request.name,
            api_key=request.api_key.get_secret_value(),
            is_default=request.is_default,
        )

        return credential_to_response(credential)

    except DuplicateCredentialError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    except EncryptionConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error


@router.get(
    "",
    response_model=list[CredentialResponse],
)
async def list_credentials(
    include_inactive: bool = Query(default=True),
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> list[CredentialResponse]:
    service = CredentialService()

    credentials = service.list_credentials(
        database,
        project_id=project.id,
        include_inactive=include_inactive,
    )

    return [credential_to_response(credential) for credential in credentials]


@router.get(
    "/audit",
    response_model=list[CredentialAuditResponse],
)
async def list_credential_audit_logs(
    credential_id: int | None = Query(
        default=None,
        ge=1,
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> list[CredentialAuditResponse]:
    service = CredentialService()

    logs = service.list_audit_logs(
        database,
        project_id=project.id,
        credential_id=credential_id,
        limit=limit,
    )

    return [
        CredentialAuditResponse(
            id=log.id,
            credential_id=log.credential_id,
            provider=log.provider,
            action=log.action,
            succeeded=log.succeeded,
            details=log.details,
            created_at=log.created_at,
        )
        for log in logs
    ]


@router.get(
    "/{credential_id}",
    response_model=CredentialResponse,
)
async def get_credential(
    credential_id: int,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> CredentialResponse:
    try:
        service = CredentialService()

        credential = service.get_by_id(
            database,
            credential_id,
            project_id=project.id,
        )

        return credential_to_response(credential)

    except CredentialNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error


@router.patch(
    "/{credential_id}",
    response_model=CredentialResponse,
)
async def update_credential(
    credential_id: int,
    request: CredentialUpdateRequest,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> CredentialResponse:
    try:
        service = CredentialService()

        credential = service.update(
            database,
            credential_id=credential_id,
            project_id=project.id,
            name=request.name,
            is_active=request.is_active,
            is_default=request.is_default,
        )

        return credential_to_response(credential)

    except CredentialNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except DuplicateCredentialError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error


@router.post(
    "/{credential_id}/rotate",
    response_model=CredentialResponse,
)
async def rotate_credential(
    credential_id: int,
    request: CredentialRotateRequest,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> CredentialResponse:
    try:
        service = CredentialService()

        credential = service.rotate(
            database,
            credential_id=credential_id,
            project_id=project.id,
            new_api_key=(request.api_key.get_secret_value()),
        )

        return credential_to_response(credential)

    except CredentialNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except EncryptionConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error


@router.post(
    "/{credential_id}/test",
    response_model=CredentialTestResponse,
)
async def test_credential(
    credential_id: int,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> CredentialTestResponse:
    try:
        service = CredentialService()

        credential, valid, message = await service.test_credential(
            database,
            credential_id=credential_id,
            project_id=project.id,
        )

        return CredentialTestResponse(
            credential_id=credential.id,
            provider=credential.provider,
            valid=valid,
            message=message,
            tested_at=(credential.last_tested_at or datetime.now(UTC)),
        )

    except CredentialNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    except CredentialDecryptionError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error


@router.delete(
    "/{credential_id}",
    response_model=CredentialDeleteResponse,
)
async def delete_credential(
    credential_id: int,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> CredentialDeleteResponse:
    try:
        service = CredentialService()

        service.delete(
            database,
            credential_id=credential_id,
            project_id=project.id,
        )

        return CredentialDeleteResponse(
            credential_id=credential_id,
            deleted=True,
            message="Credential deleted.",
        )

    except CredentialNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
