"""
Integration tests for the end-to-end identity → organization → project flow.

These tests use an in-memory SQLite database with dependency overrides,
mirroring the existing API test style, while exercising the full JWT + RBAC
path that production traffic will use after Phase 18.
"""

from __future__ import annotations

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base, get_database_session
from app.main import app
from app.services.identity_service import IdentityService


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session: Session):
    app.dependency_overrides[get_database_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.mark.integration
def test_login_create_org_create_project_flow(
    db_session: Session,
    client: TestClient,
):
    IdentityService().create_user(
        db_session,
        email="owner@example.com",
        display_name="Org Owner",
        password="ValidPassword123!",
        is_platform_admin=False,
    )

    login = client.post(
        "/v1/auth/login",
        json={
            "email": "owner@example.com",
            "password": "ValidPassword123!",
        },
    )
    assert login.status_code == status.HTTP_200_OK
    access_token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    me = client.get("/v1/auth/me", headers=headers)
    assert me.status_code == status.HTTP_200_OK
    assert me.json()["email"] == "owner@example.com"

    org = client.post(
        "/v1/organizations",
        json={
            "name": "Integration Org",
            "slug": "integration-org",
        },
        headers=headers,
    )
    assert org.status_code == status.HTTP_201_CREATED
    organization_id = org.json()["id"]

    project = client.post(
        f"/v1/organizations/{organization_id}/projects",
        json={"name": "integration-project"},
        headers=headers,
    )
    assert project.status_code == status.HTTP_201_CREATED
    project_id = project.json()["id"]

    listed = client.get(
        f"/v1/organizations/{organization_id}/projects",
        headers=headers,
    )
    assert listed.status_code == status.HTTP_200_OK
    assert any(item["id"] == project_id for item in listed.json())

    fetched = client.get(
        f"/v1/organizations/{organization_id}/projects/{project_id}",
        headers=headers,
    )
    assert fetched.status_code == status.HTTP_200_OK
    assert fetched.json()["name"] == "integration-project"


@pytest.mark.integration
def test_refresh_rotation_keeps_session_usable(
    db_session: Session,
    client: TestClient,
):
    IdentityService().create_user(
        db_session,
        email="rotator@example.com",
        display_name="Rotator",
        password="ValidPassword123!",
    )

    login = client.post(
        "/v1/auth/login",
        json={
            "email": "rotator@example.com",
            "password": "ValidPassword123!",
        },
    ).json()

    refresh = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
    )
    assert refresh.status_code == status.HTTP_200_OK
    new_access = refresh.json()["access_token"]

    me = client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {new_access}"},
    )
    assert me.status_code == status.HTTP_200_OK
