"""Bot handlers: approval cards, owner commands. Business logic runs in sync services in a thread."""

import asyncio
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.core.db import sync_session
from app.core.numbers import parse_amount
from app.models import Listing, Thread, User
from app.models import Message as Msg
from app.models.enums import ListingStatus, MessageDirection, MessageStatus
from app.services import settings_store as ss
from app.services.audit import audit_sync
from app.services.cards import card_keyboard, card_text, needs_double_confirmation, short_hash
from app.services.listings import ManualListingInput, create_manual_listing
from app.services.proposal_doc import money
from app.services.proposals import ProposalError, approve, postpone, reject, update_text

router = Router()

STATUS_NAMES = {
    ListingStatus.FOUND: "Найдены",
    ListingStatus.SHORTLISTED: "Отобраны",
    ListingStatus.PROPOSAL_PENDING: "КП на согласовании",
    ListingStatus.PROPOSAL_SENT: "КП отправлено",
    ListingStatus.NEGOTIATION: "Переговоры",
    ListingStatus.CONTRACT: "Договор",
    ListingStatus.WON: "Выиграны",
    ListingStatus.LOST: "Проиграны",
    ListingStatus.REJECTED: "Отклонены",
    ListingStatus.EXCLUDED: "Исключены",
}


class Edit(StatesGroup):
    price = State()
    text = State()


async def in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await asyncio.to_thread(fn, *args, **kwargs)


def _user_id(db, tg_id: int) -> int | None:
    return db.execute(select(User.id).where(User.telegram_id == tg_id)).scalar_one_or_none()


def _card(message_id: int, confirm: bool = False) -> tuple[str, dict] | None:
    with sync_session() as db:
        message = db.get(Msg, message_id)
        if message is None:
            return None
        return card_text(db, message), card_keyboard(db, message, confirm=confirm)


def _check_hash(message_id: int, h: str) -> str | None:
    """Return an error if the card is stale or the message is no longer awaiting approval."""
    with sync_session() as db:
        message = db.get(Msg, message_id)
        if message is None:
            return "Письмо не найдено"
        if message.status != MessageStatus.PENDING_APPROVAL:
            return f"Письмо уже обработано (статус: {message.status})"
        if short_hash(message) != h:
            return "stale"
        return None


def _approve(message_id: int, h: str, tg_id: int, confirmed: bool) -> tuple[str, Decimal | None]:
    with sync_session() as db:
        message = db.get(Msg, message_id, with_for_update=True)
        if message is None or short_hash(message) != h:
            raise ProposalError("Черновик изменился. Открываю актуальную версию.")
        if not confirmed and needs_double_confirmation(db, message):
            return "confirm", None
        result = approve(db, message_id, user_id=_user_id(db, tg_id), channel="bot", confirmed=confirmed)
        db.commit()
        return result.outcome.value, result.amount


async def _send_card(target: Message, message_id: int) -> None:
    card = await in_thread(_card, message_id)
    if card:
        await target.answer(card[0], parse_mode="HTML", reply_markup=card[1], disable_web_page_preview=True)


