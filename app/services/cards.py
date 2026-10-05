"""Approval cards for Telegram: what will be sent, where to, from which address, for how much."""

from decimal import Decimal
from html import escape

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Estimate, Listing, Message, Organization, ProposalVersion, Thread
from app.services import settings_store as ss
from app.services.proposal_doc import money
from app.services.proposals import message_amount, payload_hash, version_evaluation, version_sections

SCENARIO_NAMES = {"min": "мин", "base": "база", "max": "макс"}


def short_hash(message: Message) -> str:
    return payload_hash(message)[:12]


def _short(name: str) -> str:
    """«Архитектурные решения (АР)» -> «АР» to keep the card compact."""
    if name.endswith(")") and "(" in name:
        return name[name.rindex("(") + 1 : -1]
    return name[:40]


def card_text(db: Session, message: Message) -> str:
    thread = db.get(Thread, message.thread_id)
    listing = db.get(Listing, thread.listing_id) if thread and thread.listing_id else None
    org = db.get(Organization, message.organization_id) if message.organization_id else None
    version = db.get(ProposalVersion, message.proposal_version_id) if message.proposal_version_id else None
    estimate = db.get(Estimate, version.estimate_id) if version and version.estimate_id else None
    amount = message_amount(db, message)
    lines = ["📋 <b>КП на согласование</b>"]
    if listing:
        lines.append(f"<b>{escape(listing.title)}</b>")
        if listing.url:
            lines.append(f'🔗 <a href="{escape(listing.url, quote=True)}">Заявка на площадке</a>')
    if org:
        lines.append(f"Заказчик: {escape(org.name)}")
    if listing:
        if listing.area_m2:
            lines.append(f"Площадь: {listing.area_m2:,.0f} м²".replace(",", " "))
        if listing.region or listing.address:
            lines.append(f"Адрес: {escape(listing.address or listing.region or '')}")
        if listing.deadline_at:
            lines.append(f"Срок подачи: {listing.deadline_at:%d.%m.%Y %H:%M}")
        if listing.score is not None:
            lines.append(f"Оценка: {listing.score}/100")
    if amount is not None:
        lines.append(f"\n💰 <b>Сумма КП: {money(amount)}</b>" + (f" (версия {version.version})" if version else ""))
    if version:
        for item in version_sections(version):
            lines.append(
                f"  {escape(item['code'])} {escape(_short(item['name']))}: {money(Decimal(str(item['price'])))}"
            )
        evaluation = version_evaluation(db, version)
        if evaluation:
            lines.append(f"При этой сумме: прибыль {money(evaluation.profit)} · маржа {evaluation.margin:.0%}")
            if evaluation.below_minimum:
                lines.append("⚠️ Ниже минимально допустимой цены для этого набора разделов")
    if estimate:
        parts = []
        for key in ("min", "base", "max"):
            sc = estimate.scenarios.get(key)
            if sc:
                parts.append(f"{SCENARIO_NAMES[key]} {money(Decimal(sc['price']))} · маржа {Decimal(sc['margin']):.0%}")
        lines.append("Сценарии: " + "; ".join(parts))
        for warning in estimate.breakdown.get("warnings", []):
            lines.append(f"⚠️ {escape(warning)}")
    lines.append(f"\n✉️ Кому: {escape(message.to_addr or '— не указан —')}")
    lines.append(f"От: {escape(message.from_addr or '— почта не настроена —')}")
    lines.append(f"Тема: {escape(message.subject or '')}")
    if message.attachments:
        lines.append("Вложение: " + ", ".join(escape(a["name"]) for a in message.attachments))
    body = message.body_text or ""
    preview = body if len(body) <= 900 else body[:900] + "…"
    lines.append(f"\n<blockquote expandable>{escape(preview)}</blockquote>")
    if message.error:
        lines.append(f"ℹ️ {escape(message.error)}")
    return "\n".join(lines)[:4000]


def card_keyboard(db: Session, message: Message, *, confirm: bool = False) -> dict:
    h = short_hash(message)
    mid = message.id
    if confirm:
        amount = message_amount(db, message)
        rows = [
            [
                {
                    "text": f"✅ Да, отправить {money(amount) if amount else ''}".strip(),
                    "callback_data": f"a:{mid}:ok2:{h}",
                }
            ],
            [{"text": "↩️ Назад", "callback_data": f"a:{mid}:back:{h}"}],
        ]
    else:
        rows = [
            [
                {"text": "✅ Согласовать", "callback_data": f"a:{mid}:ok:{h}"},
                {"text": "✏️ Изменить сумму", "callback_data": f"a:{mid}:price:{h}"},
            ],
            [{"text": "🧮 Цены по разделам", "callback_data": f"a:{mid}:sections:{h}"}],
            [
                {"text": "📝 Редактировать текст", "callback_data": f"a:{mid}:text:{h}"},
                {"text": "⏸ Отложить", "callback_data": f"a:{mid}:later:{h}"},
            ],
            [{"text": "❌ Отклонить", "callback_data": f"a:{mid}:no:{h}"}],
        ]
    public_url = get_settings().public_url
    thread = db.get(Thread, message.thread_id)
    if public_url.startswith("https://") and thread and thread.listing_id:
        rows.append([{"text": "🌐 Открыть в панели", "url": f"{public_url}/listings/{thread.listing_id}"}])
    return {"inline_keyboard": rows}


def needs_double_confirmation(db: Session, message: Message) -> bool:
    amount = message_amount(db, message)
    return amount is not None and amount >= ss.load_sync(db, ss.SendingRules).double_confirm_from
