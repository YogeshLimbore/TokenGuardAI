"""Discover configured providers and models without exposing stored keys."""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.connection import get_database_session
from app.database.models import Project, ProjectModelPrice
from app.dependencies.authentication import get_current_project
from app.observability.context import get_correlation_id
from app.providers.registry import get_provider, provider_catalog
from app.schemas.providers import ProjectModelPriceRequest, ProjectModelPriceResponse
from app.services.credential_service import CredentialNotFoundError, CredentialService
from app.services.pricing_service import PricingService
from app.services.security_audit_service import SecurityAuditService

router = APIRouter(prefix="/v1/providers", tags=["Providers"])


@router.get("")
def list_providers(
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> list[dict[str, Any]]:
    pricing = PricingService()
    return [
        {
            "id": spec.name,
            "protocol": spec.protocol,
            "default_model": spec.default_model or None,
            "streaming": True,
            "requires_api_key": spec.name != "ollama",
            "pricing_configured": pricing.provider_has_pricing(
                spec.name, database=database, project_id=project.id
            ),
        }
        for spec in provider_catalog().values()
    ]


def _price_response(price: ProjectModelPrice) -> ProjectModelPriceResponse:
    return ProjectModelPriceResponse(
        id=price.id,
        project_id=price.project_id,
        provider=price.provider,
        model=price.model,
        input_usd_per_1m_tokens=price.input_usd_per_1m_tokens,
        output_usd_per_1m_tokens=price.output_usd_per_1m_tokens,
        is_active=price.is_active,
        created_at=price.created_at,
        updated_at=price.updated_at,
    )


@router.get("/pricing", response_model=list[ProjectModelPriceResponse])
def list_project_pricing(
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> list[ProjectModelPriceResponse]:
    prices = database.scalars(
        select(ProjectModelPrice)
        .where(
            ProjectModelPrice.project_id == project.id,
            ProjectModelPrice.is_active.is_(True),
        )
        .order_by(ProjectModelPrice.provider, ProjectModelPrice.model)
    ).all()
    return [_price_response(price) for price in prices]


@router.put(
    "/pricing/{provider}",
    response_model=ProjectModelPriceResponse,
)
def upsert_project_pricing(
    provider: str,
    request: ProjectModelPriceRequest,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> ProjectModelPriceResponse:
    try:
        provider = get_provider(provider).name
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Provider not found.") from exc
    price = database.scalar(
        select(ProjectModelPrice)
        .where(
            ProjectModelPrice.project_id == project.id,
            ProjectModelPrice.provider == provider,
            ProjectModelPrice.model == request.model,
        )
        .with_for_update()
    )
    if price is None:
        price = ProjectModelPrice(
            project_id=project.id,
            provider=provider,
            model=request.model,
            input_usd_per_1m_tokens=request.input_usd_per_1m_tokens,
            output_usd_per_1m_tokens=request.output_usd_per_1m_tokens,
            is_active=True,
        )
        database.add(price)
    else:
        price.input_usd_per_1m_tokens = request.input_usd_per_1m_tokens
        price.output_usd_per_1m_tokens = request.output_usd_per_1m_tokens
        price.is_active = True
    database.commit()
    database.refresh(price)
    SecurityAuditService.record(
        database,
        project_id=project.id,
        request_id=None,
        correlation_id=get_correlation_id(),
        event_type="model_pricing_updated",
        severity="info",
        action="update",
        provider=provider,
        model=request.model,
    )
    return _price_response(price)


@router.delete(
    "/pricing/{provider}/{model:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_project_pricing(
    provider: str,
    model: str,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> None:
    price = database.scalar(
        select(ProjectModelPrice)
        .where(
            ProjectModelPrice.project_id == project.id,
            ProjectModelPrice.provider == provider.lower(),
            ProjectModelPrice.model == model,
            ProjectModelPrice.is_active.is_(True),
        )
        .with_for_update()
    )
    if price is None:
        raise HTTPException(status_code=404, detail="Project model price not found.")
    price.is_active = False
    database.commit()
    SecurityAuditService.record(
        database,
        project_id=project.id,
        request_id=None,
        correlation_id=get_correlation_id(),
        event_type="model_pricing_disabled",
        severity="info",
        action="disable",
        provider=price.provider,
        model=price.model,
    )


@router.get("/{provider}/models")
async def list_models(
    provider: str,
    credential_id: int | None = Query(default=None, ge=1),
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> dict[str, Any]:
    try:
        spec = get_provider(provider)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Provider not found.") from exc
    provider = spec.name
    credentials = CredentialService()
    key = spec.api_key
    try:
        selected = (
            credentials.get_by_id(database, credential_id, project_id=project.id)
            if credential_id
            else credentials.get_default_credential(
                database, provider=provider, project_id=project.id
            )
        )
    except CredentialNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Credential not found.") from exc
    if selected:
        if selected.provider != provider or not selected.is_active:
            raise HTTPException(
                status_code=400, detail="Credential does not match this provider."
            )
        _, key = credentials.decrypt_api_key(
            database, credential_id=selected.id, project_id=project.id
        )
    if provider != "ollama" and not key:
        raise HTTPException(status_code=400, detail="Add a provider credential first.")
    headers = {"Authorization": f"Bearer {key}"}
    endpoint = spec.base_url.rstrip("/") + "/models"
    if provider == "anthropic":
        endpoint = spec.base_url.rstrip("/") + "/v1/models"
        headers = {
            "x-api-key": key or "",
            "anthropic-version": get_settings().anthropic_version,
        }
    elif provider == "ollama":
        endpoint = spec.base_url.rstrip("/") + "/api/tags"
        headers = {}
    try:
        async with httpx.AsyncClient(
            timeout=get_settings().credential_test_timeout_seconds,
            follow_redirects=False,
        ) as client:
            response = await client.get(endpoint, headers=headers)
        if response.status_code >= 300:
            raise HTTPException(
                status_code=502,
                detail=f"Provider model discovery returned HTTP {response.status_code}; specify a model manually if discovery is unsupported.",
            )
        data = response.json()
        raw_models = data.get("data") or data.get("models") or []
        if not isinstance(raw_models, list):
            raise TypeError("Provider model list is invalid.")
        pricing = PricingService()
        return {
            "provider": provider,
            "models": [
                {
                    "id": str(item.get("id") or item.get("name")),
                    "pricing_configured": pricing.get_model_price(
                        provider,
                        str(item.get("id") or item.get("name")),
                        database=database,
                        project_id=project.id,
                    )
                    is not None,
                }
                for item in raw_models[:1000]
                if isinstance(item, dict) and (item.get("id") or item.get("name"))
            ],
        }
    except (httpx.RequestError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=502, detail="Provider model discovery is unavailable."
        ) from exc
