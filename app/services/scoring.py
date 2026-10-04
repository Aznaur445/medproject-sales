"""Listing score 0–100 with an explanation «why 78» (F4). Weights are editable in the panel."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Listing
from app.models.enums import ListingStatus
from app.services import settings_store as ss
from app.services.cases import match_cases
from app.services.listings import latest_estimate


def _component(name: str, value: float, weight: int, note: str) -> dict:
    value = max(0.0, min(1.0, value))
    return {"name": name, "value": round(value, 2), "weight": weight, "points": round(value * weight, 1), "note": note}


def score_listing(db: Session, listing: Listing, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    w = ss.load_sync(db, ss.Scoring)
    caps = ss.load_sync(db, ss.Capabilities)
    filters = ss.load_sync(db, ss.Filters)
    parts = []

    fx = (listing.score_explanation or {}).get("filters", {})
    hits = len(fx.get("work", [])) + len(fx.get("object", []))
    parts.append(
        _component(
            "Соответствие профилю",
            hits / 5 if fx else 0.5,
            w.w_relevance,
            f"совпадений с профилем: {hits}" if fx else "заявка добавлена вручную",
        )
    )

    estimate = latest_estimate(db, listing.id)
    if estimate is not None:
        margin = Decimal(str(estimate.scenarios.get("base", {}).get("margin", 0)))
        parts.append(
            _component("Маржинальность", float(margin) / 0.4, w.w_margin, f"маржа при рекомендуемой цене {margin:.0%}")
        )
    else:
        parts.append(_component("Маржинальность", 0.5, w.w_margin, "расчёт ещё не сделан"))

    if listing.deadline_at:
        days = (listing.deadline_at - now).total_seconds() / 86400
        value = 0.0 if days < 1 else (0.5 if days < 3 else 1.0)
        note = "срок прошёл" if days < 0 else f"до срока подачи {days:.0f} дн."
    else:
        value, note = 0.7, "срок подачи не указан"
    parts.append(_component("Срок подачи", value, w.w_deadline, note))

    findings = (listing.extracted or {}).get("findings", [])
    reqs = [f for f in findings if f["kind"] == "requirement"]
    missing = [f["title"] for f in reqs if f["level"] == "missing"]
    high_risks = [f["title"] for f in findings if f["kind"] == "risk" and f["level"] == "high"]
    req_value = 1.0 - 0.5 * len(missing) - 0.25 * len(high_risks)
    note = ("не хватает: " + ", ".join(missing)) if missing else "требования выполнимы"
    if high_risks:
        note += "; высокие риски: " + ", ".join(high_risks)
    if not findings:
        req_value, note = 0.7, "анализ документации не проводился"
    parts.append(_component("Требования и риски", req_value, w.w_requirements, note))

    cases = match_cases(db, listing, limit=3)
    parts.append(
        _component(
            "Похожие кейсы",
            len(cases) / 3,
            w.w_cases,
            f"подходящих кейсов: {len(cases)}" if cases else "похожих кейсов в базе нет",
        )
    )

    if listing.organization_id:
        history = dict(
            db.execute(
                select(Listing.status, func.count())
                .where(Listing.organization_id == listing.organization_id, Listing.id != listing.id)
                .group_by(Listing.status)
            ).all()
        )
        won, lost = history.get(ListingStatus.WON, 0), history.get(ListingStatus.LOST, 0)
        value = 0.6 + 0.2 * won - 0.1 * lost
        note = f"история с заказчиком: выиграно {won}, проиграно {lost}" if won or lost else "новый заказчик"
    else:
        value, note = 0.5, "заказчик не определён"
    if caps.can_travel is False and listing.address:
        value -= 0.2
    parts.append(_component("Заказчик", value, w.w_customer, note))

    if listing.budget:
        ok = (filters.budget_min or 0) <= listing.budget <= (filters.budget_max or Decimal("1e12"))
        value = 1.0 if ok else 0.2
        if estimate is not None and listing.budget < estimate.min_price:
            value, note = 0.1, "бюджет ниже минимальной цены по расчёту"
        else:
            note = f"бюджет {int(listing.budget):,} ₽".replace(",", " ")
    else:
        value, note = 0.6, "бюджет не указан"
    parts.append(_component("Бюджет", value, w.w_budget, note))

    total_weight = sum(p["weight"] for p in parts) or 1
    score = round(100 * sum(p["points"] for p in parts) / total_weight)
    explanation = {**(listing.score_explanation or {}), "components": parts, "scored_at": now.isoformat()}
    listing.score = score
    listing.score_explanation = explanation
    return explanation
