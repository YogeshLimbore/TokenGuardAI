from app.providers.anthropic import AnthropicProvider
from app.providers.base import BaseLLMProvider
from app.providers.compatible import CompatibleProvider
from app.providers.groq import GroqProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai import OpenAIProvider
from app.providers.openrouter import OpenRouterProvider
from app.providers.registry import get_provider


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

        try:
            get_provider(normalized_name)
        except ValueError as error:
            raise UnsupportedProviderError(str(error)) from error
        return CompatibleProvider(api_key=api_key, provider_name=normalized_name)
