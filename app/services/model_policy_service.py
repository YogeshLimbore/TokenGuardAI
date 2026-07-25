from __future__ import annotations

import json
from pathlib import Path

from app.core.config import get_settings


class ModelAccessDeniedError(RuntimeError):
    pass


class GovernancePolicyConfigurationError(RuntimeError):
    pass


class ModelPolicyService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.policy = self._load_policy()

    def _load_policy(self) -> dict[str, object]:
        path = Path(self.settings.policy_file)

        if not path.exists():
            raise GovernancePolicyConfigurationError(
                f"Governance policy not found: {path}"
            )

        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as file:
                policy = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise GovernancePolicyConfigurationError(
                "Governance policy is invalid."
            ) from error

        if not isinstance(policy, dict):
            raise GovernancePolicyConfigurationError(
                "Governance policy must be an object."
            )

        return policy

    def get_effective_policy(
        self,
        *,
        project_id: int,
    ) -> dict[str, object]:
        policy = dict(self.policy)

        projects = policy.get("projects", {})

        if isinstance(projects, dict):
            project_override = projects.get(str(project_id))

            if isinstance(project_override, dict):
                policy.update(project_override)

        return policy

    def enforce_model_access(
        self,
        *,
        project_id: int,
        provider: str,
        model: str,
    ) -> None:
        policy = self.get_effective_policy(project_id=project_id)

        allowed_providers = policy.get(
            "allowed_providers",
            [],
        )

        if isinstance(allowed_providers, list) and provider not in allowed_providers:
            raise ModelAccessDeniedError(f"Provider '{provider}' is not allowed.")

        blocked_models = policy.get(
            "blocked_models",
            [],
        )

        if isinstance(blocked_models, list) and model in blocked_models:
            raise ModelAccessDeniedError(f"Model '{model}' is blocked.")

        allowed_models = policy.get(
            "allowed_models",
            {},
        )

        if isinstance(allowed_models, dict):
            provider_models = allowed_models.get(provider)

            if isinstance(provider_models, list) and model not in provider_models:
                raise ModelAccessDeniedError(
                    f"Model '{model}' is not allowed for provider '{provider}'."
                )
