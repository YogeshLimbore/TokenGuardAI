from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ProjectCreateRequest(BaseModel):
    name: str = Field(
        min_length=2,
        max_length=120,
    )

    description: str | None = Field(
        default=None,
        max_length=1000,
    )

    daily_token_budget: int = Field(
        default=100_000,
        ge=1,
    )

    daily_cost_budget_usd: float = Field(
        default=1.0,
        ge=0.0,
    )

    maximum_tokens_per_request: int = Field(
        default=15_000,
        ge=1,
    )

    maximum_cost_per_request_usd: float = Field(
        default=0.05,
        ge=0.0,
    )

    requests_per_minute: int = Field(
        default=30,
        ge=1,
        le=10_000,
    )

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = value.strip()

        if not cleaned:
            raise ValueError("Project name cannot be empty.")

        return cleaned


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=2,
        max_length=120,
    )

    description: str | None = Field(
        default=None,
        max_length=1000,
    )

    is_active: bool | None = None

    daily_token_budget: int | None = Field(
        default=None,
        ge=1,
    )

    daily_cost_budget_usd: float | None = Field(
        default=None,
        ge=0.0,
    )

    maximum_tokens_per_request: int | None = Field(
        default=None,
        ge=1,
    )

    maximum_cost_per_request_usd: float | None = Field(
        default=None,
        ge=0.0,
    )

    requests_per_minute: int | None = Field(
        default=None,
        ge=1,
        le=10_000,
    )


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: str | None
    is_active: bool

    daily_token_budget: int
    daily_cost_budget_usd: float
    maximum_tokens_per_request: int
    maximum_cost_per_request_usd: float
    requests_per_minute: int

    created_at: datetime
    updated_at: datetime


class ProjectAPIKeyCreateRequest(BaseModel):
    name: str = Field(
        default="Default",
        min_length=2,
        max_length=100,
    )

    expires_at: datetime | None = None


class ProjectAPIKeyCreateResponse(BaseModel):
    id: int
    project_id: int
    name: str

    api_key: str = Field(
        description=("Plaintext project key. This value is returned only once.")
    )

    masked_key: str
    is_active: bool
    expires_at: datetime | None
    created_at: datetime


class ProjectAPIKeyResponse(BaseModel):
    id: int
    project_id: int
    name: str
    masked_key: str
    is_active: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


class ProjectAPIKeyRevokeResponse(BaseModel):
    key_id: int
    revoked: bool
    message: str
