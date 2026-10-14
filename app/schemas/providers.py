from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class ProjectModelPriceRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    input_usd_per_1m_tokens: float = Field(ge=0, allow_inf_nan=False)
    output_usd_per_1m_tokens: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Model cannot be blank.")
        return value


class ProjectModelPriceResponse(BaseModel):
    id: int
    project_id: int
    provider: str
    model: str
    input_usd_per_1m_tokens: float
    output_usd_per_1m_tokens: float
    is_active: bool
    created_at: datetime
    updated_at: datetime
