import httpx

from app.core.config import get_settings
from app.providers.base import BaseLLMProvider, ProviderResponse
from app.schemas.chat import ChatMessage


class OpenAIProviderError(Exception):
    """Raised when OpenAI cannot successfully process a request."""


class OpenAIProvider(BaseLLMProvider):
    provider_name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.api_key = api_key or self.settings.openai_api_key

    async def generate(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        if not self.api_key:
            raise OpenAIProviderError("No OpenAI API key is available.")

        selected_model = model or self.settings.openai_default_model

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
            payload["max_completion_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        endpoint = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"

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
            raise OpenAIProviderError("Could not connect to OpenAI.") from error

        except httpx.TimeoutException as error:
            raise OpenAIProviderError(
                "OpenAI did not respond before the timeout."
            ) from error

        if response.status_code == 401:
            raise OpenAIProviderError("OpenAI rejected the API key.")

        if response.status_code == 429:
            raise OpenAIProviderError("OpenAI rate limit or API quota was exceeded.")

        if response.status_code >= 400:
            try:
                error_data = response.json()
                error_message = error_data.get("error", {}).get("message")
            except ValueError:
                error_message = response.text

            raise OpenAIProviderError(
                error_message or (f"OpenAI returned HTTP {response.status_code}.")
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise OpenAIProviderError("OpenAI returned an invalid response.") from error

        usage = data.get("usage", {})

        prompt_details = usage.get(
            "prompt_tokens_details",
            {},
        )

        return ProviderResponse(
            content=content,
            model=data.get("model", selected_model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cached_input_tokens=prompt_details.get("cached_tokens"),
        )
