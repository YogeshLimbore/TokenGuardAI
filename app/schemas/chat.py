from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator

from app.providers.registry import validate_provider
from app.schemas.budget import BudgetDecision
from app.schemas.governance import GovernanceDecision
from app.schemas.reliability import (
    ReliabilityDetails,
)
from app.schemas.routing import RoutingDecision


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        cleaned_value = value.strip()

        if not cleaned_value:
            raise ValueError("Message content cannot be empty.")

        return value


class ChatCompletionRequest(BaseModel):
    _stream_upstream: bool = PrivateAttr(default=False)
    provider: str = "ollama"

    _provider_validator = field_validator("provider")(validate_provider)

    credential_id: int | None = Field(
        default=None,
        ge=1,
    )

    model: str | None = Field(default=None, max_length=200)
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)

    temperature: float = Field(
        default=0.7,
        ge=0.0,
        le=2.0,
    )

    max_tokens: int | None = Field(
        default=None,
        ge=1,
        le=32_000,
    )

    use_cache: bool = True
    use_semantic_cache: bool = True

    auto_route: bool = False

    routing_preference: Literal[
        "cost",
        "balanced",
        "quality",
    ] = "balanced"

    maximum_request_cost_usd: float | None = Field(
        default=None,
        ge=0.0,
    )

    maximum_total_tokens: int | None = Field(
        default=None,
        ge=1,
        le=100_000,
    )

    allow_budget_downgrade: bool = True


class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class CostDetails(BaseModel):
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    pricing_found: bool


class OptimizationDetails(BaseModel):
    cache_enabled: bool
    cache_hit: bool
    cache_type: Literal[
        "none",
        "exact",
        "semantic",
    ]

    semantic_similarity_score: float | None = None

    tokens_avoided: int
    cost_avoided_usd: float

    original_latency_ms: float | None = None
    cache_lookup_latency_ms: float | None = None


class ChatCompletionResponse(BaseModel):
    request_id: str
    provider: str
    model: str
    content: str
    usage: TokenUsage
    cost: CostDetails
    latency_ms: float
    optimization: OptimizationDetails
    routing: RoutingDecision
    budget: BudgetDecision
    reliability: ReliabilityDetails
    governance: GovernanceDecision
