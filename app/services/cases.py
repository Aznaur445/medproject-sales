"""Case studies (F10): pick the 3–5 most relevant own projects for a request and explain why."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseStudy, Listing


@dataclass
class CaseMatch:
    case: CaseStudy
    score: float
    reasons: list[str]

    def line(self) -> str:
        """Customer-facing line for the proposal (customer name only if allowed)."""
        c = self.case
        parts = [c.title]
        if c.area_m2:
            parts.append(f"{int(c.area_m2)} м²")
        if c.year:
            parts.append(str(c.year))
        if c.can_mention_customer and c.customer_name:
            parts.append(f"заказчик {c.customer_name}")
        if c.stages:
            parts.append("стадии " + ", ".join(c.stages))
        if c.sections:
            parts.append("разделы " + ", ".join(c.sections))
        return " · ".join(parts)


def match_cases(db: Session, listing: Listing, limit: int = 5) -> list[CaseMatch]:
    cases = db.execute(select(CaseStudy)).scalars().all()
    wanted_sections = set((listing.extracted or {}).get("fields", {}).get("sections", {}).get("value") or [])
    year_now = datetime.now(UTC).year
    matches = []
    for case in cases:
        score, reasons = 0.0, []
        if listing.object_type and case.object_type == listing.object_type:
            score += 0.4
            reasons.append(f"тот же тип объекта ({case.object_type})")
        if listing.area_m2 and case.area_m2:
            ratio = abs(math.log(float(listing.area_m2) / float(case.area_m2)))
            closeness = max(0.0, 1 - ratio / math.log(4))
            if closeness > 0:
                score += 0.3 * closeness
                if closeness > 0.5:
                    reasons.append(f"близкая площадь ({int(case.area_m2)} м²)")
        if wanted_sections and case.sections:
            overlap = wanted_sections & set(case.sections)
            if overlap:
                score += 0.2 * len(overlap) / len(wanted_sections)
                reasons.append("разделы " + ", ".join(sorted(overlap)))
        if case.year:
            score += 0.1 * max(0.0, 1 - (year_now - case.year) / 10)
        if listing.region and case.region and case.region.lower() in listing.region.lower():
            score += 0.05
            reasons.append("тот же регион")
        if score > 0.15:
            matches.append(CaseMatch(case, round(score, 3), reasons or ["медицинский объект"]))
    return sorted(matches, key=lambda m: m.score, reverse=True)[:limit]
