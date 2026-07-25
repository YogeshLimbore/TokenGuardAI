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


def test_cross_organization_access_denied(db_session: Session, client: TestClient):
    identity_svc = IdentityService()
    org_svc = OrganizationService()
    jwt_svc = JWTService()

    # User A & Org A
    user_a = identity_svc.create_user(
        db_session,
        email="usera@example.com",
        display_name="User A",
        password="ValidPassword123!",
    )
    org_svc.create(db_session, creator=user_a, name="Org A", slug="org-a")

    # User B & Org B
    user_b = identity_svc.create_user(
        db_session,
        email="userb@example.com",
        display_name="User B",
        password="ValidPassword123!",
    )
    org_b = org_svc.create(db_session, creator=user_b, name="Org B", slug="org-b")

    token_a, _ = jwt_svc.create_access_token(user_id=user_a.id)

    # User A trying to access Org B members -> 403 Forbidden
    res = client.get(
        f"/v1/organizations/{org_b.id}/members",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res.status_code == status.HTTP_403_FORBIDDEN


def test_project_lookup_is_scoped_by_organization_id(
    db_session: Session, client: TestClient
):
    """A project created in Org A must not be reachable through Org B's
    project routes, even by a member of Org B, and even though the
    project ID itself is a globally unique, guessable integer."""
    identity_svc = IdentityService()
    org_svc = OrganizationService()
    jwt_svc = JWTService()

    user_a = identity_svc.create_user(
        db_session,
        email="projowner@example.com",
        display_name="Project Owner",
        password="ValidPassword123!",
    )
    org_a = org_svc.create(
        db_session, creator=user_a, name="Project Org A", slug="project-org-a"
    )
    token_a, _ = jwt_svc.create_access_token(user_id=user_a.id)

    create_res = client.post(
        f"/v1/organizations/{org_a.id}/projects",
        json={"name": "org-a-secret-project"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert create_res.status_code == status.HTTP_201_CREATED
    project_id = create_res.json()["id"]

    user_b = identity_svc.create_user(
        db_session,
        email="otherorguser@example.com",
        display_name="Other Org User",
        password="ValidPassword123!",
    )
    org_b = org_svc.create(
        db_session, creator=user_b, name="Project Org B", slug="project-org-b"
    )
    token_b, _ = jwt_svc.create_access_token(user_id=user_b.id)

    # Org B member cannot fetch Org A's project through Org B's route.
    cross_res = client.get(
        f"/v1/organizations/{org_b.id}/projects/{project_id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert cross_res.status_code == status.HTTP_404_NOT_FOUND

    # Org B's project list must not include Org A's project.
    list_res = client.get(
        f"/v1/organizations/{org_b.id}/projects",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert list_res.status_code == status.HTTP_200_OK
    assert all(project["id"] != project_id for project in list_res.json())

    # Org A member can fetch it correctly through the right organization.
    own_res = client.get(
        f"/v1/organizations/{org_a.id}/projects/{project_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert own_res.status_code == status.HTTP_200_OK


def test_membership_id_cannot_bypass_organization_scope(
    db_session: Session, client: TestClient
):
    """A membership ID from Org A must not be usable to modify a
    membership through Org B's routes, even by an Org B admin."""
    identity_svc = IdentityService()
    org_svc = OrganizationService()
    jwt_svc = JWTService()

    owner_a = identity_svc.create_user(
        db_session,
        email="ownera@example.com",
        display_name="Owner A",
        password="ValidPassword123!",
    )
    org_a = org_svc.create(
        db_session, creator=owner_a, name="Membership Org A", slug="membership-org-a"
    )
    membership_a = org_svc.get_members(db_session, organization_id=org_a.id)[0][0]

    owner_b = identity_svc.create_user(
        db_session,
        email="ownerb@example.com",
        display_name="Owner B",
        password="ValidPassword123!",
    )
    org_b = org_svc.create(
        db_session, creator=owner_b, name="Membership Org B", slug="membership-org-b"
    )
    token_b, _ = jwt_svc.create_access_token(user_id=owner_b.id)

    # Owner B tries to use Org A's membership ID via Org B's route.
    res = client.patch(
        f"/v1/organizations/{org_b.id}/members/{membership_a.id}",
        json={"role": "viewer"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert res.status_code == status.HTTP_404_NOT_FOUND
