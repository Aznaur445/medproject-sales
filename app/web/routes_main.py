from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models import AuditLog, Listing, Message, User
from app.models.enums import MessageStatus
from app.services import settings_store as ss
from app.services.health import check_db, full_health
from app.web.auth import current_user
from app.web.templating import render

router = APIRouter()


@router.get("/health/live")
async def live():
    """Liveness: the process answers. Used by Docker healthcheck of the api container."""
    from app.services.updates import current_version

    version = current_version()
    return {"status": "ok", "version": version[:7] if version else None}


@router.get("/health")
async def health():
    """Readiness + components (db, redis, worker/scheduler/bot heartbeats). Used by Uptime Kuma."""
    data = await full_health()
    return JSONResponse(data, status_code=200 if data["status"] == "ok" else 503)


@router.get("/health/db")
async def health_db():
    data = await check_db()
    return JSONResponse(data, status_code=200 if data["ok"] else 503)


@router.get("/")
async def dashboard(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from datetime import UTC, datetime, timedelta

    from app.models import Organization, Source
    from app.models.enums import ListingStatus, SourceLegalStatus
    from app.web.routes_sales import STATUS_NAMES, to_local

    async def count(query) -> int:
        return (await db.execute(query)).scalar_one()

    now = datetime.now(UTC)
    in_work = [ListingStatus.FOUND, ListingStatus.SHORTLISTED, ListingStatus.PROPOSAL_PENDING]
    stats = {
        "new_day": await count(
            select(func.count())
            .select_from(Listing)
            .where(Listing.created_at >= now - timedelta(days=1), Listing.status != ListingStatus.EXCLUDED)
        ),
        "pending": await count(
            select(func.count()).select_from(Message).where(Message.status == MessageStatus.PENDING_APPROVAL)
        ),
        "in_work": await count(select(func.count()).select_from(Listing).where(Listing.status.in_(in_work))),
        "sent_month": await count(
            select(func.count())
            .select_from(Message)
            .where(Message.status == MessageStatus.SENT, Message.sent_at >= now - timedelta(days=30))
        ),
        "excluded_day": await count(
            select(func.count())
            .select_from(Listing)
            .where(Listing.created_at >= now - timedelta(days=1), Listing.status == ListingStatus.EXCLUDED)
        ),
        "queued": await count(
            select(func.count())
            .select_from(Message)
            .where(Message.status.in_([MessageStatus.APPROVED, MessageStatus.QUEUED]))
        ),
        "failed": await count(select(func.count()).select_from(Message).where(Message.status == MessageStatus.FAILED)),
    }
    recent = (
        await db.execute(
            select(Listing, Organization.name)
            .outerjoin(Organization, Organization.id == Listing.organization_id)
            .where(Listing.status != ListingStatus.EXCLUDED, Listing.merged_into_id.is_(None))
            .order_by(Listing.id.desc())
            .limit(8)
        )
    ).all()
    sources = (
        (
            await db.execute(
                select(Source)
                .where(
                    Source.enabled.is_(True),
                    Source.legal_status == SourceLegalStatus.ALLOWED,
                    Source.connector != "manual",  # internal source of hand-added listings
                )
                .order_by(Source.id)
            )
        )
        .scalars()
        .all()
    )

    def minutes_to_next(source: Source) -> int:
        if source.last_run_at is None:
            return 0
        due = source.last_run_at + timedelta(minutes=source.schedule_minutes)
        return max(0, int((due - now).total_seconds() // 60))

    sources = [(source, minutes_to_next(source)) for source in sources]
    rules = await ss.load(db, ss.SendingRules)
    return render(
        request,
        "dashboard.html",
        user=user,
        stats=stats,
        recent=recent,
        sources=sources,
        now=now,
        status_names=STATUS_NAMES,
        to_local=to_local,
        rules=rules,
        health=await full_health(),
        flash=request.session.pop("flash", None),
    )


@router.get("/audit")
async def audit_page(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(200))).scalars().all()
    return render(request, "audit.html", user=user, rows=rows)
