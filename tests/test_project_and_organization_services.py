"""
Project and organization lifecycle services.

These two services own the tenant boundary: which project a key belongs to,
which organization owns a project, and who is a member of what. The tests cover
the mutating paths (create, update, revoke, membership changes) plus the
conflict and not-found branches that the API layer maps to HTTP status codes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import (
    Organization,
    OrganizationMembership,
    ProjectAuditLog,
    User,
)
from app.services.organization_service import (
    MemberAlreadyExistsError,
    MembershipNotFoundError,
    OrganizationNotFoundError,
    OrganizationService,
    OrganizationSlugConflictError,
)
from app.services.project_key_service import ProjectKeyService
from app.services.project_service import (
    DuplicateProjectError,
    ProjectAPIKeyNotFoundError,
    ProjectNotFoundError,
    ProjectService,
)
from tests.conftest import create_organization, create_user

pytestmark = pytest.mark.integration


@pytest.fixture
def projects() -> ProjectService:
    return ProjectService()


@pytest.fixture
def organizations() -> OrganizationService:
    return OrganizationService()


def _audit_details(
    database: Session,
    project_id: int,
    action: str,
) -> list[str | None]:
    return [
        row.details
        for row in database.scalars(
            select(ProjectAuditLog)
            .where(ProjectAuditLog.project_id == project_id)
            .where(ProjectAuditLog.action == action)
            .order_by(ProjectAuditLog.id)
        ).all()
    ]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_creating_a_project_records_an_audit_entry(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(
        database,
        name="Audited Project",
        daily_token_budget=50_000,
    )

    assert project.id is not None
    assert project.is_active is True
    assert _audit_details(database, project.id, "project_created") == [
        "Project created."
    ]


def test_duplicate_project_names_are_rejected(
    database: Session,
    projects: ProjectService,
) -> None:
    projects.create_project(database, name="Unique Project")

    with pytest.raises(DuplicateProjectError):
        projects.create_project(database, name="Unique Project")


def test_a_project_can_be_created_again_after_the_conflict_rolls_back(
    database: Session,
    projects: ProjectService,
) -> None:
    projects.create_project(database, name="First Project")

    with pytest.raises(DuplicateProjectError):
        projects.create_project(database, name="First Project")

    recovered = projects.create_project(database, name="Second Project")

    assert recovered.id is not None


def test_get_by_id_rejects_an_unknown_project(
    database: Session,
    projects: ProjectService,
) -> None:
    with pytest.raises(ProjectNotFoundError):
        projects.get_by_id(database, 999_999)


def test_projects_are_listed_newest_first(
    database: Session,
    projects: ProjectService,
) -> None:
    older = projects.create_project(database, name="Older Project")
    newer = projects.create_project(database, name="Newer Project")

    listed = [project.id for project in projects.list_projects(database)]

    assert listed.index(newer.id) < listed.index(older.id)


def test_updating_a_project_ignores_omitted_fields(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(
        database,
        name="Budget Project",
        daily_token_budget=10_000,
        requests_per_minute=30,
    )

    updated = projects.update_project(
        database,
        project_id=project.id,
        updates={
            "daily_token_budget": 25_000,
            "requests_per_minute": None,
        },
    )

    assert updated.daily_token_budget == 25_000
    assert updated.requests_per_minute == 30
    assert _audit_details(database, project.id, "project_updated") == [
        "Changed fields: daily_token_budget"
    ]


def test_renaming_a_project_onto_an_existing_name_is_rejected(
    database: Session,
    projects: ProjectService,
) -> None:
    projects.create_project(database, name="Taken Name")
    other = projects.create_project(database, name="Free Name")

    with pytest.raises(DuplicateProjectError):
        projects.update_project(
            database,
            project_id=other.id,
            updates={"name": "Taken Name"},
        )


def test_updating_an_unknown_project_is_rejected(
    database: Session,
    projects: ProjectService,
) -> None:
    with pytest.raises(ProjectNotFoundError):
        projects.update_project(
            database,
            project_id=999_999,
            updates={"name": "Nowhere"},
        )


# ---------------------------------------------------------------------------
# Project API keys
# ---------------------------------------------------------------------------


def test_a_new_api_key_is_returned_once_and_stored_hashed(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(database, name="Keyed Project")

    record, plaintext_key = projects.create_api_key(
        database,
        project_id=project.id,
        name="  Primary Key  ",
        expires_at=None,
    )

    assert record.name == "Primary Key"
    assert record.is_active is True
    assert record.expires_at is None

    assert plaintext_key not in record.key_hash
    assert plaintext_key not in record.masked_key
    assert record.key_prefix == ProjectKeyService().extract_prefix(plaintext_key)
    assert ProjectKeyService().hash_key(plaintext_key) == record.key_hash


def test_an_api_key_can_carry_an_expiry(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(database, name="Expiring Project")
    expires_at = datetime.now(UTC) + timedelta(days=7)

    record, _ = projects.create_api_key(
        database,
        project_id=project.id,
        name="Temporary Key",
        expires_at=expires_at,
    )

    assert record.expires_at is not None


def test_an_inactive_project_cannot_issue_new_keys(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(database, name="Retired Project")

    projects.update_project(
        database,
        project_id=project.id,
        updates={"is_active": False},
    )

    with pytest.raises(ValueError, match="inactive project"):
        projects.create_api_key(
            database,
            project_id=project.id,
            name="Rejected Key",
            expires_at=None,
        )


def test_creating_a_key_for_an_unknown_project_is_rejected(
    database: Session,
    projects: ProjectService,
) -> None:
    with pytest.raises(ProjectNotFoundError):
        projects.create_api_key(
            database,
            project_id=999_999,
            name="Orphan Key",
            expires_at=None,
        )


def test_keys_are_listed_per_project_newest_first(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(database, name="Listed Project")
    other = projects.create_project(database, name="Unlisted Project")

    first, _ = projects.create_api_key(
        database,
        project_id=project.id,
        name="First",
        expires_at=None,
    )
    second, _ = projects.create_api_key(
        database,
        project_id=project.id,
        name="Second",
        expires_at=None,
    )
    projects.create_api_key(
        database,
        project_id=other.id,
        name="Foreign",
        expires_at=None,
    )

    listed = [
        key.id
        for key in projects.list_api_keys(
            database,
            project_id=project.id,
        )
    ]

    assert listed == [second.id, first.id]


def test_revoking_a_key_deactivates_it_and_is_audited(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(database, name="Revoking Project")

    record, _ = projects.create_api_key(
        database,
        project_id=project.id,
        name="Doomed Key",
        expires_at=None,
    )

    revoked = projects.revoke_api_key(
        database,
        project_id=project.id,
        key_id=record.id,
    )

    assert revoked.is_active is False
    assert _audit_details(database, project.id, "api_key_revoked") == [
        f"Project API key {record.id} revoked."
    ]


def test_a_key_cannot_be_revoked_through_another_project(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(database, name="Owner Project")
    attacker = projects.create_project(database, name="Attacker Project")

    record, _ = projects.create_api_key(
        database,
        project_id=project.id,
        name="Target Key",
        expires_at=None,
    )

    with pytest.raises(ProjectAPIKeyNotFoundError):
        projects.revoke_api_key(
            database,
            project_id=attacker.id,
            key_id=record.id,
        )

    database.refresh(record)
    assert record.is_active is True


def test_revoking_an_unknown_key_is_rejected(
    database: Session,
    projects: ProjectService,
) -> None:
    project = projects.create_project(database, name="Empty Project")

    with pytest.raises(ProjectAPIKeyNotFoundError):
        projects.revoke_api_key(
            database,
            project_id=project.id,
            key_id=999_999,
        )


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


@pytest.fixture
def creator(database: Session) -> User:
    return create_user(database, email="creator@example.com")


def test_creating_an_organization_makes_the_creator_an_owner(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    organization = organizations.create(
        database,
        creator=creator,
        name="  Acme Corp  ",
        slug="  ACME-Corp  ",
    )

    assert organization.name == "Acme Corp"
    assert organization.slug == "acme-corp"
    assert organization.created_by_user_id == creator.id

    membership = database.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == organization.id,
            OrganizationMembership.user_id == creator.id,
        )
    )

    assert membership is not None
    assert membership.role == "owner"
    assert membership.is_active is True


def test_a_duplicate_slug_is_rejected(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    organizations.create(
        database,
        creator=creator,
        name="First",
        slug="shared-slug",
    )

    with pytest.raises(OrganizationSlugConflictError):
        organizations.create(
            database,
            creator=creator,
            name="Second",
            slug="shared-slug",
        )


def test_an_unknown_organization_is_rejected(
    database: Session,
    organizations: OrganizationService,
) -> None:
    with pytest.raises(OrganizationNotFoundError):
        organizations.get_by_id(database, 999_999)


def test_a_deactivated_organization_behaves_as_missing(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    organization = organizations.create(
        database,
        creator=creator,
        name="Closing Down",
        slug="closing-down",
    )

    organization.is_active = False
    database.commit()

    with pytest.raises(OrganizationNotFoundError):
        organizations.get_by_id(database, organization.id)


def test_users_only_see_organizations_they_actively_belong_to(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    member = create_user(database, email="member@example.com")
    outsider = create_user(database, email="outsider@example.com")

    joined = organizations.create(
        database,
        creator=creator,
        name="Joined Org",
        slug="joined-org",
    )
    left = organizations.create(
        database,
        creator=creator,
        name="Left Org",
        slug="left-org",
    )
    deactivated = organizations.create(
        database,
        creator=creator,
        name="Deactivated Org",
        slug="deactivated-org",
    )

    organizations.add_member(
        database,
        organization_id=joined.id,
        user_id=member.id,
        role="member",
        invited_by_user_id=creator.id,
    )
    departed = organizations.add_member(
        database,
        organization_id=left.id,
        user_id=member.id,
        role="member",
    )
    organizations.remove_member(
        database,
        organization_id=left.id,
        membership_id=departed.id,
    )
    organizations.add_member(
        database,
        organization_id=deactivated.id,
        user_id=member.id,
        role="member",
    )

    deactivated_row = database.get(Organization, deactivated.id)
    assert deactivated_row is not None
    deactivated_row.is_active = False
    database.commit()

    assert [
        organization.id
        for organization in organizations.list_for_user(
            database,
            user_id=member.id,
        )
    ] == [joined.id]

    assert organizations.list_for_user(database, user_id=outsider.id) == []


def test_members_are_returned_with_their_user_records(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    organization = organizations.create(
        database,
        creator=creator,
        name="Staffed Org",
        slug="staffed-org",
    )
    member = create_user(database, email="staff@example.com")

    membership = organizations.add_member(
        database,
        organization_id=organization.id,
        user_id=member.id,
        role="admin",
    )

    rows = organizations.get_members(
        database,
        organization_id=organization.id,
    )

    assert {row[1].email for row in rows} == {
        creator.email,
        member.email,
    }

    organizations.remove_member(
        database,
        organization_id=organization.id,
        membership_id=membership.id,
    )

    remaining = organizations.get_members(
        database,
        organization_id=organization.id,
    )

    assert [row[1].email for row in remaining] == [creator.email]


def test_a_user_cannot_be_added_twice(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    organization = organizations.create(
        database,
        creator=creator,
        name="Single Membership Org",
        slug="single-membership-org",
    )

    with pytest.raises(MemberAlreadyExistsError):
        organizations.add_member(
            database,
            organization_id=organization.id,
            user_id=creator.id,
            role="member",
        )


def test_membership_lookups_are_scoped_to_the_organization(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    owned = organizations.create(
        database,
        creator=creator,
        name="Owned Org",
        slug="owned-org",
    )
    foreign_owner = create_user(database, email="foreign@example.com")
    foreign = create_organization(
        database,
        owner=foreign_owner,
        name="Foreign Org",
        slug="foreign-org",
    )

    membership = database.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == owned.id
        )
    )
    assert membership is not None

    with pytest.raises(MembershipNotFoundError):
        organizations.get_membership(
            database,
            organization_id=foreign.id,
            membership_id=membership.id,
        )


def test_an_unknown_membership_is_rejected(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    organization = organizations.create(
        database,
        creator=creator,
        name="Empty Org",
        slug="empty-org",
    )

    with pytest.raises(MembershipNotFoundError):
        organizations.get_membership(
            database,
            organization_id=organization.id,
            membership_id=999_999,
        )


def test_a_membership_role_can_be_changed_independently_of_status(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    organization = organizations.create(
        database,
        creator=creator,
        name="Promoting Org",
        slug="promoting-org",
    )
    member = create_user(database, email="promotable@example.com")

    membership = organizations.add_member(
        database,
        organization_id=organization.id,
        user_id=member.id,
        role="viewer",
    )

    promoted = organizations.update_membership(
        database,
        organization_id=organization.id,
        membership_id=membership.id,
        role="admin",
    )

    assert promoted.role == "admin"
    assert promoted.is_active is True

    suspended = organizations.update_membership(
        database,
        organization_id=organization.id,
        membership_id=membership.id,
        is_active=False,
    )

    assert suspended.role == "admin"
    assert suspended.is_active is False


def test_updating_a_membership_from_another_organization_is_rejected(
    database: Session,
    organizations: OrganizationService,
    creator: User,
) -> None:
    owned = organizations.create(
        database,
        creator=creator,
        name="Target Org",
        slug="target-org",
    )
    attacker_owner = create_user(database, email="attacker@example.com")
    attacker = create_organization(
        database,
        owner=attacker_owner,
        name="Attacker Org",
        slug="attacker-org",
    )

    membership = database.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == owned.id
        )
    )
    assert membership is not None

    with pytest.raises(MembershipNotFoundError):
        organizations.update_membership(
            database,
            organization_id=attacker.id,
            membership_id=membership.id,
            role="owner",
        )

    with pytest.raises(MembershipNotFoundError):
        organizations.remove_member(
            database,
            organization_id=attacker.id,
            membership_id=membership.id,
        )

    database.refresh(membership)
    assert membership.role == "owner"
    assert membership.is_active is True
