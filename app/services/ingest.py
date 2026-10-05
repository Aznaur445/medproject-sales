"""Store found items as listings: government exclusion, filters, de-duplication and versions (F1–F5)."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Listing, ListingVersion, Source
from app.models.enums import ListingStatus
from app.services import settings_store as ss
from app.services.filtering import check, extract_budget, lemmas
from app.services.gov_filter import government_reason
from app.services.listings import add_email_channel, get_or_create_org
from app.sources.base import FoundItem

FUZZY_THRESHOLD = 90
DUPLICATE_WINDOW_DAYS = 120
TRACKED_FIELDS = {
    "title": "название",
    "text": "описание",
    "budget": "бюджет",
    "deadline_at": "срок подачи",
    "url": "ссылка",
    "documents": "документы",
}


@dataclass
class IngestStats:
    seen: int = 0
    new_relevant: list[int] = field(default_factory=list)
    new_excluded: int = 0
    duplicates: int = 0
    changed: list[tuple[int, dict[str, Any]]] = field(default_factory=list)


def _norm_title(text: str) -> str:
    return " ".join(sorted(set(w for w in lemmas(text) if len(w) > 2)))


def dedup_hash(item: FoundItem) -> str:
    who = item.customer_inn or (item.customer_name or "").lower().strip()
    when = item.deadline_at.date().isoformat() if item.deadline_at else ""
    return hashlib.sha256(f"{who}|{_norm_title(item.title)}|{when}".encode()).hexdigest()


def snapshot(item: FoundItem, budget: Decimal | None) -> dict[str, Any]:
    return {
        "title": item.title,
        "text": item.text,
        "url": item.url,
        "budget": str(budget) if budget is not None else None,
        "deadline_at": item.deadline_at.isoformat() if item.deadline_at else None,
        "documents": sorted(d.get("url") or d.get("name", "") for d in item.documents),
    }


def content_hash(snap: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, label in TRACKED_FIELDS.items():
        if old.get(key) != new.get(key):
            before, after = old.get(key), new.get(key)
            if key == "text":
                before, after = (str(before or "")[:200], str(after or "")[:200])
            changes[label] = [before, after]
    return changes


def find_duplicate(db: Session, item: FoundItem, digest: str, source_id: int) -> Listing | None:
    since = datetime.now(UTC) - timedelta(days=DUPLICATE_WINDOW_DAYS)
    exact = db.execute(
        select(Listing).where(Listing.dedup_hash == digest, Listing.merged_into_id.is_(None)).limit(1)
    ).scalar_one_or_none()
    if exact:
        return exact
    if item.url:
        same_url = db.execute(
            select(Listing).where(Listing.url == item.url, Listing.merged_into_id.is_(None)).limit(1)
        ).scalar_one_or_none()
        if same_url:
            return same_url
    title = _norm_title(item.title)
    if not title:
        return None
    candidates = db.execute(
        select(Listing)
        .where(
            Listing.created_at >= since,
            Listing.merged_into_id.is_(None),
            or_(Listing.source_id.is_(None), Listing.source_id != source_id),
        )
        .order_by(Listing.id.desc())
        .limit(500)
    ).scalars()
    for listing in candidates:
        if fuzz.token_set_ratio(title, _norm_title(listing.title)) >= FUZZY_THRESHOLD:
            same_deadline = (
                not item.deadline_at
                or not listing.deadline_at
                or abs((item.deadline_at - listing.deadline_at).days) <= 3
            )
            if same_deadline:
                return listing
    return None


def _add_version(db: Session, listing: Listing, snap: dict[str, Any], changes: dict[str, Any]) -> None:
    listing.current_version = (listing.current_version or 0) + 1
    db.add(
        ListingVersion(
            listing_id=listing.id,
            version=listing.current_version,
            content_hash=content_hash(snap),
            snapshot=snap,
            diff=changes,
        )
    )


def ingest(db: Session, source: Source, items: list[FoundItem]) -> IngestStats:
    filters = ss.load_sync(db, ss.Filters)
    stats = IngestStats()
    for item in items:
        stats.seen += 1
        full_text = f"{item.title}\n{item.text}"
        budget = item.budget if item.budget is not None else extract_budget(full_text)
        snap = snapshot(item, budget)
        existing = db.execute(
            select(Listing).where(Listing.source_id == source.id, Listing.external_id == item.external_id)
        ).scalar_one_or_none()
        if existing is not None:
            last = db.execute(
                select(ListingVersion)
                .where(ListingVersion.listing_id == existing.id)
                .order_by(ListingVersion.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last is not None and last.content_hash != content_hash(snap):
                changes = diff(last.snapshot, snap)
                _add_version(db, existing, snap, changes)
                existing.title, existing.description, existing.url = item.title, item.text, item.url
                existing.budget, existing.budget_unknown = budget, budget is None
                existing.deadline_at = item.deadline_at or existing.deadline_at
                target = existing.merged_into_id or existing.id
                if existing.status != ListingStatus.EXCLUDED or existing.merged_into_id:
                    stats.changed.append((target, changes))
            continue

        digest = dedup_hash(item)
        org = get_or_create_org(db, item.customer_name, item.customer_inn) if item.customer_name else None
        listing = Listing(
            source_id=source.id,
            external_id=item.external_id,
            url=item.url,
            title=item.title,
            description=item.text,
            organization_id=org.id if org else None,
            region=item.region,
            address=item.address,
            budget=budget,
            budget_unknown=budget is None,
            published_at=item.published_at,
            deadline_at=item.deadline_at,
            dedup_hash=digest,
            current_version=0,
            tags=[],
            extracted={},
            score_explanation={},
        )
        duplicate = find_duplicate(db, item, digest, source.id)
        gov = government_reason(item.customer_name, item.title, item.text, item.url)
        result = check(full_text, filters, region=item.region, budget=budget)
        listing.score_explanation = {
            "filters": {
                "work": result.matched_work,
                "object": result.matched_object,
                "stop": result.stop_hits,
                "reasons": result.reasons,
            }
        }
        if duplicate is not None:
            listing.status = ListingStatus.EXCLUDED
            listing.merged_into_id = duplicate.id
            listing.exclusion_reason = f"дубль заявки #{duplicate.id}"
            stats.duplicates += 1
        elif gov:
            listing.status = ListingStatus.EXCLUDED
            listing.exclusion_reason = gov
            stats.new_excluded += 1
        elif item.exclude_reason:
            listing.status = ListingStatus.EXCLUDED
            listing.exclusion_reason = item.exclude_reason[:300]
            stats.new_excluded += 1
        elif not result.relevant:
            listing.status = ListingStatus.EXCLUDED
            listing.exclusion_reason = "; ".join(result.reasons)[:300]
            stats.new_excluded += 1
        else:
            listing.status = ListingStatus.FOUND
        db.add(listing)
        db.flush()
        _add_version(db, listing, snap, {})
        if org and item.contact_email and item.contact_source:
            add_email_channel(db, org, item.contact_email, item.contact_source)
        if listing.status == ListingStatus.FOUND:
            from app.services.scoring import score_listing

            score_listing(db, listing)
            threshold = ss.load_sync(db, ss.Scoring).auto_exclude_below
            if threshold and (listing.score or 0) < threshold:
                listing.status = ListingStatus.EXCLUDED
                listing.exclusion_reason = f"низкая оценка {listing.score} (порог {threshold})"
                stats.new_excluded += 1
            else:
                stats.new_relevant.append(listing.id)
    db.flush()
    return stats
