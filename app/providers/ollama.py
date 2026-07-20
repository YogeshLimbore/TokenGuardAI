from typing import Any

import httpx

from app.core.config import get_settings
from app.providers.base import BaseProvider


class OllamaProvider(BaseProvider):
    """Provider that talks to a local Ollama server."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.ollama_base_url
        self.default_model = settings.ollama_default_model
        self.timeout = settings.request_timeout_seconds

    async def chat(self, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(
                "/api/chat",
                json={
                    "model": model or self.default_model,
                    "messages": messages,
                    "stream": False,
                },
            )
            response.raise_for_status()
            return response.json()
