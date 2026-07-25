import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.database.connection import Base
from app.database.models import BudgetDecisionLog, Project
from app.services import budget_service as budget_service_module
from app.services.budget_service import (
    BudgetConfigurationError,
    BudgetService,
)


def _make_project(**overrides: object) -> Project:
    defaults: dict[str, object] = {
        "id": 1,
        "name": "test-project",
        "is_active": True,
        "daily_token_budget": 10_000_000,
        "daily_cost_budget_usd": 1_000.0,
        "maximum_tokens_per_request": 1_000_000,
        "maximum_cost_per_request_usd": 1_000.0,
        "requests_per_minute": 10_000,
    }
    defaults.update(overrides)

    return Project(**defaults)


DEFAULT_POLICY = {
    "global": {
        "maximum_input_tokens_per_request": 12000,
        "maximum_output_tokens_per_request": 3000,
        "maximum_total_tokens_per_request": 15000,
        "maximum_estimated_cost_usd_per_request": 0.05,
        "daily_cost_budget_usd": 2.0,
        "daily_token_budget": 500000,
        "requests_per_minute": 30,
        "fallback_to_ollama_on_budget_exceeded": True,
    },
    "providers": {
        "ollama": {
            "daily_cost_budget_usd": 0.0,
            "daily_token_budget": 1000000,
            "requests_per_minute": 60,
        },
        "openrouter": {
            "daily_cost_budget_usd": 2.0,
            "daily_token_budget": 300000,
            "requests_per_minute": 20,
        },
    },
    "downgrade_routes": {
        "openrouter": {
            "provider": "ollama",
            "model": "llama3.2",
        },
    },
}


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()


def _write_policy(tmp_path: Path, policy: dict) -> Path:
    policy_path = tmp_path / "budget_policy.json"

    policy_path.write_text(
        json.dumps(policy),
        encoding="utf-8",
    )

    return policy_path


def _make_budget_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: dict,
) -> BudgetService:
    policy_path = _write_policy(tmp_path, policy)

    test_settings = Settings(
        budget_policy_file=str(policy_path),
        model_pricing_file="config/model_pricing.json",
    )

    monkeypatch.setattr(
        budget_service_module,
        "get_settings",
        lambda: test_settings,
    )

    return BudgetService()


def test_missing_policy_file_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path = tmp_path / "missing.json"

    test_settings = Settings(
        budget_policy_file=str(missing_path),
        model_pricing_file="config/model_pricing.json",
    )

    monkeypatch.setattr(
        budget_service_module,
        "get_settings",
        lambda: test_settings,
    )

    with pytest.raises(BudgetConfigurationError):
        BudgetService()


def test_request_within_budget_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_budget_service(
        tmp_path,
        monkeypatch,
        DEFAULT_POLICY,
    )

    decision = service.evaluate(
        db_session,
        project=_make_project(),
        request_id="req-allow-1",
        provider="ollama",
        model="llama3.2",
        estimated_input_tokens=50,
        requested_output_tokens=100,
        request_maximum_cost_usd=None,
        request_maximum_total_tokens=None,
        allow_downgrade=True,
    )

    assert decision.allowed is True
    assert decision.action == "allow"
    assert decision.final_provider == "ollama"
    assert decision.final_model == "llama3.2"
    assert decision.reasons == ["The request is within budget."]


def test_cost_over_limit_downgrades_when_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_budget_service(
        tmp_path,
        monkeypatch,
        DEFAULT_POLICY,
    )

    decision = service.evaluate(
        db_session,
        project=_make_project(),
        request_id="req-downgrade-1",
        provider="openrouter",
        model="openai/gpt-4.1-mini",
        estimated_input_tokens=100,
        requested_output_tokens=2000,
        request_maximum_cost_usd=0.000001,
        request_maximum_total_tokens=None,
        allow_downgrade=True,
    )

    assert decision.allowed is True
    assert decision.action == "downgrade"
    assert decision.original_provider == "openrouter"
    assert decision.original_model == "openai/gpt-4.1-mini"
    assert decision.final_provider == "ollama"
    assert decision.final_model == "llama3.2"
    assert (
        "Estimated request cost exceeds the per-request cost limit."
    ) in decision.reasons


def test_cost_over_limit_rejects_when_downgrade_disallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_budget_service(
        tmp_path,
        monkeypatch,
        DEFAULT_POLICY,
    )

    decision = service.evaluate(
        db_session,
        project=_make_project(),
        request_id="req-reject-1",
        provider="openrouter",
        model="openai/gpt-4.1-mini",
        estimated_input_tokens=100,
        requested_output_tokens=2000,
        request_maximum_cost_usd=0.000001,
        request_maximum_total_tokens=None,
        allow_downgrade=False,
    )

    assert decision.allowed is False
    assert decision.action == "reject"
    assert decision.final_provider == "openrouter"


def test_total_token_limit_is_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_budget_service(
        tmp_path,
        monkeypatch,
        DEFAULT_POLICY,
    )

    decision = service.evaluate(
        db_session,
        project=_make_project(),
        request_id="req-tokens-1",
        provider="openrouter",
        model="openai/gpt-4.1-mini",
        estimated_input_tokens=50,
        requested_output_tokens=3000,
        request_maximum_cost_usd=1.0,
        request_maximum_total_tokens=100,
        allow_downgrade=False,
    )

    assert decision.allowed is False
    assert (
        "Estimated total tokens exceed the per-request token limit."
    ) in decision.reasons


def test_decision_is_logged_to_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    service = _make_budget_service(
        tmp_path,
        monkeypatch,
        DEFAULT_POLICY,
    )

    service.evaluate(
        db_session,
        project=_make_project(),
        request_id="req-log-1",
        provider="ollama",
        model="llama3.2",
        estimated_input_tokens=10,
        requested_output_tokens=10,
        request_maximum_cost_usd=None,
        request_maximum_total_tokens=None,
        allow_downgrade=True,
    )

    logs = list(db_session.scalars(select(BudgetDecisionLog)).all())

    assert len(logs) == 1
    assert logs[0].request_id == "req-log-1"
    assert logs[0].allowed is True
    assert logs[0].action == "allow"
