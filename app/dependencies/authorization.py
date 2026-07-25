"""
FastAPI dependencies for organization-scoped authorization.

Usage pattern
-------------
Read-only endpoint::

    @router.get("/{organization_id}/members")
    async def list_members(
        access: OrganizationAccess = Depends(
            require_permission(Permission.MEMBERS_READ)
        ),
        ...
    ):
        ...

Write endpoint::

    @router.post("/{organization_id}/members")
    async def add_member(
        access: OrganizationAccess = Depends(
            require_permission(Permission.MEMBERS_MANAGE)
        ),
        ...
    ):
        ...

Platform admins bypass all permission checks.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import (
    Depends,
    HTTPException,
    Path,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.database.models import (
    Organization,
    OrganizationMembership,
    User,
)
from app.dependencies.user_authentication import get_current_user
from app.security.permissions import (
    ROLE_PERMISSIONS,
    Permission,
)


@dataclass(frozen=True)
class OrganizationAccess:
    """Bundles the resolved context for an org-scoped request."""

    user: User
    organization: Organization
    membership: OrganizationMembership
    permissions: set[Permission]


async def get_organization_access(
    organization_id: int = Path(...),
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> OrganizationAccess:
    """
    Resolve the organization and verify the user is an active member.

    Raises:
        HTTP 404: organization not found or inactive.
        HTTP 403: user has no active membership.
    """
    organization = database.get(Organization, organization_id)

    if organization is None or not organization.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization was not found.",
        )

    # Platform admins get full access to every organization even without
    # an explicit membership record.
    if user.is_platform_admin:
        # Construct a synthetic membership for compatibility.
        synthetic_membership = OrganizationMembership(
            organization_id=organization.id,
            user_id=user.id,
            role="owner",
            is_active=True,
        )
        return OrganizationAccess(
            user=user,
            organization=organization,
            membership=synthetic_membership,
            permissions=set(Permission),
        )

    statement = select(OrganizationMembership).where(
        OrganizationMembership.organization_id == organization.id,
        OrganizationMembership.user_id == user.id,
        OrganizationMembership.is_active.is_(True),
    )
    membership = database.scalar(statement)

    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=("You do not have access to this organization."),
        )

    permissions = ROLE_PERMISSIONS.get(membership.role, set())

    return OrganizationAccess(
        user=user,
        organization=organization,
        membership=membership,
        permissions=permissions,
    )


def require_permission(
    permission: Permission,
) -> Callable[..., Awaitable[OrganizationAccess]]:
    """
    Return a dependency that enforces a specific permission.

    Platform admins always pass this check regardless of membership.
    """

    async def dependency(
        access: OrganizationAccess = Depends(get_organization_access),
    ) -> OrganizationAccess:
        if not access.user.is_platform_admin and permission not in access.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(f"Permission required: {permission.value}"),
            )
        return access

    return dependency
