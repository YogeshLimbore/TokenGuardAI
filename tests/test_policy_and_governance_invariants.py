"""
Tests for the invariants that keep audit records and policy reads honest.

Both behaviours exist because the alternative is a silent failure: an audit
record with no event type cannot be triaged, and a malformed policy value used
to raise an opaque TypeError deep inside a request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.budget_service import (
    BudgetConfigurationError,
    _policy_float,
    _policy_int,
)
from app.services.governance_service import (
    GovernanceCheckResult,
)

pytestmark = pytest.mark.unit


class TestGovernanceCheckResultAttribution:
    def test_safe_result_needs_no_attribution(self) -> None:
        result = GovernanceCheckResult(safe=True)

        assert result.safe is True
        assert result.event_type is None

    def test_blocked_result_exposes_attribution(
        self,
    ) -> None:
        result = GovernanceCheckResult(
            safe=False,
            event_type="output_secret_detected",
            severity="critical",
        )

        assert result.audit_event_type == "output_secret_detected"
        assert result.audit_severity == "critical"

    @pytest.mark.parametrize(
        ("event_type", "severity"),
        [
            (None, "high"),
            ("moderation_input_blocked", None),
            (None, None),
        ],
    )
    def test_blocked_result_rejects_missing_attribution(
        self,
        event_type: str | None,
        severity: str | None,
    ) -> None:
        with pytest.raises(ValueError, match="blocked"):
            GovernanceCheckResult(
                safe=False,
                event_type=event_type,
                severity=severity,
            )

    def test_safe_result_refuses_to_invent_attribution(
        self,
    ) -> None:
        """
        Reading the audit accessors on a safe result is a caller bug; failing
        loudly beats writing an audit row for a request that was allowed.
        """
        result = GovernanceCheckResult(safe=True)

        with pytest.raises(ValueError):
            _ = result.audit_event_type

        with pytest.raises(ValueError):
            _ = result.audit_severity


class TestBudgetPolicyReaders:
    def test_absent_key_uses_the_default(self) -> None:
        assert _policy_int({}, "daily_token_budget", 500) == 500
        assert _policy_float({}, "daily_cost_budget_usd", 2.5) == 2.5

    def test_numeric_values_are_read(self) -> None:
        policy: dict[str, object] = {
            "daily_token_budget": 1_000,
            "daily_cost_budget_usd": 0.75,
        }

        assert _policy_int(policy, "daily_token_budget", 1) == 1_000
        assert _policy_float(policy, "daily_cost_budget_usd", 0.0) == 0.75

    def test_numeric_strings_are_accepted(self) -> None:
        """JSON policies are hand-edited, so "30" is a realistic value."""
        policy: dict[str, object] = {"requests_per_minute": "30"}

        assert _policy_int(policy, "requests_per_minute", 1) == 30

    def test_float_is_truncated_for_integer_settings(
        self,
    ) -> None:
        policy: dict[str, object] = {"daily_token_budget": 1_500.9}

        assert _policy_int(policy, "daily_token_budget", 1) == 1_500

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-number",
            None,
            True,
            [1, 2],
            {"nested": 1},
        ],
    )
    def test_non_numeric_values_are_configuration_errors(
        self,
        value: object,
    ) -> None:
        policy: dict[str, object] = {"daily_token_budget": value}

        with pytest.raises(BudgetConfigurationError):
            _policy_int(policy, "daily_token_budget", 1)


class TestShippedBudgetPolicy:
    def test_every_numeric_value_in_the_shipped_policy_parses(
        self,
    ) -> None:
        """
        Guards against a typo in config/budget_policy.json reaching production,
        where it would only surface on a live request.
        """
        policy_path = (
            Path(__file__).resolve().parent.parent / "config" / "budget_policy.json"
        )

        data = json.loads(policy_path.read_text(encoding="utf-8"))

        numeric_keys = (
            "maximum_input_tokens_per_request",
            "maximum_output_tokens_per_request",
            "maximum_total_tokens_per_request",
            "maximum_estimated_cost_usd_per_request",
            "daily_cost_budget_usd",
            "daily_token_budget",
            "requests_per_minute",
        )

        sections: list[dict[str, object]] = []

        global_policy = data.get("global")

        if isinstance(global_policy, dict):
            sections.append(global_policy)

        providers = data.get("providers")

        if isinstance(providers, dict):
            sections.extend(
                section for section in providers.values() if isinstance(section, dict)
            )

        assert sections, "budget_policy.json defines no global or provider sections."

        for section in sections:
            for key in numeric_keys:
                # Raises BudgetConfigurationError on a malformed value.
                _policy_float(section, key, 0.0)
