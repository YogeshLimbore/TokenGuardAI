import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.models import Organization, User
from app.services.model_policy_service import ModelAccessDeniedError, ModelPolicyService
from tests.conftest import bearer, create_project_with_key


def test_project_governance_policy_lifecycle_and_enforcement(
    client: TestClient,
    database: Session,
    organization_a: tuple[User, Organization],
) -> None:
    owner, organization = organization_a
    project, _ = create_project_with_key(
        database,
        name="Governed Project",
        organization_id=organization.id,
    )
    path = (
        f"/v1/organizations/{organization.id}/projects/{project.id}/governance-policy"
    )
    headers = bearer(owner)

    empty = client.get(path, headers=headers)
    assert empty.status_code == 200
    assert empty.json() == {"project_id": project.id, "is_active": False, "policy": {}}

    policy = {
        "allowed_providers": ["openai"],
        "allowed_models": {"openai": ["approved-model"]},
        "blocked_models": ["blocked-model"],
    }
    saved = client.put(path, headers=headers, json={"policy": policy})
    assert saved.status_code == 200
    assert saved.json()["policy"] == policy
    assert client.get(path, headers=headers).json()["is_active"] is True

    service = ModelPolicyService()
    service.enforce_model_access(
        project_id=project.id,
        provider="openai",
        model="approved-model",
        database=database,
    )
    with pytest.raises(ModelAccessDeniedError):
        service.enforce_model_access(
            project_id=project.id,
            provider="anthropic",
            model="approved-model",
            database=database,
        )
    with pytest.raises(ModelAccessDeniedError):
        service.enforce_model_access(
            project_id=project.id,
            provider="openai",
            model="blocked-model",
            database=database,
        )

    invalid = client.put(
        path,
        headers=headers,
        json={"policy": {"allowed_providers": ["not-a-provider"]}},
    )
    assert invalid.status_code == 422

    disabled = client.delete(path, headers=headers)
    assert disabled.status_code == 204
    assert client.get(path, headers=headers).json()["is_active"] is False
