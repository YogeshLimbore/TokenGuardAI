from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class UsageReservation(Base):
    """Durable per-attempt admission; uncertain calls retain their reservation."""

    __tablename__ = "usage_reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(100), index=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    total_tokens: Mapped[int] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class User(Base):
    """
    Represents a human user of the TokenGuard platform.

    Passwords are hashed with Argon2 via pwdlib. The plaintext
    password must never be stored or logged.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    email: Mapped[str] = mapped_column(
        String(320),
        unique=True,
        index=True,
        nullable=False,
    )

    display_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    is_email_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    failed_login_attempts: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    session_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class Organization(Base):
    """
    Groups users under a shared billing and access boundary.
    """

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
    )

    slug: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class OrganizationMembership(Base):
    """
    Links a user to an organization with a specific role.
    """

    __tablename__ = "organization_memberships"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    organization_id: Mapped[int] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )

    role: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    invited_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_membership",
        ),
        Index(
            "ix_membership_org_role",
            "organization_id",
            "role",
        ),
    )


class RefreshToken(Base):
    """
    Stores hashed refresh tokens.

    Only the SHA-256 hash of the plaintext token is persisted.
    Refresh tokens are single-use; each rotation revokes the old
    record and inserts a new one.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    token_id: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )

    token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    replaced_by_token_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    user_agent_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    ip_address_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class IdentityAuditEvent(Base):
    """
    Records identity-related events such as login attempts,
    token issuance, and membership changes.
    """

    __tablename__ = "identity_audit_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
        ),
        index=True,
        nullable=True,
    )

    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="SET NULL",
        ),
        index=True,
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )

    succeeded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    correlation_id: Mapped[str | None] = mapped_column(
        String(100),
        index=True,
        nullable=True,
    )

    details_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
        nullable=False,
    )


class LLMRequestLog(Base):
    """
    Stores information about every request processed by TokenGuard.
    """

    __tablename__ = "llm_request_logs"
    tokens_avoided: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_avoided_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    request_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    response: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    total_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    estimated_cost_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    latency_ms: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="pending",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    __table_args__ = (
        Index(
            "ix_llm_request_project_created",
            "project_id",
            "created_at",
        ),
        Index(
            "ix_llm_request_project_provider",
            "project_id",
            "provider",
        ),
        Index(
            "ix_llm_request_project_status",
            "project_id",
            "status",
        ),
    )


class ExactResponseCache(Base):
    """
    Stores successful LLM responses for exact request matching.
    """

    __tablename__ = "exact_response_cache"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    cache_key: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    response: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    original_latency_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    hit_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )


class ProjectModelPrice(Base):
    """Project-controlled price snapshot used for budget admission."""

    __tablename__ = "project_model_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    input_usd_per_1m_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    output_usd_per_1m_tokens: Mapped[float] = mapped_column(Float, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id", "provider", "model", name="uq_project_provider_model_price"
        ),
    )


class ProviderCredential(Base):
    """
    Stores encrypted provider credentials.

    The plaintext API key must never be written to this table.
    """

    __tablename__ = "provider_credentials"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        index=True,
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    encrypted_api_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    masked_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    is_default: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    last_test_succeeded: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
    )

    last_test_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_provider_credential_project_provider",
            "project_id",
            "provider",
        ),
        Index(
            "ix_provider_credential_default",
            "project_id",
            "provider",
            "is_default",
        ),
    )


class CredentialAuditLog(Base):
    """
    Records credential-management events without storing secret values.
    """

    __tablename__ = "credential_audit_logs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "provider_credentials.id",
            ondelete="SET NULL",
        ),
        index=True,
        nullable=True,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    succeeded: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    details: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class SemanticResponseCache(Base):
    """
    Stores embeddings and responses for semantic cache matching.

    Embeddings are stored as JSON text for the SQLite development version.
    A vector database will replace this approach in production.
    """

    __tablename__ = "semantic_response_cache"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        index=True,
        nullable=False,
    )

    credential_id: Mapped[int | None] = mapped_column(
        Integer,
        index=True,
        nullable=True,
    )

    model: Mapped[str] = mapped_column(
        String(150),
        index=True,
        nullable=False,
    )

    system_fingerprint: Mapped[str] = mapped_column(
        String(64),
        index=True,
        nullable=False,
    )

    user_prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    embedding_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    response: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    temperature: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    max_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    original_latency_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    original_cost_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    hit_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    last_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ModelRoutingLog(Base):
    """
    Stores each automatic or manual model-routing decision.
    """

    __tablename__ = "model_routing_logs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    request_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
    )

    automatic_routing_used: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    routing_preference: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    routing_tier: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    original_provider: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    original_model: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )

    selected_provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    selected_model: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    complexity_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    confidence: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    reasons_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class ProviderAttemptLog(Base):
    """
    Records every provider and credential attempt made for a chat request.
    """

    __tablename__ = "provider_attempt_logs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    request_id: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )

    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    model: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    credential_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    credential_source: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    succeeded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    error_type: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    latency_ms: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class BudgetDecisionLog(Base):
    """
    Stores every budget decision made before a provider call.
    """

    __tablename__ = "budget_decision_logs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    request_id: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    model: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    estimated_input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    requested_output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    estimated_total_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    estimated_cost_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    daily_tokens_before_request: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    daily_cost_before_request_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    original_provider: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    original_model: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class RateLimitEvent(Base):
    """
    Stores request timestamps used for database-backed rate limiting.
    """

    __tablename__ = "rate_limit_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    scope: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        index=True,
        nullable=False,
    )

    request_id: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
        nullable=False,
    )


