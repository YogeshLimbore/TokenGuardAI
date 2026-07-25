import httpx

from app.core.config import get_settings
from app.providers.base import BaseLLMProvider, ProviderResponse
from app.schemas.chat import ChatMessage


class GroqProviderError(Exception):
    """Raised when Groq cannot successfully process a request."""


class GroqProvider(BaseLLMProvider):
    provider_name = "groq"

    def __init__(
        self,
        api_key: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.api_key = api_key or self.settings.groq_api_key

    async def generate(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        if not self.api_key:
            raise GroqProviderError("No Groq API key is available.")

        selected_model = model or self.settings.groq_default_model

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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        endpoint = f"{self.settings.groq_base_url.rstrip('/')}/chat/completions"

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
            raise GroqProviderError("Could not connect to Groq.") from error

        except httpx.TimeoutException as error:
            raise GroqProviderError(
                "Groq did not respond before the timeout."
            ) from error

        if response.status_code == 401:
            raise GroqProviderError("Groq rejected the API key.")

        if response.status_code == 429:
            raise GroqProviderError("Groq rate limit or API quota was exceeded.")

        if response.status_code >= 400:
            try:
                error_data = response.json()
                error_message = error_data.get("error", {}).get("message")
            except ValueError:
                error_message = response.text

            raise GroqProviderError(
                error_message or (f"Groq returned HTTP {response.status_code}.")
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise GroqProviderError("Groq returned an invalid response.") from error

        usage = data.get("usage", {})

        return ProviderResponse(
            content=content,
            model=data.get("model", selected_model),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
