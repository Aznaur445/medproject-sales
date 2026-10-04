"""Panel: listings, estimates, proposals, approval and sending."""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import sync_session
from app.core.numbers import parse_amount, parse_decimal
from app.models import (
    Approval,
    ContactChannel,
    Estimate,
    Listing,
    ListingDocument,
    ListingVersion,
    Message,
    Organization,
    PriceTable,
    Proposal,
    ProposalVersion,
    Thread,
    User,
)
from app.models.enums import ListingStatus, MessageStatus
from app.services import mailer, storage
from app.services import settings_store as ss
from app.services.analysis import FIELD_TITLES
from app.services.calculator import EstimateInput, EstimateResult, PriceTableData, evaluate_price
from app.services.cases import match_cases
from app.services.listings import (
    ManualListingInput,
    active_price_table,
    create_manual_listing,
    latest_estimate,
    listing_email,
    make_estimate,
)
from app.services.proposal_doc import money
from app.services.proposals import (
    ApproveOutcome,
    ProposalError,
    approve,
    message_amount,
    payload_hash,
    pending_message,
    postpone,
    prepare_proposal,
    reject,
    update_text,
    version_evaluation,
    version_sections,
)
from app.web.auth import current_user, require_owner, verify_csrf
from app.web.templating import render

router = APIRouter(dependencies=[Depends(verify_csrf)])

STATUS_NAMES = {
    "found": "Найдена",
    "shortlisted": "Отобрана",
    "proposal_pending": "КП на согласовании",
    "proposal_sent": "КП отправлено",
    "negotiation": "Переговоры",
    "contract": "Договор",
    "won": "Выиграна",
    "lost": "Проиграна",
    "rejected": "Отклонена",
    "excluded": "Исключена",
}
MESSAGE_STATUS_NAMES = {
    "draft": "черновик",
    "pending_approval": "ждёт согласования",
    "approved": "согласовано, в очереди",
    "queued": "в очереди",
    "sending": "отправляется",
    "sent": "отправлено",
    "failed": "ошибка",
    "bounced": "не доставлено",
    "cancelled": "отменено",
    "received": "получено",
}


