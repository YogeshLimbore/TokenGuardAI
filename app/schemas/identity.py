"""
Pydantic schemas for identity, authentication, and organization management.
"""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
)

# ---------------------------------------------------------------------------
# Role type
# ---------------------------------------------------------------------------

OrganizationRole = Literal[
    "owner",
    "admin",
    "developer",
    "analyst",
    "viewer",
]

# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class UserCreateRequest(BaseModel):
    email: EmailStr

    display_name: str = Field(
        min_length=2,
        max_length=120,
    )

    password: str = Field(
        min_length=12,
        max_length=256,
    )


class UserProvisionRequest(UserCreateRequest):
    is_platform_admin: bool = False
    is_email_verified: bool = True


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    display_name: str
    is_active: bool
    is_email_verified: bool
    is_platform_admin: bool
    last_login_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str = Field(min_length=1, max_length=512)
    token_type: Literal["bearer"] = "bearer"
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class SessionResponse(BaseModel):
    token_id: str
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Organization
# ---------------------------------------------------------------------------


class OrganizationCreateRequest(BaseModel):
    name: str = Field(
        min_length=2,
        max_length=160,
    )

    slug: str = Field(
        min_length=2,
        max_length=100,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )


class OrganizationUpdateRequest(BaseModel):
    name: str | None = Field(
        default=None,
        min_length=2,
        max_length=160,
    )


class OrganizationResponse(BaseModel):
    id: int
    name: str
    slug: str
    is_active: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


class MembershipCreateRequest(BaseModel):
    email: EmailStr
    role: OrganizationRole


class MembershipUpdateRequest(BaseModel):
    role: OrganizationRole | None = None
    is_active: bool | None = None


class MembershipResponse(BaseModel):
    id: int
    organization_id: int
    user_id: int
    email: EmailStr
    display_name: str
    role: OrganizationRole
    is_active: bool
    joined_at: datetime
