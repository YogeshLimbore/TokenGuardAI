import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import get_settings
from app.providers.base import (
    BaseLLMProvider,
    ProviderResponse,
    ProviderStreamEvent,
)
from app.schemas.chat import ChatMessage


class OllamaProviderError(Exception):
    """
    Raised when Ollama cannot process the request successfully.
    """


class OllamaProvider(BaseLLMProvider):
    provider_name = "ollama"

    def __init__(self) -> None:
        self.settings = get_settings()

    async def generate(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        selected_model = model or self.settings.ollama_default_model

        options: dict[str, float | int] = {
            "temperature": temperature,
        }

        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload = {
            "model": selected_model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in messages
            ],
            "stream": False,
            "options": options,
        }

        endpoint = f"{self.settings.ollama_base_url}/api/chat"

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds
            ) as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                )

                response.raise_for_status()

        except httpx.ConnectError as error:
            raise OllamaProviderError(
                "Could not connect to Ollama. Make sure Ollama is running."
            ) from error

        except httpx.TimeoutException as error:
            raise OllamaProviderError(
                "Ollama did not respond before the timeout."
            ) from error

        except httpx.HTTPStatusError as error:
            raise OllamaProviderError(
                f"Ollama returned HTTP status {error.response.status_code}."
            ) from error

        try:
            data = response.json()
        except ValueError as error:
            raise OllamaProviderError(
                "Ollama returned an invalid JSON response."
            ) from error

        content = data.get("message", {}).get("content")

        if not content:
            raise OllamaProviderError("Ollama returned an empty response.")

        return ProviderResponse(
            content=content,
            model=data.get("model", selected_model),
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
        )

    async def stream(
        self,
        *,
        messages: list[ChatMessage],
        model: str | None,
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        selected_model = model or self.settings.ollama_default_model

        options: dict[str, float | int] = {
            "temperature": temperature,
        }

        if max_tokens is not None:
            options["num_predict"] = max_tokens

        payload = {
            "model": selected_model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in messages
            ],
            "stream": True,
            "options": options,
        }

        endpoint = f"{self.settings.ollama_base_url}/api/chat"

        try:
            # The read timeout applies between chunks, not to the whole
            # stream, so a long generation keeps flowing while a stalled
            # provider still releases the connection.
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=self.settings.request_timeout_seconds,
                    write=10.0,
                    pool=10.0,
                )
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line:
                            continue

                        data = json.loads(line)

                        message = data.get(
                            "message",
                            {},
                        )

                        content = message.get("content")

                        if content:
                            yield ProviderStreamEvent(
                                event_type="token",
                                content=content,
                                model=selected_model,
                            )

                        if data.get("done"):
                            input_tokens = int(
                                data.get(
                                    "prompt_eval_count",
                                    0,
                                )
                            )

                            output_tokens = int(
                                data.get(
                                    "eval_count",
                                    0,
                                )
                            )

                            yield ProviderStreamEvent(
                                event_type="usage",
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                total_tokens=(input_tokens + output_tokens),
                                model=selected_model,
                            )

                            yield ProviderStreamEvent(
                                event_type="completed",
                                model=selected_model,
                            )

        except httpx.ConnectError as error:
            raise OllamaProviderError(
                "Could not connect to Ollama. Make sure Ollama is running."
            ) from error

        except httpx.TimeoutException as error:
            raise OllamaProviderError(
                "Ollama did not respond before the timeout."
            ) from error

        except httpx.HTTPStatusError as error:
            raise OllamaProviderError(
                f"Ollama returned HTTP status {error.response.status_code}."
            ) from error

        except (
            httpx.HTTPError,
            json.JSONDecodeError,
        ) as error:
            raise OllamaProviderError(f"Ollama streaming failed: {error}") from error
