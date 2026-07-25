"""
Organization and member management API.

Endpoints:
    POST   /v1/organizations
    GET    /v1/organizations
    GET    /v1/organizations/{organization_id}
    PATCH  /v1/organizations/{organization_id}

    GET    /v1/organizations/{organization_id}/members
    POST   /v1/organizations/{organization_id}/members
    PATCH  /v1/organizations/{organization_id}/members/{membership_id}
    DELETE /v1/organizations/{organization_id}/members/{membership_id}

    POST   /v1/organizations/{organization_id}/projects
    GET    /v1/organizations/{organization_id}/projects
    GET    /v1/organizations/{organization_id}/projects/{project_id}
"""

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.projects import project_to_response
from app.database.connection import get_database_session
from app.database.models import (
    Organization,
    OrganizationMembership,
    Project,
    User,
)
from app.dependencies.authorization import (
    OrganizationAccess,
    get_organization_access,
    require_permission,
)
from app.dependencies.user_authentication import get_current_user
from app.schemas.identity import (
    MembershipCreateRequest,
    MembershipResponse,
    MembershipUpdateRequest,
    OrganizationCreateRequest,
    OrganizationResponse,
    OrganizationUpdateRequest,
)
from app.schemas.projects import (
    ProjectCreateRequest,
    ProjectResponse,
)
from app.security.permissions import Permission
from app.services.organization_service import (
    MemberAlreadyExistsError,
    MembershipNotFoundError,
    OrganizationService,
    OrganizationSlugConflictError,
)
from app.services.project_service import (
    DuplicateProjectError,
    ProjectService,
)


def get_organization_project(
    database: Session,
    *,
    organization_id: int,
    project_id: int,
) -> Project:
    """
    Return a project only if it belongs to *organization_id*.

    This must be used instead of ``database.get(Project, project_id)``
    for every organization-scoped project route, otherwise a caller
    could guess another organization's project ID and access it.
    """
    statement = select(Project).where(
        Project.id == project_id,
        Project.organization_id == organization_id,
    )
    project = database.scalar(statement)

    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project was not found.",
        )

    return project


router = APIRouter(
    prefix="/v1/organizations",
    tags=["Organizations"],
)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def org_to_response(org: Organization) -> OrganizationResponse:
    return OrganizationResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        is_active=org.is_active,
        created_at=org.created_at,
    )


def membership_to_response(
    membership: OrganizationMembership,
    user: User,
) -> MembershipResponse:
    return MembershipResponse(
        id=membership.id,
        organization_id=membership.organization_id,
        user_id=membership.user_id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,  # type: ignore[arg-type]
        is_active=membership.is_active,
        joined_at=membership.joined_at,
    )


# ---------------------------------------------------------------------------
# Organization endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new organization",
)
async def create_organization(
    request: OrganizationCreateRequest,
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> OrganizationResponse:
    service = OrganizationService()

    try:
        org = service.create(
            database,
            creator=user,
            name=request.name,
            slug=request.slug,
        )
    except OrganizationSlugConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return org_to_response(org)


@router.get(
    "",
    response_model=list[OrganizationResponse],
    summary="List organizations the current user belongs to",
)
async def list_organizations(
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> list[OrganizationResponse]:
    service = OrganizationService()

    return [
        org_to_response(org)
        for org in service.list_for_user(
            database,
            user_id=user.id,
        )
    ]


@router.get(
    "/{organization_id}",
    response_model=OrganizationResponse,
    summary="Get a specific organization",
)
async def get_organization(
    access: OrganizationAccess = Depends(get_organization_access),
) -> OrganizationResponse:
    return org_to_response(access.organization)


@router.patch(
    "/{organization_id}",
    response_model=OrganizationResponse,
    summary="Update an organization",
)
async def update_organization(
    request: OrganizationUpdateRequest,
    access: OrganizationAccess = Depends(
        require_permission(Permission.ORGANIZATIONS_UPDATE)
    ),
    database: Session = Depends(get_database_session),
) -> OrganizationResponse:
    org = access.organization

    if request.name is not None:
        org.name = request.name.strip()

    database.commit()
    database.refresh(org)

    return org_to_response(org)


def _guard_owner_membership(
    access: OrganizationAccess,
    service: OrganizationService,
    database: Session,
    membership_id: int,
) -> None:
    """
    Prevent non-owners from modifying or removing an owner's membership.

    Only another owner (or a platform admin) may change an owner's role
    or remove them, otherwise an admin could demote/lock out the owner.
    """
    if access.membership.role == "owner" or access.user.is_platform_admin:
        return

    try:
        target = service.get_membership(
            database,
            organization_id=access.organization.id,
            membership_id=membership_id,
        )
    except MembershipNotFoundError:
        return

    if target.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an owner can modify another owner's membership.",
        )


# ---------------------------------------------------------------------------
# Member endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{organization_id}/members",
    response_model=list[MembershipResponse],
    summary="List organization members",
)
async def list_members(
    access: OrganizationAccess = Depends(require_permission(Permission.MEMBERS_READ)),
    database: Session = Depends(get_database_session),
) -> list[MembershipResponse]:
    service = OrganizationService()

    rows = service.get_members(
        database,
        organization_id=access.organization.id,
    )

    return [membership_to_response(membership, user) for membership, user in rows]


