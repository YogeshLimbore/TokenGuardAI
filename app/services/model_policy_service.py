from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import ProjectGovernancePolicy
from app.providers.registry import get_provider


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
        database: Session | None = None,
    ) -> dict[str, object]:
        policy = dict(self.policy)

        projects = policy.get("projects", {})

        if isinstance(projects, dict):
            project_override = projects.get(str(project_id))

            if isinstance(project_override, dict):
                policy.update(project_override)

        if database is not None:
            stored = database.scalar(
                select(ProjectGovernancePolicy).where(
                    ProjectGovernancePolicy.project_id == project_id,
                    ProjectGovernancePolicy.is_active.is_(True),
                )
            )
            if stored is not None:
                try:
                    stored_policy = json.loads(stored.policy_json)
                except json.JSONDecodeError as error:
                    raise GovernancePolicyConfigurationError(
                        "Stored project governance policy is invalid."
                    ) from error
                self.validate_project_policy(stored_policy)
                policy.update(stored_policy)

        return policy

    @staticmethod
    def validate_project_policy(policy: Any) -> dict[str, object]:
        if not isinstance(policy, dict):
            raise GovernancePolicyConfigurationError(
                "Project governance policy must be an object."
            )
        supported = {"allowed_providers", "allowed_models", "blocked_models"}
        if set(policy) - supported:
            raise GovernancePolicyConfigurationError(
                "Project policy contains unsupported fields."
            )
        providers = policy.get("allowed_providers")
        if providers is not None:
            if not isinstance(providers, list) or not providers:
                raise GovernancePolicyConfigurationError(
                    "allowed_providers must be a non-empty list."
                )
            for provider in providers:
                if not isinstance(provider, str):
                    raise GovernancePolicyConfigurationError(
                        "Provider names must be strings."
                    )
                try:
                    get_provider(provider)
                except ValueError as error:
                    raise GovernancePolicyConfigurationError(
                        f"Unsupported provider in policy: {provider}"
                    ) from error
        models = policy.get("allowed_models")
        if models is not None:
            if not isinstance(models, dict):
                raise GovernancePolicyConfigurationError(
                    "allowed_models must be an object."
                )
            for provider, names in models.items():
                try:
                    get_provider(str(provider))
                except ValueError as error:
                    raise GovernancePolicyConfigurationError(
                        f"Unsupported provider in policy: {provider}"
                    ) from error
                if (
                    not isinstance(names, list)
                    or not names
                    or not all(isinstance(name, str) and name.strip() for name in names)
                ):
                    raise GovernancePolicyConfigurationError(
                        "Each provider model allow-list must contain model names."
                    )
        blocked = policy.get("blocked_models")
        if blocked is not None and (
            not isinstance(blocked, list)
            or not all(isinstance(name, str) and name.strip() for name in blocked)
        ):
            raise GovernancePolicyConfigurationError(
                "blocked_models must be a list of model names."
            )
        return policy

    def enforce_model_access(
        self,
        *,
        project_id: int,
        provider: str,
        model: str,
        database: Session | None = None,
    ) -> None:
        if not getattr(self.settings, "policy_enforcement_enabled", True):
            return
        policy = self.get_effective_policy(project_id=project_id, database=database)

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

            if (
                isinstance(provider_models, list)
                and "*" not in provider_models
                and model not in provider_models
            ):
                raise ModelAccessDeniedError(
                    f"Model '{model}' is not allowed for provider '{provider}'."
                )
