"""
httpx transport doubles.

Providers construct their own ``httpx.AsyncClient``, so tests swap in a
subclass that routes every request through ``httpx.MockTransport``. No socket
is ever opened, and each provider's real request-building and response-parsing
code still runs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import httpx
import pytest


def install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """
    Route all httpx.AsyncClient traffic through *handler*.

    Returns the list that captured requests are appended to.
    """
    captured: list[httpx.Request] = []

    def capturing_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    class MockedAsyncClient(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs) -> None:
            kwargs["transport"] = httpx.MockTransport(capturing_handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockedAsyncClient)

    return captured


def json_responder(
    payload: dict,
    *,
    status_code: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def text_responder(
    body: str,
    *,
    status_code: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    return handler


def raising_responder(
    error: Exception,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


def stream_responder(
    lines: Iterable[str],
    *,
    status_code: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a streaming response that yields *lines* separated by newlines."""
    body = "\n".join(lines).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return handler
