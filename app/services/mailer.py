"""Outbound e-mail via SMTP (Mail.ru / VK WorkSpace by default).

Guarantees:
* only approved messages whose content still matches the approved payload are sent;
* at most once: status SENDING is committed before talking to SMTP; a message found in SENDING
  (crash mid-send) is never re-sent automatically — the owner is asked to check the mailbox;
* limits: global pause, opt-out registry, sending window, daily limit with warm-up, 1 letter per
  organisation per N days (replies to the customer's own letters are exempt).
"""

import smtplib
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models import Approval, Listing, Message, OptOut, Proposal, ProposalVersion, Thread
from app.models.enums import ApprovalDecision, ListingStatus, MessageDirection, MessageStatus, ProposalStatus
from app.services import settings_store as ss
from app.services import storage
from app.services.audit import audit_sync
from app.services.proposals import payload_hash

log = get_logger(__name__)


class SendOutcome(StrEnum):
    SENT = "sent"
    ALREADY_SENT = "already_sent"
    DEFERRED = "deferred"  # will be retried by the dispatcher
    BLOCKED = "blocked"  # needs the owner (no approval, opt-out, bad address)
    FAILED = "failed"


@dataclass
class SendResult:
    outcome: SendOutcome
    reason: str = ""


SmtpFactory = Callable[[Settings], smtplib.SMTP]


def default_smtp(settings: Settings) -> smtplib.SMTP:
    if settings.smtp_ssl:
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=60, context=ssl.create_default_context()
        )
    else:
        client = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
        if settings.smtp_starttls:
            client.starttls(context=ssl.create_default_context())
    client.ehlo_or_helo_if_needed()
    if client.has_extn("auth"):
        password = settings.mail_app_password.get_secret_value() if settings.mail_app_password else ""
        client.login(settings.mail_user or "", password)
    return client


def normalize_email(value: str) -> str:
    return value.strip().lower()


def is_opted_out(db: Session, email: str, organization_id: int | None) -> bool:
    email = normalize_email(email)
    domain = email.rsplit("@", 1)[-1]
    q = select(OptOut.id).where(OptOut.value.in_([email, domain]))
    if organization_id is not None:
        q = select(OptOut.id).where((OptOut.value.in_([email, domain])) | (OptOut.organization_id == organization_id))
    return db.execute(q.limit(1)).first() is not None


def add_optout(
    db: Session,
    value: str,
    *,
    kind: str = "email",
    source: str = "manual",
    reason: str = "",
    organization_id: int | None = None,
) -> OptOut:
    value = normalize_email(value)
    existing = db.execute(select(OptOut).where(OptOut.value == value)).scalar_one_or_none()
    if existing:
        return existing
    row = OptOut(value=value, kind=kind, source=source, reason=reason, organization_id=organization_id)
    db.add(row)
    # Stop everything not yet sent to this recipient.
    pending = db.execute(
        select(Message).where(
            Message.direction == MessageDirection.OUTBOUND,
            Message.status.in_([MessageStatus.PENDING_APPROVAL, MessageStatus.APPROVED, MessageStatus.QUEUED]),
        )
    ).scalars()
    for message in pending:
        to = normalize_email(message.to_addr or "")
        if to == value or to.endswith("@" + value) or (organization_id and message.organization_id == organization_id):
            message.status = MessageStatus.CANCELLED
            message.error = "получатель отказался от писем"
    db.flush()
    return row


def daily_limit(db: Session, rules: ss.SendingRules, now: datetime) -> int:
    if not rules.warmup_enabled:
        return rules.daily_limit
    first = db.execute(
        select(func.min(Message.sent_at)).where(
            Message.direction == MessageDirection.OUTBOUND, Message.status == MessageStatus.SENT
        )
    ).scalar_one()
    days = 0 if first is None else (now.date() - first.date()).days
    return min(rules.daily_limit, rules.warmup_start + rules.warmup_step * days)


