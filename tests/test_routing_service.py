"""
Automatic model routing.

Routing decides which provider and model serve a request, so it directly
controls both answer quality and spend. These tests pin the tier boundaries,
the preference adjustments, and the failure modes of a malformed policy file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.schemas.chat import ChatCompletionRequest, ChatMessage
from app.services.routing_service import (
    RoutingConfigurationError,
    RoutingService,
)

pytestmark = pytest.mark.unit

SIMPLE_PROMPT = "Translate this to French in one sentence."
BALANCED_PROMPT = (
    "Implement a python function that parses a docker compose file "
    "and returns the service names."
)
ADVANCED_PROMPT = (
    "Walk through the architecture and system design of a distributed "
    "queue, then optimize it and explain the trade-off in each decision. "
    "Include a threat model and a mathematical proof of the ordering "
    "guarantee, and reason step by step about failure modes in "
    "production-grade deployments. Implement the consumer in python, "
    "debug the redelivery path, and show the sql for the outbox table."
)

NEUTRAL_PROMPT = "Summarize the attached quarterly report for the board."


def _request(prompt: str, **overrides) -> ChatCompletionRequest:
    payload = {
        "provider": "ollama",
        "messages": [ChatMessage(role="user", content=prompt)],
        "auto_route": True,
    }
    payload.update(overrides)

    return ChatCompletionRequest(**payload)


@pytest.fixture(autouse=True)
def _enable_auto_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auto_routing_enabled", True)


def test_manual_routing_keeps_the_requested_provider_and_model() -> None:
    decision = RoutingService().decide(
        _request(
            SIMPLE_PROMPT,
            auto_route=False,
            provider="openrouter",
            model="openai/gpt-4.1-mini",
        )
    )

    assert decision.automatic_routing_used is False
    assert decision.tier == "manual"
    assert decision.provider == "openrouter"
    assert decision.model == "openai/gpt-4.1-mini"
    assert decision.confidence == 1.0
    assert decision.maximum_output_tokens is None


@pytest.mark.parametrize(
    ("provider", "expected_setting"),
    [
        ("ollama", "ollama_default_model"),
        ("openai", "openai_default_model"),
        ("anthropic", "anthropic_default_model"),
        ("groq", "groq_default_model"),
        ("openrouter", "openrouter_default_model"),
    ],
)
def test_manual_routing_falls_back_to_the_provider_default_model(
    provider: str,
    expected_setting: str,
) -> None:
    decision = RoutingService().decide(
        _request(SIMPLE_PROMPT, auto_route=False, provider=provider)
    )

    assert decision.model == getattr(get_settings(), expected_setting)


def test_disabled_auto_routing_is_reported_as_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        get_settings(),
        "auto_routing_enabled",
        False,
    )

    decision = RoutingService().decide(_request(ADVANCED_PROMPT))

    assert decision.automatic_routing_used is False
    assert decision.reasons == ["Automatic routing was disabled."]


def test_simple_prompt_routes_to_the_local_tier() -> None:
    decision = RoutingService().decide(_request(SIMPLE_PROMPT, max_tokens=150))

    assert decision.tier == "simple"
    assert decision.provider == "ollama"
    assert decision.maximum_output_tokens == 300
    assert decision.complexity_score == 0


def test_coding_prompt_routes_to_the_balanced_tier() -> None:
    decision = RoutingService().decide(_request(BALANCED_PROMPT))

    assert decision.tier == "balanced"
    assert decision.provider == "openrouter"
    assert decision.model == "openai/gpt-4.1-mini"


def test_complex_prompt_routes_to_the_advanced_tier() -> None:
    decision = RoutingService().decide(_request(ADVANCED_PROMPT))

    assert decision.tier == "advanced"
    assert decision.model == "anthropic/claude-sonnet-4"
    assert decision.complexity_score >= 7
    assert decision.confidence >= 0.65


def test_cost_preference_makes_upgrades_harder() -> None:
    service = RoutingService()

    balanced = service.decide(_request(BALANCED_PROMPT, routing_preference="balanced"))
    cost = service.decide(_request(BALANCED_PROMPT, routing_preference="cost"))

    assert balanced.tier == "balanced"
    assert cost.tier == "simple"


def test_quality_preference_makes_upgrades_easier() -> None:
    service = RoutingService()

    balanced = service.decide(_request(BALANCED_PROMPT, routing_preference="balanced"))
    quality = service.decide(_request(BALANCED_PROMPT, routing_preference="quality"))

    assert quality.complexity_score == balanced.complexity_score
    assert quality.tier in {"balanced", "advanced"}


def test_long_output_requests_increase_complexity() -> None:
    service = RoutingService()

    short = service.decide(_request(NEUTRAL_PROMPT, max_tokens=100))
    long = service.decide(_request(NEUTRAL_PROMPT, max_tokens=3000))

    assert long.complexity_score > short.complexity_score
    assert any("long output" in reason for reason in long.reasons)
    assert any("short output" in reason for reason in short.reasons)


def test_long_conversation_history_increases_complexity() -> None:
    messages = [
        ChatMessage(role="user", content="Question one."),
        ChatMessage(role="assistant", content="Answer one."),
        ChatMessage(role="user", content="Question two."),
        ChatMessage(role="assistant", content="Answer two."),
        ChatMessage(role="user", content="Question three."),
        ChatMessage(role="assistant", content="Answer three."),
    ]

    decision = RoutingService().decide(
        ChatCompletionRequest(
            provider="ollama",
            messages=messages,
            auto_route=True,
        )
    )

    assert any("history" in reason for reason in decision.reasons)


def test_verbose_prompts_increase_complexity() -> None:
    decision = RoutingService().decide(_request("word " * 200))

    assert any("150 words" in reason for reason in decision.reasons)
    assert decision.complexity_score >= 3


def test_simple_task_indicators_reduce_complexity() -> None:
    service = RoutingService()

    with_indicator = service.decide(
        _request("Refactor this python function. Give a short answer in one sentence.")
    )

    assert any("Simple-task indicators" in reason for reason in with_indicator.reasons)


def test_complexity_score_never_goes_negative() -> None:
    decision = RoutingService().decide(
        _request(
            "Classify this in one word.",
            max_tokens=50,
        )
    )

    assert decision.complexity_score == 0


def test_decision_records_the_originally_requested_route() -> None:
    decision = RoutingService().decide(
        _request(
            ADVANCED_PROMPT,
            provider="ollama",
            model="llama3.2",
        )
    )

    assert decision.original_provider == "ollama"
    assert decision.original_model == "llama3.2"
    assert decision.provider != "ollama"


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------


def _service_with_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: object,
    *,
    raw: str | None = None,
) -> RoutingService:
    policy_file = tmp_path / "routing_policy.json"

    if raw is not None:
        policy_file.write_text(raw, encoding="utf-8")
    else:
        policy_file.write_text(json.dumps(policy), encoding="utf-8")

    monkeypatch.setattr(
        get_settings(),
        "routing_policy_file",
        str(policy_file),
    )

    return RoutingService()


def test_missing_policy_file_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        get_settings(),
        "routing_policy_file",
        str(tmp_path / "absent.json"),
    )

    with pytest.raises(RoutingConfigurationError, match="not found"):
        RoutingService()


def test_invalid_json_policy_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(RoutingConfigurationError, match="invalid"):
        _service_with_policy(monkeypatch, tmp_path, None, raw="{not json")


def test_non_object_policy_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with pytest.raises(RoutingConfigurationError, match="JSON object"):
        _service_with_policy(monkeypatch, tmp_path, ["not", "an", "object"])


def test_policy_without_routes_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = _service_with_policy(monkeypatch, tmp_path, {})

    with pytest.raises(RoutingConfigurationError, match="no routes"):
        service.decide(_request(SIMPLE_PROMPT))


def test_policy_missing_the_selected_tier_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = _service_with_policy(
        monkeypatch,
        tmp_path,
        {"routes": {"balanced": {}}},
    )

    with pytest.raises(RoutingConfigurationError, match="missing"):
        service.decide(_request(SIMPLE_PROMPT))


def test_incomplete_route_definition_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = _service_with_policy(
        monkeypatch,
        tmp_path,
        {"routes": {"simple": {"provider": "ollama"}}},
    )

    with pytest.raises(RoutingConfigurationError, match="invalid"):
        service.decide(_request(SIMPLE_PROMPT))


def test_custom_thresholds_are_honoured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = _service_with_policy(
        monkeypatch,
        tmp_path,
        {
            "routes": {
                "simple": {
                    "provider": "ollama",
                    "model": "llama3.2",
                    "maximum_output_tokens": 300,
                },
                "balanced": {
                    "provider": "openrouter",
                    "model": "openai/gpt-4.1-mini",
                    "maximum_output_tokens": 1000,
                },
                "advanced": {
                    "provider": "openrouter",
                    "model": "anthropic/claude-sonnet-4",
                    "maximum_output_tokens": 3000,
                },
            },
            "thresholds": {"balanced_score": 1, "advanced_score": 2},
        },
    )

    decision = service.decide(_request(BALANCED_PROMPT))

    assert decision.tier == "advanced"


def test_malformed_thresholds_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = _service_with_policy(
        monkeypatch,
        tmp_path,
        {
            "routes": {
                "simple": {
                    "provider": "ollama",
                    "model": "llama3.2",
                    "maximum_output_tokens": 300,
                },
                "balanced": {
                    "provider": "openrouter",
                    "model": "openai/gpt-4.1-mini",
                    "maximum_output_tokens": 1000,
                },
            },
            "thresholds": "not-an-object",
        },
    )

    decision = service.decide(_request(BALANCED_PROMPT))

    assert decision.tier == "balanced"
