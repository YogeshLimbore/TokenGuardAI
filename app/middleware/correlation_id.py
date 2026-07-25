from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.responses import Response

from app.observability.context import (
    reset_correlation_id,
    set_correlation_id,
)

CORRELATION_HEADER = "X-Correlation-ID"


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        supplied_id = request.headers.get(CORRELATION_HEADER)

        correlation_id = supplied_id.strip() if supplied_id else str(uuid4())

        token = set_correlation_id(correlation_id)

        request.state.correlation_id = correlation_id

        try:
            response = await call_next(request)
        finally:
            reset_correlation_id(token)

        response.headers[CORRELATION_HEADER] = correlation_id

        return response
