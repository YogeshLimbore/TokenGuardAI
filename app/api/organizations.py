"""Organization and member management API.

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

import json

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.projects import (
    create_project_api_key,
    list_project_api_keys,
    project_to_response,
    revoke_project_api_key,
    update_project,
)
from app.database.connection import get_database_session
from app.database.models import (
    Organization,
    OrganizationMembership,
    Project,
    ProjectGovernancePolicy,
    User,
)
from app.dependencies.authorization import (
    OrganizationAccess,
    get_organization_access,
    require_permission,
)
from app.dependencies.user_authentication import get_current_user
from app.observability.context import get_correlation_id
from app.schemas.governance import (
    ProjectGovernancePolicyRequest,
    ProjectGovernancePolicyResponse,
)
from app.schemas.identity import (
    MembershipCreateRequest,
    MembershipResponse,
    MembershipUpdateRequest,
    OrganizationCreateRequest,
    OrganizationResponse,
    OrganizationUpdateRequest,
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
from app.security.permissions import Permission
from app.services.model_policy_service import (
    GovernancePolicyConfigurationError,
    ModelPolicyService,
)
from app.services.organization_service import (
    LastOwnerError,
    MemberAlreadyExistsError,
    MembershipNotFoundError,
    OrganizationService,
    OrganizationSlugConflictError,
)
from app.services.project_service import (
    DuplicateProjectError,
    ProjectService,
)
from app.services.security_audit_service import SecurityAuditService


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
    if (
        request.role == "owner"
        and access.membership.role != "owner"
        and not access.user.is_platform_admin
    ):
        raise HTTPException(
            status_code=403, detail="Only an owner can assign the owner role."
        )
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
    if (
        request.role == "owner"
        and access.membership.role != "owner"
        and not access.user.is_platform_admin
    ):
        raise HTTPException(
            status_code=403, detail="Only an owner can assign the owner role."
        )

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
    except LastOwnerError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
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
    except LastOwnerError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
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


@router.patch(
    "/{organization_id}/projects/{project_id}", response_model=ProjectResponse
)
async def update_organization_project(
    project_id: int,
    request: ProjectUpdateRequest,
    access: OrganizationAccess = Depends(
        require_permission(Permission.PROJECTS_UPDATE)
    ),
    database: Session = Depends(get_database_session),
) -> ProjectResponse:
    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    return await update_project(project_id, request, database)


@router.post(
    "/{organization_id}/projects/{project_id}/api-keys",
    response_model=ProjectAPIKeyCreateResponse,
    status_code=201,
)
async def issue_organization_key(
    project_id: int,
    request: ProjectAPIKeyCreateRequest,
    access: OrganizationAccess = Depends(
        require_permission(Permission.PROJECT_KEYS_MANAGE)
    ),
    database: Session = Depends(get_database_session),
) -> ProjectAPIKeyCreateResponse:
    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    return await create_project_api_key(project_id, request, database)


@router.get(
    "/{organization_id}/projects/{project_id}/api-keys",
    response_model=list[ProjectAPIKeyResponse],
)
async def list_organization_keys(
    project_id: int,
    access: OrganizationAccess = Depends(
        require_permission(Permission.PROJECT_KEYS_MANAGE)
    ),
    database: Session = Depends(get_database_session),
) -> list[ProjectAPIKeyResponse]:
    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    return await list_project_api_keys(project_id, database)


@router.delete(
    "/{organization_id}/projects/{project_id}/api-keys/{key_id}",
    response_model=ProjectAPIKeyRevokeResponse,
)
async def revoke_organization_key(
    project_id: int,
    key_id: int,
    access: OrganizationAccess = Depends(
        require_permission(Permission.PROJECT_KEYS_MANAGE)
    ),
    database: Session = Depends(get_database_session),
) -> ProjectAPIKeyRevokeResponse:
    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    return await revoke_project_api_key(project_id, key_id, database)


@router.delete("/{organization_id}/projects/{project_id}/cache", status_code=204)
def purge_project_cache(
    project_id: int,
    access: OrganizationAccess = Depends(
        require_permission(Permission.GOVERNANCE_MANAGE)
    ),
    database: Session = Depends(get_database_session),
) -> None:
    from sqlalchemy import delete

    from app.cache.redis_client import get_redis_client
    from app.database.models import ExactResponseCache, SemanticResponseCache
    from app.services.redis_cache_service import RedisExactCacheService

    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    RedisExactCacheService(get_redis_client()).delete_project_cache(
        project_id=project_id
    )
    database.execute(
        delete(ExactResponseCache).where(ExactResponseCache.project_id == project_id)
    )
    database.execute(
        delete(SemanticResponseCache).where(
            SemanticResponseCache.project_id == project_id
        )
    )
    database.commit()


@router.get(
    "/{organization_id}/projects/{project_id}/governance-policy",
    response_model=ProjectGovernancePolicyResponse,
)
def get_project_governance_policy(
    project_id: int,
    access: OrganizationAccess = Depends(
        require_permission(Permission.GOVERNANCE_READ)
    ),
    database: Session = Depends(get_database_session),
) -> ProjectGovernancePolicyResponse:
    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    stored = database.scalar(
        select(ProjectGovernancePolicy).where(
            ProjectGovernancePolicy.project_id == project_id
        )
    )
    return ProjectGovernancePolicyResponse(
        project_id=project_id,
        is_active=bool(stored and stored.is_active),
        policy=json.loads(stored.policy_json) if stored else {},
    )


@router.put(
    "/{organization_id}/projects/{project_id}/governance-policy",
    response_model=ProjectGovernancePolicyResponse,
)
def set_project_governance_policy(
    project_id: int,
    request: ProjectGovernancePolicyRequest,
    access: OrganizationAccess = Depends(
        require_permission(Permission.GOVERNANCE_MANAGE)
    ),
    database: Session = Depends(get_database_session),
) -> ProjectGovernancePolicyResponse:
    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    try:
        policy = ModelPolicyService.validate_project_policy(request.policy)
    except GovernancePolicyConfigurationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    stored = database.scalar(
        select(ProjectGovernancePolicy)
        .where(ProjectGovernancePolicy.project_id == project_id)
        .with_for_update()
    )
    if stored is None:
        stored = ProjectGovernancePolicy(
            project_id=project_id,
            policy_json=json.dumps(policy, sort_keys=True),
            is_active=True,
        )
        database.add(stored)
    else:
        stored.policy_json = json.dumps(policy, sort_keys=True)
        stored.is_active = True
    database.commit()
    SecurityAuditService.record(
        database,
        project_id=project_id,
        request_id=None,
        correlation_id=get_correlation_id(),
        event_type="governance_policy_updated",
        severity="info",
        action="update",
        metadata={"actor_user_id": access.user.id},
    )
    return ProjectGovernancePolicyResponse(
        project_id=project_id, is_active=True, policy=policy
    )


@router.delete(
    "/{organization_id}/projects/{project_id}/governance-policy",
    status_code=204,
)
def disable_project_governance_policy(
    project_id: int,
    access: OrganizationAccess = Depends(
        require_permission(Permission.GOVERNANCE_MANAGE)
    ),
    database: Session = Depends(get_database_session),
) -> None:
    get_organization_project(
        database, organization_id=access.organization.id, project_id=project_id
    )
    stored = database.scalar(
        select(ProjectGovernancePolicy)
        .where(ProjectGovernancePolicy.project_id == project_id)
        .with_for_update()
    )
    if stored is not None:
        stored.is_active = False
        database.commit()
    SecurityAuditService.record(
        database,
        project_id=project_id,
        request_id=None,
        correlation_id=get_correlation_id(),
        event_type="governance_policy_disabled",
        severity="info",
        action="disable",
        metadata={"actor_user_id": access.user.id},
    )
