from datetime import UTC

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.main import app
from app.services.identity_service import IdentityService
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


def test_auth_login_and_me_flow(db_session: Session, client: TestClient):
    service = IdentityService()
    user = service.create_user(
        db_session,
        email="testuser@example.com",
        display_name="Test User",
        password="ValidPassword123!",
    )

    # Login
    response = client.post(
        "/v1/auth/login",
        json={"email": "testuser@example.com", "password": "ValidPassword123!"},
    )
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data

    access_token = data["access_token"]

    # Get profile without token -> 401
    assert client.get("/v1/auth/me").status_code == status.HTTP_401_UNAUTHORIZED

    # Get profile with bearer token
    res_me = client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert res_me.status_code == status.HTTP_200_OK
    me_data = res_me.json()
    assert me_data["email"] == "testuser@example.com"
    assert me_data["id"] == user.id


def test_token_issued_in_account_creation_second_is_valid(
    db_session: Session, client: TestClient
):
    user = IdentityService().create_user(
        db_session,
        email="same-second@example.com",
        display_name="Same Second",
        password="ValidPassword123!",
    )
    token, _ = JWTService().create_access_token(user_id=user.id)
    issued_at = JWTService().decode_access_token(token)["iat"]
    assert int(issued_at) == int(
        user.password_changed_at.replace(tzinfo=UTC).timestamp()
    )

    response = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_200_OK
