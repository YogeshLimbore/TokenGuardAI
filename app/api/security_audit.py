import json

from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.connection import (
    get_database_session,
)
from app.database.models import (
    SecurityAuditEvent,
)
from app.dependencies.admin_authentication import (
    require_admin,
)

router = APIRouter(
    prefix="/v1/admin/security-events",
    tags=["Admin Security"],
    dependencies=[
        Depends(require_admin),
    ],
)


@router.get("")
async def list_security_events(
    project_id: int | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
    ),
    database: Session = Depends(get_database_session),
) -> list[dict[str, object]]:
    statement = select(SecurityAuditEvent)

    if project_id is not None:
        statement = statement.where(SecurityAuditEvent.project_id == project_id)

    if severity:
        statement = statement.where(SecurityAuditEvent.severity == severity)

    if event_type:
        statement = statement.where(SecurityAuditEvent.event_type == event_type)

    statement = statement.order_by(SecurityAuditEvent.created_at.desc()).limit(limit)

    rows = database.scalars(statement).all()

    return [
        {
            "id": row.id,
            "project_id": row.project_id,
            "request_id": row.request_id,
            "correlation_id": row.correlation_id,
            "event_type": row.event_type,
            "severity": row.severity,
            "action": row.action,
            "provider": row.provider,
            "model": row.model,
            "risk_score": row.risk_score,
            "detected_categories": (
                json.loads(row.detected_categories_json)
                if row.detected_categories_json
                else []
            ),
            "sanitized_summary": (row.sanitized_summary),
            "created_at": row.created_at,
        }
        for row in rows
    ]
