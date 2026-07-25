import httpx

from app.core.config import get_settings
from app.providers.base import BaseLLMProvider, ProviderResponse
from app.schemas.chat import ChatMessage

DEFAULT_MAX_TOKENS = 1024


class AnthropicProviderError(Exception):
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
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                )

        except httpx.ConnectError as error:
            raise AnthropicProviderError("Could not connect to Anthropic.") from error

        except httpx.TimeoutException as error:
            raise AnthropicProviderError(
                "Anthropic did not respond before the timeout."
            ) from error

        if response.status_code == 401:
            raise AnthropicProviderError("Anthropic rejected the API key.")

        if response.status_code == 429:
            raise AnthropicProviderError(
                "Anthropic rate limit or API quota was exceeded."
            )

        if response.status_code >= 400:
            try:
                error_data = response.json()
                error_message = error_data.get("error", {}).get("message")
            except ValueError:
                error_message = response.text

            raise AnthropicProviderError(
                error_message or (f"Anthropic returned HTTP {response.status_code}.")
            )

        try:
            data = response.json()
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
