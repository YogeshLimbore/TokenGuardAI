"""
Deterministic provider doubles for integration and end-to-end tests.

Tests must never depend on OpenRouter, OpenAI, Anthropic, Groq, or a locally
running Ollama instance. Patch ``ProviderFactory.create`` (as resolved inside
``app.services.fallback_service``) with one of these fakes instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.providers.base import ProviderResponse, ProviderStreamEvent

DEFAULT_CONTENT = "This is a deterministic test response."


class FakeProvider:
    """Returns fixed content and records every call it receives."""

    def __init__(
        self,
        *,
        provider_name: str = "ollama",
        content: str = DEFAULT_CONTENT,
        input_tokens: int = 20,
        output_tokens: int = 10,
    ) -> None:
        self.provider_name = provider_name
        self.content = content
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[dict] = []

    async def generate(
        self,
        messages,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )

        return ProviderResponse(
            content=self.content,
            model=model or "llama3.2",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )

    async def stream(
        self,
        *,
        messages,
        model: str | None,
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
        )

        for token in ["This ", "is ", "a ", "test."]:
            yield ProviderStreamEvent(
                event_type="token",
                content=token,
                model=model,
            )

        yield ProviderStreamEvent(
            event_type="usage",
            input_tokens=20,
            output_tokens=4,
            total_tokens=24,
            model=model,
        )

        yield ProviderStreamEvent(
            event_type="completed",
            model=model,
        )


class FailingProvider:
    """Always raises, for outage and fallback simulations."""

    def __init__(
        self,
        *,
        provider_name: str = "openrouter",
        error: Exception | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.error = error or RuntimeError("Simulated provider outage.")
        self.calls = 0

    async def generate(
        self,
        messages,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        self.calls += 1
        raise self.error
