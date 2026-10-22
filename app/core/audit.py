from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AuditLog


def record_audit(
    session: Session,
    *,
    actor_id: UUID | None,
    action: str,
    entity_type: str,
    entity_id: UUID | str,
    details: dict | None = None,
    request_id: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            details=details or {},
            request_id=request_id,
        )
    )
