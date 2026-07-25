from pydantic import BaseModel


class ReliabilityDetails(BaseModel):
    correlation_id: str
    provider_attempts: int
    retry_count: int
    circuit_breaker_state: str
    provider_health_score: float | None
    fallback_used: bool
