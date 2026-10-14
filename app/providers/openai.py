from app.providers.base import ProviderError
from app.providers.compatible import CompatibleProvider
from app.providers.compatible import httpx as httpx


class OpenAIProviderError(ProviderError):
    """Sanitized OpenAI provider failure."""


class OpenAIProvider(CompatibleProvider):
    provider_name = "openai"
    error_type = OpenAIProviderError