@router.post(
    "/{organization_id}/members",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a member to the organization",
)
async def add_member(
    request: MembershipCreateRequest,
    access: OrganizationAccess = Depends(require_permission(Permission.MEMBERS_MANAGE)),
    database: Session = Depends(get_database_session),
) -> MembershipResponse:
    # Look up the target user by email.
    target_user = database.scalar(
        select(User).where(User.email == request.email.lower())
    )

    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    service = OrganizationService()

    try:
        membership = service.add_member(
            database,
            organization_id=access.organization.id,
            user_id=target_user.id,
            role=request.role,
            invited_by_user_id=access.user.id,
        )
    except MemberAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return membership_to_response(membership, target_user)


@router.patch(
    "/{organization_id}/members/{membership_id}",
    response_model=MembershipResponse,
    summary="Update a membership role or status",
)
async def update_member(
    membership_id: int,
    request: MembershipUpdateRequest,
    access: OrganizationAccess = Depends(require_permission(Permission.MEMBERS_MANAGE)),
    database: Session = Depends(get_database_session),
) -> MembershipResponse:
    service = OrganizationService()

    _guard_owner_membership(access, service, database, membership_id)

    try:
        membership = service.update_membership(
            database,
            organization_id=access.organization.id,
            membership_id=membership_id,
            role=request.role,
            is_active=request.is_active,
        )
    except MembershipNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error

    member_user = database.get(User, membership.user_id)

    if member_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member user not found.",
        )

    return membership_to_response(membership, member_user)


@router.delete(
    "/{organization_id}/members/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a member from the organization",
)
async def remove_member(
    membership_id: int,
    access: OrganizationAccess = Depends(require_permission(Permission.MEMBERS_MANAGE)),
    database: Session = Depends(get_database_session),
) -> None:
    service = OrganizationService()

    _guard_owner_membership(access, service, database, membership_id)

    try:
        service.remove_member(
            database,
            organization_id=access.organization.id,
            membership_id=membership_id,
        )
    except MembershipNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error


# ---------------------------------------------------------------------------
# Organization-scoped project endpoints
#
# These are the JWT/RBAC-protected equivalents of the shared-admin-key
# ``/v1/projects`` routes. The organization ID is always taken from the
# authenticated access context, never from the request body, and every
# lookup below is scoped by ``organization_id`` to prevent cross-tenant
# access.
# ---------------------------------------------------------------------------


@router.post(
    "/{organization_id}/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project within this organization",
)
async def create_organization_project(
    request: ProjectCreateRequest,
    access: OrganizationAccess = Depends(
        require_permission(Permission.PROJECTS_CREATE)
    ),
    database: Session = Depends(get_database_session),
) -> ProjectResponse:
    service = ProjectService()

    try:
        project = service.create_project(
            database,
            organization_id=access.organization.id,
            **request.model_dump(),
        )
    except DuplicateProjectError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return project_to_response(project)


@router.get(
    "/{organization_id}/projects",
    response_model=list[ProjectResponse],
    summary="List projects within this organization",
)
async def list_organization_projects(
    access: OrganizationAccess = Depends(require_permission(Permission.PROJECTS_READ)),
    database: Session = Depends(get_database_session),
) -> list[ProjectResponse]:
    statement = (
        select(Project)
        .where(Project.organization_id == access.organization.id)
        .order_by(Project.created_at.desc())
    )

    projects = list(database.scalars(statement))

    return [project_to_response(project) for project in projects]


@router.get(
    "/{organization_id}/projects/{project_id}",
    response_model=ProjectResponse,
    summary="Get a project within this organization",
)
async def get_single_organization_project(
    project_id: int,
    access: OrganizationAccess = Depends(require_permission(Permission.PROJECTS_READ)),
    database: Session = Depends(get_database_session),
) -> ProjectResponse:
    project = get_organization_project(
        database,
        organization_id=access.organization.id,
        project_id=project_id,
    )

    return project_to_response(project)
