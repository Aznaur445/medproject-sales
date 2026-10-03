"""Proposal workflow: prepare (estimate -> DOCX/PDF -> draft e-mail) and owner approval.

Approval binds to an exact payload (recipient, sender, subject, text, attachments with checksums).
Any change after approval invalidates it, so the sender only ever transmits what the owner saw.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import Approval, CalendarEvent, Listing, Message, Organization, Proposal, ProposalVersion, Thread
from app.models.enums import ApprovalDecision, ListingStatus, MessageDirection, MessageStatus, ProposalStatus
from app.services import settings_store as ss
from app.services import storage
from app.services.audit import audit_sync
from app.services.calculator import EstimateResult, distribute
from app.services.listings import latest_estimate, listing_email
from app.services.proposal_doc import ProposalContent, ProposalSection, docx_to_pdf, money, render_docx

log = get_logger(__name__)


class ProposalError(Exception):
    """Business error shown to the owner as is."""


def sender_address() -> str | None:
    return get_settings().mail_user


def _fmt(text: str, **values: str) -> str:
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text  # owner-edited text with stray braces: use verbatim


def cover_letter(
    listing: Listing, total: Decimal, req: ss.Requisites, defaults: ss.ProposalDefaults, rules: ss.SendingRules
) -> tuple[str, str]:
    object_name = listing.title
    subject = f"Коммерческое предложение: проектирование «{object_name}»"[:250]
    lines = [
        "Здравствуйте!",
        "",
        f"Направляем коммерческое предложение на разработку проектной и рабочей документации по объекту "
        f"«{object_name}»{', ' + listing.address if listing.address else ''}.",
        "",
        f"Стоимость работ: {money(total)}. Срок: {defaults.duration_text}.",
        "Подробный состав разделов, условия оплаты и сроки приведены в приложенном КП.",
        "",
        "Готовы ответить на вопросы, уточнить состав работ под ваше техническое задание и при необходимости "
        "выехать на объект.",
        "",
        _fmt(defaults.signature, brand=req.brand),
        req.legal_name,
        " · ".join(x for x in (req.phone, req.website) if x),
        "",
        "—",
        rules.unsubscribe_text,
    ]
    return subject, "\n".join(lines)


def payload_hash(message: Message) -> str:
    payload = {
        "to": (message.to_addr or "").lower(),
        "from": (message.from_addr or "").lower(),
        "subject": message.subject or "",
        "body": message.body_text or "",
        "attachments": [{"key": a["key"], "sha256": a["sha256"]} for a in message.attachments or []],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _thread(db: Session, listing: Listing) -> Thread:
    thread = db.execute(
        select(Thread).where(Thread.listing_id == listing.id, Thread.channel == "email").limit(1)
    ).scalar_one_or_none()
    if thread is None:
        thread = Thread(listing_id=listing.id, organization_id=listing.organization_id, channel="email")
        db.add(thread)
        db.flush()
    return thread


def pending_message(db: Session, listing_id: int) -> Message | None:
    return db.execute(
        select(Message)
        .join(Thread, Message.thread_id == Thread.id)
        .where(
            Thread.listing_id == listing_id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.status.in_([MessageStatus.PENDING_APPROVAL, MessageStatus.APPROVED, MessageStatus.QUEUED]),
        )
        .order_by(Message.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def prepare_proposal(
    db: Session,
    listing_id: int,
    price: Decimal | None = None,
    user_id: int | None = None,
    actor: str = "system",
    render_pdf: bool = True,
) -> Message:
    """Create a new proposal version for the latest estimate and the draft e-mail awaiting approval."""
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ProposalError("Заявка не найдена")
    if listing.status == ListingStatus.EXCLUDED:
        raise ProposalError(f"Заявка исключена: {listing.exclusion_reason}")
    estimate = latest_estimate(db, listing_id)
    if estimate is None:
        raise ProposalError("Сначала выполните расчёт стоимости")
    result = EstimateResult.model_validate(estimate.breakdown)
    total = Decimal(price) if price is not None else result.recommended_price
    if total <= 0:
        raise ProposalError("Сумма должна быть больше нуля")
    section_prices = distribute(result.sections, total)

    req = ss.load_sync(db, ss.Requisites)
    defaults = ss.load_sync(db, ss.ProposalDefaults)
    rules = ss.load_sync(db, ss.SendingRules)
    org = db.get(Organization, listing.organization_id) if listing.organization_id else None
    customer = org.name if org else "Заказчик"
    content = ProposalContent(
        customer_name=customer,
        object_name=listing.title,
        address=listing.address or "",
        area_m2=listing.area_m2,
        intro=_fmt(defaults.intro_text, brand=req.brand, object=listing.title),
        sections=[
            ProposalSection(code=s.code, name=s.name, description=s.description, stage=s.stage, price=p)
            for s, p in zip(result.sections, section_prices, strict=True)
        ],
        total=total,
        duration=defaults.duration_text,
        payment_options=defaults.payment_options,
        vat_note=defaults.vat_note,
        quality=defaults.quality_text,
        advantages=defaults.advantages,
        signature=_fmt(defaults.signature, brand=req.brand),
        valid_until=date.today() + timedelta(days=defaults.validity_days),
        requisites=req,
    )

    proposal = db.execute(select(Proposal).where(Proposal.listing_id == listing_id)).scalar_one_or_none()
    if proposal is None:
        proposal = Proposal(listing_id=listing_id, estimate_id=estimate.id, current_version=0)
        db.add(proposal)
        db.flush()
    proposal.current_version += 1
    proposal.estimate_id = estimate.id
    proposal.status = ProposalStatus.PENDING_APPROVAL
    version_no = proposal.current_version

    base = f"proposals/{listing_id}/v{version_no}"
    filename = storage.safe_name(f"КП_{req.brand}_{customer}")
    docx = render_docx(content)
    docx_key = storage.save_bytes(f"{base}/{filename}.docx", docx)
    pdf_key = None
    if render_pdf:
        try:
            pdf_key = storage.save_bytes(f"{base}/{filename}.pdf", docx_to_pdf(docx))
        except Exception:  # noqa: BLE001 - the owner can still review/send the DOCX
            log.exception("pdf_conversion_failed", listing_id=listing_id)
    subject, body = cover_letter(listing, total, req, defaults, rules)
    version = ProposalVersion(
        proposal_id=proposal.id,
        version=version_no,
        price=total,
        content=content.model_dump(mode="json"),
        cover_letter=body,
        docx_key=docx_key,
        pdf_key=pdf_key,
        estimate_id=estimate.id,
        created_by_id=user_id,
    )
    db.add(version)
    db.flush()

    attachment_key = pdf_key or docx_key
    attachments = [
        {"key": attachment_key, "name": attachment_key.rsplit("/", 1)[-1], "sha256": storage.sha256_of(attachment_key)}
    ]
    message = pending_message(db, listing_id)
    if message is None:
        thread = _thread(db, listing)
        thread.subject = subject
        message = Message(
            thread_id=thread.id,
            direction=MessageDirection.OUTBOUND,
            channel="email",
            organization_id=listing.organization_id,
        )
        db.add(message)
    message.status = MessageStatus.PENDING_APPROVAL
    message.approval_id = None
    message.from_addr = sender_address()
    message.to_addr = listing_email(db, listing)
    message.subject = subject
    message.body_text = body
    message.attachments = attachments
    message.proposal_version_id = version.id
    message.error = None
    listing.status = ListingStatus.PROPOSAL_PENDING
    db.flush()
    audit_sync(
        db,
        "proposal_prepared",
        actor=actor,
        user_id=user_id,
        entity_type="listing",
        entity_id=listing_id,
        details={"version": version_no, "price": str(total), "message_id": message.id},
    )
    return message


def update_text(
    db: Session,
    message_id: int,
    *,
    body: str | None = None,
    subject: str | None = None,
    to_addr: str | None = None,
    user_id: int | None = None,
    actor: str = "web",
) -> Message:
    message = _editable(db, message_id)
    if body is not None:
        if not body.strip():
            raise ProposalError("Текст письма пустой")
        message.body_text = body.strip()
    if subject is not None:
        message.subject = subject.strip()
    if to_addr is not None:
        message.to_addr = to_addr.strip().lower() or None
    message.status = MessageStatus.PENDING_APPROVAL
    message.approval_id = None  # any edit requires a fresh approval
    audit_sync(db, "message_edited", actor=actor, user_id=user_id, entity_type="message", entity_id=message.id)
    return message


def _editable(db: Session, message_id: int) -> Message:
    message = db.get(Message, message_id, with_for_update=True)
    if message is None:
        raise ProposalError("Письмо не найдено")
    if message.status not in (MessageStatus.PENDING_APPROVAL, MessageStatus.APPROVED, MessageStatus.QUEUED):
        raise ProposalError("Это письмо уже отправлено или отменено")
    return message


class ApproveOutcome(StrEnum):
    APPROVED = "approved"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass
class ApproveResult:
    outcome: ApproveOutcome
    message: Message
    amount: Decimal | None


def message_amount(db: Session, message: Message) -> Decimal | None:
    if message.proposal_version_id is None:
        return None
    version = db.get(ProposalVersion, message.proposal_version_id)
    return version.price if version else None


def approve(
    db: Session,
    message_id: int,
    *,
    user_id: int | None,
    channel: str,
    confirmed: bool = False,
    expected_hash: str | None = None,
) -> ApproveResult:
    """Owner's «Согласовано». Large amounts need `confirmed=True` (second step).

    `expected_hash` is the payload hash the owner was shown; if the draft changed meanwhile, approval fails.
    """
    message = _editable(db, message_id)
    if message.status != MessageStatus.PENDING_APPROVAL:
        raise ProposalError("Письмо уже согласовано")
    if not message.to_addr:
        raise ProposalError("Не указан адрес получателя")
    if not message.from_addr:
        raise ProposalError("Не настроена почта отправителя (MAIL_USER в .env)")
    current = payload_hash(message)
    if expected_hash is not None and expected_hash != current:
        raise ProposalError("Черновик изменился, пока вы его смотрели. Откройте актуальную версию.")
    amount = message_amount(db, message)
    rules = ss.load_sync(db, ss.SendingRules)
    if amount is not None and amount >= rules.double_confirm_from and not confirmed:
        return ApproveResult(ApproveOutcome.NEEDS_CONFIRMATION, message, amount)
    approval = Approval(
        entity_type="message",
        entity_id=message.id,
        decision=ApprovalDecision.APPROVED,
        user_id=user_id,
        channel=channel,
        amount=amount,
        payload_hash=current,
        payload_snapshot={
            "to": message.to_addr,
            "from": message.from_addr,
            "subject": message.subject,
            "attachments": message.attachments,
        },
        double_confirmed=confirmed,
    )
    db.add(approval)
    db.flush()
    message.approval_id = approval.id
    message.status = MessageStatus.APPROVED
    if message.proposal_version_id:
        version = db.get(ProposalVersion, message.proposal_version_id)
        proposal = db.get(Proposal, version.proposal_id)
        proposal.status = ProposalStatus.APPROVED
    audit_sync(
        db,
        "message_approved",
        actor=channel,
        user_id=user_id,
        entity_type="message",
        entity_id=message.id,
        details={"amount": str(amount) if amount is not None else None, "double_confirmed": confirmed},
    )
    return ApproveResult(ApproveOutcome.APPROVED, message, amount)


def reject(db: Session, message_id: int, *, user_id: int | None, channel: str, reason: str = "") -> Message:
    message = _editable(db, message_id)
    db.add(
        Approval(
            entity_type="message",
            entity_id=message.id,
            decision=ApprovalDecision.REJECTED,
            user_id=user_id,
            channel=channel,
            amount=message_amount(db, message),
            payload_hash=payload_hash(message),
            payload_snapshot={"reason": reason},
        )
    )
    message.status = MessageStatus.CANCELLED
    message.approval_id = None
    thread = db.get(Thread, message.thread_id)
    if thread and thread.listing_id:
        listing = db.get(Listing, thread.listing_id)
        listing.status = ListingStatus.REJECTED
        listing.lost_reason = reason or "отклонено владельцем"
        proposal = db.execute(select(Proposal).where(Proposal.listing_id == listing.id)).scalar_one_or_none()
        if proposal:
            proposal.status = ProposalStatus.REJECTED
    audit_sync(
        db,
        "message_rejected",
        actor=channel,
        user_id=user_id,
        entity_type="message",
        entity_id=message.id,
        details={"reason": reason},
    )
    return message


def postpone(db: Session, message_id: int, *, user_id: int | None, channel: str, hours: int = 24) -> Message:
    message = _editable(db, message_id)
    until = datetime.now(UTC) + timedelta(hours=hours)
    db.add(
        Approval(
            entity_type="message",
            entity_id=message.id,
            decision=ApprovalDecision.POSTPONED,
            user_id=user_id,
            channel=channel,
            amount=message_amount(db, message),
            payload_hash=payload_hash(message),
            payload_snapshot={"until": until.isoformat()},
        )
    )
    message.status = MessageStatus.PENDING_APPROVAL
    message.approval_id = None
    thread = db.get(Thread, message.thread_id)
    db.add(
        CalendarEvent(
            title="Вернуться к согласованию КП",
            kind="reminder",
            starts_at=until,
            listing_id=thread.listing_id if thread else None,
            remind_before_minutes=[0],
            reminders_sent=[],
        )
    )
    audit_sync(
        db,
        "message_postponed",
        actor=channel,
        user_id=user_id,
        entity_type="message",
        entity_id=message.id,
        details={"hours": hours},
    )
    return message


def latest_version_for(db: Session, message: Message) -> ProposalVersion | None:
    return db.get(ProposalVersion, message.proposal_version_id) if message.proposal_version_id else None
