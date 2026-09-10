import json
from typing import Any

from sqlalchemy.orm import Session

from .models import AuditLog


def record(
    db: Session,
    *,
    tenant_id: int | None,
    action: str,
    entity_type: str,
    entity_id: str | int | None = None,
    user_id: int | None = None,
    actor: str = "system",
    detail: Any = None,
    ip_address: str | None = None,
) -> AuditLog:
    """Append an audit entry. Callers commit as part of their own transaction."""
    log = AuditLog(
        tenant_id=tenant_id,
        user_id=user_id,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        detail=json.dumps(detail, default=str) if detail is not None else None,
        ip_address=ip_address,
    )
    db.add(log)
    return log
