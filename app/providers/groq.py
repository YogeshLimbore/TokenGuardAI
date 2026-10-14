from app.providers.base import ProviderError
from app.providers.compatible import CompatibleProvider
from app.providers.compatible import httpx as httpx


class GroqProviderError(ProviderError):
    """Sanitized Groq provider failure."""


class GroqProvider(CompatibleProvider):
    provider_name = "groq"
    error_type = GroqProviderError
