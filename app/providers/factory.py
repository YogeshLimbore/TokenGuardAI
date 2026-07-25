from app.providers.anthropic import AnthropicProvider
from app.providers.base import BaseLLMProvider
from app.providers.groq import GroqProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai import OpenAIProvider
from app.providers.openrouter import OpenRouterProvider


class UnsupportedProviderError(ValueError):
    """Raised when TokenGuard does not support a provider."""


class ProviderFactory:
    @staticmethod
    def create(
        provider_name: str,
        api_key: str | None = None,
    ) -> BaseLLMProvider:
        normalized_name = provider_name.strip().lower()

        if normalized_name == "ollama":
            return OllamaProvider()

        if normalized_name == "openai":
            return OpenAIProvider(api_key=api_key)

        if normalized_name == "anthropic":
            return AnthropicProvider(api_key=api_key)

        if normalized_name == "openrouter":
            return OpenRouterProvider(api_key=api_key)

        if normalized_name == "groq":
            return GroqProvider(api_key=api_key)

        raise UnsupportedProviderError(f"Unsupported provider: {provider_name}")
