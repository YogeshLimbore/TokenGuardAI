"""Shared, bounded text completion and SSE adapter for compatible APIs."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import get_settings
from app.providers.base import (
    BaseLLMProvider,
    ProviderError,
    ProviderResponse,
    ProviderStreamEvent,
)
from app.providers.registry import get_provider
from app.schemas.chat import ChatMessage


class CompatibleProvider(BaseLLMProvider):
    provider_name = "openai"
    error_type: type[ProviderError] = ProviderError

    def __init__(
        self, api_key: str | None = None, *, provider_name: str | None = None
    ) -> None:
        if provider_name is not None:
            self.provider_name = provider_name
        self.settings = get_settings()
        self.spec = get_provider(self.provider_name)
        self.api_key = api_key or self.spec.api_key

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise self.error_type(
                f"No {self.provider_name} API key is available.", status_code=401
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.provider_name == "openrouter":
            headers["HTTP-Referer"] = self.settings.token_guard_api_base_url
            headers["X-Title"] = self.settings.app_name
        if self.provider_name == "gemini":
            headers["x-goog-api-client"] = f"tokenguard/{self.settings.app_version}"
        return headers

    def _payload(
        self,
        messages: list[ChatMessage],
        model: str | None,
        temperature: float,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        selected = model or self.spec.default_model
        if not selected:
            raise self.error_type(
                "An explicit model is required for this provider.", status_code=400
            )
        return {
            "model": selected,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            self.spec.token_parameter: max_tokens
            or self.settings.default_output_tokens,
        }

    def _check_status(self, response: httpx.Response) -> None:
        if response.status_code >= 300:
            # Never return/log the upstream body: it can echo prompts or secrets.
            if response.status_code in {401, 403}:
                message = f"{self.provider_name} authentication failed."
            elif response.status_code == 429:
                message = f"{self.provider_name} rate limit was exceeded."
            elif response.status_code == 402:
                message = f"{self.provider_name} account has insufficient credit."
            else:
                message = f"{self.provider_name} returned HTTP {response.status_code}."
            raise self.error_type(
                message,
                status_code=response.status_code,
            )

    @staticmethod
    def _content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                str(part.get("text", "")) for part in value if isinstance(part, dict)
            )
        raise ValueError("Missing text content")

    async def generate(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        payload = self._payload(messages, model, temperature, max_tokens)
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds, follow_redirects=False
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.spec.base_url.rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as response:
                    self._check_status(response)
                    content_length = response.headers.get("content-length")
                    if (
                        content_length is not None
                        and content_length.isdigit()
                        and int(content_length)
                        > self.settings.maximum_provider_response_bytes
                    ):
                        raise self.error_type(
                            "Provider response is too large.", status_code=502
                        )
                    chunks: list[bytes] = []
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if received > self.settings.maximum_provider_response_bytes:
                            raise self.error_type(
                                "Provider response is too large.", status_code=502
                            )
                        chunks.append(chunk)
            data = json.loads(b"".join(chunks))
            if data.get("error"):
                raise self.error_type(
                    "Provider returned an error response.", status_code=502
                )
            usage = data.get("usage") or {}
            return ProviderResponse(
                content=self._content(data["choices"][0]["message"]["content"]),
                model=data.get("model") or payload["model"],
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
                cached_input_tokens=(usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens"
                ),
            )
        except httpx.TimeoutException as exc:
            raise self.error_type(
                "Provider request timed out.", status_code=504
            ) from exc
        except httpx.ConnectError as exc:
            raise self.error_type(
                "Could not connect to provider.", status_code=503
            ) from exc
        except httpx.RequestError as exc:
            raise self.error_type(
                "Provider transport failed.", status_code=503
            ) from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise self.error_type(
                "Provider returned an invalid response.", status_code=502
            ) from exc

    async def stream(
        self,
        *,
        messages: list[ChatMessage],
        model: str | None,
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        payload = self._payload(messages, model, temperature, max_tokens)
        payload["stream"] = True
        # Usage is optional on compatible servers; request it for known support.
        if self.provider_name in {
            "openai",
            "openrouter",
            "groq",
            "together",
            "deepseek",
            "xai",
        }:
            payload["stream_options"] = {"include_usage": True}
        terminal = False
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.request_timeout_seconds, follow_redirects=False
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.spec.base_url.rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                ) as response:
                    self._check_status(response)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw:
                            continue
                        if raw == "[DONE]":
                            terminal = True
                            break
                        if len(raw) > self.settings.maximum_request_bytes:
                            raise self.error_type(
                                "Provider stream event is too large.", status_code=502
                            )
                        data = json.loads(raw)
                        if data.get("error"):
                            raise self.error_type(
                                "Provider stream reported an error.", status_code=502
                            )
                        for choice in data.get("choices") or []:
                            if choice.get("finish_reason") == "error":
                                raise self.error_type(
                                    "Provider stream failed.", status_code=502
                                )
                            if choice.get("finish_reason") is not None:
                                terminal = True
                            content = (choice.get("delta") or {}).get("content")
                            if content:
                                yield ProviderStreamEvent(
                                    event_type="token", content=self._content(content)
                                )
                        usage = data.get("usage")
                        if usage:
                            yield ProviderStreamEvent(
                                event_type="usage",
                                input_tokens=usage.get("prompt_tokens"),
                                output_tokens=usage.get("completion_tokens"),
                                total_tokens=usage.get("total_tokens"),
                            )
            if not terminal:
                raise self.error_type(
                    "Provider stream ended without a completion marker.",
                    status_code=502,
                )
            yield ProviderStreamEvent(
                event_type="completed", model=str(payload["model"])
            )
        except httpx.TimeoutException as exc:
            raise self.error_type(
                "Provider stream timed out.", status_code=504
            ) from exc
        except httpx.ConnectError as exc:
            raise self.error_type(
                "Could not connect to provider stream.", status_code=503
            ) from exc
        except httpx.RequestError as exc:
            raise self.error_type(
                "Provider stream transport failed.", status_code=503
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise self.error_type(
                "Provider returned invalid stream data.", status_code=502
            ) from exc
