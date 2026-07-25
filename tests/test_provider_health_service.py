import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.database.connection import Base
from app.database.models import ProviderHealthEvent
from app.services import (
    provider_health_service as provider_health_service_module,
)
from app.services.provider_health_service import (
    ProviderHealthService,
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
    provider_health_window_minutes: int = 15,
    provider_unhealthy_score_threshold: float = 40.0,
) -> ProviderHealthService:
    test_settings = Settings(
        provider_health_window_minutes=(provider_health_window_minutes),
        provider_unhealthy_score_threshold=(provider_unhealthy_score_threshold),
    )

    monkeypatch.setattr(
        provider_health_service_module,
        "get_settings",
        lambda: test_settings,
    )

    return ProviderHealthService()


def _add_event(
    db_session: Session,
    *,
    provider: str = "ollama",
    model: str = "llama3.2",
    succeeded: bool,
    latency_ms: float,
) -> None:
    db_session.add(
        ProviderHealthEvent(
            provider=provider,
            model=model,
            succeeded=succeeded,
            latency_ms=latency_ms,
            error_type=(None if succeeded else "SomeProviderError"),
            retry_count=0,
        )
    )
    db_session.commit()


def test_perfect_provider_receives_high_score(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(monkeypatch)

    for _ in range(10):
        _add_event(
            db_session,
            succeeded=True,
            latency_ms=50.0,
        )

    result = service.calculate_health(
        db_session,
        provider="ollama",
        model="llama3.2",
    )

    assert result.attempts == 10
    assert result.failures == 0
    assert result.success_rate_percent == 100.0
    assert result.score >= 95.0
    assert result.healthy is True


def test_failures_reduce_score(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(monkeypatch)

    for _ in range(5):
        _add_event(
            db_session,
            succeeded=True,
            latency_ms=50.0,
        )

    for _ in range(5):
        _add_event(
            db_session,
            succeeded=False,
            latency_ms=50.0,
        )

    result = service.calculate_health(
        db_session,
        provider="ollama",
        model="llama3.2",
    )

    assert result.attempts == 10
    assert result.failures == 5
    assert result.success_rate_percent == 50.0
    assert result.score < 100.0
    # 50% failure rate incurs a 37.5-point penalty
    # (failure_penalty = (100 - 50) * 0.75).
    assert result.score == pytest.approx(
        62.5,
        abs=0.5,
    )


def test_high_latency_reduces_score(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(monkeypatch)

    for _ in range(5):
        _add_event(
            db_session,
            succeeded=True,
            latency_ms=5_000.0,
        )

    result = service.calculate_health(
        db_session,
        provider="ollama",
        model="llama3.2",
    )

    assert result.success_rate_percent == 100.0
    # Latency penalty is capped at 25 points, so a fully
    # successful but very slow provider still loses points.
    assert result.score == pytest.approx(
        75.0,
        abs=0.5,
    )


def test_no_history_uses_neutral_healthy_score(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(monkeypatch)

    result = service.calculate_health(
        db_session,
        provider="never-called-provider",
        model="some-model",
    )

    assert result.attempts == 0
    assert result.failures == 0
    assert result.score == 100.0
    assert result.healthy is True


def test_unhealthy_threshold_is_respected(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_service(
        monkeypatch,
        provider_unhealthy_score_threshold=90.0,
    )

    for _ in range(5):
        _add_event(
            db_session,
            succeeded=True,
            latency_ms=50.0,
        )

    for _ in range(5):
        _add_event(
            db_session,
            succeeded=False,
            latency_ms=50.0,
        )

    result = service.calculate_health(
        db_session,
        provider="ollama",
        model="llama3.2",
    )

    # 50% failures score well under a 90 threshold.
    assert result.healthy is False
