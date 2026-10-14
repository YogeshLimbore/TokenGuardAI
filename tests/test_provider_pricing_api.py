from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import create_project_with_key


def test_project_pricing_lifecycle_is_tenant_scoped(
    client: TestClient, database: Session
) -> None:
    first_project, first_key = create_project_with_key(
        database, name="Pricing Project A"
    )
    _, second_key = create_project_with_key(database, name="Pricing Project B")
    first_headers = {"X-TokenGuard-Key": first_key}
    second_headers = {"X-TokenGuard-Key": second_key}

    saved = client.put(
        "/v1/providers/pricing/openai",
        headers=first_headers,
        json={
            "model": "gpt-test",
            "input_usd_per_1m_tokens": 1.25,
            "output_usd_per_1m_tokens": 5.0,
        },
    )

    assert saved.status_code == 200
    assert saved.json()["project_id"] == first_project.id
    assert saved.json()["provider"] == "openai"

    first_prices = client.get("/v1/providers/pricing", headers=first_headers)
    second_prices = client.get("/v1/providers/pricing", headers=second_headers)

    assert first_prices.status_code == 200
    assert [item["model"] for item in first_prices.json()] == ["gpt-test"]
    assert second_prices.status_code == 200
    assert second_prices.json() == []

    removed = client.delete(
        "/v1/providers/pricing/openai/gpt-test", headers=first_headers
    )
    assert removed.status_code == 204
    assert client.get("/v1/providers/pricing", headers=first_headers).json() == []


def test_project_pricing_rejects_unknown_provider(
    client: TestClient, database: Session
) -> None:
    _, key = create_project_with_key(database, name="Unknown Provider Pricing")
    response = client.put(
        "/v1/providers/pricing/not-registered",
        headers={"X-TokenGuard-Key": key},
        json={
            "model": "model",
            "input_usd_per_1m_tokens": 1,
            "output_usd_per_1m_tokens": 2,
        },
    )
    assert response.status_code == 404
