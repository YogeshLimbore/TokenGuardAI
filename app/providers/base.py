from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from app.schemas.chat import ChatMessage


class ProviderError(RuntimeError):
    """Sanitized provider failure retaining machine-readable retry status."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ProviderResponse:
    content: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class ProviderStreamEvent:
    event_type: Literal[
        "token",
        "usage",
        "completed",
    ]

    content: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    model: str | None = None


class BaseLLMProvider(ABC):
    provider_name: str

    @abstractmethod
    async def generate(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        raise NotImplementedError

    async def stream(
        self,
        *,
        messages: list[ChatMessage],
        model: str | None,
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        """
        Yield ``ProviderStreamEvent`` items as tokens arrive.

        Providers that do not support streaming raise
        ``NotImplementedError`` instead of overriding this method.
        """

        # The unreachable yield is what makes this an async generator, so
        # non-streaming providers still satisfy the return type.
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]

        raise NotImplementedError("This provider does not support streaming.")
