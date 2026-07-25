import json

from sqlalchemy.orm import Session

from app.database.models import (
    SecurityAuditEvent,
)


class SecurityAuditService:
    @staticmethod
    def record(
        database: Session,
        *,
        project_id: int | None,
        request_id: str | None,
        correlation_id: str | None,
        event_type: str,
        severity: str,
        action: str,
        provider: str | None = None,
        model: str | None = None,
        risk_score: float | None = None,
        detected_categories: list[str] | None = None,
        sanitized_summary: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> SecurityAuditEvent:
        event = SecurityAuditEvent(
            project_id=project_id,
            request_id=request_id,
            correlation_id=correlation_id,
            event_type=event_type,
            severity=severity,
            action=action,
            provider=provider,
            model=model,
            risk_score=risk_score,
            detected_categories_json=(
                json.dumps(detected_categories) if detected_categories else None
            ),
            sanitized_summary=sanitized_summary,
            metadata_json=(json.dumps(metadata) if metadata else None),
        )

        database.add(event)
        database.commit()
        database.refresh(event)

        return event
