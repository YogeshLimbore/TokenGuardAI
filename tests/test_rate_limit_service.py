import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.database.connection import Base
from app.database.models import RateLimitEvent
from app.services import rate_limit_service as rate_limit_service_module
from app.services.rate_limit_service import (
    RateLimitExceededError,
    RateLimitService,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()


def _make_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rate_limiting_enabled: bool = True,
) -> RateLimitService:
    test_settings = Settings(rate_limiting_enabled=rate_limiting_enabled)

    monkeypatch.setattr(
        rate_limit_service_module,
        "get_settings",
        lambda: test_settings,
    )

    return RateLimitService()


def test_requests_within_limit_are_allowed(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(monkeypatch)

    for index in range(3):
        service.enforce(
            db_session,
            request_id=f"req-{index}",
            provider="ollama",
            requests_per_minute=3,
        )

    events = list(db_session.scalars(select(RateLimitEvent)).all())

    assert len(events) == 3


def test_request_over_limit_raises(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(monkeypatch)

    for index in range(2):
        service.enforce(
            db_session,
            request_id=f"req-{index}",
            provider="ollama",
            requests_per_minute=2,
        )

    with pytest.raises(RateLimitExceededError) as exc_info:
        service.enforce(
            db_session,
            request_id="req-over-limit",
            provider="ollama",
            requests_per_minute=2,
        )

    assert exc_info.value.provider == "ollama"
    assert exc_info.value.limit == 2
    assert exc_info.value.retry_after_seconds == 60

    events = list(db_session.scalars(select(RateLimitEvent)).all())

    assert len(events) == 2


def test_different_providers_have_independent_limits(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(monkeypatch)

    service.enforce(
        db_session,
        request_id="req-ollama-1",
        provider="ollama",
        requests_per_minute=1,
    )

    # A different provider's counter is independent of
    # ollama's, so this call should not be blocked.
    service.enforce(
        db_session,
        request_id="req-openrouter-1",
        provider="openrouter",
        requests_per_minute=1,
    )

    with pytest.raises(RateLimitExceededError):
        service.enforce(
            db_session,
            request_id="req-ollama-2",
            provider="ollama",
            requests_per_minute=1,
        )


def test_disabled_rate_limiting_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(
        monkeypatch,
        rate_limiting_enabled=False,
    )

    for index in range(5):
        service.enforce(
            db_session,
            request_id=f"req-{index}",
            provider="ollama",
            requests_per_minute=1,
        )

    events = list(db_session.scalars(select(RateLimitEvent)).all())

    assert len(events) == 0
