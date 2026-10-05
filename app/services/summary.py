"""Consolidated analysis of a listing («сводный анализ»): one report over the listing text and ALL its documents.

With an AI provider the report is written by the model (prompts/tender_summary.md) and also gives the niche verdict
(design for a medical organisation or not). Without AI it is assembled from the rule-based extraction, risks and
the strict relevance check, so the owner always gets the same structure.
"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models import Listing, ListingDocument
from app.services.relevance import check_medical_design

log = get_logger(__name__)
RECOMMENDATIONS = ("участвовать", "уточнить", "не участвовать")


class TenderSummary(BaseModel):
    is_medical_design: bool = True
    relevance_reason: str = ""
    summary: str = ""
    scope_of_work: list[str] = Field(default_factory=list)
    participant_requirements: list[str] = Field(default_factory=list)
    submission: str | None = None
    evaluation_criteria: list[str] = Field(default_factory=list)
    deadline_submission: str | None = None
    deadline_works: str | None = None
    payment_terms: str | None = None
    budget: str | None = None
    risks: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    recommendation: str = "уточнить"
    recommendation_reason: str = ""
    documents_reviewed: list[str] = Field(default_factory=list)


def _value(fields: dict, name: str) -> Any:
    return (fields.get(name) or {}).get("value")


def _text(value: Any) -> str | None:
    if value in (None, "", []):
        return None
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def rule_summary(listing: Listing, analysis: dict, documents: list[str]) -> TenderSummary:
    fields = analysis.get("fields", {})
    findings = analysis.get("findings", [])
    niche = check_medical_design(f"{listing.title}\n{listing.description or ''}")
    risks = [f["title"] for f in findings if f.get("kind") == "risk"]
    missing = [f["title"] for f in findings if f.get("kind") == "requirement" and f.get("level") == "missing"]
    requirements = [f["title"] for f in findings if f.get("kind") == "requirement"]
    parts = [f"{listing.title}."]
    for name, title in (("object_type", "Объект"), ("address", "Адрес"), ("area_m2", "Площадь, м²")):
        if text := _text(_value(fields, name)):
            parts.append(f"{title}: {text}.")
    if sections := _text(_value(fields, "sections")):
        parts.append(f"Разделы: {sections}.")
    high = [f for f in findings if f.get("kind") == "risk" and f.get("level") == "high"]
    if not niche.relevant:
        recommendation, why = "не участвовать", niche.reason or "вне профиля"
    elif missing or high:
        recommendation, why = "уточнить", "; ".join((missing + [f["title"] for f in high])[:3])
    else:
        recommendation, why = "участвовать", "профильная заявка, явных препятствий не найдено"
    return TenderSummary(
        is_medical_design=niche.relevant,
        relevance_reason=niche.reason or "проектирование медицинского объекта",
        summary=" ".join(parts),
        scope_of_work=[s for s in [_text(_value(fields, "sections")), _text(_value(fields, "stages"))] if s],
        participant_requirements=requirements,
        submission=_text(_value(fields, "selection_criteria")),
        deadline_submission=_text(_value(fields, "deadline")),
        deadline_works=_text(_value(fields, "work_duration")),
        payment_terms=_text(_value(fields, "payment_terms")),
        budget=f"{listing.budget:,.0f} ₽".replace(",", " ") if listing.budget else None,
        risks=risks,
        questions=list(fields.get("questions") or []),
        recommendation=recommendation,
        recommendation_reason=why,
        documents_reviewed=documents,
    )


def build_summary(db: Session, listing: Listing, analysis: dict, *, use_ai: bool = True) -> dict:
    from app.llm import LLMError, get_provider
    from app.services.analysis import listing_chunks
    from app.services.documents import as_text

    documents = list(
        db.execute(select(ListingDocument.filename).where(ListingDocument.listing_id == listing.id)).scalars()
    )
    summary = rule_summary(listing, analysis, documents)
    source, error = "правила", None
    provider = get_provider() if use_ai else None
    if provider is not None:
        try:
            ai = provider.complete_json("tender_summary", as_text(listing_chunks(db, listing)), TenderSummary)
            if ai.recommendation not in RECOMMENDATIONS:
                ai.recommendation = "уточнить"
            ai.documents_reviewed = ai.documents_reviewed or documents
            ai.questions = ai.questions or summary.questions
            summary, source = ai, provider.name
        except LLMError as exc:
            error = str(exc)[:300]
            log.warning("summary_ai_failed", listing_id=listing.id, error=error)
    data = {**summary.model_dump(), "source": source, "ai_error": error, "made_at": datetime.now(UTC).isoformat()}
    listing.extracted = {**(listing.extracted or {}), "summary": data}  # new dict: JSONB change tracking
    db.flush()
    return data
