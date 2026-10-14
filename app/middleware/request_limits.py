"""Bound request bodies before JSON parsing/NLP, including chunked uploads."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import get_settings


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body = event.get("body", b"")
            size += len(body)
            if size > get_settings().maximum_request_bytes:
                await JSONResponse(
                    {"detail": "Request body is too large."}, status_code=413
                )(scope, receive, send)
                return
            chunks.append(body)
            if not event.get("more_body", False):
                break
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, bounded_receive, send)
