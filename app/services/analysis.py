"""Listing analysis (F6): structured data from the request and attached documents.

Rule-based extraction always runs; when an AI provider is configured its answer is merged in (higher
confidence wins). Each field keeps confidence and where it was found.
"""

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.llm import LLMError, get_provider
from app.models import Listing, ListingDocument
from app.services import settings_store as ss
from app.services import storage
from app.services.documents import Chunk, as_text, extract
from app.services.risks import analyze as analyze_risks

log = get_logger(__name__)

SECTION_CODES = ["АР", "КР", "ОВиК", "ЭОМ", "ВК", "СС", "ТХ", "ПОС", "ПБ", "ООС", "ОДИ", "СМ"]
STAGE_WORDS = {
    "ПД": r"\bПД\b|проектн\w+\s+(?:и\s+рабоч\w+\s+)?документац",
    "РД": r"\bРД\b|рабоч\w+\s+документац",
    "обмеры": r"обмер",
    "обследование": r"обследовани",
    "авторский надзор": r"авторск\w+\s+надзор",
    "экспертиза": r"экспертиз",
}
OBJECT_TYPES = {
    "Стоматология": r"стоматолог",
    "Лаборатория": r"лаборатор",
    "Диагностический центр": r"диагност|МРТ|КТ\b",
    "Поликлиника": r"поликлиник",
    "Больница / стационар": r"больниц|стационар|госпитал",
    "Медицинский центр / клиника": r"клиник|медицинск\w+\s+центр|медцентр",
}


class Field_(BaseModel):
    value: Any = None
    confidence: float = 0.0
    source: str | None = None


class Extraction(BaseModel):
    customer: Field_ = Field(default_factory=Field_)
    object_name: Field_ = Field(default_factory=Field_)
    address: Field_ = Field(default_factory=Field_)
    object_type: Field_ = Field(default_factory=Field_)
    area_m2: Field_ = Field(default_factory=Field_)
    floors: Field_ = Field(default_factory=Field_)
    stages: Field_ = Field(default_factory=Field_)
    sections: Field_ = Field(default_factory=Field_)
    deadline: Field_ = Field(default_factory=Field_)
    work_duration: Field_ = Field(default_factory=Field_)
    payment_terms: Field_ = Field(default_factory=Field_)
    penalties: Field_ = Field(default_factory=Field_)
    requirements: Field_ = Field(default_factory=Field_)
    selection_criteria: Field_ = Field(default_factory=Field_)
    contacts: Field_ = Field(default_factory=Field_)
    questions: list[str] = Field(default_factory=list)


FIELD_TITLES = {
    "customer": "Заказчик",
    "object_name": "Объект",
    "address": "Адрес",
    "object_type": "Тип объекта",
    "area_m2": "Площадь, м²",
    "floors": "Этажность",
    "stages": "Стадии",
    "sections": "Разделы",
    "deadline": "Срок подачи",
    "work_duration": "Срок работ",
    "payment_terms": "Оплата",
    "penalties": "Штрафы",
    "requirements": "Требования к исполнителю",
    "selection_criteria": "Критерии выбора",
    "contacts": "Контакты",
}


def _where(chunks: list[Chunk], pattern: str) -> tuple[re.Match | None, str | None]:
    for chunk in chunks:
        match = re.search(pattern, chunk.text, re.IGNORECASE)
        if match:
            quote = " ".join(chunk.text[max(0, match.start() - 40) : match.end() + 40].split())
            return match, f"{chunk.file}, {chunk.location}: «{quote}»"
    return None, None


def rule_based(chunks: list[Chunk]) -> Extraction:
    ex = Extraction()
    match, src = _where(chunks, r"(?:общ\w+\s+)?площад\w*[^\d]{0,40}(\d[\d\s]*[.,]?\d*)\s*(?:м2|м²|кв\.?\s*м)")
    if not match:
        match, src = _where(chunks, r"(\d[\d\s]*[.,]?\d*)\s*(?:м2|м²|кв\.?\s*м)")
    if match:
        try:
            ex.area_m2 = Field_(
                value=float(re.sub(r"\s", "", match.group(1)).replace(",", ".")), confidence=0.6, source=src
            )
        except ValueError:
            pass
    match, src = _where(chunks, r"(\d{1,2})\s*(?:-?х\s*)?этаж")
    if match:
        ex.floors = Field_(value=int(match.group(1)), confidence=0.5, source=src)
    found_sections, src_s = [], None
    for code in SECTION_CODES:
        match, src = _where(chunks, rf"(?<![А-Яа-яA-Za-z]){re.escape(code)}(?![А-Яа-яA-Za-z])")
        if match:
            found_sections.append(code)
            src_s = src_s or src
    if found_sections:
        ex.sections = Field_(value=found_sections, confidence=0.6, source=src_s)
    stages, src_st = [], None
    for stage, pattern in STAGE_WORDS.items():
        match, src = _where(chunks, pattern)
        if match:
            stages.append(stage)
            src_st = src_st or src
    if stages:
        ex.stages = Field_(value=stages, confidence=0.5, source=src_st)
    for object_type, pattern in OBJECT_TYPES.items():
        match, src = _where(chunks, pattern)
        if match:
            ex.object_type = Field_(value=object_type, confidence=0.5, source=src)
            break
    match, src = _where(chunks, r"(?:до|не\s+позднее|срок\s+подачи[^\d]{0,20})\s*(\d{2})\.(\d{2})\.(\d{4})")
    if match:
        day, month, year = (int(x) for x in match.groups())
        try:
            ex.deadline = Field_(value=datetime(year, month, day, tzinfo=UTC).isoformat(), confidence=0.5, source=src)
        except ValueError:
            pass
    match, src = _where(chunks, r"аванс\w*[^.]{0,60}")
    if match:
        ex.payment_terms = Field_(value=match.group(0)[:200], confidence=0.4, source=src)
    match, src = _where(chunks, r"(неустойк|пени|штраф)\w*[^.]{0,100}")
    if match:
        ex.penalties = Field_(value=match.group(0)[:200], confidence=0.4, source=src)
    return ex


