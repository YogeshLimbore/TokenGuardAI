"""One operator-controlled catalog for credentials, routing and adapters.

Custom OpenAI-compatible endpoints belong in PROVIDER_CONFIGS, never in an
inference request. This prevents project keys from selecting arbitrary URLs.
"""

from dataclasses import dataclass

from app.core.config import get_settings


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    default_model: str = ""
    protocol: str = "openai"
    token_parameter: str = "max_tokens"  # noqa: S105 -- API field name, not a secret
    api_key: str | None = None


def provider_catalog() -> dict[str, ProviderSpec]:
    settings = get_settings()
    catalog = {
        name: ProviderSpec(
            name=name,
            base_url=getattr(settings, f"{name}_base_url"),
            default_model=getattr(settings, f"{name}_default_model"),
            protocol="anthropic"
            if name == "anthropic"
            else ("ollama" if name == "ollama" else "openai"),
            token_parameter="max_completion_tokens"
            if name == "openai"
            else "max_tokens",
            api_key=getattr(settings, f"{name}_api_key", None),
        )
        for name in ("ollama", "openai", "anthropic", "openrouter", "groq")
    }
    for name, url in {
        "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
        "deepseek": "https://api.deepseek.com/v1",
        "mistral": "https://api.mistral.ai/v1",
        "together": "https://api.together.xyz/v1",
        "fireworks": "https://api.fireworks.ai/inference/v1",
        "xai": "https://api.x.ai/v1",
        "perplexity": "https://api.perplexity.ai",
    }.items():
        catalog[name] = ProviderSpec(name=name, base_url=url)
    for name, config in settings.provider_configs.items():
        url = str(config["base_url"]).rstrip("/")
        parameter = str(config.get("token_parameter", "max_tokens"))
        catalog[name] = ProviderSpec(
            name=name,
            base_url=url,
            default_model=str(config.get("default_model", "")),
            token_parameter=parameter,
        )
    return catalog


def get_provider(name: str) -> ProviderSpec:
    try:
        return provider_catalog()[name.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unsupported provider: {name}") from exc


def validate_provider(name: str) -> str:
    normalized = name.strip().lower()
    get_provider(normalized)
    return normalized
