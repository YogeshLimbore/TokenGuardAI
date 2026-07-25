from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_JWT_PLACEHOLDERS = {
    "change_me_replace_with_long_random_secret_at_least_48_chars",
    "replace_with_a_long_random_secret",
}


class Settings(BaseSettings):
    app_name: str = "TokenGuard AI"
    app_version: str = "1.5.0"
    environment: str = "development"
    debug: bool = True

    log_level: str = "INFO"
    log_format: str = "json"

    enable_prometheus_metrics: bool = True
    metrics_path: str = "/metrics"

    enable_opentelemetry: bool = True
    otel_service_name: str = "tokenguard-api"
    otel_exporter_otlp_endpoint: str = "http://localhost:4318"
    otel_exporter_otlp_traces_endpoint: str = "http://localhost:4318/v1/traces"

    provider_retry_enabled: bool = True
    provider_max_attempts: int = 3
    provider_retry_min_wait_seconds: float = 1.0
    provider_retry_max_wait_seconds: float = 8.0

    circuit_breaker_enabled: bool = True
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_timeout_seconds: int = 30

    provider_health_window_minutes: int = 15
    provider_unhealthy_score_threshold: float = 40.0

    database_url: str = (
        "postgresql+psycopg://tokenguard:tokenguard_password@localhost:5432/tokenguard"
    )
    test_database_url: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    test_redis_url: str | None = None

    redis_cache_ttl_seconds: int = 86_400
    redis_rate_limit_prefix: str = "tokenguard:rate-limit"
    redis_exact_cache_prefix: str = "tokenguard:exact-cache"

    use_redis_exact_cache: bool = True
    use_redis_rate_limiting: bool = True

    ollama_base_url: str = "http://localhost:11434"
    ollama_default_model: str = "llama3.2"

    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_default_model: str = "gpt-4.1-mini"

    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_default_model: str = "openai/gpt-4.1-mini"

    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_default_model: str = "claude-haiku-4-5-20251001"
    anthropic_version: str = "2023-06-01"

    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_default_model: str = "openai/gpt-oss-20b"

    model_pricing_file: str = "config/model_pricing.json"

    token_guard_master_key: str | None = None
    credential_test_timeout_seconds: float = 20.0

    semantic_cache_enabled: bool = True
    semantic_cache_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    semantic_cache_threshold: float = 0.85
    semantic_cache_max_candidates: int = 500

    request_timeout_seconds: float = 120.0

    routing_policy_file: str = "config/routing_policy.json"
    auto_routing_enabled: bool = True

    budget_policy_file: str = "config/budget_policy.json"
    budget_enforcement_enabled: bool = True
    rate_limiting_enabled: bool = True

    project_api_key_prefix: str = "tg_live_"
    project_api_key_bytes: int = 32
    require_project_authentication: bool = True

    admin_api_key_hash: str | None = None
    admin_api_key_prefix: str = "tg_admin_"
    require_admin_authentication: bool = True

    token_guard_api_base_url: str = "http://127.0.0.1:8000"

    celery_broker_url: str = "redis://localhost:6379/2"
    celery_result_backend: str = "redis://localhost:6379/3"

    celery_task_time_limit_seconds: int = 1800
    celery_task_soft_time_limit_seconds: int = 1700
    celery_result_expires_seconds: int = 86_400

    enable_streaming: bool = True
    stream_heartbeat_seconds: int = 15

    job_result_retention_days: int = 7
    request_log_retention_days: int = 90
    cache_cleanup_interval_hours: int = 6
    usage_aggregation_interval_minutes: int = 15

    policy_enforcement_enabled: bool = True
    policy_file: str = "config/governance_policy.json"

    prompt_injection_detection_enabled: bool = True
    prompt_injection_block_threshold: int = 80
    prompt_injection_review_threshold: int = 45

    pii_detection_enabled: bool = True
    pii_default_action: str = "redact"
    pii_score_threshold: float = 0.6

    input_moderation_enabled: bool = False
    output_moderation_enabled: bool = False
    openai_moderation_model: str = "omni-moderation-latest"

    output_secret_scan_enabled: bool = True
    store_raw_security_content: bool = False

    jwt_secret_key: str = "change_me_replace_with_long_random_secret_at_least_48_chars"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "tokenguard"
    jwt_audience: str = "tokenguard-api"

    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    authentication_enabled: bool = True
    allow_public_registration: bool = False
    require_email_verification: bool = False

    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None

    auth_login_rate_limit_per_minute: int = 10
    auth_refresh_rate_limit_per_minute: int = 20

    # Transport / HTTP hardening
    cors_allowed_origins: str = "http://localhost:8501,http://127.0.0.1:8501"
    allowed_hosts: str = "*"
    enable_docs: bool | None = None
    force_https_redirect: bool = False

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(
        cls,
        value: str,
    ) -> str:
        if len(value) < 48:
            raise ValueError("JWT_SECRET_KEY must contain at least 48 characters.")
        return value

    @model_validator(mode="after")
    def enforce_production_guards(self) -> "Settings":
        """
        Refuse to start with unsafe defaults when ENVIRONMENT=production.
        """
        if self.environment.lower() != "production":
            return self

        problems: list[str] = []

        if self.debug:
            problems.append("DEBUG must be false in production.")

        if self.jwt_secret_key.lower() in _INSECURE_JWT_PLACEHOLDERS:
            problems.append(
                "JWT_SECRET_KEY must be replaced with a unique production secret."
            )

        if not self.token_guard_master_key:
            problems.append("TOKEN_GUARD_MASTER_KEY is required in production.")

        if self.bootstrap_admin_password:
            problems.append(
                "BOOTSTRAP_ADMIN_PASSWORD must not remain set in production."
            )

        if self.allow_public_registration:
            problems.append("ALLOW_PUBLIC_REGISTRATION must be false in production.")

        if problems:
            raise ValueError(
                "Production configuration is unsafe: " + " ".join(problems)
            )

        return self

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_allowed_origins.strip():
            return []
        return [
            origin.strip()
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]

    @property
    def allowed_hosts_list(self) -> list[str]:
        if not self.allowed_hosts.strip() or self.allowed_hosts.strip() == "*":
            return ["*"]
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def docs_enabled(self) -> bool:
        if self.enable_docs is not None:
            return self.enable_docs
        return self.environment.lower() != "production"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