class ProviderHealthEvent(Base):
    """
    Records the outcome of every provider attempt for health scoring.
    """

    __tablename__ = "provider_health_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        index=True,
        nullable=False,
    )

    model: Mapped[str] = mapped_column(
        String(150),
        index=True,
        nullable=False,
    )

    succeeded: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    latency_ms: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    error_type: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    retry_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
        nullable=False,
    )


class Project(Base):
    """
    Represents an isolated TokenGuard tenant or application project.
    """

    __tablename__ = "projects"

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_project_org_name"),
        Index(
            "uq_project_legacy_name",
            "name",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
            sqlite_where=text("organization_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(120),
        index=True,
        nullable=False,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    daily_token_budget: Mapped[int] = mapped_column(
        Integer,
        default=100_000,
        nullable=False,
    )

    daily_cost_budget_usd: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        nullable=False,
    )

    maximum_tokens_per_request: Mapped[int] = mapped_column(
        Integer,
        default=15_000,
        nullable=False,
    )

    maximum_cost_per_request_usd: Mapped[float] = mapped_column(
        Float,
        default=0.05,
        nullable=False,
    )

    requests_per_minute: Mapped[int] = mapped_column(
        Integer,
        default=30,
        nullable=False,
    )

    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "organizations.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class BackgroundJob(Base):
    """
    Tracks the lifecycle and result of an asynchronous Celery task.
    """

    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    job_id: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        index=True,
        nullable=False,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )

    job_type: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        index=True,
        nullable=False,
        default="queued",
    )

    progress_percent: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    input_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    result_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ProjectUsageDaily(Base):
    """
    Stores per-project usage aggregated once per calendar day.
    """

    __tablename__ = "project_usage_daily"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )

    usage_date: Mapped[date] = mapped_column(
        Date,
        index=True,
        nullable=False,
    )

    request_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    successful_requests: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    failed_requests: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    cache_hits: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    total_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    estimated_cost_usd: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "usage_date",
            name="uq_project_usage_daily",
        ),
    )


class ProjectAPIKey(Base):
    """
    Stores hashed TokenGuard project API keys.

    The plaintext key is shown only once when created.
    """

    __tablename__ = "project_api_keys"
    scopes_json: Mapped[str] = mapped_column(Text, default='["chat"]', nullable=False)

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        index=True,
        nullable=False,
    )

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    key_prefix: Mapped[str] = mapped_column(
        String(24),
        index=True,
        nullable=False,
    )

    key_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=False,
    )

    masked_key: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "name",
            name="uq_project_api_key_name",
        ),
    )


class ProjectAuditLog(Base):
    __tablename__ = "project_audit_logs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="SET NULL",
        ),
        index=True,
        nullable=True,
    )

    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    succeeded: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    details: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )


class SecurityAuditEvent(Base):
    __tablename__ = "security_audit_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="SET NULL",
        ),
        index=True,
        nullable=True,
    )

    request_id: Mapped[str | None] = mapped_column(
        String(100),
        index=True,
        nullable=True,
    )

    correlation_id: Mapped[str | None] = mapped_column(
        String(100),
        index=True,
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )

    severity: Mapped[str] = mapped_column(
        String(20),
        index=True,
        nullable=False,
    )

    action: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    provider: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )

    model: Mapped[str | None] = mapped_column(
        String(150),
        nullable=True,
    )

    risk_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    detected_categories_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    sanitized_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    metadata_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
        nullable=False,
    )


class ProjectGovernancePolicy(Base):
    __tablename__ = "project_governance_policies"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    project_id: Mapped[int] = mapped_column(
        ForeignKey(
            "projects.id",
            ondelete="CASCADE",
        ),
        unique=True,
        index=True,
        nullable=False,
    )

    policy_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
