"""
HTTP security headers middleware.

These headers harden browser-facing responses against common web attacks.
They complement application-layer controls (JWT, RBAC, rate limits) and
are always applied — including on error responses.
"""

from __future__ import annotations

from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)

        response.headers.setdefault(
            "X-Content-Type-Options",
            "nosniff",
        )
        response.headers.setdefault(
            "X-Frame-Options",
            "DENY",
        )
        response.headers.setdefault(
            "Referrer-Policy",
            "strict-origin-when-cross-origin",
        )
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        response.headers.setdefault(
            "Cross-Origin-Opener-Policy",
            "same-origin",
        )
        response.headers.setdefault(
            "X-Permitted-Cross-Domain-Policies",
            "none",
        )

        # Only advertise HSTS when the request arrived over HTTPS
        # (typically after a reverse proxy has terminated TLS).
        if (
            request.url.scheme == "https"
            or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        ):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )

        return response
