import json

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import ProjectModelPrice
from app.services import pricing_service as pricing_service_module
from app.services.pricing_service import PricingService
from tests.conftest import create_project_with_key


def test_operator_pricing_makes_custom_provider_billable(tmp_path, monkeypatch):
    pricing_file = tmp_path / "pricing.json"
    pricing_file.write_text(
        json.dumps(
            {
                "ollama": {
                    "*": {
                        "input_usd_per_1m_tokens": 0.0,
                        "output_usd_per_1m_tokens": 0.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        model_pricing_file=str(pricing_file),
        provider_pricing={
            "gemini": {
                "gemini-test-model": {
                    "input_usd_per_1m_tokens": 1.25,
                    "output_usd_per_1m_tokens": 5.0,
                }
            }
        },
    )
    monkeypatch.setattr(pricing_service_module, "get_settings", lambda: settings)

    cost = PricingService().calculate_cost(
        provider="gemini",
        model="gemini-test-model",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    assert cost.pricing_found is True
    assert cost.total_cost_usd == 6.25


@pytest.mark.parametrize(
    "pricing",
    [
        {
            "Gemini": {
                "model": {
                    "input_usd_per_1m_tokens": 1.0,
                    "output_usd_per_1m_tokens": 1.0,
                }
            }
        },
        {
            "gemini": {
                "model": {
                    "input_usd_per_1m_tokens": -1.0,
                    "output_usd_per_1m_tokens": 1.0,
                }
            }
        },
        {"gemini": {"model": {"input_usd_per_1m_tokens": 1.0}}},
    ],
)
def test_invalid_operator_pricing_is_rejected(pricing):
    with pytest.raises(ValueError):
        Settings(provider_pricing=pricing)


def test_project_price_overrides_operator_price(database: Session):
    project, _ = create_project_with_key(database, name="Project Pricing")
    database.add(
        ProjectModelPrice(
            project_id=project.id,
            provider="openai",
            model="gpt-4o-mini",
            input_usd_per_1m_tokens=9.0,
            output_usd_per_1m_tokens=12.0,
            is_active=True,
        )
    )
    database.commit()

    cost = PricingService().calculate_cost(
        provider="openai",
        model="gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        database=database,
        project_id=project.id,
    )

    assert cost.pricing_found is True
    assert cost.total_cost_usd == 21.0
