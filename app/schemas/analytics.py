from datetime import datetime

from pydantic import BaseModel, Field


class RequestStatistics(BaseModel):
    total_requests: int
    successful_requests: int
    failed_requests: int
    generated_requests: int
    cache_hits: int
    cache_hit_rate_percent: float


class TokenStatistics(BaseModel):
    actual_tokens_consumed: int
    tokens_avoided: int
    total_potential_tokens: int
    token_saving_percent: float


class LatencyStatistics(BaseModel):
    average_generated_latency_ms: float
    average_cache_latency_ms: float
    estimated_time_saved_ms: float


class CacheStatistics(BaseModel):
    unique_cached_requests: int
    total_cache_hits: int
    most_used_cache_entry_hits: int


class AnalyticsSummaryResponse(BaseModel):
    requests: RequestStatistics
    tokens: TokenStatistics
    latency: LatencyStatistics
    cache: CacheStatistics
    estimated_cost_saved_usd: float = Field(
        description=(
            "Estimated provider cost avoided by exact and semantic cache hits."
        )
    )


class ProjectIdentity(BaseModel):
    id: int
    name: str


class ProviderUsageStatistics(BaseModel):
    provider: str
    request_count: int
    generated_requests: int
    exact_cache_hits: int
    semantic_cache_hits: int
    failed_requests: int
    total_tokens: int
    total_cost_usd: float
    average_latency_ms: float


class ModelUsageStatistics(BaseModel):
    provider: str
    model: str
    request_count: int
    total_tokens: int
    total_cost_usd: float


class DailyUsagePoint(BaseModel):
    date: str
    requests: int
    generated_requests: int
    exact_cache_hits: int
    semantic_cache_hits: int
    failed_requests: int
    actual_tokens: int
    cost_usd: float


class RecentRequestItem(BaseModel):
    request_id: str
    provider: str
    model: str
    status: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    latency_ms: float
    created_at: datetime
    error_message: str | None


class ProjectAnalyticsResponse(BaseModel):
    project: ProjectIdentity
    summary: AnalyticsSummaryResponse
    provider_usage: list[ProviderUsageStatistics]
    model_usage: list[ModelUsageStatistics]
    daily_usage: list[DailyUsagePoint]
    recent_requests: list[RecentRequestItem]
