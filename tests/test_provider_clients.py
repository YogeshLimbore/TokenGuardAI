"""
Provider HTTP clients: request shaping, response parsing, error mapping.

Every provider talks to a mock transport, so these tests never reach
OpenRouter, OpenAI, Anthropic, Groq, or a local Ollama instance. They exist
because a provider that mis-parses usage data silently corrupts cost
accounting, and one that leaks a raw HTTP error leaks credentials.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.anthropic import (
    AnthropicProvider,
    AnthropicProviderError,
)
from app.providers.base import BaseLLMProvider
from app.providers.factory import (
    ProviderFactory,
    UnsupportedProviderError,
)
from app.providers.groq import GroqProvider, GroqProviderError
from app.providers.ollama import OllamaProvider, OllamaProviderError
from app.providers.openai import OpenAIProvider, OpenAIProviderError
from app.providers.openrouter import (
    OpenRouterProvider,
    OpenRouterProviderError,
)
from app.schemas.chat import ChatMessage
from tests.fakes.http import (
    install_mock_transport,
    json_responder,
    raising_responder,
    stream_responder,
    text_responder,
)

pytestmark = pytest.mark.unit

MESSAGES = [ChatMessage(role="user", content="What is 2 + 2?")]

OPENAI_STYLE_SUCCESS = {
    "model": "gpt-4.1-mini",
    "choices": [{"message": {"content": "4"}}],
    "usage": {
        "prompt_tokens": 11,
        "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 5},
    },
}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [
        ("ollama", OllamaProvider),
        ("openai", OpenAIProvider),
        ("anthropic", AnthropicProvider),
        ("openrouter", OpenRouterProvider),
        ("groq", GroqProvider),
        ("  OpenRouter  ", OpenRouterProvider),
    ],
)
def test_factory_creates_each_supported_provider(
    name: str,
    expected_type: type[BaseLLMProvider],
) -> None:
    provider = ProviderFactory.create(name, api_key="sk-testonly-key")

    assert isinstance(provider, expected_type)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(UnsupportedProviderError):
        ProviderFactory.create("not-a-provider")


# ---------------------------------------------------------------------------
# OpenAI-compatible providers (OpenAI, Groq, OpenRouter)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider_factory", "error_type"),
    [
        (lambda: OpenAIProvider(api_key="sk-testonly-openai"), OpenAIProviderError),
        (lambda: GroqProvider(api_key="gsk-testonly-groq"), GroqProviderError),
        (
            lambda: OpenRouterProvider(api_key="sk-or-testonly-key"),
            OpenRouterProviderError,
        ),
    ],
)
async def test_openai_style_success_is_parsed(
    monkeypatch: pytest.MonkeyPatch,
    provider_factory,
    error_type: type[Exception],
) -> None:
    captured = install_mock_transport(
        monkeypatch,
        json_responder(OPENAI_STYLE_SUCCESS),
    )

    response = await provider_factory().generate(
        messages=MESSAGES,
        model="gpt-4.1-mini",
        temperature=0.1,
        max_tokens=64,
    )

    assert response.content == "4"
    assert response.model == "gpt-4.1-mini"
    assert response.input_tokens == 11
    assert response.output_tokens == 1

    body = json.loads(captured[0].content)
    assert body["model"] == "gpt-4.1-mini"
    assert body["messages"][0]["content"] == "What is 2 + 2?"
    assert body["temperature"] == 0.1


@pytest.mark.anyio
async def test_openai_reports_cached_input_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        json_responder(OPENAI_STYLE_SUCCESS),
    )

    response = await OpenAIProvider(api_key="sk-testonly-openai").generate(
        messages=MESSAGES
    )

    assert response.cached_input_tokens == 5


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status_code", "expected_fragment"),
    [
        (401, "rejected the API key"),
        (429, "rate limit"),
    ],
)
async def test_openai_maps_authentication_and_rate_limit_errors(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_fragment: str,
) -> None:
    install_mock_transport(
        monkeypatch,
        json_responder({"error": {"message": "nope"}}, status_code=status_code),
    )

    with pytest.raises(OpenAIProviderError) as raised:
        await OpenAIProvider(api_key="sk-testonly-openai").generate(messages=MESSAGES)

    assert expected_fragment in str(raised.value)


@pytest.mark.anyio
async def test_openai_surfaces_provider_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        json_responder(
            {"error": {"message": "model_not_found"}},
            status_code=404,
        ),
    )

    with pytest.raises(OpenAIProviderError, match="model_not_found"):
        await OpenAIProvider(api_key="sk-testonly-openai").generate(messages=MESSAGES)


@pytest.mark.anyio
async def test_openai_handles_non_json_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        text_responder("<html>gateway error</html>", status_code=502),
    )

    with pytest.raises(OpenAIProviderError, match="gateway error"):
        await OpenAIProvider(api_key="sk-testonly-openai").generate(messages=MESSAGES)


@pytest.mark.anyio
async def test_openai_rejects_malformed_success_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        json_responder({"choices": []}),
    )

    with pytest.raises(OpenAIProviderError, match="invalid response"):
        await OpenAIProvider(api_key="sk-testonly-openai").generate(messages=MESSAGES)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected_fragment"),
    [
        (httpx.ConnectError("refused"), "connect"),
        (httpx.ReadTimeout("timed out"), "timeout"),
    ],
)
async def test_openai_maps_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_fragment: str,
) -> None:
    install_mock_transport(monkeypatch, raising_responder(error))

    with pytest.raises(OpenAIProviderError) as raised:
        await OpenAIProvider(api_key="sk-testonly-openai").generate(messages=MESSAGES)

    assert expected_fragment in str(raised.value).lower()


@pytest.mark.anyio
async def test_providers_require_an_api_key() -> None:
    for provider, error_type in (
        (OpenAIProvider(api_key=""), OpenAIProviderError),
        (GroqProvider(api_key=""), GroqProviderError),
        (AnthropicProvider(api_key=""), AnthropicProviderError),
        (OpenRouterProvider(api_key=""), OpenRouterProviderError),
    ):
        # An empty key falls back to settings; only assert when settings
        # also have no key configured for that provider.
        if provider.api_key:
            continue

        with pytest.raises(error_type, match="API key"):
            await provider.generate(messages=MESSAGES)


@pytest.mark.anyio
async def test_openrouter_sends_attribution_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_mock_transport(
        monkeypatch,
        json_responder(OPENAI_STYLE_SUCCESS),
    )

    await OpenRouterProvider(api_key="sk-or-testonly-key").generate(
        messages=MESSAGES,
        model="gpt-4.1-mini",
    )

    headers = captured[0].headers
    assert headers["authorization"] == "Bearer sk-or-testonly-key"
    assert headers["x-title"] == "TokenGuard AI"


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_anthropic_splits_system_prompt_and_parses_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_mock_transport(
        monkeypatch,
        json_responder(
            {
                "model": "claude-sonnet-4",
                "content": [
                    {"type": "text", "text": "Four"},
                    {"type": "thinking", "text": "ignored"},
                ],
                "usage": {"input_tokens": 9, "output_tokens": 2},
            }
        ),
    )

    response = await AnthropicProvider(api_key="sk-ant-testonly-key").generate(
        messages=[
            ChatMessage(role="system", content="Be terse."),
            ChatMessage(role="user", content="What is 2 + 2?"),
        ],
        model="claude-sonnet-4",
        max_tokens=32,
    )

    assert response.content == "Four"
    assert response.input_tokens == 9
    assert response.output_tokens == 2

    body = json.loads(captured[0].content)
    assert body["system"] == "Be terse."
    assert [message["role"] for message in body["messages"]] == ["user"]
    assert captured[0].headers["x-api-key"] == "sk-ant-testonly-key"


@pytest.mark.anyio
async def test_anthropic_requires_a_non_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(monkeypatch, json_responder({}))

    with pytest.raises(AnthropicProviderError, match="at least one"):
        await AnthropicProvider(api_key="sk-ant-testonly-key").generate(
            messages=[ChatMessage(role="system", content="Only system.")]
        )


@pytest.mark.anyio
async def test_anthropic_rejects_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        json_responder({"content": [{"type": "text", "text": ""}]}),
    )

    with pytest.raises(AnthropicProviderError, match="empty"):
        await AnthropicProvider(api_key="sk-ant-testonly-key").generate(
            messages=MESSAGES
        )


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ollama_generate_parses_native_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = install_mock_transport(
        monkeypatch,
        json_responder(
            {
                "model": "llama3.2",
                "message": {"content": "4"},
                "prompt_eval_count": 12,
                "eval_count": 3,
            }
        ),
    )

    response = await OllamaProvider().generate(
        messages=MESSAGES,
        model="llama3.2",
        max_tokens=50,
    )

    assert response.content == "4"
    assert response.input_tokens == 12
    assert response.output_tokens == 3

    body = json.loads(captured[0].content)
    assert body["stream"] is False
    assert body["options"]["num_predict"] == 50


@pytest.mark.anyio
async def test_ollama_maps_http_status_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        text_responder("model not found", status_code=404),
    )

    with pytest.raises(OllamaProviderError, match="HTTP status 404"):
        await OllamaProvider().generate(messages=MESSAGES)


@pytest.mark.anyio
async def test_ollama_rejects_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        json_responder({"message": {"content": ""}}),
    )

    with pytest.raises(OllamaProviderError, match="empty"):
        await OllamaProvider().generate(messages=MESSAGES)


@pytest.mark.anyio
async def test_ollama_rejects_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(monkeypatch, text_responder("not json"))

    with pytest.raises(OllamaProviderError, match="invalid JSON"):
        await OllamaProvider().generate(messages=MESSAGES)


@pytest.mark.anyio
async def test_ollama_connection_failure_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        raising_responder(httpx.ConnectError("refused")),
    )

    with pytest.raises(OllamaProviderError, match="Ollama is running"):
        await OllamaProvider().generate(messages=MESSAGES)


@pytest.mark.anyio
async def test_ollama_stream_emits_tokens_then_usage_then_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        stream_responder(
            [
                json.dumps({"message": {"content": "Fo"}, "done": False}),
                json.dumps({"message": {"content": "ur"}, "done": False}),
                json.dumps(
                    {
                        "message": {"content": ""},
                        "done": True,
                        "prompt_eval_count": 7,
                        "eval_count": 2,
                        "model": "llama3.2",
                    }
                ),
            ]
        ),
    )

    events = [
        event
        async for event in OllamaProvider().stream(
            messages=MESSAGES,
            model="llama3.2",
            temperature=0.0,
            max_tokens=16,
        )
    ]

    kinds = [event.event_type for event in events]
    assert kinds[0] == "token"
    assert kinds[-1] == "completed"
    assert "usage" in kinds

    tokens = "".join(
        event.content or "" for event in events if event.event_type == "token"
    )
    assert tokens == "Four"

    usage_event = next(event for event in events if event.event_type == "usage")
    assert usage_event.input_tokens == 7
    assert usage_event.output_tokens == 2


@pytest.mark.anyio
async def test_ollama_stream_maps_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        raising_responder(httpx.ConnectError("refused")),
    )

    with pytest.raises(OllamaProviderError):
        async for _event in OllamaProvider().stream(
            messages=MESSAGES,
            model="llama3.2",
            temperature=0.0,
            max_tokens=None,
        ):
            pass


@pytest.mark.anyio
async def test_openrouter_stream_parses_server_sent_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        stream_responder(
            [
                "data: "
                + json.dumps(
                    {
                        "model": "gpt-4.1-mini",
                        "choices": [{"delta": {"content": "Fo"}}],
                    }
                ),
                "data: "
                + json.dumps(
                    {
                        "choices": [{"delta": {"content": "ur"}}],
                        "usage": {
                            "prompt_tokens": 8,
                            "completion_tokens": 2,
                            "total_tokens": 10,
                        },
                    }
                ),
                "data: [DONE]",
            ]
        ),
    )

    events = [
        event
        async for event in OpenRouterProvider(api_key="sk-or-testonly-key").stream(
            messages=MESSAGES,
            model="gpt-4.1-mini",
            temperature=0.0,
            max_tokens=None,
        )
    ]

    assert [event.event_type for event in events][-1] == "completed"

    tokens = "".join(
        event.content or "" for event in events if event.event_type == "token"
    )
    assert tokens == "Four"


@pytest.mark.anyio
async def test_openrouter_stream_reports_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(
        monkeypatch,
        text_responder("unauthorized", status_code=401),
    )

    with pytest.raises(OpenRouterProviderError):
        async for _event in OpenRouterProvider(api_key="sk-or-testonly-key").stream(
            messages=MESSAGES,
            model="gpt-4.1-mini",
            temperature=0.0,
            max_tokens=None,
        ):
            pass


@pytest.mark.anyio
async def test_streaming_is_not_implemented_for_openai_style_providers() -> None:
    with pytest.raises(NotImplementedError):
        async for _event in OpenAIProvider(api_key="sk-testonly-openai").stream(
            messages=MESSAGES,
            model=None,
            temperature=0.0,
            max_tokens=None,
        ):
            pass
