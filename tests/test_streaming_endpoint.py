import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.streaming import _pump_with_heartbeat
from app.database.connection import Base, get_database_session
from app.database.models import ExactResponseCache, LLMRequestLog
from app.dependencies.admin_authentication import require_admin
from app.main import app
from app.providers.base import ProviderStreamEvent
from app.providers.ollama import OllamaProviderError


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    def override_get_database_session():
        database = TestingSessionLocal()

        try:
            yield database
        finally:
            database.close()

    app.dependency_overrides[get_database_session] = override_get_database_session
    app.dependency_overrides[require_admin] = lambda: True

    with TestClient(app) as test_client:
        yield test_client, TestingSessionLocal

    app.dependency_overrides.clear()


def _create_project_and_key(client: TestClient) -> str:
    create_response = client.post(
        "/v1/projects",
        json={"name": "Streaming Test Project"},
    )
    assert create_response.status_code == 201
    project_id = create_response.json()["id"]

    key_response = client.post(
        f"/v1/projects/{project_id}/api-keys",
        json={"name": "Streaming Test Key"},
    )
    assert key_response.status_code == 201

    return key_response.json()["api_key"]


def _make_fake_provider(events, *, delay_seconds: float = 0):
    async def fake_stream(**_kwargs):
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
        for event in events:
            yield event

    class FakeProvider:
        provider_name = "ollama"
        stream = staticmethod(fake_stream)

    return FakeProvider()


def _make_failing_provider(error: Exception, *, tokens_before_error=None):
    tokens_before_error = tokens_before_error or []

    async def fake_stream(**_kwargs):
        for token_content in tokens_before_error:
            yield ProviderStreamEvent(
                event_type="token",
                content=token_content,
                model="llama3.2",
            )
        raise error

    class FakeProvider:
        provider_name = "ollama"
        stream = staticmethod(fake_stream)

    return FakeProvider()


def _parse_sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    for block in text.split("\n\n"):
        block = block.strip()

        if not block or block.startswith(":"):
            continue

        event_name = None
        data_payload = None

        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_payload = json.loads(line[len("data:") :].strip())

        if event_name is not None:
            events.append((event_name, data_payload))

    return events


CHAT_REQUEST_BODY = {
    "provider": "ollama",
    "model": "llama3.2",
    "messages": [{"role": "user", "content": "Hello there"}],
    "temperature": 0.3,
    "use_cache": True,
    "use_semantic_cache": False,
    "auto_route": False,
}


def test_missing_api_key_returns_401(client):
    test_client, _ = client

    response = test_client.post(
        "/v1/chat/completions/stream",
        json=CHAT_REQUEST_BODY,
    )

    assert response.status_code == 401


def test_stream_event_ordering_and_usage_totals(client):
    test_client, SessionLocal = client
    api_key = _create_project_and_key(test_client)

    fake_provider = _make_fake_provider(
        [
            ProviderStreamEvent(
                event_type="token",
                content="Hello",
                model="llama3.2",
            ),
            ProviderStreamEvent(
                event_type="token",
                content=" world",
                model="llama3.2",
            ),
            ProviderStreamEvent(
                event_type="usage",
                input_tokens=5,
                output_tokens=2,
                total_tokens=7,
                model="llama3.2",
            ),
            ProviderStreamEvent(
                event_type="completed",
                model="llama3.2",
            ),
        ]
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=fake_provider,
    ):
        response = test_client.post(
            "/v1/chat/completions/stream",
            json=CHAT_REQUEST_BODY,
            headers={"X-TokenGuard-Key": api_key},
        )

    assert response.status_code == 200

    events = _parse_sse_events(response.text)
    event_names = [name for name, _ in events]

    assert event_names[0] == "metadata"
    assert event_names[-1] == "completed"
    assert event_names.count("token") == 1

    token_events = [data for name, data in events if name == "token"]
    assert [event["content"] for event in token_events] == ["Hello world"]

    usage_event = next(data for name, data in events if name == "usage")
    assert usage_event["input_tokens"] == 5
    assert usage_event["output_tokens"] == 2
    assert usage_event["total_tokens"] == 7

    # The request log for the completed stream is persisted.
    with SessionLocal() as database:
        log = database.scalar(select(LLMRequestLog))
        assert log is not None
        assert log.status == "completed"
        assert log.response is None


