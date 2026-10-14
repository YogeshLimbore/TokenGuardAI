"""
Organization management service.

Every membership lookup includes both organization_id and user_id to
prevent cross-tenant data leakage.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    Organization,
    OrganizationMembership,
    User,
)


class OrganizationSlugConflictError(ValueError):
    """Raised when an organization slug is already taken."""


class OrganizationNotFoundError(LookupError):
    """Raised when an organization does not exist or is inactive."""


class MembershipNotFoundError(LookupError):
    """Raised when a membership record cannot be located."""


class MemberAlreadyExistsError(ValueError):
    """Raised when a user is already a member of the organization."""


class LastOwnerError(ValueError):
    """Raised when a change would leave an organization without an owner."""


class OrganizationService:
    """Manages organization lifecycle and membership operations."""

    # ------------------------------------------------------------------
    # Organization CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        database: Session,
        *,
        creator: User,
        name: str,
        slug: str,
    ) -> Organization:
        """
        Create an organization and automatically add *creator* as owner.

        Raises:
            OrganizationSlugConflictError: if the slug is already taken.
        """
        organization = Organization(
            name=name.strip(),
            slug=slug.strip().lower(),
            created_by_user_id=creator.id,
            is_active=True,
        )

        try:
            database.add(organization)
            database.flush()  # obtain organization.id before membership

            database.add(
                OrganizationMembership(
                    organization_id=organization.id,
                    user_id=creator.id,
                    role="owner",
                    is_active=True,
                )
            )

            database.commit()
            database.refresh(organization)
            return organization
        except IntegrityError as error:
            database.rollback()
            raise OrganizationSlugConflictError(
                "Organization slug already exists."
            ) from error

    def get_by_id(
        self,
        database: Session,
        organization_id: int,
    ) -> Organization:
        """
        Return an active organization by primary key.

        Raises:
            OrganizationNotFoundError: if not found or inactive.
        """
        organization = database.get(Organization, organization_id)

        if organization is None or not organization.is_active:
            raise OrganizationNotFoundError("Organization was not found.")

        return organization

    def list_for_user(
        self,
        database: Session,
        *,
        user_id: int,
    ) -> list[Organization]:
        """Return all active organizations the user is an active member of."""
        statement = (
            select(Organization)
            .join(
                OrganizationMembership,
                OrganizationMembership.organization_id == Organization.id,
            )
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.is_active.is_(True),
                Organization.is_active.is_(True),
            )
            .order_by(Organization.name)
        )

        return list(database.scalars(statement))

    # ------------------------------------------------------------------
    # Membership management
    # ------------------------------------------------------------------

    def get_members(
        self,
        database: Session,
        *,
        organization_id: int,
    ) -> list[tuple[OrganizationMembership, User]]:
        """
        Return all active memberships with their associated user records.
        """
        statement = (
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.is_active.is_(True),
            )
            .order_by(OrganizationMembership.joined_at)
        )

        return [
            (membership, user) for membership, user in database.execute(statement).all()
        ]

    def add_member(
        self,
        database: Session,
        *,
        organization_id: int,
        user_id: int,
        role: str,
        invited_by_user_id: int | None = None,
    ) -> OrganizationMembership:
        """
        Add a user to an organization with the given role.

        Raises:
            MemberAlreadyExistsError: if the user is already a member.
        """
        membership = OrganizationMembership(
            organization_id=organization_id,
            user_id=user_id,
            role=role,
            is_active=True,
            invited_by_user_id=invited_by_user_id,
        )

        try:
            database.add(membership)
            database.commit()
            database.refresh(membership)
            return membership
        except IntegrityError as error:
            database.rollback()
            raise MemberAlreadyExistsError(
                "User is already a member of this organization."
            ) from error

    def get_membership(
        self,
        database: Session,
        *,
        organization_id: int,
        membership_id: int,
    ) -> OrganizationMembership:
        """
        Return a specific membership record scoped to the organization.

        Raises:
            MembershipNotFoundError: if the record does not exist.
        """
        membership = database.get(OrganizationMembership, membership_id)

        if membership is None or membership.organization_id != organization_id:
            raise MembershipNotFoundError("Membership was not found.")

        return membership

    def update_membership(
        self,
        database: Session,
        *,
        organization_id: int,
        membership_id: int,
        role: str | None = None,
        is_active: bool | None = None,
    ) -> OrganizationMembership:
        """
        Update role and/or active status of a membership.

        Raises:
            MembershipNotFoundError: if the record does not exist.
        """
        self._lock_organization(database, organization_id)
        membership = self.get_membership(
            database,
            organization_id=organization_id,
            membership_id=membership_id,
        )

        would_remove_owner = (
            membership.role == "owner"
            and membership.is_active
            and (role not in {None, "owner"} or is_active is False)
        )
        if would_remove_owner:
            self._require_another_owner(
                database,
                organization_id=organization_id,
                excluding_membership_id=membership.id,
            )

        if role is not None:
            membership.role = role

        if is_active is not None:
            membership.is_active = is_active

        database.commit()
        database.refresh(membership)
        return membership

    def remove_member(
        self,
        database: Session,
        *,
        organization_id: int,
        membership_id: int,
    ) -> None:
        """
        Deactivate (soft-delete) a membership.

        Raises:
            MembershipNotFoundError: if the record does not exist.
        """
        self._lock_organization(database, organization_id)
        membership = self.get_membership(
            database,
            organization_id=organization_id,
            membership_id=membership_id,
        )

        if membership.role == "owner" and membership.is_active:
            self._require_another_owner(
                database,
                organization_id=organization_id,
                excluding_membership_id=membership.id,
            )

        membership.is_active = False
        database.commit()

    @staticmethod
    def _lock_organization(database: Session, organization_id: int) -> None:
        organization = database.scalar(
            select(Organization)
            .where(Organization.id == organization_id)
            .with_for_update()
        )
        if organization is None:
            raise OrganizationNotFoundError("Organization was not found.")

    @staticmethod
    def _require_another_owner(
        database: Session,
        *,
        organization_id: int,
        excluding_membership_id: int,
    ) -> None:
        another_owner = database.scalar(
            select(OrganizationMembership.id).where(
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.id != excluding_membership_id,
                OrganizationMembership.role == "owner",
                OrganizationMembership.is_active.is_(True),
            )
        )
        if another_owner is None:
            raise LastOwnerError(
                "Assign another active owner before changing this membership."
            )
