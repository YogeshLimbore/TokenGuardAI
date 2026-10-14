from app.providers.base import ProviderError
from app.providers.compatible import CompatibleProvider
from app.providers.compatible import httpx as httpx


class OpenRouterProviderError(ProviderError):
    """Sanitized OpenRouter provider failure."""


class OpenRouterProvider(CompatibleProvider):
    provider_name = "openrouter"
    error_type = OpenRouterProviderError
