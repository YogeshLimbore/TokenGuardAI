from typing import Literal

from pydantic import BaseModel

RoutingTier = Literal[
    "manual",
    "simple",
    "balanced",
    "advanced",
]


class FallbackDetails(BaseModel):
    fallback_used: bool
    attempts_made: int
    final_credential_id: int | None = None
    credential_source: str | None = None


class RoutingDecision(BaseModel):
    automatic_routing_used: bool
    tier: RoutingTier
    provider: str
    model: str
    complexity_score: int
    confidence: float
    reasons: list[str]
    original_provider: str | None = None
    original_model: str | None = None
    fallback: FallbackDetails | None = None
