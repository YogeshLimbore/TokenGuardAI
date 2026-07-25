from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import (
    BudgetDecisionLog,
    LLMRequestLog,
    Project,
)
from app.services.pricing_service import PricingService


class BudgetConfigurationError(RuntimeError):
    pass


def _policy_number(
    policy: dict[str, object],
    key: str,
) -> float | None:
    """
    Read a numeric budget-policy value.

    Policy files are user-edited JSON, so a value can be any type. Returning
    None for an absent key lets the caller apply its own default, while a
    present-but-non-numeric value is a configuration error rather than
    something to silently paper over.
    """
    if key not in policy:
        return None

    value = policy[key]

    if isinstance(value, bool) or not isinstance(
        value,
        int | float | str,
    ):
        raise BudgetConfigurationError(f"Budget policy value '{key}' must be a number.")

    try:
        return float(value)
    except ValueError as error:
        raise BudgetConfigurationError(
            f"Budget policy value '{key}' must be a number."
        ) from error


def _policy_int(
    policy: dict[str, object],
    key: str,
    default: int,
) -> int:
    value = _policy_number(policy, key)

    return default if value is None else int(value)


def _policy_float(
    policy: dict[str, object],
    key: str,
    default: float,
) -> float:
    value = _policy_number(policy, key)

    return default if value is None else value


class BudgetExceededError(RuntimeError):
    def __init__(
        self,
        message: str,
        reasons: list[str],
    ) -> None:
        super().__init__(message)
        self.reasons = reasons


@dataclass(frozen=True)
class InternalBudgetDecision:
    enforcement_enabled: bool
    allowed: bool
    action: str

    original_provider: str
    original_model: str

    final_provider: str
    final_model: str

    estimated_input_tokens: int
    requested_output_tokens: int
    estimated_total_tokens: int
    estimated_cost_usd: float

    daily_tokens_used: int
    daily_token_budget: int

    daily_cost_used_usd: float
    daily_cost_budget_usd: float

    requests_per_minute: int
    reasons: list[str]


