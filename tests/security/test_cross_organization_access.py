"""
Cross-organization isolation at the HTTP layer.

Every organization-scoped route must resolve the caller's membership before
touching data. A user who belongs to Organization A must never be able to
read or modify Organization B by guessing its numeric ID.

403 and 404 are both acceptable outcomes: 404 leaks less about which IDs
exist, 403 is clearer for legitimate users who lost access.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.models import Organization, User
from tests.conftest import bearer, create_project_with_key

pytestmark = [pytest.mark.security, pytest.mark.integration]

DENIED = {403, 404}


@pytest.fixture
def foreign_project_id(
    database: Session,
    organization_b: tuple[User, Organization],
) -> int:
    _, organization = organization_b

    project, _ = create_project_with_key(
        database,
        name="Organization B Project",
        organization_id=organization.id,
    )

    return project.id


def test_user_cannot_read_other_organization(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
) -> None:
    user_a, _ = organization_a
    _, org_b = organization_b

    response = client.get(
        f"/v1/organizations/{org_b.id}",
        headers=bearer(user_a),
    )

    assert response.status_code in DENIED
    assert org_b.name not in response.text


def test_user_cannot_list_other_organization_members(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
) -> None:
    user_a, _ = organization_a
    owner_b, org_b = organization_b

    response = client.get(
        f"/v1/organizations/{org_b.id}/members",
        headers=bearer(user_a),
    )

    assert response.status_code in DENIED
    assert owner_b.email not in response.text


def test_user_cannot_add_member_to_other_organization(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
) -> None:
    user_a, _ = organization_a
    _, org_b = organization_b

    response = client.post(
        f"/v1/organizations/{org_b.id}/members",
        headers=bearer(user_a),
        json={"email": user_a.email, "role": "admin"},
    )

    assert response.status_code in DENIED


def test_user_cannot_update_other_organization(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
) -> None:
    user_a, _ = organization_a
    _, org_b = organization_b

    response = client.patch(
        f"/v1/organizations/{org_b.id}",
        headers=bearer(user_a),
        json={"name": "Hijacked Organization"},
    )

    assert response.status_code in DENIED


def test_user_cannot_create_project_in_other_organization(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
) -> None:
    user_a, _ = organization_a
    _, org_b = organization_b

    response = client.post(
        f"/v1/organizations/{org_b.id}/projects",
        headers=bearer(user_a),
        json={"name": "Injected Project"},
    )

    assert response.status_code in DENIED


def test_user_cannot_list_other_organization_projects(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
    foreign_project_id: int,
) -> None:
    user_a, _ = organization_a
    _, org_b = organization_b

    response = client.get(
        f"/v1/organizations/{org_b.id}/projects",
        headers=bearer(user_a),
    )

    assert response.status_code in DENIED
    assert "Organization B Project" not in response.text


def test_user_cannot_read_other_organization_project_by_id(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
    foreign_project_id: int,
) -> None:
    user_a, org_a = organization_a
    _, org_b = organization_b

    through_owner_org = client.get(
        f"/v1/organizations/{org_b.id}/projects/{foreign_project_id}",
        headers=bearer(user_a),
    )
    assert through_owner_org.status_code in DENIED

    # Even from an organization the user *does* belong to, another
    # organization's project ID must not resolve.
    through_own_org = client.get(
        f"/v1/organizations/{org_a.id}/projects/{foreign_project_id}",
        headers=bearer(user_a),
    )
    assert through_own_org.status_code == 404


def test_listing_organizations_returns_only_own_memberships(
    client: TestClient,
    organization_a: tuple[User, Organization],
    organization_b: tuple[User, Organization],
) -> None:
    user_a, org_a = organization_a
    _, org_b = organization_b

    response = client.get("/v1/organizations", headers=bearer(user_a))

    assert response.status_code == 200

    slugs = {item["slug"] for item in response.json()}
    assert slugs == {org_a.slug}
    assert org_b.slug not in slugs


def test_platform_admin_can_reach_every_organization(
    client: TestClient,
    platform_admin_user: User,
    organization_b: tuple[User, Organization],
) -> None:
    _, org_b = organization_b

    response = client.get(
        f"/v1/organizations/{org_b.id}",
        headers=bearer(platform_admin_user),
    )

    assert response.status_code == 200
    assert response.json()["slug"] == org_b.slug


def test_unauthenticated_organization_access_is_rejected(
    client: TestClient,
    organization_b: tuple[User, Organization],
) -> None:
    _, org_b = organization_b

    response = client.get(f"/v1/organizations/{org_b.id}")

    assert response.status_code == 401
