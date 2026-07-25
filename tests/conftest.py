"""
Shared pytest configuration and fixtures.

Two database strategies coexist in this suite:

1. Legacy per-test SQLite sessions built inside individual test modules
   (they define their own ``db_session`` / ``client`` fixtures).
2. The PostgreSQL fixtures defined here (``database``, ``client``), which run
   each test inside a transaction that is rolled back afterwards. Integration,
   end-to-end, and security tests use these because they exercise real
   Postgres behaviour (constraints, cascades, JSON, indexes).

The FastAPI app also holds a *module-level* SQLAlchemy engine and Redis client
(used by ``app.main``'s lifespan reachability checks and by the Redis-backed
cache/rate-limit services, which are called directly rather than through a
FastAPI dependency). Those module-level singletons read
``DATABASE_URL`` / ``REDIS_URL`` from application settings the first time they
are constructed, so we point them at the dedicated test database/keyspace
*before* anything under ``app`` gets imported. This must happen at conftest
import time (not inside a fixture), since pytest always imports
``conftest.py`` before collecting test modules in the same directory.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://tokenguard:tokenguard_password"
    "@localhost:5433/tokenguard_test",
)

os.environ["REDIS_URL"] = os.environ.get(
    "TEST_REDIS_URL",
    "redis://localhost:6379/1",
)

os.environ.setdefault(
    "JWT_SECRET_KEY",
    "test-jwt-secret-that-is-longer-than-forty-eight-characters",
)

from fastapi.testclient import TestClient
from redis import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database.connection import (
    get_database_session,
)
from app.database.models import (
    Organization,
    OrganizationMembership,
    Project,
    User,
)
from app.main import app
from app.services.identity_service import IdentityService
from app.services.project_service import ProjectService
from app.services.token_service_jwt import JWTService

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
TEST_PASSWORD = "Secure-Test-Password-123!"

test_engine = create_engine(
    TEST_DATABASE_URL,
    pool_pre_ping=True,
)

TestingSessionLocal = sessionmaker(
    bind=test_engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    # Application code commits freely; savepoints let the outer test
    # transaction still roll everything back afterwards.
    join_transaction_mode="create_savepoint",
)


@pytest.fixture(scope="session", autouse=True)
def verify_test_database() -> None:
    """Refuse to run the suite against a non-test database."""
    if "tokenguard_test" not in TEST_DATABASE_URL:
        raise RuntimeError(
            f"Tests must run against tokenguard_test, got: {TEST_DATABASE_URL}"
        )


@pytest.fixture(autouse=True)
def _flush_test_redis_keyspace() -> Generator[None]:
    """
    Clear the dedicated test Redis keyspace before every test.

    Unlike the per-test SQLite databases, the Redis connection points at a
    real, shared Redis server. Without this, cache entries, rate-limit
    counters, and circuit-breaker state written by one test (e.g. an
    exact-cache hit keyed by project ID 1) can leak into a later, unrelated
    test that also happens to create a project with ID 1.
    """
    from app.cache.redis_client import get_redis_client

    client = get_redis_client()
    client.flushdb()

    yield

    client.flushdb()


@pytest.fixture
def database() -> Generator[Session]:
    """A Postgres session whose changes are rolled back after each test."""
    connection = test_engine.connect()
    transaction = connection.begin()

    session = TestingSessionLocal(bind=connection)

    try:
        yield session
    finally:
        session.close()

        if transaction.is_active:
            transaction.rollback()

        connection.close()


@pytest.fixture
def redis_client() -> Generator[Redis]:
    client = Redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
    )

    client.flushdb()

    try:
        yield client
    finally:
        client.flushdb()
        client.close()


@pytest.fixture
def client(database: Session) -> Generator[TestClient]:
    def override_database() -> Generator[Session]:
        yield database

    app.dependency_overrides[get_database_session] = override_database

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def create_user(
    database: Session,
    *,
    email: str,
    is_platform_admin: bool = False,
    display_name: str = "Test User",
) -> User:
    return IdentityService().create_user(
        database,
        email=email,
        display_name=display_name,
        password=TEST_PASSWORD,
        is_platform_admin=is_platform_admin,
    )


def access_token_for(user: User) -> str:
    token, _ = JWTService().create_access_token(user_id=user.id)
    return token


def bearer(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token_for(user)}"}


def create_organization(
    database: Session,
    *,
    owner: User,
    name: str,
    slug: str,
) -> Organization:
    organization = Organization(
        name=name,
        slug=slug,
        created_by_user_id=owner.id,
        is_active=True,
    )
    database.add(organization)
    database.flush()

    database.add(
        OrganizationMembership(
            organization_id=organization.id,
            user_id=owner.id,
            role="owner",
            is_active=True,
        )
    )
    database.commit()
    database.refresh(organization)

    return organization


def create_project_with_key(
    database: Session,
    *,
    name: str,
    organization_id: int | None = None,
) -> tuple[Project, str]:
    service = ProjectService()

    project = service.create_project(
        database,
        name=name,
        organization_id=organization_id,
    )

    _, plaintext_key = service.create_api_key(
        database,
        project_id=project.id,
        name="Test Key",
        expires_at=None,
    )

    return project, plaintext_key


@pytest.fixture
def platform_admin_user(database: Session) -> User:
    return create_user(
        database,
        email="platform-admin@example.com",
        display_name="Platform Administrator",
        is_platform_admin=True,
    )


@pytest.fixture
def organization_a(database: Session) -> tuple[User, Organization]:
    owner = create_user(
        database,
        email="owner-a@example.com",
        display_name="Owner A",
    )
    organization = create_organization(
        database,
        owner=owner,
        name="Organization A",
        slug="organization-a",
    )
    return owner, organization


@pytest.fixture
def organization_b(database: Session) -> tuple[User, Organization]:
    owner = create_user(
        database,
        email="owner-b@example.com",
        display_name="Owner B",
    )
    organization = create_organization(
        database,
        owner=owner,
        name="Organization B",
        slug="organization-b",
    )
    return owner, organization
