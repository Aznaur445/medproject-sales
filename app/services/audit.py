from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import AuditLog

log = get_logger(__name__)


def _entry(
    action: str,
    actor: str,
    user_id: int | None,
    entity_type: str | None,
    entity_id: int | None,
    details: dict[str, Any] | None,
    ip: str | None,
) -> AuditLog:
    log.info("audit", action=action, actor=actor, user_id=user_id, entity_type=entity_type, entity_id=entity_id)
    return AuditLog(
        action=action,
        actor=actor,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details or {},
        ip=ip,
    )


async def audit(
    db: AsyncSession,
    action: str,
    *,
    actor: str = "web",
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """Adds an audit entry to the session. The caller commits."""
    db.add(_entry(action, actor, user_id, entity_type, entity_id, details, ip))


def audit_sync(
    db: Session,
    action: str,
    *,
    actor: str = "worker",
    user_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(_entry(action, actor, user_id, entity_type, entity_id, details, None))
