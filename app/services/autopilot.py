"""Autopilot for a new suitable listing: documents -> analysis -> estimate -> КП draft for approval.

    found listing ─► open its page, download ТЗ/documentation (public files only)
                 ─► read text + documents, extract fields, risks, questions to the customer
                 ─► area known? ─► estimate ─► КП version + e-mail draft ─► Telegram card «Согласовать»
                              └─► no: ask the owner (and list questions for the customer)

Nothing leaves the system here: the draft waits for the owner's approval like any other proposal.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html import escape

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models import Listing
from app.models.enums import ListingStatus
from app.services import settings_store as ss
from app.services.calculator import EstimateInput, PriceTableData
from app.services.listings import active_price_table, add_email_channel, get_or_create_org, listing_email
from app.services.tender_docs import CollectReport, collect_documents

log = get_logger(__name__)

# Section codes used in analysis prompts -> codes of the price table.
SECTION_CODES = {
    "АР": "AR",
    "ЭОМ": "EOM",
    "ЭМ": "EOM",
    "ВК": "VK",
    "ОВИК": "OVIK",
    "ОВ": "OVIK",
    "СС": "SS",
    "ТХ": "TX",
    "ПБ": "PB",
    "ПОС": "POS",
    "ООС": "OOS",
    "ОДИ": "ODI",
    "СМ": "SM",
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PROCESSABLE = {ListingStatus.FOUND, ListingStatus.SHORTLISTED}


@dataclass
class AutopilotResult:
    listing_id: int
    skipped: str | None = None
    documents: CollectReport | None = None
    analysis: dict | None = None
    message_id: int | None = None  # КП draft waiting for approval
    proposal_error: str | None = None
    missing_for_proposal: list[str] = field(default_factory=list)


def _confident(fields: dict, name: str, threshold: float = 0.5):
    item = fields.get(name) or {}
    return item.get("value") if item.get("confidence", 0) >= threshold else None


def estimate_input(listing: Listing, fields: dict, table: PriceTableData) -> EstimateInput | None:
    if not listing.area_m2:
        return None
    sections = None
    raw = _confident(fields, "sections")
    if isinstance(raw, list):
        codes = []
        for item in raw:
            code = SECTION_CODES.get(str(item).upper().replace(" ", ""))
            if code and code in {s.code for s in table.sections} and code not in codes:
                codes.append(code)
        sections = codes if len(codes) >= 2 else None
    object_type = listing.object_type if listing.object_type in table.object_types else None
    return EstimateInput(area_m2=listing.area_m2, object_type=object_type, sections=sections, region=listing.region)


def _attach_customer(db: Session, listing: Listing, fields: dict) -> None:
    """Customer name and the e-mail published in the tender documents (source kept for 152-ФЗ)."""
    customer = _confident(fields, "customer", 0.6)
    if listing.organization_id is None and isinstance(customer, str) and len(customer) > 3:
        listing.organization_id = get_or_create_org(db, customer[:500], None).id
    if listing.organization_id is None or listing_email(db, listing):
        return
    contacts = fields.get("contacts") or {}
    match = EMAIL_RE.search(str(contacts.get("value") or ""))
    if match:
        from app.models import Organization

        org = db.get(Organization, listing.organization_id)
        add_email_channel(db, org, match.group(0), listing.url or f"документация заявки #{listing.id}")


def process_listing(db: Session, listing_id: int, http: httpx.Client, *, force: bool = False) -> AutopilotResult:
    from app.services.analysis import analyze_listing
    from app.services.listings import latest_estimate, make_estimate
    from app.services.proposals import ProposalError, pending_message, prepare_proposal

    result = AutopilotResult(listing_id)
    listing = db.get(Listing, listing_id)
    if listing is None:
        result.skipped = "заявка не найдена"
        return result
    if listing.status not in PROCESSABLE and not (force and listing.status != ListingStatus.EXCLUDED):
        result.skipped = f"статус {listing.status}"
        return result
    auto = ss.load_sync(db, ss.Automation)

    if auto.download_documents:
        result.documents = collect_documents(db, listing, http)
        db.flush()
    if not (auto.auto_analyze or force):
        return result
    result.analysis = analyze_listing(db, listing_id)
    fields = result.analysis.get("fields", {})
    _attach_customer(db, listing, fields)
    db.flush()

    if not auto.auto_proposal or listing.status == ListingStatus.EXCLUDED:
        return result
    if (listing.score or 0) < auto.min_score_for_proposal:
        result.missing_for_proposal.append(f"оценка {listing.score} ниже порога {auto.min_score_for_proposal}")
        return result
    if pending_message(db, listing_id) is not None and not force:
        return result  # a draft already waits for the owner
    table = PriceTableData.model_validate(active_price_table(db).data)
    inputs = estimate_input(listing, fields, table)
    if inputs is None:
        result.missing_for_proposal.append("площадь объекта")
        return result
    estimate = latest_estimate(db, listing_id)
    if estimate is None or force:
        make_estimate(db, listing, inputs)
    try:
        message = prepare_proposal(db, listing_id, actor="autopilot")
        result.message_id = message.id
    except ProposalError as exc:
        result.proposal_error = str(exc)
    return result


def summary_text(db: Session, result: AutopilotResult) -> str:
    """Telegram (HTML) summary: link to the tender, what was read, key facts, what is missing."""
    listing = db.get(Listing, result.listing_id)
    public_url = get_settings().public_url
    lines = [f"🧾 <b>Заявка #{listing.id}</b>: {escape(listing.title[:200])}"]
    if listing.url:
        lines.append(f'🔗 <a href="{escape(listing.url, quote=True)}">Открыть заявку на площадке</a>')
    if public_url.startswith("https://"):
        lines.append(f'🌐 <a href="{public_url}/listings/{listing.id}">Карточка в панели</a>')
    docs = result.documents
    if docs:
        if docs.saved:
            lines.append(f"📎 Скачано документов: {len(docs.saved)} ({escape(', '.join(docs.saved[:5]))[:300]})")
        elif docs.needs_login:
            lines.append(
                "🔒 Документация доступна только после входа на площадку. Скачайте ТЗ и пришлите файл боту "
                f"с подписью <b>#{listing.id}</b> — разберу и посчитаю КП."
            )
        else:
            lines.append("📎 Файлов документации на странице не найдено — разбор по тексту заявки.")
    if result.analysis:
        fields = result.analysis.get("fields", {})
        facts = []
        for name, title in (
            ("customer", "Заказчик"),
            ("object_type", "Объект"),
            ("address", "Адрес"),
            ("area_m2", "Площадь, м²"),
            ("sections", "Разделы"),
            ("deadline", "Срок подачи"),
            ("work_duration", "Срок работ"),
            ("payment_terms", "Оплата"),
        ):
            value = (fields.get(name) or {}).get("value")
            if value not in (None, "", []):
                text = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
                facts.append(f"• {title}: {escape(text[:150])}")
        if facts:
            lines.append("\n".join(facts))
        risks = [f["title"] for f in result.analysis.get("findings", []) if f.get("level") == "high"]
        if risks:
            lines.append("⚠️ Риски: " + escape(", ".join(risks[:5])))
        if listing.score is not None:
            lines.append(f"Оценка: {listing.score}/100")
        questions = fields.get("questions") or []
        if questions:
            lines.append("❓ Уточнить у заказчика:\n" + "\n".join(f"– {escape(q)}" for q in questions[:7]))
    if result.message_id:
        lines.append("📋 КП рассчитано — карточка на согласование ниже.")
    elif result.missing_for_proposal:
        need = ", ".join(result.missing_for_proposal)
        lines.append(
            f"✋ Для КП не хватает: {escape(need)}. Ответьте боту, например: <b>#{listing.id} 450</b> (площадь, м²) "
            "— или укажите в панели."
        )
    elif result.proposal_error:
        lines.append(f"✋ КП не подготовлено: {escape(result.proposal_error)}")
    return "\n".join(lines)[:4000]


AREA_REPLY_RE = re.compile(r"^\s*#(\d+)\s+(?:площадь\s*)?(\d[\d\s]*(?:[.,]\d+)?)\s*(?:м2|м²|кв\.?\s*м)?\s*$", re.I)


def parse_area_reply(text: str) -> tuple[int, Decimal] | None:
    """«#12 450» or «#12 площадь 450 м2» -> (12, 450)."""
    match = AREA_REPLY_RE.match(text or "")
    if not match:
        return None
    area = parse_area(match.group(2))
    return (int(match.group(1)), area) if area else None


def parse_area(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    return value if value > 0 else None
