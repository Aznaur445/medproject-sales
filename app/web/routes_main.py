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
    return {"status": "ok"}


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
    listings = (await db.execute(select(func.count()).select_from(Listing))).scalar_one()
    pending = (
        await db.execute(
            select(func.count()).select_from(Message).where(Message.status == MessageStatus.PENDING_APPROVAL)
        )
    ).scalar_one()
    queued = (
        await db.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.status.in_([MessageStatus.APPROVED, MessageStatus.QUEUED]))
        )
    ).scalar_one()
    failed = (
        await db.execute(select(func.count()).select_from(Message).where(Message.status == MessageStatus.FAILED))
    ).scalar_one()
    rules = await ss.load(db, ss.SendingRules)
    return render(
        request,
        "dashboard.html",
        user=user,
        listings=listings,
        pending=pending,
        queued=queued,
        failed=failed,
        rules=rules,
        health=await full_health(),
        flash=request.session.pop("flash", None),
    )


@router.get("/audit")
async def audit_page(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(200))).scalars().all()
    return render(request, "audit.html", user=user, rows=rows)