class BudgetService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.pricing_service = PricingService()
        self.policy = self._load_policy()

    def _load_policy(self) -> dict[str, object]:
        path = Path(self.settings.budget_policy_file)

        if not path.exists():
            raise BudgetConfigurationError(f"Budget policy not found: {path}")

        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise BudgetConfigurationError("Budget policy is invalid.") from error

        if not isinstance(data, dict):
            raise BudgetConfigurationError("Budget policy must be a JSON object.")

        return data

    def _provider_policy(
        self,
        provider: str,
    ) -> dict[str, object]:
        global_policy = self.policy.get(
            "global",
            {},
        )

        providers = self.policy.get(
            "providers",
            {},
        )

        provider_policy = {}

        if isinstance(global_policy, dict):
            provider_policy.update(global_policy)

        if isinstance(providers, dict):
            configured_provider = providers.get(
                provider,
                {},
            )

            if isinstance(configured_provider, dict):
                provider_policy.update(configured_provider)

        return provider_policy

    @staticmethod
    def _start_of_today_utc() -> datetime:
        now = datetime.now(UTC)

        return datetime(
            year=now.year,
            month=now.month,
            day=now.day,
            tzinfo=UTC,
        )

    def get_daily_usage(
        self,
        database: Session,
        *,
        provider: str,
        project_id: int,
    ) -> tuple[int, float]:
        statement = select(
            func.coalesce(
                func.sum(LLMRequestLog.total_tokens),
                0,
            ),
            func.coalesce(
                func.sum(LLMRequestLog.estimated_cost_usd),
                0.0,
            ),
        ).where(
            LLMRequestLog.provider == provider,
            LLMRequestLog.project_id == project_id,
            LLMRequestLog.status == "completed",
            LLMRequestLog.created_at >= self._start_of_today_utc(),
        )

        row = database.execute(statement).one()

        return (
            int(row[0] or 0),
            float(row[1] or 0.0),
        )

    def evaluate(
        self,
        database: Session,
        *,
        project: Project,
        request_id: str,
        provider: str,
        model: str,
        estimated_input_tokens: int,
        requested_output_tokens: int,
        request_maximum_cost_usd: float | None,
        request_maximum_total_tokens: int | None,
        allow_downgrade: bool,
    ) -> InternalBudgetDecision:
        policy = self._provider_policy(provider)

        maximum_input_tokens = _policy_int(
            policy,
            "maximum_input_tokens_per_request",
            12_000,
        )

        maximum_output_tokens = _policy_int(
            policy,
            "maximum_output_tokens_per_request",
            3_000,
        )

        maximum_total_tokens = _policy_int(
            policy,
            "maximum_total_tokens_per_request",
            15_000,
        )

        policy_maximum_cost = _policy_float(
            policy,
            "maximum_estimated_cost_usd_per_request",
            0.05,
        )

        daily_cost_budget = _policy_float(
            policy,
            "daily_cost_budget_usd",
            2.0,
        )

        daily_token_budget = _policy_int(
            policy,
            "daily_token_budget",
            500_000,
        )

        requests_per_minute = _policy_int(
            policy,
            "requests_per_minute",
            30,
        )

        if request_maximum_total_tokens is not None:
            maximum_total_tokens = min(
                maximum_total_tokens,
                request_maximum_total_tokens,
            )

        maximum_cost = policy_maximum_cost

        if request_maximum_cost_usd is not None:
            maximum_cost = min(
                maximum_cost,
                request_maximum_cost_usd,
            )

        # Project-level limits always tighten (never loosen) the
        # policy-derived limits above.
        maximum_total_tokens = min(
            maximum_total_tokens,
            project.maximum_tokens_per_request,
        )

        maximum_cost = min(
            maximum_cost,
            project.maximum_cost_per_request_usd,
        )

        daily_token_budget = min(
            daily_token_budget,
            project.daily_token_budget,
        )

        daily_cost_budget = min(
            daily_cost_budget,
            project.daily_cost_budget_usd,
        )

        requests_per_minute = min(
            requests_per_minute,
            project.requests_per_minute,
        )

        estimated_total_tokens = estimated_input_tokens + requested_output_tokens

        estimated_cost = self.pricing_service.calculate_cost(
            provider=provider,
            model=model,
            input_tokens=estimated_input_tokens,
            output_tokens=requested_output_tokens,
        )

        daily_tokens_used, daily_cost_used = self.get_daily_usage(
            database,
            provider=provider,
            project_id=project.id,
        )

        reasons: list[str] = []

        if estimated_input_tokens > maximum_input_tokens:
            reasons.append(
                "Estimated input tokens exceed the per-request input-token limit."
            )

        if requested_output_tokens > maximum_output_tokens:
            reasons.append(
                "Requested output tokens exceed the per-request output-token limit."
            )

        if estimated_total_tokens > maximum_total_tokens:
            reasons.append("Estimated total tokens exceed the per-request token limit.")

        if (
            estimated_cost.pricing_found
            and estimated_cost.total_cost_usd > maximum_cost
        ):
            reasons.append("Estimated request cost exceeds the per-request cost limit.")

        if daily_tokens_used + estimated_total_tokens > daily_token_budget:
            reasons.append("The provider daily token budget would be exceeded.")

        if (
            estimated_cost.pricing_found
            and daily_cost_used + estimated_cost.total_cost_usd > daily_cost_budget
        ):
            reasons.append("The provider daily cost budget would be exceeded.")

        final_provider = provider
        final_model = model
        action = "allow"
        allowed = True

        downgrade_enabled = bool(
            policy.get(
                "fallback_to_ollama_on_budget_exceeded",
                True,
            )
        )

        if reasons:
            downgrade_routes = self.policy.get(
                "downgrade_routes",
                {},
            )

            downgrade_route = (
                downgrade_routes.get(provider)
                if isinstance(
                    downgrade_routes,
                    dict,
                )
                else None
            )

            if (
                allow_downgrade
                and downgrade_enabled
                and isinstance(
                    downgrade_route,
                    dict,
                )
            ):
                final_provider = str(downgrade_route["provider"])

                final_model = str(downgrade_route["model"])

                action = "downgrade"
                allowed = True

                reasons.append(
                    f"The request was downgraded to {final_provider}/{final_model}."
                )

            else:
                action = "reject"
                allowed = False

        decision = InternalBudgetDecision(
            enforcement_enabled=(self.settings.budget_enforcement_enabled),
            allowed=allowed,
            action=action,
            original_provider=provider,
            original_model=model,
            final_provider=final_provider,
            final_model=final_model,
            estimated_input_tokens=(estimated_input_tokens),
            requested_output_tokens=(requested_output_tokens),
            estimated_total_tokens=(estimated_total_tokens),
            estimated_cost_usd=(estimated_cost.total_cost_usd),
            daily_tokens_used=daily_tokens_used,
            daily_token_budget=daily_token_budget,
            daily_cost_used_usd=round(
                daily_cost_used,
                10,
            ),
            daily_cost_budget_usd=(daily_cost_budget),
            requests_per_minute=(requests_per_minute),
            reasons=(reasons or ["The request is within budget."]),
        )

        self.log_decision(
            database,
            project_id=project.id,
            request_id=request_id,
            decision=decision,
        )

        return decision

    @staticmethod
    def log_decision(
        database: Session,
        *,
        project_id: int,
        request_id: str,
        decision: InternalBudgetDecision,
    ) -> None:
        database.add(
            BudgetDecisionLog(
                project_id=project_id,
                request_id=request_id,
                provider=decision.final_provider,
                model=decision.final_model,
                estimated_input_tokens=(decision.estimated_input_tokens),
                requested_output_tokens=(decision.requested_output_tokens),
                estimated_total_tokens=(decision.estimated_total_tokens),
                estimated_cost_usd=(decision.estimated_cost_usd),
                daily_tokens_before_request=(decision.daily_tokens_used),
                daily_cost_before_request_usd=(decision.daily_cost_used_usd),
                allowed=decision.allowed,
                action=decision.action,
                reason=" ".join(decision.reasons),
                original_provider=(decision.original_provider),
                original_model=(decision.original_model),
            )
        )

        database.commit()