def _local_day_start(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def check_limits(db: Session, message: Message, rules: ss.SendingRules, now: datetime) -> str | None:
    """Return a reason to defer, or None if the message may go now."""
    tz = ZoneInfo(get_settings().timezone)
    local = now.astimezone(tz)
    if rules.work_days_only and local.weekday() >= 5:
        return "выходной день: отправка в рабочие дни"
    if not (rules.send_window_start_hour <= local.hour < rules.send_window_end_hour):
        return f"вне окна отправки {rules.send_window_start_hour}:00–{rules.send_window_end_hour}:00 МСК"
    sent_today = db.execute(
        select(func.count())
        .select_from(Message)
        .where(
            Message.direction == MessageDirection.OUTBOUND,
            Message.status == MessageStatus.SENT,
            Message.sent_at >= _local_day_start(now, tz),
        )
    ).scalar_one()
    limit = daily_limit(db, rules, now)
    if sent_today >= limit:
        return f"дневной лимит {limit} писем исчерпан"
    if message.organization_id is not None:
        last_out = db.execute(
            select(func.max(Message.sent_at)).where(
                Message.organization_id == message.organization_id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.status == MessageStatus.SENT,
                Message.id != message.id,
            )
        ).scalar_one()
        if last_out is not None and now - last_out < timedelta(days=rules.org_interval_days):
            last_in = db.execute(
                select(func.max(Message.received_at)).where(
                    Message.thread_id == message.thread_id, Message.direction == MessageDirection.INBOUND
                )
            ).scalar_one()
            if last_in is None or last_in < last_out:  # not a reply to the customer
                return f"этой организации уже писали за последние {rules.org_interval_days} дн."
    return None


def build_email(message: Message, settings: Settings) -> EmailMessage:
    email = EmailMessage()
    email["From"] = formataddr((settings.mail_from_name, message.from_addr or ""))
    email["To"] = message.to_addr or ""
    email["Subject"] = message.subject or ""
    email["Date"] = format_datetime(datetime.now(UTC))
    email["Message-ID"] = message.email_message_id or make_msgid()
    if message.in_reply_to:
        email["In-Reply-To"] = message.in_reply_to
    if message.references:
        email["References"] = message.references
    email["List-Unsubscribe"] = f"<mailto:{message.from_addr}?subject=STOP>"
    email.set_content(message.body_text or "")
    for attachment in message.attachments or []:
        data = storage.read_bytes(attachment["key"])
        name = attachment["name"]
        maintype, subtype = (
            ("application", "pdf")
            if name.lower().endswith(".pdf")
            else ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
        )
        email.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return email


def _approval_valid(db: Session, message: Message) -> bool:
    if message.approval_id is None:
        return False
    approval = db.get(Approval, message.approval_id)
    return (
        approval is not None
        and approval.decision == ApprovalDecision.APPROVED
        and approval.entity_type == "message"
        and approval.entity_id == message.id
        and approval.payload_hash == payload_hash(message)
    )


def send_message(
    db: Session, message_id: int, *, smtp_factory: SmtpFactory = default_smtp, now: datetime | None = None
) -> SendResult:
    """Send one approved message. Commits its own state transitions."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    message = db.get(Message, message_id, with_for_update=True)
    if message is None:
        return SendResult(SendOutcome.BLOCKED, "письмо не найдено")
    if message.status == MessageStatus.SENT:
        db.commit()
        return SendResult(SendOutcome.ALREADY_SENT)
    if message.status == MessageStatus.SENDING:
        message.status = MessageStatus.FAILED
        message.error = "Отправка была прервана. Проверьте папку «Отправленные» и при необходимости повторите."
        db.commit()
        return SendResult(SendOutcome.FAILED, message.error)
    if message.direction != MessageDirection.OUTBOUND or message.status not in (
        MessageStatus.APPROVED,
        MessageStatus.QUEUED,
    ):
        db.commit()
        return SendResult(SendOutcome.BLOCKED, f"статус {message.status}")
    if not _approval_valid(db, message):
        message.status = MessageStatus.PENDING_APPROVAL
        message.approval_id = None
        message.error = "нет действующего согласования"
        db.commit()
        log.warning("send_blocked_no_approval", message_id=message_id)
        return SendResult(SendOutcome.BLOCKED, message.error)
    if not message.to_addr or not message.from_addr:
        message.status = MessageStatus.PENDING_APPROVAL
        message.error = "не указан получатель или отправитель"
        db.commit()
        return SendResult(SendOutcome.BLOCKED, message.error)
    if is_opted_out(db, message.to_addr, message.organization_id):
        message.status = MessageStatus.CANCELLED
        message.error = "получатель в реестре отказов"
        db.commit()
        return SendResult(SendOutcome.BLOCKED, message.error)
    rules = ss.load_sync(db, ss.SendingRules)
    if rules.paused:
        reason = "отправка на паузе"
    elif not settings.mail_app_password:
        reason = "почта не настроена (MAIL_APP_PASSWORD)"
    else:
        reason = check_limits(db, message, rules, now)
    if reason:
        message.status = MessageStatus.QUEUED
        message.error = reason
        db.commit()
        return SendResult(SendOutcome.DEFERRED, reason)

    domain = message.from_addr.rsplit("@", 1)[-1]
    message.email_message_id = message.email_message_id or f"<mp-{message.id}-{uuid.uuid4().hex[:12]}@{domain}>"
    message.idempotency_key = message.idempotency_key or f"message:{message.id}"
    message.status = MessageStatus.SENDING
    message.send_attempts += 1
    message.error = None
    db.commit()  # point of no automatic return: from here the message is never re-sent blindly

    try:
        email = build_email(message, settings)
        client = smtp_factory(settings)  # connect + login: nothing handed over yet, safe to retry
    except Exception as exc:  # noqa: BLE001
        message.status = MessageStatus.QUEUED
        message.error = f"почтовый сервер недоступен: {type(exc).__name__}"
        db.commit()
        log.warning("smtp_connect_failed", message_id=message.id, error=type(exc).__name__)
        return SendResult(SendOutcome.DEFERRED, message.error)
    try:
        client.send_message(email)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        return _fail(db, message, f"сервер отклонил письмо: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - outcome unknown: never resend automatically
        return _fail(db, message, f"отправка прервана ({type(exc).__name__}). Проверьте папку «Отправленные».")
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001,S110
            pass

    message.status = MessageStatus.SENT
    message.sent_at = now
    _mark_proposal_sent(db, message)
    audit_sync(
        db,
        "message_sent",
        actor="worker",
        entity_type="message",
        entity_id=message.id,
        details={"to_domain": message.to_addr.rsplit("@", 1)[-1]},
    )
    db.commit()
    log.info("message_sent", message_id=message.id)
    return SendResult(SendOutcome.SENT)


def _fail(db: Session, message: Message, reason: str) -> SendResult:
    message.status = MessageStatus.FAILED
    message.error = reason
    db.commit()
    log.error("message_failed", message_id=message.id, reason=reason)
    return SendResult(SendOutcome.FAILED, reason)


def _mark_proposal_sent(db: Session, message: Message) -> None:
    if message.proposal_version_id:
        version = db.get(ProposalVersion, message.proposal_version_id)
        proposal = db.get(Proposal, version.proposal_id)
        proposal.status = ProposalStatus.SENT
    thread = db.get(Thread, message.thread_id)
    if thread and thread.listing_id:
        listing = db.get(Listing, thread.listing_id)
        if listing.status in (ListingStatus.FOUND, ListingStatus.SHORTLISTED, ListingStatus.PROPOSAL_PENDING):
            listing.status = ListingStatus.PROPOSAL_SENT


def retry_failed(db: Session, message_id: int, *, user_id: int | None, channel: str) -> Message:
    """Owner checked the mailbox and wants to resend a FAILED message. The approval must still match."""
    message = db.get(Message, message_id, with_for_update=True)
    if message is None or message.status != MessageStatus.FAILED:
        raise ValueError("Повторить можно только письмо со статусом «ошибка»")
    message.status = MessageStatus.APPROVED if _approval_valid(db, message) else MessageStatus.PENDING_APPROVAL
    message.error = None
    message.email_message_id = None
    audit_sync(db, "message_retry", actor=channel, user_id=user_id, entity_type="message", entity_id=message.id)
    return message
