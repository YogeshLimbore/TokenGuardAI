from typing import Literal

from pydantic import BaseModel

GovernanceAction = Literal[
    "allow",
    "redact",
    "review",
    "block",
]


class PromptInjectionFinding(BaseModel):
    category: str
    description: str
    score: int
    matched_pattern: str | None = None


class SensitiveDataFinding(BaseModel):
    entity_type: str
    score: float
    start: int
    end: int
    action: GovernanceAction


class ModerationFinding(BaseModel):
    category: str
    flagged: bool
    score: float | None = None


class GovernanceDecision(BaseModel):
    allowed: bool
    action: GovernanceAction
    original_provider: str
    original_model: str

    sanitized: bool = False
    sanitized_messages_count: int = 0

    prompt_injection_score: int = 0
    prompt_injection_findings: list[PromptInjectionFinding] = []

    sensitive_data_findings: list[SensitiveDataFinding] = []

    moderation_findings: list[ModerationFinding] = []

    reasons: list[str] = []


class ProjectGovernancePolicyRequest(BaseModel):
    policy: dict[str, object]


class ProjectGovernancePolicyResponse(BaseModel):
    project_id: int
    is_active: bool
    policy: dict[str, object]
