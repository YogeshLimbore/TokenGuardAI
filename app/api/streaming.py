"""Safety-buffered SSE using the shared chat pipeline."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.chat import create_chat_completion
from app.core.config import get_settings
from app.database.connection import get_database_session
from app.database.models import Project
from app.dependencies.authentication import get_current_project
from app.schemas.chat import ChatCompletionRequest
from app.services.sse_service import SSEService

router = APIRouter(prefix="/v1/chat", tags=["Chat"])
settings = get_settings()


async def _pump_with_heartbeat(
    stream: AsyncIterator[Any],
    *,
    heartbeat_seconds: float,
) -> AsyncIterator[tuple[str, Any]]:
    """
    Wrap a provider event stream so that a heartbeat marker is
    produced whenever no real event arrives within
    ``heartbeat_seconds``. This keeps the SSE connection alive
    through proxies that close idle connections.
    """

    aiter = stream.__aiter__()

    # NOTE: We deliberately do not use ``asyncio.wait_for`` around
    # ``aiter.__anext__()`` here. ``wait_for`` cancels its inner
    # coroutine on timeout, and cancelling an in-flight
    # ``__anext__()`` call corrupts the underlying async generator
    # (the cancellation is raised inside the generator body, which
    # closes it permanently). That would mean a single heartbeat
    # tick silently ends the provider stream forever. Instead, keep
    # the same pending task alive across timeout cycles and only
    # retrieve/replace it once it actually completes.
    pending = asyncio.ensure_future(aiter.__anext__())

    try:
        while True:
            done, _ = await asyncio.wait(
                {pending},
                timeout=heartbeat_seconds,
            )

            if pending not in done:
                yield "heartbeat", None
                continue

            try:
                event = pending.result()
            except StopAsyncIteration:
                return

            yield "event", event

            pending = asyncio.ensure_future(aiter.__anext__())

    finally:
        if not pending.done():
            pending.cancel()


@router.post("/completions/stream", response_class=StreamingResponse)
async def stream_chat_completion(
    request: ChatCompletionRequest,
    http_request: Request,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> StreamingResponse:
    if not settings.enable_streaming:
        raise HTTPException(status_code=503, detail="Streaming is disabled.")
    request._stream_upstream = True

    async def events() -> AsyncIterator[str]:
        task = asyncio.create_task(create_chat_completion(request, project, database))
        try:
            yield SSEService.encode(
                event="metadata",
                data={"type": "metadata", "output_mode": "safety_buffered"},
            )
            while not task.done():
                done, _ = await asyncio.wait(
                    {task}, timeout=min(settings.stream_heartbeat_seconds, 1.0)
                )
                if await http_request.is_disconnected():
                    return
                if not done:
                    yield SSEService.heartbeat()
            response = await task
            # No output is delivered until the complete response passes policy.
            yield SSEService.encode(
                event="token", data={"type": "token", "content": response.content}
            )
            yield SSEService.encode(
                event="usage",
                data={
                    "type": "usage",
                    **response.usage.model_dump(),
                    "cost": response.cost.model_dump(),
                },
            )
            yield SSEService.encode(
                event="completed",
                data={"type": "completed", **response.model_dump(exclude={"content"})},
            )
        except HTTPException as error:
            yield SSEService.encode(
                event="error",
                data={
                    "type": "error",
                    "error_type": "http_error",
                    "status_code": error.status_code,
                    "detail": error.detail,
                },
            )
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError, HTTPException):
                await task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
