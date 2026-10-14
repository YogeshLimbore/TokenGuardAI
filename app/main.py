import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import text

from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.credentials import router as credentials_router
from app.api.evaluation import router as evaluation_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.api.metrics import router as metrics_router
from app.api.organizations import router as organizations_router
from app.api.projects import router as projects_router
from app.api.provider_health import (
    router as provider_health_router,
)
from app.api.providers import router as providers_router
from app.api.security_audit import (
    router as security_audit_router,
)
from app.api.streaming import router as streaming_router
from app.cache.redis_client import (
    RedisUnavailableError,
    verify_redis_connection,
)
from app.core.config import get_settings
from app.database.connection import engine
from app.middleware.correlation_id import (
    CorrelationIDMiddleware,
)
from app.middleware.metrics import (
    PrometheusMetricsMiddleware,
)
from app.middleware.request_limits import RequestLimitsMiddleware
from app.middleware.security_headers import (
    SecurityHeadersMiddleware,
)
from app.observability.logging import (
    configure_logging,
)
from app.observability.tracing import (
    configure_tracing,
)

configure_logging()

settings = get_settings()

logger = logging.getLogger("tokenguard.startup")


@asynccontextmanager
async def lifespan(
    application: FastAPI,
) -> AsyncIterator[None]:
    # Database structure is managed by Alembic migrations, not
    # Base.metadata.create_all(). Startup only verifies that
    # PostgreSQL is reachable.
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    try:
        verify_redis_connection()
    except RedisUnavailableError as error:
        # Redis powers caching and rate limiting, both of which have
        # database-backed fallbacks. Log and continue in degraded
        # mode instead of blocking startup.
        logger.warning(
            "Redis is unavailable at startup: %s",
            error,
        )

    yield


_docs_url = "/docs" if settings.docs_enabled else None
_redoc_url = "/redoc" if settings.docs_enabled else None
_openapi_url = "/openapi.json" if settings.docs_enabled else None

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "An intelligent LLM gateway for tracking, optimizing, "
        "and controlling AI token usage."
    ),
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=_openapi_url,
)

# Middleware order: last added runs first on the request path.
app.add_middleware(RequestLimitsMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIDMiddleware)
app.add_middleware(PrometheusMetricsMiddleware)

if settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-TokenGuard-Key",
            "X-TokenGuard-Project-ID",
            "X-TokenGuard-Admin-Key",
            "X-Correlation-ID",
            "X-Request-ID",
        ],
        expose_headers=["X-Correlation-ID", "Retry-After"],
    )

if settings.allowed_hosts_list != ["*"]:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts_list,
    )

if settings.force_https_redirect:
    app.add_middleware(HTTPSRedirectMiddleware)

configure_tracing(
    app=app,
    engine=engine,
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(organizations_router)
app.include_router(chat_router)
app.include_router(analytics_router)
app.include_router(credentials_router)
app.include_router(evaluation_router)
app.include_router(projects_router)
app.include_router(providers_router)
app.include_router(metrics_router)
app.include_router(provider_health_router)
app.include_router(streaming_router)
app.include_router(jobs_router)
app.include_router(security_audit_router)


@app.get("/", tags=["Root"])
async def root() -> dict[str, str]:
    payload = {
        "message": f"{settings.app_name} is running.",
        "health": "/health",
        "auth": "/v1/auth/login",
        "organizations": "/v1/organizations",
        "chat": "/v1/chat/completions",
        "analytics": "/v1/analytics/summary",
        "credentials": "/v1/credentials",
        "projects": "/v1/projects",
        "semantic_evaluation": ("/v1/evaluation/semantic-cache"),
    }

    if settings.docs_enabled:
        payload["documentation"] = "/docs"

    return payload