@router.callback_query(F.data.startswith("a:"))
async def on_card_action(cb: CallbackQuery, state: FSMContext) -> None:
    try:
        _, mid_raw, action, h = cb.data.split(":", 3)
        mid = int(mid_raw)
    except ValueError:
        await cb.answer("Некорректная кнопка")
        return
    problem = await in_thread(_check_hash, mid, h)
    if problem == "stale":
        await cb.answer("Черновик изменился")
        await _send_card(cb.message, mid)
        return
    if problem:
        await cb.answer(problem, show_alert=True)
        return

    if action in ("ok", "ok2"):
        try:
            outcome, amount = await in_thread(_approve, mid, h, cb.from_user.id, action == "ok2")
        except ProposalError as exc:
            await cb.answer(str(exc), show_alert=True)
            return
        if outcome == "confirm":
            card = await in_thread(_card, mid, True)
            await cb.message.edit_reply_markup(reply_markup=card[1])
            await cb.answer("Крупная сумма: подтвердите ещё раз")
            return
        from app.worker.tasks_sales import send_message_task

        send_message_task.delay(mid)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(
            f"✅ Согласовано{(' на ' + money(amount)) if amount else ''}. Письмо поставлено в очередь."
        )
        await cb.answer("Согласовано")
    elif action == "back":
        card = await in_thread(_card, mid)
        await cb.message.edit_reply_markup(reply_markup=card[1])
        await cb.answer()
    elif action == "price":
        await state.set_state(Edit.price)
        await state.update_data(message_id=mid)
        await cb.message.answer("Введите новую сумму КП в рублях, например: 1450000\n/cancel — отмена")
        await cb.answer()
    elif action == "text":
        await state.set_state(Edit.text)
        await state.update_data(message_id=mid)
        await cb.message.answer("Пришлите новый текст письма целиком одним сообщением.\n/cancel — отмена")
        await cb.answer()
    elif action == "later":
        await in_thread(_postpone, mid, cb.from_user.id)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("⏸ Отложено на сутки. Напомню.")
        await cb.answer()
    elif action == "no":
        await in_thread(_reject, mid, cb.from_user.id)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("❌ Отклонено. Заявка перенесена в «Отклонены».")
        await cb.answer()
    else:
        await cb.answer("Неизвестное действие")


def _postpone(mid: int, tg_id: int) -> None:
    with sync_session() as db:
        postpone(db, mid, user_id=_user_id(db, tg_id), channel="bot", hours=24)
        db.commit()


def _reject(mid: int, tg_id: int) -> None:
    with sync_session() as db:
        reject(db, mid, user_id=_user_id(db, tg_id), channel="bot", reason="отклонено в боте")
        db.commit()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


def _listing_for_message(mid: int) -> int | None:
    with sync_session() as db:
        message = db.get(Msg, mid)
        thread = db.get(Thread, message.thread_id) if message else None
        return thread.listing_id if thread else None