def test_provider_failure_before_first_token_emits_error_event(
    client,
):
    test_client, SessionLocal = client
    api_key = _create_project_and_key(test_client)

    failing_provider = _make_failing_provider(
        OllamaProviderError("Could not connect to Ollama.")
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=failing_provider,
    ):
        response = test_client.post(
            "/v1/chat/completions/stream",
            json=CHAT_REQUEST_BODY,
            headers={"X-TokenGuard-Key": api_key},
        )

    assert response.status_code == 200

    events = _parse_sse_events(response.text)
    event_names = [name for name, _ in events]

    assert "error" in event_names
    assert "token" not in event_names
    assert "completed" not in event_names

    error_event = next(data for name, data in events if name == "error")
    # The raw provider error message is not required to be
    # forwarded verbatim; only a safe, generic error type/message.
    assert error_event["type"] == "error"
    assert isinstance(error_event["error_type"], str)

    with SessionLocal() as database:
        log = database.scalar(select(LLMRequestLog))
        assert log is not None
        assert log.status == "failed"


def test_partial_response_is_not_cached(client):
    test_client, SessionLocal = client
    api_key = _create_project_and_key(test_client)

    # The provider sends one upstream token, then fails. TokenGuard buffers
    # the stream until output policy checks pass, so partial content is never
    # exposed to the client or written to the cache.
    failing_provider = _make_failing_provider(
        OllamaProviderError("Ollama connection dropped."),
        tokens_before_error=["Partial"],
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=failing_provider,
    ):
        response = test_client.post(
            "/v1/chat/completions/stream",
            json=CHAT_REQUEST_BODY,
            headers={"X-TokenGuard-Key": api_key},
        )

    assert response.status_code == 200

    events = _parse_sse_events(response.text)
    event_names = [name for name, _ in events]

    assert event_names.count("token") == 0
    assert "error" in event_names
    assert "completed" not in event_names

    with SessionLocal() as database:
        assert database.scalar(select(ExactResponseCache)) is None

        log = database.scalar(select(LLMRequestLog))
        assert log is not None
        assert log.status == "failed"


def test_client_disconnect_stops_generation(client, monkeypatch):
    test_client, SessionLocal = client
    api_key = _create_project_and_key(test_client)

    fake_provider = _make_fake_provider(
        [
            ProviderStreamEvent(
                event_type="token",
                content="one",
                model="llama3.2",
            ),
            ProviderStreamEvent(
                event_type="token",
                content="two",
                model="llama3.2",
            ),
            ProviderStreamEvent(
                event_type="token",
                content="three",
                model="llama3.2",
            ),
            ProviderStreamEvent(
                event_type="usage",
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                model="llama3.2",
            ),
            ProviderStreamEvent(
                event_type="completed",
                model="llama3.2",
            ),
        ],
        delay_seconds=2,
    )

    call_count = {"value": 0}

    async def fake_is_disconnected(self) -> bool:
        call_count["value"] += 1
        # Report "connected" for the first token, then
        # "disconnected" for everything afterwards.
        return call_count["value"] > 0

    monkeypatch.setattr(
        "starlette.requests.Request.is_disconnected",
        fake_is_disconnected,
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=fake_provider,
    ):
        response = test_client.post(
            "/v1/chat/completions/stream",
            json=CHAT_REQUEST_BODY,
            headers={"X-TokenGuard-Key": api_key},
        )

    assert response.status_code == 200

    events = _parse_sse_events(response.text)
    event_names = [name for name, _ in events]

    # Generation stopped after the disconnect was detected, well
    # before all three tokens or the completed event were sent.
    assert event_names.count("token") < 3
    assert "completed" not in event_names

    with SessionLocal() as database:
        assert database.scalar(select(ExactResponseCache)) is None

        log = database.scalar(select(LLMRequestLog))
        assert log is not None
        assert log.status == "cancelled"


@pytest.mark.anyio
async def test_pump_with_heartbeat_emits_heartbeat_on_timeout():
    async def slow_stream():
        await asyncio.sleep(0.2)
        yield "real-event"

    kinds = []

    async for kind, _value in _pump_with_heartbeat(
        slow_stream(),
        heartbeat_seconds=0.05,
    ):
        kinds.append(kind)

        if kind == "event":
            break

    assert kinds[0] == "heartbeat"
    assert kinds[-1] == "event"


@pytest.mark.anyio
async def test_pump_with_heartbeat_passes_through_fast_events():
    async def fast_stream():
        yield "a"
        yield "b"

    results = [
        value
        async for kind, value in _pump_with_heartbeat(
            fast_stream(),
            heartbeat_seconds=5,
        )
    ]

    assert results == ["a", "b"]
