"""Run a source: connector -> ingest, with retries handled by the scheduler and a circuit breaker (F1, §9)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Source
from app.models.enums import SourceLegalStatus
from app.services.audit import audit_sync
from app.services.ingest import IngestStats, ingest
from app.sources import SourceError, get_connector

log = get_logger(__name__)
FAILURES_BEFORE_OPEN = 3
MAX_PAUSE = timedelta(hours=24)


@dataclass
class RunResult:
    source_id: int
    ok: bool
    stats: IngestStats | None = None
    error: str | None = None
    circuit_opened: bool = False
    skipped: str | None = None


def is_due(source: Source, now: datetime) -> bool:
    if not source.enabled or source.legal_status != SourceLegalStatus.ALLOWED:
        return False
    if source.circuit_open_until and source.circuit_open_until > now:
        return False
    if source.last_run_at is None:
        return True
    return now - source.last_run_at >= timedelta(minutes=source.schedule_minutes)


def due_sources(db: Session, now: datetime | None = None) -> list[int]:
    now = now or datetime.now(UTC)
    sources = db.execute(select(Source).where(Source.enabled.is_(True))).scalars().all()
    return [s.id for s in sources if get_connector(s.connector).runnable and is_due(s, now)]


def run_source(
    db: Session, source_id: int, *, http: httpx.Client | None = None, now: datetime | None = None
) -> RunResult:
    now = now or datetime.now(UTC)
    source = db.get(Source, source_id, with_for_update=True)
    if source is None:
        return RunResult(source_id, ok=False, skipped="not found")
    if source.circuit_open_until and source.circuit_open_until > now:
        return RunResult(source_id, ok=False, skipped="circuit open")
    connector = get_connector(source.connector)
    source.last_run_at = now
    db.commit()
    own_client = http is None
    client = http or httpx.Client(timeout=30)
    try:
        items = list(connector.run(source, client))
        stats = ingest(db, source, items)
        source.last_success_at = now
        source.consecutive_failures = 0
        source.circuit_open_until = None
        source.last_error = None
        db.commit()
        connector.after_ingest(source)
        log.info("source_ok", source_id=source_id, seen=stats.seen, new=len(stats.new_relevant))
        return RunResult(source_id, ok=True, stats=stats)
    except Exception as exc:  # noqa: BLE001 - every failure counts towards the breaker
        db.rollback()
        source = db.get(Source, source_id)
        message = str(exc) if isinstance(exc, SourceError) else f"{type(exc).__name__}: {str(exc)[:200]}"
        source.consecutive_failures += 1
        source.last_error = message[:2000]
        opened = False
        if source.consecutive_failures >= FAILURES_BEFORE_OPEN:
            pause = min(timedelta(minutes=30) * 2 ** (source.consecutive_failures - FAILURES_BEFORE_OPEN), MAX_PAUSE)
            source.circuit_open_until = now + pause
            opened = True
        audit_sync(
            db,
            "source_failed",
            entity_type="source",
            entity_id=source_id,
            details={"error": message[:300], "failures": source.consecutive_failures},
        )
        db.commit()
        log.warning("source_failed", source_id=source_id, error=message[:200])
        return RunResult(source_id, ok=False, error=message, circuit_opened=opened)
    finally:
        if own_client:
            client.close()
