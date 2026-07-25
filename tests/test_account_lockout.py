from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.database.models import User
from app.main import app
from app.services.identity_service import IdentityService


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


def test_account_lockout_after_failed_attempts(db_session: Session, client: TestClient):
    service = IdentityService()
    service.create_user(
        db_session,
        email="lockme@example.com",
        display_name="Lock Me",
        password="ValidPassword123!",
    )

    # 4 bad attempts
    for _ in range(4):
        res = client.post(
            "/v1/auth/login",
            json={"email": "lockme@example.com", "password": "WrongPassword!"},
        )
        assert res.status_code == status.HTTP_401_UNAUTHORIZED
        assert "Invalid email or password" in res.json()["detail"]

    # 5th bad attempt triggers lockout
    res_5th = client.post(
        "/v1/auth/login",
        json={"email": "lockme@example.com", "password": "WrongPassword!"},
    )
    assert res_5th.status_code == status.HTTP_401_UNAUTHORIZED

    # Subsequent attempt with CORRECT password fails due to lock
    res_correct = client.post(
        "/v1/auth/login",
        json={"email": "lockme@example.com", "password": "ValidPassword123!"},
    )
    assert res_correct.status_code == status.HTTP_401_UNAUTHORIZED
    assert "temporarily locked" in res_correct.json()["detail"]


def test_account_unlocks_after_configured_interval(
    db_session: Session, client: TestClient
):
    service = IdentityService()
    service.create_user(
        db_session,
        email="unlocks@example.com",
        display_name="Unlocks Eventually",
        password="ValidPassword123!",
    )

    for _ in range(5):
        client.post(
            "/v1/auth/login",
            json={
                "email": "unlocks@example.com",
                "password": "WrongPassword!",
            },
        )

    user = db_session.query(User).filter_by(email="unlocks@example.com").one()
    assert user.locked_until is not None

    # Simulate the lock window having already elapsed.
    user.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()

    res = client.post(
        "/v1/auth/login",
        json={
            "email": "unlocks@example.com",
            "password": "ValidPassword123!",
        },
    )
    assert res.status_code == status.HTTP_200_OK


def test_successful_login_clears_failure_count(db_session: Session, client: TestClient):
    service = IdentityService()
    service.create_user(
        db_session,
        email="resets@example.com",
        display_name="Resets Count",
        password="ValidPassword123!",
    )

    # A couple of failures, but not enough to trigger a lockout.
    for _ in range(2):
        client.post(
            "/v1/auth/login",
            json={
                "email": "resets@example.com",
                "password": "WrongPassword!",
            },
        )

    ok_res = client.post(
        "/v1/auth/login",
        json={
            "email": "resets@example.com",
            "password": "ValidPassword123!",
        },
    )
    assert ok_res.status_code == status.HTTP_200_OK

    user = db_session.query(User).filter_by(email="resets@example.com").one()
    assert user.failed_login_attempts == 0
    assert user.locked_until is None
