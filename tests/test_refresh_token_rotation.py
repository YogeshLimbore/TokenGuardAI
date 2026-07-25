from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.database.models import RefreshToken
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


def test_refresh_token_rotation_and_revocation(db_session: Session, client: TestClient):
    service = IdentityService()
    service.create_user(
        db_session,
        email="rotator@example.com",
        display_name="Rotator",
        password="ValidPassword123!",
    )

    login_res = client.post(
        "/v1/auth/login",
        json={"email": "rotator@example.com", "password": "ValidPassword123!"},
    ).json()

    old_refresh_token = login_res["refresh_token"]

    # Rotate refresh token
    refresh_res = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": old_refresh_token},
    )
    assert refresh_res.status_code == status.HTTP_200_OK
    new_data = refresh_res.json()
    new_refresh_token = new_data["refresh_token"]

    assert new_refresh_token != old_refresh_token

    # Reusing old refresh token must fail with 401
    reuse_res = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": old_refresh_token},
    )
    assert reuse_res.status_code == status.HTTP_401_UNAUTHORIZED

    # Logout with new token
    logout_res = client.post(
        "/v1/auth/logout",
        json={"refresh_token": new_refresh_token},
    )
    assert logout_res.status_code == status.HTTP_204_NO_CONTENT

    # Trying to refresh after logout must fail
    after_logout_res = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": new_refresh_token},
    )
    assert after_logout_res.status_code == status.HTTP_401_UNAUTHORIZED


def test_expired_refresh_token_is_rejected(db_session: Session, client: TestClient):
    service = IdentityService()
    service.create_user(
        db_session,
        email="expiredrefresh@example.com",
        display_name="Expired Refresh",
        password="ValidPassword123!",
    )

    login_res = client.post(
        "/v1/auth/login",
        json={
            "email": "expiredrefresh@example.com",
            "password": "ValidPassword123!",
        },
    ).json()

    plaintext_token = login_res["refresh_token"]
    token_hash = JWTService.hash_refresh_token(plaintext_token)

    record = db_session.query(RefreshToken).filter_by(token_hash=token_hash).one()
    record.expires_at = datetime.now(UTC) - timedelta(days=1)
    db_session.commit()

    res = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": plaintext_token},
    )
    assert res.status_code == status.HTTP_401_UNAUTHORIZED


def test_refresh_token_is_never_stored_in_plaintext(
    db_session: Session, client: TestClient
):
    service = IdentityService()
    service.create_user(
        db_session,
        email="hashedrefresh@example.com",
        display_name="Hashed Refresh",
        password="ValidPassword123!",
    )

    login_res = client.post(
        "/v1/auth/login",
        json={
            "email": "hashedrefresh@example.com",
            "password": "ValidPassword123!",
        },
    ).json()

    plaintext_token = login_res["refresh_token"]

    records = db_session.query(RefreshToken).all()
    assert len(records) >= 1
    for record in records:
        assert record.token_hash != plaintext_token
        assert plaintext_token not in record.token_hash
