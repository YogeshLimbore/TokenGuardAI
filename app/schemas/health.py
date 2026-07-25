from typing import Literal

from pydantic import BaseModel


class DependencyHealth(BaseModel):
    status: Literal["healthy", "unhealthy"]
    message: str | None = None


class DetailedHealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    application: str
    version: str
    database: DependencyHealth
    redis: DependencyHealth