def merge(base: Extraction, ai: Extraction) -> Extraction:
    data = base.model_dump()
    for name, value in ai.model_dump().items():
        if name == "questions":
            data["questions"] = value or data["questions"]
        elif value.get("value") not in (None, "", []) and value.get("confidence", 0) >= data[name].get("confidence", 0):
            data[name] = value
    return Extraction.model_validate(data)


def questions_for(ex: Extraction) -> list[str]:
    asks = {
        "area_m2": "Уточните, пожалуйста, общую площадь помещений, входящих в проектирование.",
        "sections": "Какие разделы документации требуются (АР, ЭОМ, ВК, ОВиК, СС, ТХ и др.)?",
        "stages": "Нужны ли стадия П (проектная документация) и прохождение экспертизы, или только РД?",
        "object_type": "Какой медицинский профиль у объекта (кабинеты, процедуры, оборудование)?",
        "deadline": "До какой даты принимаются предложения и какие сроки выполнения работ ожидаются?",
        "address": "Уточните адрес объекта (нужно для расчёта выездов на обмеры).",
    }
    return [text for name, text in asks.items() if not getattr(ex, name).value]


def listing_chunks(db: Session, listing: Listing) -> list[Chunk]:
    chunks = [Chunk("заявка", "описание", f"{listing.title}\n{listing.description or ''}")]
    docs = db.execute(select(ListingDocument).where(ListingDocument.listing_id == listing.id)).scalars().all()
    for doc in docs:
        if doc.storage_key:
            try:
                chunks += extract(storage.read_bytes(doc.storage_key), doc.filename)
            except FileNotFoundError:
                log.warning("document_missing", key=doc.storage_key)
    return chunks


def analyze_listing(db: Session, listing_id: int, use_ai: bool = True) -> dict[str, Any]:
    listing = db.get(Listing, listing_id)
    chunks = listing_chunks(db, listing)
    extraction = rule_based(chunks)
    provider = get_provider() if use_ai else None
    ai_error = None
    if provider is not None:
        try:
            ai = provider.complete_json("extract_listing", as_text(chunks), Extraction)
            extraction = merge(extraction, ai)
        except LLMError as exc:
            ai_error = str(exc)
            log.warning("ai_extraction_failed", listing_id=listing_id, error=ai_error)
    if not extraction.questions:
        extraction.questions = questions_for(extraction)
    caps = ss.load_sync(db, ss.Capabilities)
    findings = analyze_risks("\n".join(c.text for c in chunks), caps)
    result = {
        "fields": extraction.model_dump(),
        "findings": findings,
        "analyzed_at": datetime.now(UTC).isoformat(),
        "ai": provider.name if provider else None,
        "ai_error": ai_error,
        "documents": len({c.file for c in chunks}) - 1,
    }
    listing.extracted = result
    # Fill empty listing fields with confident values.
    area = extraction.area_m2
    if listing.area_m2 is None and area.value and area.confidence >= 0.5:
        listing.area_m2 = Decimal(str(area.value))
    if not listing.object_type and extraction.object_type.value and extraction.object_type.confidence >= 0.5:
        listing.object_type = extraction.object_type.value
    if not listing.address and extraction.address.value and extraction.address.confidence >= 0.6:
        listing.address = str(extraction.address.value)[:1000]
    if listing.deadline_at is None and extraction.deadline.value and extraction.deadline.confidence >= 0.5:
        try:
            listing.deadline_at = datetime.fromisoformat(str(extraction.deadline.value))
        except ValueError:
            pass
    complex_markers = (
        "личн\\w+ кабинет",
        "аккредитац",
        "электронн\\w+ площадк",
        "ЭЦП",
        "КЭП",
        "оригинал",
        "конкурсн\\w+ документац",
        "регистрац\\w+ на площадке",
    )
    text = "\n".join(c.text for c in chunks)
    listing.complex_procedure = any(re.search(m, text, re.IGNORECASE) for m in complex_markers)
    db.flush()
    from app.services.scoring import score_listing

    score_listing(db, listing)
    return result
