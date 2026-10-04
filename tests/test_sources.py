import email
from datetime import UTC, datetime, timedelta
from email.policy import default as default_policy
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.core.db import sync_session
from app.models import Listing, ListingVersion, Source
from app.models.enums import ListingStatus
from app.services.source_runner import FAILURES_BEFORE_OPEN, due_sources, run_source
from app.sources import base as source_base
from app.sources import get_connector
from app.sources.email_alerts import items_from_message

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture(autouse=True)
def _robots_ok(monkeypatch):
    monkeypatch.setattr(source_base, "robots_allowed", lambda url: True)


def client_serving(pages: dict[str, str], status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = pages.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def make_source(connector: str, config: dict, name: str = "src") -> int:
    with sync_session() as db:
        source = Source(
            name=name, kind="web_page", connector=connector, config=config, enabled=True, schedule_minutes=60
        )
        db.add(source)
        db.commit()
        return source.id


def listings() -> list[Listing]:
    with sync_session() as db:
        return db.execute(select(Listing).order_by(Listing.id)).scalars().all()


def test_web_page_connector_filters_and_stores():
    url = "https://zdorovie-example.ru/tenders"
    sid = make_source(
        "web_page", {"url": url, "item_selector": "li.tender", "customer_name": "ООО «Здоровье»", "region": "Москва"}
    )
    with sync_session() as db:
        result = run_source(db, sid, http=client_serving({url: (SAMPLES / "clinic_tenders.html").read_text()}))
    assert result.ok, result.error
    rows = listings()
    assert len(rows) == 3
    found = [r for r in rows if r.status == ListingStatus.FOUND]
    assert len(found) == 1 and "поликлиники" in found[0].title
    assert found[0].budget == 2_400_000 and found[0].url == "https://zdorovie-example.ru/tenders/101"
    excluded = {r.title[:20]: r.exclusion_reason for r in rows if r.status == ListingStatus.EXCLUDED}
    assert len(excluded) == 2
    assert result.stats.new_relevant == [found[0].id]


def test_rerun_creates_no_duplicates_but_detects_changes():
    url = "https://zdorovie-example.ru/tenders"
    html = (SAMPLES / "clinic_tenders.html").read_text()
    sid = make_source("web_page", {"url": url, "item_selector": "li.tender"})
    with sync_session() as db:
        run_source(db, sid, http=client_serving({url: html}))
    with sync_session() as db:
        result = run_source(db, sid, http=client_serving({url: html}), now=datetime.now(UTC) + timedelta(hours=2))
    assert result.ok and result.stats.new_relevant == [] and result.stats.changed == []
    assert len(listings()) == 3
    changed_html = html.replace("2 400 000", "2 900 000").replace("20.10.2026", "27.10.2026")
    with sync_session() as db:
        result = run_source(
            db, sid, http=client_serving({url: changed_html}), now=datetime.now(UTC) + timedelta(hours=4)
        )
    assert len(result.stats.changed) == 1
    listing_id, change = result.stats.changed[0]
    assert "бюджет" in change and change["бюджет"] == ["2400000", "2900000"]
    with sync_session() as db:
        versions = db.execute(select(ListingVersion).where(ListingVersion.listing_id == listing_id)).scalars().all()
        assert [v.version for v in versions] == [1, 2]
        assert db.get(Listing, listing_id).budget == 2_900_000


def test_same_tender_from_two_sources_is_merged():
    url1, url2 = "https://a-example.ru/t", "https://b-example.ru/t"
    page1 = '<li class="t"><a href="/x">Проектирование стоматологической клиники на ул. Ленина, 250 м2</a></li>'
    page2 = '<li class="t"><a href="/y">Проектирование стоматологической клиники ул. Ленина 250 м2</a></li>'
    s1 = make_source("web_page", {"url": url1, "item_selector": "li.t"}, name="a")
    s2 = make_source("web_page", {"url": url2, "item_selector": "li.t"}, name="b")
    with sync_session() as db:
        run_source(db, s1, http=client_serving({url1: page1}))
        result = run_source(db, s2, http=client_serving({url2: page2}))
    assert result.stats.duplicates == 1 and result.stats.new_relevant == []
    first, second = listings()
    assert second.merged_into_id == first.id and second.status == ListingStatus.EXCLUDED


def test_telegram_public_connector():
    sid = make_source("telegram_public", {"channel": "@tenders_med"})
    with sync_session() as db:
        result = run_source(
            db,
            sid,
            http=client_serving({"https://t.me/s/tenders_med": (SAMPLES / "telegram_channel.html").read_text()}),
        )
    assert result.ok
    rows = listings()
    assert len(rows) == 2  # the photo-only post is skipped
    relevant = [r for r in rows if r.status == ListingStatus.FOUND]
    assert len(relevant) == 1 and relevant[0].url == "https://t.me/tenders_med/501"
    assert relevant[0].budget == 900_000


def test_email_alert_parsing_excludes_government_and_unsubscribe():
    message = email.message_from_bytes((SAMPLES / "b2b_alert.eml").read_bytes(), policy=default_policy)
    items = items_from_message(message)
    titles = [i.title for i in items]
    assert len(items) == 3 and not any("Отписаться" in t for t in titles)
    sid = make_source("email_alerts", {"user": "x"})
    from app.services.ingest import ingest

    with sync_session() as db:
        stats = ingest(db, db.get(Source, sid), items)
        db.commit()
    rows = {r.title[:30]: r for r in listings()}
    assert len(stats.new_relevant) == 1
    gov = next(r for r in rows.values() if "ГБУЗ" in r.title)
    assert gov.status == ListingStatus.EXCLUDED and "гос" in gov.exclusion_reason


def test_circuit_breaker_opens_after_failures_and_due_logic():
    url = "https://down-example.ru/t"
    sid = make_source("web_page", {"url": url})
    now = datetime.now(UTC)
    for i in range(FAILURES_BEFORE_OPEN):
        with sync_session() as db:
            result = run_source(db, sid, http=client_serving({url: "x"}, status=500), now=now + timedelta(hours=i))
        assert not result.ok
    assert result.circuit_opened
    with sync_session() as db:
        source = db.get(Source, sid)
        assert source.consecutive_failures == FAILURES_BEFORE_OPEN and source.circuit_open_until is not None
        # third failure at now+2h opens the breaker for 30 minutes
        assert sid not in due_sources(db, now + timedelta(hours=2, minutes=10))
        assert sid in due_sources(db, now + timedelta(days=2))


def test_blocked_by_site_reported_as_manual_check(monkeypatch):
    url = "https://blocked-example.ru/t"
    sid = make_source("web_page", {"url": url})
    with sync_session() as db:
        result = run_source(db, sid, http=client_serving({url: "x"}, status=403))
    assert "ручная проверка" in result.error


def test_robots_disallow_is_respected(monkeypatch):
    monkeypatch.setattr(source_base, "robots_allowed", lambda url: False)
    sid = make_source("web_page", {"url": "https://norobots-example.ru/t"})
    with sync_session() as db:
        result = run_source(db, sid, http=client_serving({}))
    assert not result.ok and "robots.txt" in result.error


def test_manual_link_is_not_runnable():
    assert not get_connector("manual_link").runnable
