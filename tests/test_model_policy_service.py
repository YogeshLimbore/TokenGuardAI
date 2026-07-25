import json

import pytest

from app.services.model_policy_service import (
    GovernancePolicyConfigurationError,
    ModelAccessDeniedError,
    ModelPolicyService,
)


class _FakeSettings:
    def __init__(self, policy_file: str) -> None:
        self.policy_file = policy_file


def _write_policy(tmp_path, policy: dict) -> str:
    path = tmp_path / "governance_policy.json"
    path.write_text(
        json.dumps(policy),
        encoding="utf-8",
    )
    return str(path)


def _make_service(
    monkeypatch,
    tmp_path,
    policy: dict,
) -> ModelPolicyService:
    policy_file = _write_policy(tmp_path, policy)

    monkeypatch.setattr(
        "app.services.model_policy_service.get_settings",
        lambda: _FakeSettings(policy_file),
    )

    return ModelPolicyService()


BASE_POLICY = {
    "allowed_providers": ["ollama", "openrouter"],
    "allowed_models": {
        "ollama": ["llama3.2", "llama3.1"],
        "openrouter": ["openai/gpt-4.1-mini"],
    },
    "blocked_models": ["restricted-model"],
}


def test_allowed_model_does_not_raise(monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, BASE_POLICY)

    service.enforce_model_access(
        project_id=1,
        provider="ollama",
        model="llama3.2",
    )


def test_disallowed_provider_is_denied(monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, BASE_POLICY)

    with pytest.raises(ModelAccessDeniedError):
        service.enforce_model_access(
            project_id=1,
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
        )


def test_blocked_model_is_denied(monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, BASE_POLICY)

    with pytest.raises(ModelAccessDeniedError):
        service.enforce_model_access(
            project_id=1,
            provider="ollama",
            model="restricted-model",
        )


def test_model_not_in_provider_allowlist_is_denied(monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, BASE_POLICY)

    with pytest.raises(ModelAccessDeniedError):
        service.enforce_model_access(
            project_id=1,
            provider="ollama",
            model="mistral",
        )


def test_auto_routed_model_is_still_enforced(monkeypatch, tmp_path):
    """
    ``enforce_model_access`` has no notion of how the provider/model
    pair was chosen, so a model selected by automatic routing is
    denied exactly like a manually-requested one.
    """
    service = _make_service(monkeypatch, tmp_path, BASE_POLICY)

    auto_routed_model = "mistral"

    with pytest.raises(ModelAccessDeniedError):
        service.enforce_model_access(
            project_id=1,
            provider="ollama",
            model=auto_routed_model,
        )


def test_project_override_restricts_allowed_models(monkeypatch, tmp_path):
    policy = {
        **BASE_POLICY,
        "projects": {
            "5": {
                "allowed_models": {
                    "ollama": ["llama3.2"],
                }
            }
        },
    }

    service = _make_service(monkeypatch, tmp_path, policy)

    # Project 5's override drops "llama3.1" from the allowlist.
    with pytest.raises(ModelAccessDeniedError):
        service.enforce_model_access(
            project_id=5,
            provider="ollama",
            model="llama3.1",
        )

    # A different project without an override still uses the base
    # policy, so the same model/provider pair is allowed.
    service.enforce_model_access(
        project_id=6,
        provider="ollama",
        model="llama3.1",
    )


def test_get_effective_policy_without_override_matches_base(monkeypatch, tmp_path):
    service = _make_service(monkeypatch, tmp_path, BASE_POLICY)

    effective = service.get_effective_policy(project_id=999)

    assert effective["allowed_providers"] == BASE_POLICY["allowed_providers"]
    assert effective["allowed_models"] == BASE_POLICY["allowed_models"]


def test_missing_policy_file_raises_configuration_error(monkeypatch, tmp_path):
    missing_path = str(tmp_path / "does_not_exist.json")

    monkeypatch.setattr(
        "app.services.model_policy_service.get_settings",
        lambda: _FakeSettings(missing_path),
    )

    with pytest.raises(GovernancePolicyConfigurationError):
        ModelPolicyService()
