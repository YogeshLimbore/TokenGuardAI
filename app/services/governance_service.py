from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import get_settings
from app.schemas.chat import ChatMessage
from app.schemas.governance import (
    GovernanceDecision,
)
from app.services.model_policy_service import (
    ModelPolicyService,
)
from app.services.moderation_service import (
    ModerationService,
)
from app.services.output_security_service import (
    OutputSecurityService,
)
from app.services.pii_service import PIIService
from app.services.prompt_injection_service import (
    PromptInjectionService,
)


@dataclass(frozen=True)
class GovernanceResult:
    decision: GovernanceDecision
    messages: list[ChatMessage]


@dataclass(frozen=True)
class GovernanceCheckResult:
    """
    Result of a single moderation/output-security check. Unlike
    ``GovernanceResult`` (which governs an entire request), this
    covers one specific check so the caller can attribute the
    correct security-audit ``event_type`` when it blocks.

    A blocked result must always carry its attribution: an audit record
    with a missing event type or severity cannot be triaged later.
    """

    safe: bool
    event_type: str | None = None
    severity: str | None = None
    detected_categories: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.safe and (self.event_type is None or self.severity is None):
            raise ValueError(
                "A blocked governance check requires both an event_type and a severity."
            )

    @property
    def audit_event_type(self) -> str:
        if self.event_type is None:
            raise ValueError("A safe governance check has no audit event type.")

        return self.event_type

    @property
    def audit_severity(self) -> str:
        if self.severity is None:
            raise ValueError("A safe governance check has no audit severity.")

        return self.severity


class GovernanceService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.model_policy = ModelPolicyService()
        self.prompt_injection = PromptInjectionService()
        self.pii_service = PIIService()
        self.output_security = OutputSecurityService()
        self.moderation = ModerationService()

    def evaluate_input(
        self,
        *,
        project_id: int,
        provider: str,
        model: str,
        messages: list[ChatMessage],
    ) -> GovernanceResult:
        self.model_policy.enforce_model_access(
            project_id=project_id,
            provider=provider,
            model=model,
        )

        policy = self.model_policy.get_effective_policy(project_id=project_id)

        pii_policy = policy.get("pii", {})

        entities = (
            pii_policy.get("entities", []) if isinstance(pii_policy, dict) else []
        )

        allowed_entities = set(
            pii_policy.get(
                "allow_entities",
                [],
            )
            if isinstance(pii_policy, dict)
            else []
        )

        entity_actions = (
            pii_policy.get(
                "entity_actions",
                {},
            )
            if isinstance(pii_policy, dict)
            else {}
        )

        all_injection_findings = []
        all_pii_findings = []
        sanitized_messages: list[ChatMessage] = []

        blocked_reasons: list[str] = []
        sanitized_count = 0
        maximum_injection_score = 0

        for message in messages:
            injection = self.prompt_injection.analyze(message.content)

            maximum_injection_score = max(
                maximum_injection_score,
                injection.score,
            )

            all_injection_findings.extend(injection.findings)

            if injection.blocked:
                blocked_reasons.append(
                    "Prompt-injection risk exceeded the configured block threshold."
                )

            pii_result = self.pii_service.scan(
                text=message.content,
                entities=list(entities),
                allowed_entities=allowed_entities,
                entity_actions=dict(entity_actions),
            )

            all_pii_findings.extend(pii_result.findings)

            if pii_result.blocked:
                blocked_reasons.append(
                    "The request contains blocked sensitive-data categories."
                )

            if pii_result.sanitized:
                sanitized_count += 1

            sanitized_messages.append(
                ChatMessage(
                    role=message.role,
                    content=pii_result.processed_text,
                )
            )

        allowed = not blocked_reasons

        if not allowed:
            action = "block"
        elif sanitized_count:
            action = "redact"
        elif maximum_injection_score >= self.settings.prompt_injection_review_threshold:
            action = "review"
        else:
            action = "allow"

        reasons = blocked_reasons or ["The request passed governance checks."]

        decision = GovernanceDecision(
            allowed=allowed,
            action=action,
            original_provider=provider,
            original_model=model,
            sanitized=(sanitized_count > 0),
            sanitized_messages_count=(sanitized_count),
            prompt_injection_score=(maximum_injection_score),
            prompt_injection_findings=(all_injection_findings),
            sensitive_data_findings=(all_pii_findings),
            moderation_findings=[],
            reasons=reasons,
        )

        return GovernanceResult(
            decision=decision,
            messages=sanitized_messages,
        )

    async def evaluate_input_moderation(
        self,
        *,
        messages: list[ChatMessage],
    ) -> GovernanceCheckResult:
        """
        Runs content moderation over the (already PII-sanitized)
        input messages. Kept separate from ``evaluate_input`` so a
        moderation block is recorded under its own security-audit
        ``event_type`` rather than the generic governance one.
        """

        if not self.settings.input_moderation_enabled:
            return GovernanceCheckResult(safe=True)
        if not self.settings.openai_api_key:
            return GovernanceCheckResult(
                safe=not self.settings.moderation_fail_closed,
                event_type="moderation_unavailable",
                severity="high",
                detected_categories=["moderation_not_configured"],
            )

        combined_text = "\n".join(message.content for message in messages)

        try:
            moderation_result = await self.moderation.moderate(
                text=combined_text,
                api_key=self.settings.openai_api_key,
            )
        except Exception:
            return GovernanceCheckResult(
                safe=not self.settings.moderation_fail_closed,
                event_type="moderation_unavailable",
                severity="high",
                detected_categories=["moderation_unavailable"],
            )

        if not moderation_result.flagged:
            return GovernanceCheckResult(safe=True)

        return GovernanceCheckResult(
            safe=False,
            event_type="moderation_input_blocked",
            severity="high",
            detected_categories=[
                finding.category
                for finding in moderation_result.findings
                if finding.flagged
            ],
        )

    async def evaluate_output(
        self,
        *,
        text: str,
    ) -> GovernanceCheckResult:
        """
        Scans finished provider output for leaked secrets and
        (optionally) policy-violating content before it is cached
        or returned. Secret scanning runs first, since a leaked
        credential is unambiguous and does not require an external
        moderation call to confirm.
        """

        if self.settings.output_secret_scan_enabled:
            secret_scan = self.output_security.analyze(text)

            if not secret_scan.safe:
                return GovernanceCheckResult(
                    safe=False,
                    event_type="output_secret_detected",
                    severity="critical",
                    detected_categories=list(secret_scan.findings),
                )

        if not self.settings.output_moderation_enabled:
            return GovernanceCheckResult(safe=True)
        if not self.settings.openai_api_key:
            return GovernanceCheckResult(
                safe=not self.settings.moderation_fail_closed,
                event_type="moderation_unavailable",
                severity="high",
                detected_categories=["moderation_not_configured"],
            )

        try:
            moderation_result = await self.moderation.moderate(
                text=text,
                api_key=self.settings.openai_api_key,
            )
        except Exception:
            return GovernanceCheckResult(
                safe=not self.settings.moderation_fail_closed,
                event_type="moderation_unavailable",
                severity="high",
                detected_categories=["moderation_unavailable"],
            )

        if not moderation_result.flagged:
            return GovernanceCheckResult(safe=True)

        return GovernanceCheckResult(
            safe=False,
            event_type="moderation_output_blocked",
            severity="high",
            detected_categories=[
                finding.category
                for finding in moderation_result.findings
                if finding.flagged
            ],
        )
