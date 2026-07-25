from pydantic import BaseModel


class ProviderHealthResponse(BaseModel):
    provider: str
    model: str
    score: float
    success_rate_percent: float
    average_latency_ms: float
    attempts: int
    failures: int
    healthy: bool
    circuit_state: str