async def in_db(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run sync service code (shared with worker and bot) in a thread with its own session."""

    def wrapper() -> Any:
        with sync_session() as db:
            result = fn(db, *args, **kwargs)
            db.commit()
            return result

    return await run_in_threadpool(wrapper)


def to_local(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(ZoneInfo(get_settings().timezone)).strftime("%d.%m.%Y %H:%M")


def _flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"text": text, "kind": kind}


def _back(listing_id: int, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/listings/{listing_id}{anchor}", status_code=303)


# --- listings -------------------------------------------------------------------------------------


@router.get("/listings")
async def listings_page(request: Request, status: str | None = None, user: User = Depends(current_user)):
    def load(db):
        q = select(Listing, Organization.name).outerjoin(Organization, Listing.organization_id == Organization.id)
        if status:
            q = q.where(Listing.status == status)
        else:
            q = q.where(Listing.status != ListingStatus.EXCLUDED)
        return db.execute(q.order_by(Listing.id.desc()).limit(300)).all()

    rows = await in_db(load)
    return render(
        request, "listings.html", user=user, rows=rows, status=status, status_names=STATUS_NAMES, to_local=to_local
    )


@router.get("/listings/new")
async def listing_new_form(request: Request, user: User = Depends(require_owner)):
    table = PriceTableData.model_validate((await in_db(active_price_table)).data)
    return render(request, "listing_new.html", user=user, object_types=list(table.object_types), form={})


@router.post("/listings/new")
async def listing_new(request: Request, user: User = Depends(require_owner)):
    form = dict(await request.form())
    try:
        deadline = form.get("deadline_at") or None
        data = ManualListingInput(
            title=form.get("title", ""),
            customer_name=form.get("customer_name", ""),
            customer_inn=form.get("customer_inn") or None,
            region=form.get("region") or None,
            address=form.get("address") or None,
            object_type=form.get("object_type") or None,
            area_m2=parse_decimal(form.get("area_m2")),
            description=form.get("description") or None,
            url=form.get("url") or None,
            deadline_at=datetime.fromisoformat(deadline).replace(tzinfo=ZoneInfo(get_settings().timezone))
            if deadline
            else None,
            contact_email=form.get("contact_email") or None,
            contact_source=form.get("contact_source") or None,
        )
    except (ValidationError, ValueError) as exc:
        table = PriceTableData.model_validate((await in_db(active_price_table)).data)
        errors = [e["msg"] for e in exc.errors()] if isinstance(exc, ValidationError) else [str(exc)]
        return render(
            request, "listing_new.html", 422, user=user, object_types=list(table.object_types), form=form, errors=errors
        )
    listing = await in_db(lambda db: create_manual_listing(db, data, user_id=user.id))
    if listing.status == ListingStatus.EXCLUDED:
        _flash(request, f"Заявка добавлена, но исключена: {listing.exclusion_reason}", "warn")
    return _back(listing.id)


def _listing_context(db, listing_id: int) -> dict[str, Any]:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Заявка не найдена")
    org = db.get(Organization, listing.organization_id) if listing.organization_id else None
    channels = (
        db.execute(select(ContactChannel).where(ContactChannel.organization_id == listing.organization_id))
        .scalars()
        .all()
        if org
        else []
    )
    table_row = active_price_table(db)
    table = PriceTableData.model_validate(table_row.data)
    estimate = latest_estimate(db, listing_id)
    result = EstimateResult.model_validate(estimate.breakdown) if estimate else None
    proposal = db.execute(select(Proposal).where(Proposal.listing_id == listing_id)).scalar_one_or_none()
    versions = (
        db.execute(
            select(ProposalVersion)
            .where(ProposalVersion.proposal_id == proposal.id)
            .order_by(ProposalVersion.version.desc())
        )
        .scalars()
        .all()
        if proposal
        else []
    )
    draft = pending_message(db, listing_id)
    messages = (
        db.execute(
            select(Message)
            .join(Thread, Message.thread_id == Thread.id)
            .where(Thread.listing_id == listing_id)
            .order_by(Message.id.desc())
        )
        .scalars()
        .all()
    )
    approvals = (
        db.execute(
            select(Approval)
            .where(Approval.entity_type == "message", Approval.entity_id.in_([m.id for m in messages] or [0]))
            .order_by(Approval.id.desc())
        )
        .scalars()
        .all()
    )
    rules = ss.load_sync(db, ss.SendingRules)
    amount = message_amount(db, draft) if draft else None
    current_version = versions[0] if versions else None
    if current_version:
        current_sections = version_sections(current_version)
    elif result:
        current_sections = [{"code": x.code, "name": x.name, "price": str(x.price)} for x in result.sections]
    else:
        current_sections = []
    used = {x["code"] for x in current_sections}
    addable = [x for x in table.sections if x.code not in used]
    inputs = EstimateInput.model_validate(estimate.inputs) if estimate else None
    return {
        "listing": listing,
        "org": org,
        "channels": channels,
        "table": table,
        "table_row": table_row,
        "estimate": estimate,
        "result": result,
        "inputs": inputs,
        "proposal": proposal,
        "versions": versions,
        "draft": draft,
        "draft_hash": payload_hash(draft) if draft else None,
        "draft_amount": amount,
        "needs_confirm": amount is not None and amount >= rules.double_confirm_from,
        "rules": rules,
        "messages": messages,
        "approvals": approvals,
        "contact_email": listing_email(db, listing),
        "requisites_ok": ss.load_sync(db, ss.Requisites).is_complete,
        "versions_of_listing": db.execute(
            select(ListingVersion).where(ListingVersion.listing_id == listing_id).order_by(ListingVersion.version)
        )
        .scalars()
        .all(),
        "documents": db.execute(
            select(ListingDocument).where(ListingDocument.listing_id == listing_id).order_by(ListingDocument.id)
        )
        .scalars()
        .all(),
        "case_matches": match_cases(db, listing),
        "field_titles": FIELD_TITLES,
        "current_sections": current_sections,
        "addable": addable,
        "version_eval": version_evaluation(db, current_version) if current_version else None,
    }


@router.get("/listings/{listing_id}")
async def listing_page(request: Request, listing_id: int, user: User = Depends(current_user)):
    ctx = await in_db(_listing_context, listing_id)
    flash = request.session.pop("flash", None)
    return render(
        request,
        "listing.html",
        user=user,
        flash=flash,
        status_names=STATUS_NAMES,
        message_status_names=MESSAGE_STATUS_NAMES,
        money=money,
        to_local=to_local,
        **ctx,
    )


@router.post("/listings/{listing_id}/estimate")
async def listing_estimate(request: Request, listing_id: int, user: User = Depends(require_owner)):
    form = await request.form()
    try:
        inputs = EstimateInput(
            area_m2=parse_decimal(str(form.get("area_m2") or "")) or Decimal("0"),
            object_type=str(form.get("object_type") or "") or None,
            sections=[str(s) for s in form.getlist("sections")],
            modifiers=[str(m) for m in form.getlist("modifiers")],
            region=str(form.get("region") or "") or None,
            trips=int(str(form.get("trips") or "1")),
        )

        def run(db):
            listing = db.get(Listing, listing_id)
            make_estimate(db, listing, inputs)

        await in_db(run)
        _flash(request, "Расчёт выполнен")
    except (ValidationError, ValueError) as exc:
        msg = "; ".join(e["msg"] for e in exc.errors()) if isinstance(exc, ValidationError) else str(exc)
        _flash(request, f"Ошибка расчёта: {msg}", "bad")
    return _back(listing_id, "#estimate")


@router.get("/listings/{listing_id}/price-check")
async def price_check(request: Request, listing_id: int, price: str = "", user: User = Depends(current_user)):
    """HTMX: instant margin for a price typed by the owner."""

    def run(db):
        estimate = latest_estimate(db, listing_id)
        if estimate is None:
            return None
        table = PriceTableData.model_validate(db.get(PriceTable, estimate.price_table_id).data)
        return evaluate_price(table, estimate.cost_total, parse_amount(price), estimate.min_price)

    try:
        evaluation = await in_db(run)
        error = None
    except ValueError as exc:
        evaluation, error = None, str(exc)
    return render(request, "_margin.html", evaluation=evaluation, error=error, money=money)


@router.post("/listings/{listing_id}/proposal")
async def listing_proposal(request: Request, listing_id: int, user: User = Depends(require_owner)):
    form = await request.form()
    raw = str(form.get("price") or "").strip()
    try:
        section_prices = None
        if form.get("mode") == "sections":
            section_prices = {}
            for key, value in form.multi_items():
                if key.startswith("sp_") and str(value).strip():
                    text = str(value).strip()
                    section_prices[key[3:]] = Decimal("0") if text in {"0", "-"} else parse_amount(text)
        price = parse_amount(raw) if raw and section_prices is None else None
        message = await in_db(
            lambda db: prepare_proposal(
                db, listing_id, price, user_id=user.id, actor="web", section_prices=section_prices
            )
        )
    except (ProposalError, ValueError) as exc:
        _flash(request, str(exc), "bad")
        return _back(listing_id, "#proposal")
    from app.worker.tasks_sales import send_card_task

    send_card_task.delay(message.id)
    _flash(request, "КП подготовлено. Проверьте документ и письмо, затем согласуйте.")
    return _back(listing_id, "#approval")


@router.post("/listings/{listing_id}/status")
async def listing_status(request: Request, listing_id: int, user: User = Depends(require_owner)):
    form = await request.form()
    status = str(form.get("status") or "")
    if status not in STATUS_NAMES:
        raise HTTPException(400, "Неизвестный статус")

    def run(db):
        listing = db.get(Listing, listing_id)
        listing.status = status
        if status == ListingStatus.LOST:
            listing.lost_reason = str(form.get("reason") or "")[:300] or listing.lost_reason
        from app.services.audit import audit_sync

        audit_sync(
            db,
            "listing_status",
            actor="web",
            user_id=user.id,
            entity_type="listing",
            entity_id=listing_id,
            details={"status": status},
        )

    await in_db(run)
    return _back(listing_id)


# --- messages -------------------------------------------------------------------------------------


def _listing_of(db, message_id: int) -> int:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(404, "Письмо не найдено")
    return db.get(Thread, message.thread_id).listing_id


@router.post("/messages/{message_id}/edit")
async def message_edit(request: Request, message_id: int, user: User = Depends(require_owner)):
    form = await request.form()
    listing_id = await in_db(_listing_of, message_id)
    try:
        await in_db(
            lambda db: update_text(
                db,
                message_id,
                body=str(form.get("body") or ""),
                subject=str(form.get("subject") or ""),
                to_addr=str(form.get("to_addr") or ""),
                user_id=user.id,
            )
        )
        _flash(request, "Письмо сохранено. Согласуйте его заново.")
    except ProposalError as exc:
        _flash(request, str(exc), "bad")
    return _back(listing_id, "#approval")


@router.post("/messages/{message_id}/approve")
async def message_approve(request: Request, message_id: int, user: User = Depends(require_owner)):
    form = await request.form()
    listing_id = await in_db(_listing_of, message_id)
    try:
        result = await in_db(
            lambda db: approve(
                db,
                message_id,
                user_id=user.id,
                channel="web",
                confirmed=form.get("confirm") == "1",
                expected_hash=str(form.get("expected_hash") or ""),
            )
        )
    except ProposalError as exc:
        _flash(request, str(exc), "bad")
        return _back(listing_id, "#approval")
    if result.outcome == ApproveOutcome.NEEDS_CONFIRMATION:
        _flash(request, f"Сумма {money(result.amount)}: отметьте «Подтверждаю сумму и адресата».", "warn")
        return _back(listing_id, "#approval")
    from app.worker.tasks_sales import send_message_task

    send_message_task.delay(message_id)
    _flash(request, "Согласовано. Письмо поставлено в очередь отправки.")
    return _back(listing_id, "#approval")


@router.post("/messages/{message_id}/reject")
async def message_reject(request: Request, message_id: int, user: User = Depends(require_owner)):
    form = await request.form()
    listing_id = await in_db(_listing_of, message_id)
    await in_db(lambda db: reject(db, message_id, user_id=user.id, channel="web", reason=str(form.get("reason") or "")))
    _flash(request, "КП отклонено.")
    return _back(listing_id)


@router.post("/messages/{message_id}/postpone")
async def message_postpone(request: Request, message_id: int, user: User = Depends(require_owner)):
    listing_id = await in_db(_listing_of, message_id)
    await in_db(lambda db: postpone(db, message_id, user_id=user.id, channel="web", hours=24))
    _flash(request, "Отложено на сутки, напоминание придёт в Telegram.")
    return _back(listing_id)


@router.post("/messages/{message_id}/retry")
async def message_retry(request: Request, message_id: int, user: User = Depends(require_owner)):
    listing_id = await in_db(_listing_of, message_id)
    try:
        message = await in_db(lambda db: mailer.retry_failed(db, message_id, user_id=user.id, channel="web"))
    except ValueError as exc:
        _flash(request, str(exc), "bad")
        return _back(listing_id)
    if message.status == MessageStatus.APPROVED:
        from app.worker.tasks_sales import send_message_task

        send_message_task.delay(message_id)
        _flash(request, "Повторная отправка поставлена в очередь.")
    else:
        _flash(request, "Письмо изменилось после согласования: согласуйте заново.", "warn")
    return _back(listing_id, "#approval")


@router.get("/files/{key:path}")
async def download(key: str, user: User = Depends(current_user)):
    try:
        path = storage.path_for(key)
    except ValueError as exc:
        raise HTTPException(404) from exc
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=path.name)


@router.get("/estimates/{estimate_id}")
async def estimate_json(estimate_id: int, user: User = Depends(current_user)):
    estimate = await in_db(lambda db: db.get(Estimate, estimate_id))
    if estimate is None:
        raise HTTPException(404)
    return estimate.breakdown
