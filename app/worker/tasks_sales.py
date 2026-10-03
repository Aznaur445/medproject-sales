"""Celery tasks for the proposal pipeline."""

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.db import sync_session
from app.core.logging import get_logger
from app.models import CalendarEvent, Listing, Message
from app.models.enums import MessageDirection, MessageStatus
from app.services import mailer, storage
from app.services.cards import card_keyboard, card_text
from app.services.notify import alert, send_owner_message, telegram_call, telegram_send_document
from app.services.proposals import ProposalError, pending_message, prepare_proposal
from app.worker.celery_app import celery

log = get_logger(__name__)


def send_card(message_id: int) -> None:
    with sync_session() as db:
        message = db.get(Message, message_id)
        if message is None or message.status != MessageStatus.PENDING_APPROVAL:
            return
        text, keyboard = card_text(db, message), card_keyboard(db, message)
        attachments = list(message.attachments or [])
    for attachment in attachments:  # the owner sees exactly the file that will be sent
        telegram_send_document(storage.path_for(attachment["key"]), attachment["name"])
    telegram_call(
        "sendMessage", {"text": text, "parse_mode": "HTML", "reply_markup": keyboard, "disable_web_page_preview": True}
    )


@celery.task(bind=True, max_retries=3, default_retry_delay=60)
def prepare_proposal_task(
    self,
    listing_id: int,
    price: str | None = None,
    user_id: int | None = None,
    section_prices: dict[str, str] | None = None,
) -> int | None:
    from decimal import Decimal

    try:
        with sync_session() as db:
            message = prepare_proposal(
                db,
                listing_id,
                Decimal(price) if price else None,
                user_id=user_id,
                actor="worker",
                section_prices={k: Decimal(v) for k, v in section_prices.items()} if section_prices else None,
            )
            db.commit()
            message_id = message.id
    except ProposalError as exc:
        send_owner_message(f"⚠️ КП по заявке #{listing_id} не пересчитано: {exc}")
        return None
    except Exception as exc:
        log.exception("prepare_proposal_failed", listing_id=listing_id)
        if self.request.retries >= self.max_retries:
            alert("Не удалось подготовить КП", f"Заявка #{listing_id}: {type(exc).__name__}")
            return None
        raise self.retry(exc=exc) from exc
    send_card(message_id)
    return message_id


@celery.task
def send_message_task(message_id: int) -> str:
    with sync_session() as db:
        result = mailer.send_message(db, message_id)
    if result.outcome == mailer.SendOutcome.SENT:
        send_owner_message(f"✅ Письмо #{message_id} отправлено.")
    elif result.outcome == mailer.SendOutcome.FAILED:
        alert("Письмо не отправлено", f"#{message_id}: {result.reason}")
    elif result.outcome == mailer.SendOutcome.DEFERRED:
        send_owner_message(
            f"⏳ Письмо #{message_id} в очереди: {result.reason}",
            dedup_key=f"deferred:{message_id}:{result.reason}",
            dedup_seconds=6 * 3600,
        )
    return result.outcome.value


@celery.task
def dispatch_queued() -> int:
    """Retry approved/queued messages (limits, pause, mail outages). Runs every 5 minutes."""
    with sync_session() as db:
        ids = (
            db.execute(
                select(Message.id)
                .where(
                    Message.direction == MessageDirection.OUTBOUND,
                    Message.status.in_([MessageStatus.APPROVED, MessageStatus.QUEUED]),
                )
                .order_by(Message.id)
            )
            .scalars()
            .all()
        )
    for message_id in ids:
        send_message_task.delay(message_id)
    return len(ids)


@celery.task
def due_reminders() -> int:
    """Owner reminders (postponed approvals and others). Runs every 5 minutes."""
    now = datetime.now(UTC)
    sent = 0
    with sync_session() as db:
        events = (
            db.execute(
                select(CalendarEvent).where(
                    CalendarEvent.kind == "reminder",
                    CalendarEvent.starts_at <= now,
                )
            )
            .scalars()
            .all()
        )
        for event in events:
            if event.reminders_sent:
                continue
            event.reminders_sent = [now.isoformat()]
            db.commit()
            message = pending_message(db, event.listing_id) if event.listing_id else None
            if message and message.status == MessageStatus.PENDING_APPROVAL:
                send_card(message.id)
            else:
                listing = db.get(Listing, event.listing_id) if event.listing_id else None
                send_owner_message(f"⏰ {event.title}" + (f": {listing.title}" if listing else ""))
            sent += 1
    return sent


@celery.task
def send_card_task(message_id: int) -> None:
    send_card(message_id)
