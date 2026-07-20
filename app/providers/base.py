from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    """Common interface all LLM providers must implement."""

    @abstractmethod
    async def chat(self, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Send a chat completion request and return the raw provider response."""
        raise NotImplementedError