@router.message(Edit.price)
async def on_new_price(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_amount(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    await state.clear()
    listing_id = await in_thread(_listing_for_message, data["message_id"])
    from app.worker.tasks_sales import prepare_proposal_task

    prepare_proposal_task.delay(listing_id, str(amount))
    await message.answer(f"Пересчитываю КП на {money(amount)}. Новая карточка придёт через минуту.")


def _update_text(mid: int, text: str, tg_id: int) -> None:
    with sync_session() as db:
        update_text(db, mid, body=text, user_id=_user_id(db, tg_id), actor="bot")
        db.commit()


@router.message(Edit.text)
async def on_new_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < 20:
        await message.answer("Слишком короткий текст. Пришлите письмо целиком или /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    try:
        await in_thread(_update_text, data["message_id"], text, message.from_user.id)
    except ProposalError as exc:
        await message.answer(str(exc))
        return
    await message.answer("Текст обновлён. Проверьте и согласуйте:")
    await _send_card(message, data["message_id"])


def _today() -> str:
    now = datetime.now(UTC)
    with sync_session() as db:
        pending = (
            db.execute(
                select(Msg).where(
                    Msg.direction == MessageDirection.OUTBOUND, Msg.status == MessageStatus.PENDING_APPROVAL
                )
            )
            .scalars()
            .all()
        )
        queued = db.execute(
            select(func.count()).select_from(Msg).where(Msg.status.in_([MessageStatus.APPROVED, MessageStatus.QUEUED]))
        ).scalar_one()
        failed = db.execute(
            select(func.count()).select_from(Msg).where(Msg.status == MessageStatus.FAILED)
        ).scalar_one()
        deadlines = (
            db.execute(
                select(Listing)
                .where(
                    Listing.deadline_at.between(now, now + timedelta(days=2)),
                    Listing.status.notin_([ListingStatus.EXCLUDED, ListingStatus.REJECTED, ListingStatus.LOST]),
                )
                .order_by(Listing.deadline_at)
            )
            .scalars()
            .all()
        )
        rules = ss.load_sync(db, ss.SendingRules)
        lines = ["📅 <b>Сегодня</b>"]
        if rules.paused:
            lines.append("⏸ Отправка на паузе (/resume — возобновить)")
        lines.append(f"На согласовании: {len(pending)}")
        for m in pending[:10]:
            lines.append(f"  • #{m.id} {m.subject or ''}"[:120])
        lines.append(f"В очереди на отправку: {queued}")
        if failed:
            lines.append(f"⚠️ Ошибки отправки: {failed} (см. панель)")
        lines.append(f"Дедлайны в ближайшие 2 дня: {len(deadlines)}")
        for listing in deadlines[:10]:
            lines.append(f"  • {listing.deadline_at:%d.%m %H:%M} {listing.title}"[:120])
    return "\n".join(lines)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    await message.answer(await in_thread(_today), parse_mode=None)


def _pipeline() -> str:
    with sync_session() as db:
        rows = db.execute(select(Listing.status, func.count()).group_by(Listing.status)).all()
    counts = dict(rows)
    lines = ["📊 Воронка"]
    lines += [f"{name}: {counts.get(status, 0)}" for status, name in STATUS_NAMES.items()]
    return "\n".join(lines)


@router.message(Command("pipeline"))
async def cmd_pipeline(message: Message) -> None:
    await message.answer(await in_thread(_pipeline))


def _stats() -> str:
    week = datetime.now(UTC) - timedelta(days=7)
    with sync_session() as db:
        sent = db.execute(
            select(func.count()).select_from(Msg).where(Msg.status == MessageStatus.SENT, Msg.sent_at >= week)
        ).scalar_one()
        found = db.execute(select(func.count()).select_from(Listing).where(Listing.created_at >= week)).scalar_one()
        excluded = db.execute(
            select(func.count())
            .select_from(Listing)
            .where(Listing.created_at >= week, Listing.status == ListingStatus.EXCLUDED)
        ).scalar_one()
    return f"📈 За 7 дней\nНовых заявок: {found} (исключено: {excluded})\nОтправлено писем: {sent}"


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    await message.answer(await in_thread(_stats))


def _set_pause(paused: bool, tg_id: int) -> None:
    with sync_session() as db:
        rules = ss.load_sync(db, ss.SendingRules)
        rules.paused = paused
        user_id = _user_id(db, tg_id)
        ss.save_sync(db, rules, user_id)
        audit_sync(db, "sending_paused" if paused else "sending_resumed", actor="bot", user_id=user_id)
        db.commit()


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    await in_thread(_set_pause, True, message.from_user.id)
    await message.answer("⏸ Вся отправка остановлена. Согласованные письма ждут в очереди. /resume — возобновить.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    await in_thread(_set_pause, False, message.from_user.id)
    await message.answer("▶️ Отправка возобновлена.")


def _add_listing(text: str, tg_id: int) -> tuple[int, str | None]:
    url_match = re.search(r"https?://\S+", text)
    with sync_session() as db:
        data = ManualListingInput(
            title=text[:300], customer_name="Не указан", description=text, url=url_match.group(0) if url_match else None
        )
        listing = create_manual_listing(db, data, user_id=_user_id(db, tg_id), actor="bot")
        db.commit()
        return listing.id, listing.exclusion_reason


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    text = (command.args or "").strip()
    if len(text) < 5:
        await message.answer("Пример: /add https://ссылка-на-заявку или краткое описание заявки")
        return
    listing_id, excluded = await in_thread(_add_listing, text, message.from_user.id)
    if excluded:
        await message.answer(f"Заявка #{listing_id} добавлена, но исключена: {excluded}")
    else:
        await message.answer(
            f"Заявка #{listing_id} добавлена. Заполните заказчика, площадь и e-mail в панели, "
            f"затем нажмите «Рассчитать»."
        )
