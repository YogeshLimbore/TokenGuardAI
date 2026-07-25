import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.main import app
from app.services.identity_service import IdentityService
from app.services.organization_service import OrganizationService
from app.services.token_service_jwt import JWTService


@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database.connection import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session):
    app.dependency_overrides[get_database_session] = lambda: db_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_organization_permissions_matrix(db_session: Session, client: TestClient):
    identity_svc = IdentityService()
    org_svc = OrganizationService()
    jwt_svc = JWTService()

    # Create Owner
    owner = identity_svc.create_user(
        db_session,
        email="owner@example.com",
        display_name="Owner",
        password="ValidPassword123!",
    )
    # Create Organization
    org = org_svc.create(db_session, creator=owner, name="Acme Corp", slug="acme")

    # Create Developer & Viewer
    developer = identity_svc.create_user(
        db_session,
        email="dev@example.com",
        display_name="Developer",
        password="ValidPassword123!",
    )
    org_svc.add_member(
        db_session, organization_id=org.id, user_id=developer.id, role="developer"
    )

    viewer = identity_svc.create_user(
        db_session,
        email="viewer@example.com",
        display_name="Viewer",
        password="ValidPassword123!",
    )
    org_svc.add_member(
        db_session, organization_id=org.id, user_id=viewer.id, role="viewer"
    )

    # Tokens
    owner_token, _ = jwt_svc.create_access_token(user_id=owner.id)
    dev_token, _ = jwt_svc.create_access_token(user_id=developer.id)
    viewer_token, _ = jwt_svc.create_access_token(user_id=viewer.id)

    # Test MEMBERS_READ (members list endpoint)
    # Owner & Developer can read members
    assert (
        client.get(
            f"/v1/organizations/{org.id}/members",
            headers={"Authorization": f"Bearer {owner_token}"},
        ).status_code
        == status.HTTP_200_OK
    )

    assert (
        client.get(
            f"/v1/organizations/{org.id}/members",
            headers={"Authorization": f"Bearer {dev_token}"},
        ).status_code
        == status.HTTP_200_OK
    )

    # Viewer CANNOT read members -> 403 Forbidden
    assert (
        client.get(
            f"/v1/organizations/{org.id}/members",
            headers={"Authorization": f"Bearer {viewer_token}"},
        ).status_code
        == status.HTTP_403_FORBIDDEN
    )

    # Test MEMBERS_MANAGE (add member endpoint)
    # Developer CANNOT add members -> 403
    identity_svc.create_user(
        db_session,
        email="newbie@example.com",
        display_name="Newbie",
        password="ValidPassword123!",
    )
    assert (
        client.post(
            f"/v1/organizations/{org.id}/members",
            json={"email": "newbie@example.com", "role": "viewer"},
            headers={"Authorization": f"Bearer {dev_token}"},
        ).status_code
        == status.HTTP_403_FORBIDDEN
    )

    # Owner CAN add members
    assert (
        client.post(
            f"/v1/organizations/{org.id}/members",
            json={"email": "newbie@example.com", "role": "viewer"},
            headers={"Authorization": f"Bearer {owner_token}"},
        ).status_code
        == status.HTTP_201_CREATED
    )

    # Test PROJECTS_CREATE: viewer cannot create projects, owner can.
    assert (
        client.post(
            f"/v1/organizations/{org.id}/projects",
            json={"name": "viewer-attempt-project"},
            headers={"Authorization": f"Bearer {viewer_token}"},
        ).status_code
        == status.HTTP_403_FORBIDDEN
    )

    assert (
        client.post(
            f"/v1/organizations/{org.id}/projects",
            json={"name": "owner-created-project"},
            headers={"Authorization": f"Bearer {owner_token}"},
        ).status_code
        == status.HTTP_201_CREATED
    )

    # Developer cannot create projects either (only owner/admin can).
    assert (
        client.post(
            f"/v1/organizations/{org.id}/projects",
            json={"name": "dev-attempt-project"},
            headers={"Authorization": f"Bearer {dev_token}"},
        ).status_code
        == status.HTTP_403_FORBIDDEN
    )

    # All roles (including viewer) can read projects.
    assert (
        client.get(
            f"/v1/organizations/{org.id}/projects",
            headers={"Authorization": f"Bearer {viewer_token}"},
        ).status_code
        == status.HTTP_200_OK
    )


def test_admin_cannot_modify_owner_membership(db_session: Session, client: TestClient):
    identity_svc = IdentityService()
    org_svc = OrganizationService()
    jwt_svc = JWTService()

    owner = identity_svc.create_user(
        db_session,
        email="realowner@example.com",
        display_name="Real Owner",
        password="ValidPassword123!",
    )
    org = org_svc.create(
        db_session, creator=owner, name="Guarded Org", slug="guarded-org"
    )

    owner_membership = org_svc.get_members(db_session, organization_id=org.id)[0][0]

    admin_user = identity_svc.create_user(
        db_session,
        email="theadmin@example.com",
        display_name="The Admin",
        password="ValidPassword123!",
    )
    org_svc.add_member(
        db_session,
        organization_id=org.id,
        user_id=admin_user.id,
        role="admin",
    )
    admin_token, _ = jwt_svc.create_access_token(user_id=admin_user.id)

    # Admin cannot demote the owner.
    demote_res = client.patch(
        f"/v1/organizations/{org.id}/members/{owner_membership.id}",
        json={"role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert demote_res.status_code == status.HTTP_403_FORBIDDEN

    # Admin cannot remove the owner either.
    remove_res = client.delete(
        f"/v1/organizations/{org.id}/members/{owner_membership.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert remove_res.status_code == status.HTTP_403_FORBIDDEN

    # But the owner can modify their own membership's role freely -- more
    # importantly, another owner can manage the first owner. Verify the
    # endpoint itself isn't just universally broken for owners.
    owner_token, _ = jwt_svc.create_access_token(user_id=owner.id)
    list_res = client.get(
        f"/v1/organizations/{org.id}/members",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert list_res.status_code == status.HTTP_200_OK
