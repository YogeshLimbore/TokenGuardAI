from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

SupportedProvider = Literal[
    "openrouter",
    "openai",
    "anthropic",
    "gemini",
    "groq",
]


class CredentialCreateRequest(BaseModel):
    provider: SupportedProvider
    name: str = Field(
        min_length=2,
        max_length=100,
    )
    api_key: SecretStr
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned_value = value.strip()

        if not cleaned_value:
            raise ValueError("Credential name cannot be empty.")

        return cleaned_value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(
        cls,
        value: SecretStr,
    ) -> SecretStr:
        if len(value.get_secret_value().strip()) < 8:
            raise ValueError("API key appears to be too short.")

        return value


class CredentialUpdateRequest(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=2,
        max_length=100,
    )
    is_active: bool | None = None
    is_default: bool | None = None


class CredentialRotateRequest(BaseModel):
    api_key: SecretStr

    @field_validator("api_key")
    @classmethod
    def validate_api_key(
        cls,
        value: SecretStr,
    ) -> SecretStr:
        if len(value.get_secret_value().strip()) < 8:
            raise ValueError("API key appears to be too short.")

        return value


class CredentialResponse(BaseModel):
    id: int
    provider: str
    name: str
    masked_key: str
    is_active: bool
    is_default: bool
    last_test_succeeded: bool | None
    last_test_message: str | None
    last_tested_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CredentialTestResponse(BaseModel):
    credential_id: int
    provider: str
    valid: bool
    message: str
    tested_at: datetime


class CredentialDeleteResponse(BaseModel):
    credential_id: int
    deleted: bool
    message: str


class CredentialAuditResponse(BaseModel):
    id: int
    credential_id: int | None
    provider: str
    action: str
    succeeded: bool
    details: str | None
    created_at: datetime
