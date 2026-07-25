import pytest

from app.cache.redis_client import get_redis_client
from app.services.auth_rate_limit_service import (
    AuthRateLimitExceededError,
    AuthRateLimitService,
)


def test_auth_rate_limit_allows_requests_within_limit():
    service = AuthRateLimitService(get_redis_client())

    for _ in range(3):
        service.enforce(
            scope="test-login",
            identifier="127.0.0.1",
            limit=3,
        )


def test_auth_rate_limit_blocks_requests_over_limit():
    service = AuthRateLimitService(get_redis_client())

    for _ in range(2):
        service.enforce(
            scope="test-login-block",
            identifier="127.0.0.1",
            limit=2,
        )

    with pytest.raises(AuthRateLimitExceededError):
        service.enforce(
            scope="test-login-block",
            identifier="127.0.0.1",
            limit=2,
        )


def test_auth_rate_limit_is_scoped_per_identifier():
    service = AuthRateLimitService(get_redis_client())

    for _ in range(2):
        service.enforce(
            scope="test-login-scoped",
            identifier="1.1.1.1",
            limit=2,
        )

    # A different identifier has its own independent budget.
    service.enforce(
        scope="test-login-scoped",
        identifier="2.2.2.2",
        limit=2,
    )


def test_login_endpoint_enforces_rate_limit():
    """The /v1/auth/login endpoint itself returns 429 once the
    configured per-minute attempt budget for a client IP is exceeded."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.core.config import get_settings
    from app.database.connection import Base, get_database_session
    from app.main import app

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()

    app.dependency_overrides[get_database_session] = lambda: session

    limit = get_settings().auth_login_rate_limit_per_minute

    try:
        with TestClient(app) as test_client:
            statuses = []
            for _ in range(limit + 2):
                response = test_client.post(
                    "/v1/auth/login",
                    json={
                        "email": "nosuchuser@example.com",
                        "password": "WrongPassword!",
                    },
                )
                statuses.append(response.status_code)

            assert 429 in statuses
    finally:
        app.dependency_overrides.clear()
        session.close()
