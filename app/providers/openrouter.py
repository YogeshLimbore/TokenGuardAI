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


class OpenRouterProviderError(Exception):
    """Raised when OpenRouter cannot process a request."""


class OpenRouterProvider(BaseLLMProvider):
    provider_name = "openrouter"

    def __init__(
        self,
        api_key: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.api_key = api_key or self.settings.openrouter_api_key

    async def generate(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        if not self.api_key:
            raise OpenRouterProviderError("No OpenRouter API key is available.")

        selected_model = model or self.settings.openrouter_default_model

        payload: dict[str, object] = {
            "model": selected_model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in messages
            ],
            "temperature": temperature,
        }

        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        endpoint = f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "TokenGuard AI",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds
            ) as client:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                )

        except httpx.ConnectError as error:
            raise OpenRouterProviderError("Could not connect to OpenRouter.") from error

        except httpx.TimeoutException as error:
            raise OpenRouterProviderError(
                "OpenRouter did not respond before the timeout."
            ) from error

        if response.status_code == 401:
            raise OpenRouterProviderError("OpenRouter rejected the API key.")

        if response.status_code == 402:
            raise OpenRouterProviderError("OpenRouter credits are insufficient.")

        if response.status_code == 429:
            raise OpenRouterProviderError("OpenRouter rate limit was exceeded.")

        if response.status_code >= 400:
            try:
                error_data = response.json()
                error_message = error_data.get("error", {}).get("message")
            except ValueError:
                error_message = response.text

            raise OpenRouterProviderError(
                error_message or f"OpenRouter returned HTTP {response.status_code}."
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise OpenRouterProviderError(
                "OpenRouter returned an invalid response."
            ) from error

        usage = data.get("usage", {})

        return ProviderResponse(
            content=content,
            model=data.get("model", selected_model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    async def stream(
        self,
        *,
        messages: list[ChatMessage],
        model: str | None,
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if not self.api_key:
            raise OpenRouterProviderError("No OpenRouter API key is available.")

        selected_model = model or self.settings.openrouter_default_model

        payload: dict[str, object] = {
            "model": selected_model,
            "messages": [
                {
                    "role": message.role,
                    "content": message.content,
                }
                for message in messages
            ],
            "temperature": temperature,
            "stream": True,
        }

        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        endpoint = f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:8000",
            "X-Title": "TokenGuard AI",
        }

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
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        error_body = await response.aread()

                        try:
                            error_data = json.loads(
                                error_body.decode(
                                    "utf-8",
                                    errors="replace",
                                )
                            )
                            error_message = error_data.get("error", {}).get("message")
                        except ValueError:
                            error_message = error_body.decode(
                                "utf-8",
                                errors="replace",
                            )

                        raise OpenRouterProviderError(
                            error_message
                            or f"OpenRouter returned HTTP {response.status_code}."
                        )

                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue

                        payload_text = line[5:].strip()

                        if not payload_text:
                            continue

                        if payload_text == "[DONE]":
                            yield ProviderStreamEvent(
                                event_type="completed",
                                model=selected_model,
                            )
                            break

                        data = json.loads(payload_text)

                        choices = data.get("choices", [])

                        if choices:
                            delta = choices[0].get(
                                "delta",
                                {},
                            )

                            content = delta.get("content")

                            if content:
                                yield ProviderStreamEvent(
                                    event_type="token",
                                    content=content,
                                    model=selected_model,
                                )

                        usage = data.get("usage")

                        if usage:
                            yield ProviderStreamEvent(
                                event_type="usage",
                                input_tokens=int(
                                    usage.get(
                                        "prompt_tokens",
                                        0,
                                    )
                                ),
                                output_tokens=int(
                                    usage.get(
                                        "completion_tokens",
                                        0,
                                    )
                                ),
                                total_tokens=int(
                                    usage.get(
                                        "total_tokens",
                                        0,
                                    )
                                ),
                                model=selected_model,
                            )

        except httpx.ConnectError as error:
            raise OpenRouterProviderError("Could not connect to OpenRouter.") from error

        except httpx.TimeoutException as error:
            raise OpenRouterProviderError(
                "OpenRouter did not respond before the timeout."
            ) from error

        except (
            httpx.HTTPError,
            json.JSONDecodeError,
        ) as error:
            raise OpenRouterProviderError(
                f"OpenRouter streaming failed: {error}"
            ) from error
