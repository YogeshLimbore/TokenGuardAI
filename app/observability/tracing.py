from __future__ import annotations

import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.instrumentation.fastapi import (
    FastAPIInstrumentor,
)
from opentelemetry.instrumentation.httpx import (
    HTTPXClientInstrumentor,
)
from opentelemetry.instrumentation.redis import (
    RedisInstrumentor,
)
from opentelemetry.instrumentation.sqlalchemy import (
    SQLAlchemyInstrumentor,
)
from opentelemetry.sdk.resources import (
    SERVICE_NAME,
    Resource,
)
from opentelemetry.sdk.trace import (
    TracerProvider,
)
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
)
from sqlalchemy.engine import Engine

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# `uvicorn --reload` (and pytest re-importing app.main across test
# modules) can execute this module more than once in the same
# process. Instrumentors are not idempotent, so guard with a flag.
_tracing_configured = False


def configure_tracing(
    *,
    app: FastAPI,
    engine: Engine,
) -> None:
    global _tracing_configured

    settings = get_settings()

    if not settings.enable_opentelemetry:
        logger.info("OpenTelemetry tracing disabled")
        return

    if _tracing_configured:
        logger.info(
            "OpenTelemetry tracing already configured; skipping re-initialization"
        )
        return

    resource = Resource.create(
        {
            SERVICE_NAME: (settings.otel_service_name),
            "service.version": (settings.app_version),
            "deployment.environment": (settings.environment),
        }
    )

    provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter(endpoint=(settings.otel_exporter_otlp_traces_endpoint))

    provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()

    SQLAlchemyInstrumentor().instrument(engine=engine)

    _tracing_configured = True

    logger.info("OpenTelemetry tracing configured")
