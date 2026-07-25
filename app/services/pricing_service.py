import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings


class PricingConfigurationError(Exception):
    """Raised when model pricing cannot be loaded."""


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_1m_tokens: float
    output_usd_per_1m_tokens: float


@dataclass(frozen=True)
class CostBreakdown:
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float
    pricing_found: bool


class PricingService:
    def __init__(self) -> None:
        settings = get_settings()
        self.pricing_file = Path(settings.model_pricing_file)
        self._pricing = self._load_pricing()

    def _load_pricing(self) -> dict[str, object]:
        if not self.pricing_file.exists():
            raise PricingConfigurationError(
                f"Model pricing file was not found: {self.pricing_file}"
            )

        try:
            with self.pricing_file.open(
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise PricingConfigurationError("Model pricing file is invalid.") from error

        if not isinstance(data, dict):
            raise PricingConfigurationError("Model pricing must be a JSON object.")

        return data

    def get_model_price(
        self,
        provider: str,
        model: str,
    ) -> ModelPrice | None:
        provider_prices = self._pricing.get(provider.lower())

        if not isinstance(provider_prices, dict):
            return None

        price_data = provider_prices.get(model) or provider_prices.get("*")

        if not isinstance(price_data, dict):
            return None

        try:
            return ModelPrice(
                input_usd_per_1m_tokens=float(price_data["input_usd_per_1m_tokens"]),
                output_usd_per_1m_tokens=float(price_data["output_usd_per_1m_tokens"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def calculate_cost(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> CostBreakdown:
        price = self.get_model_price(
            provider=provider,
            model=model,
        )

        if price is None:
            return CostBreakdown(
                input_cost_usd=0.0,
                output_cost_usd=0.0,
                total_cost_usd=0.0,
                pricing_found=False,
            )

        input_cost = input_tokens / 1_000_000 * price.input_usd_per_1m_tokens

        output_cost = output_tokens / 1_000_000 * price.output_usd_per_1m_tokens

        return CostBreakdown(
            input_cost_usd=round(input_cost, 10),
            output_cost_usd=round(output_cost, 10),
            total_cost_usd=round(
                input_cost + output_cost,
                10,
            ),
            pricing_found=True,
        )
