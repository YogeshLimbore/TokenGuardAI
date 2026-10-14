import json
from collections.abc import AsyncIterator

import httpx

from app.core.config import get_settings
from app.providers.base import (
    BaseLLMProvider,
    ProviderError,
    ProviderResponse,
    ProviderStreamEvent,
)
from app.schemas.chat import ChatMessage

DEFAULT_MAX_TOKENS = 1024


class AnthropicProviderError(ProviderError):
    """Raised when Anthropic cannot successfully process a request."""


class AnthropicProvider(BaseLLMProvider):
    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.api_key = api_key or self.settings.anthropic_api_key

    async def generate(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        if not self.api_key:
            raise AnthropicProviderError("No Anthropic API key is available.")

        selected_model = model or self.settings.anthropic_default_model

        system_prompt = "\n\n".join(
            message.content for message in messages if message.role == "system"
        )

        conversation = [
            {
                "role": message.role,
                "content": message.content,
            }
            for message in messages
            if message.role != "system"
        ]

        if not conversation:
            raise AnthropicProviderError(
                "Anthropic requires at least one user or assistant message."
            )

        payload: dict[str, object] = {
            "model": selected_model,
            "messages": conversation,
            "temperature": temperature,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        }

        if system_prompt:
            payload["system"] = system_prompt

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.settings.anthropic_version,
            "Content-Type": "application/json",
        }

        endpoint = f"{self.settings.anthropic_base_url.rstrip('/')}/v1/messages"

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds,
            ) as client:
                async with client.stream(
                    "POST", endpoint, headers=headers, json=payload
                ) as response:
                    if response.status_code >= 400:
                        raise AnthropicProviderError(
                            f"Anthropic returned HTTP {response.status_code}.",
                            status_code=response.status_code,
                        )
                    content_length = response.headers.get("content-length")
                    if (
                        content_length is not None
                        and content_length.isdigit()
                        and int(content_length)
                        > self.settings.maximum_provider_response_bytes
                    ):
                        raise AnthropicProviderError(
                            "Anthropic response is too large.", status_code=502
                        )
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > self.settings.maximum_provider_response_bytes:
                            raise AnthropicProviderError(
                                "Anthropic response is too large.", status_code=502
                            )
                        chunks.append(chunk)

        except httpx.ConnectError as error:
            raise AnthropicProviderError("Could not connect to Anthropic.") from error

        except httpx.TimeoutException as error:
            raise AnthropicProviderError(
                "Anthropic did not respond before the timeout."
            ) from error

        try:
            data = json.loads(b"".join(chunks))
            content = "".join(
                block.get("text", "")
                for block in data["content"]
                if block.get("type") == "text"
            )
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise AnthropicProviderError(
                "Anthropic returned an invalid response."
            ) from error

        if not content:
            raise AnthropicProviderError("Anthropic returned an empty response.")

        usage = data.get("usage", {})

        return ProviderResponse(
            content=content,
            model=data.get("model", selected_model),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cache_read_input_tokens"),
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
            raise AnthropicProviderError(
                "No Anthropic API key is available.", status_code=401
            )
        payload = {
            "model": model or self.settings.anthropic_default_model,
            "messages": [m.model_dump() for m in messages if m.role != "system"],
            "system": "\n".join(m.content for m in messages if m.role == "system"),
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.default_output_tokens,
            "stream": True,
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.settings.anthropic_version,
        }
        completed = False
        input_tokens = output_tokens = 0
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds, follow_redirects=False
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.settings.anthropic_base_url.rstrip('/')}/v1/messages",
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        raise AnthropicProviderError(
                            f"Anthropic returned HTTP {response.status_code}.",
                            status_code=response.status_code,
                        )
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = json.loads(line[5:])
                        kind = data.get("type")
                        if kind == "error":
                            raise AnthropicProviderError(
                                "Anthropic stream failed.", status_code=502
                            )
                        if kind == "message_start":
                            input_tokens = (
                                data.get("message", {})
                                .get("usage", {})
                                .get("input_tokens", 0)
                            )
                        elif (
                            kind == "content_block_delta"
                            and data.get("delta", {}).get("type") == "text_delta"
                        ):
                            yield ProviderStreamEvent(
                                event_type="token", content=data["delta"]["text"]
                            )
                        elif kind == "message_delta":
                            output_tokens = data.get("usage", {}).get(
                                "output_tokens", output_tokens
                            )
                        elif kind == "message_stop":
                            completed = True
            if not completed:
                raise AnthropicProviderError(
                    "Anthropic stream ended without completion.", status_code=502
                )
            yield ProviderStreamEvent(
                event_type="usage",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            )
            yield ProviderStreamEvent(
                event_type="completed", model=str(payload["model"])
            )
        except httpx.RequestError as error:
            raise AnthropicProviderError(
                "Anthropic stream transport failed.", status_code=503
            ) from error
        except (ValueError, KeyError, TypeError) as error:
            raise AnthropicProviderError(
                "Invalid Anthropic stream.", status_code=502
            ) from error
