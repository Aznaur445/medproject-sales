import io
import zipfile
from decimal import Decimal as D

import httpx
import pytest
from docx import Document
from sqlalchemy import select

from app.core.db import sync_session
from app.models import Listing, ListingDocument, Message
from app.models.enums import ListingStatus, MessageStatus
from app.services import proposals
from app.services import settings_store as ss
from app.services.autopilot import parse_area_reply, process_listing, summary_text
from app.services.tender_docs import DocLink, LoginRequired, download, find_document_links, unpack
from app.sources import base as source_base
from app.sources import web_search as ws
from tests.factories import make_listing

PAGE_URL = "https://bidzaar-example.ru/procedures/77"
TZ_URL = "https://bidzaar-example.ru/files/tz.docx"
TZ_TEXT = (
    "Техническое задание на проектирование медицинского центра. Общая площадь 450 м2. "
    "Разделы: АР, ЭОМ, ВК, ОВиК. Срок подачи предложений до 20.11.2026. Контакт: tender@clinic-example.ru"
)
PAGE = """<html><body><nav>Меню площадки</nav><main>
<h1>Проектирование медицинского центра</h1><p>Коммерческая закупка. Документация во вложениях.</p>
<a href="/files/tz.docx">Техническое задание</a>
<a href="/download?id=5">Скачать приложение (проект договора)</a>
<a href="/about">О площадке</a>
</main></body></html>"""


def docx_bytes(text: str) -> bytes:
    doc = Document()
    doc.add_paragraph(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(source_base, "robots_allowed", lambda url: True)
    import app.services.tender_docs as td

    monkeypatch.setattr(td, "robots_allowed", lambda url: True)
    monkeypatch.setattr(proposals, "docx_to_pdf", lambda data: (_ for _ in ()).throw(RuntimeError("no soffice")))


def platform(pages: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return pages.get(str(request.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_find_document_links():
    links = find_document_links(PAGE, PAGE_URL)
    assert [link.url for link in links] == [TZ_URL, "https://bidzaar-example.ru/download?id=5"]
    assert links[0].name == "tz.docx"


def test_download_detects_login_page_and_reads_filename():
    login_url = "https://bidzaar-example.ru/download?id=5"
    login = platform({login_url: httpx.Response(200, headers={"content-type": "text/html"}, text="<form>Вход</form>")})
    with pytest.raises(LoginRequired):
        download(login, DocLink(login_url, "Скачать приложение"))
    named = platform(
        {
            TZ_URL: httpx.Response(
                200,
                headers={"content-disposition": "attachment; filename*=UTF-8''%D0%A2%D0%97.pdf"},
                content=b"%PDF-1.4 test",
            )
        }
    )
    name, data = download(named, DocLink(TZ_URL, "x"))
    assert name == "ТЗ.pdf" and data.startswith(b"%PDF")


def test_unpack_zip_with_russian_names():
    # Windows archivers store Russian names in cp866 without the UTF-8 flag: build such an archive by hand.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("AAAAAAAAAA.txt", "площадь 300 м2")
        archive.writestr("nested.zip", b"PK")
    raw = buf.getvalue().replace(b"AAAAAAAAAA", "Техзадание".encode("cp866"))
    files = unpack("docs.zip", raw)
    assert [name for name, _ in files] == ["Техзадание.txt"]


def _listing_with_url(**extra) -> int:
    lid = make_listing(customer="ООО «Клиника Плюс»", email=None, area="1", url=PAGE_URL, **extra)
    with sync_session() as db:
        listing = db.get(Listing, lid)
        listing.area_m2 = None  # unknown until the ТЗ is read
        db.commit()
    return lid


def test_autopilot_downloads_tz_analyzes_and_prepares_proposal():
    lid = _listing_with_url()
    http = platform(
        {
            PAGE_URL: httpx.Response(200, text=PAGE),
            TZ_URL: httpx.Response(200, content=docx_bytes(TZ_TEXT)),
            "https://bidzaar-example.ru/download?id=5": httpx.Response(403),
        }
    )
    with sync_session() as db:
        result = process_listing(db, lid, http)
        db.commit()
        text = summary_text(db, result)
    assert result.documents.saved == ["tz.docx"] and result.documents.needs_login
    assert result.message_id is not None, result
    with sync_session() as db:
        listing = db.get(Listing, lid)
        assert listing.area_m2 == D("450") and listing.status == ListingStatus.PROPOSAL_PENDING
        docs = db.execute(select(ListingDocument).where(ListingDocument.listing_id == lid)).scalars().all()
        assert [d.url for d in docs] == [TZ_URL]
        message = db.get(Message, result.message_id)
        assert message.status == MessageStatus.PENDING_APPROVAL  # waits for the owner, nothing is sent
    assert PAGE_URL in text and "Скачано документов: 1" in text and "КП рассчитано" in text

    # Second run does not download the same files again and keeps the pending draft.
    with sync_session() as db:
        again = process_listing(db, lid, http, force=False)
    assert again.skipped  # status is now «КП на согласовании»


def test_autopilot_without_area_asks_owner_and_login_hint():
    lid = _listing_with_url()
    http = platform({PAGE_URL: httpx.Response(403)})
    with sync_session() as db:
        result = process_listing(db, lid, http)
        db.commit()
        text = summary_text(db, result)
    assert result.documents.needs_login and result.message_id is None
    assert result.missing_for_proposal == ["площадь объекта"]
    assert f"#{lid}" in text and "после входа" in text and "Для КП не хватает" in text


def test_autopilot_respects_settings():
    lid = _listing_with_url()
    with sync_session() as db:
        ss.save_sync(db, ss.Automation(download_documents=False, auto_proposal=False))
        db.commit()
        result = process_listing(db, lid, platform({}))
    assert result.documents is None and result.analysis is not None and result.message_id is None


def test_area_reply_parser():
    assert parse_area_reply("#12 450") == (12, D("450"))
    assert parse_area_reply("#7 площадь 1 200,5 м2") == (7, D("1200.5"))
    assert parse_area_reply("#3 привет") is None


def test_bot_helpers_store_file_and_set_area():
    from app.bot.approvals import _set_area, _store_files

    lid = _listing_with_url()
    assert _store_files(lid, "ТЗ.txt", TZ_TEXT.encode()) == 1
    assert _store_files(lid, "ТЗ.txt", TZ_TEXT.encode()) == 0  # same file twice
    assert _store_files(99999, "x.txt", b"x") is None
    assert _set_area(lid, D("300"), 1)
    with sync_session() as db:
        assert db.get(Listing, lid).area_m2 == D("300")


def test_new_listings_start_autopilot(monkeypatch):
    from app.services.ingest import IngestStats
    from app.services.source_runner import RunResult
    from app.worker import tasks_sales

    lid = _listing_with_url()
    started, sent = [], []
    monkeypatch.setattr(
        "app.services.source_runner.run_source",
        lambda db, sid: RunResult(sid, ok=True, stats=IngestStats(seen=1, new_relevant=[lid])),
    )
    monkeypatch.setattr(tasks_sales.autopilot_task, "apply_async", lambda args, countdown: started.append(args))
    monkeypatch.setattr(tasks_sales, "send_owner_message", lambda text, **kw: sent.append(text))
    tasks_sales.run_source_task(1)
    assert started == [(lid,)]
    assert PAGE_URL in sent[0]


def test_search_skips_video_and_social_and_upgrades_old_defaults():
    assert ws._is_non_text("https://www.youtube.com/watch?v=1")
    assert ws._is_non_text("https://vk.com/wall-1_2")
    assert not ws._is_non_text("https://bidzaar.com/procedures/1")

    class S:
        config = {"queries": "\n".join(ws.DEFAULT_QUERIES_V1)}

    queries = ws.queries_of(S())
    assert queries[0].startswith("site:bidzaar.com") and len(queries) == len(ws.DEFAULT_QUERIES)


def test_platform_registry():
    from app.sources.platforms import platform_of

    bidzaar = platform_of("https://bidzaar.com/app/process/light/0198e4db-8ec3")
    assert bidzaar.key == "bidzaar" and bidzaar.is_procedure("https://bidzaar.com/app/process/light/1")
    assert not bidzaar.auto_documents
    roseltorg = platform_of("https://www.roseltorg.ru/procedure/COM12011700086")
    assert roseltorg.is_procedure("https://www.roseltorg.ru/procedure/COM12011700086")
    assert roseltorg.is_government("https://www.roseltorg.ru/procedure/0373100038124000001")
    assert platform_of("https://clinic-example.ru/") is None


def test_login_only_platform_is_not_scraped():
    from app.services.tender_docs import collect_documents

    lid = make_listing(email=None, url="https://www.b2b-center.ru/market/view.html?id=900001")
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, text=PAGE)

    with sync_session() as db:
        report = collect_documents(db, db.get(Listing, lid), httpx.Client(transport=httpx.MockTransport(handler)))
    assert fetched == [] and report.needs_login and "запрещает" in report.platform_note


def test_email_alert_recognises_platform_links():
    import email as email_lib
    from email.message import EmailMessage
    from email.policy import default as default_policy

    from app.sources.email_alerts import items_from_message

    def alert(message_id: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"], msg["Subject"], msg["Message-ID"] = "noreply@bidzaar.com", "Новые процедуры", message_id
        msg.set_content(
            "<table><tr><td>Проектирование медицинского центра, СМ-Клиника "
            '<a href="https://bidzaar.com/app/process/light/abc-1">Подробнее</a></td></tr>'
            '<tr><td><a href="https://www.roseltorg.ru/procedure/0373100038124000001">Госзакупка: проектирование '
            "поликлиники ГБУЗ</a></td></tr></table>",
            subtype="html",
        )
        return email_lib.message_from_bytes(msg.as_bytes(), policy=default_policy)

    items = items_from_message(alert("<m1@bidzaar.com>"))
    assert [i.url for i in items] == ["https://bidzaar.com/app/process/light/abc-1"]
    assert "Проектирование медицинского центра" in items[0].title
    again = items_from_message(alert("<m2@bidzaar.com>"))
    assert again[0].external_id == items[0].external_id  # same tender in another digest


def test_summary_rules_and_ai_out_of_niche_excludes():
    import app.llm
    from app.services.summary import TenderSummary

    lid = _listing_with_url()
    http = platform(
        {PAGE_URL: httpx.Response(200, text=PAGE), TZ_URL: httpx.Response(200, content=docx_bytes(TZ_TEXT))}
    )
    with sync_session() as db:
        result = process_listing(db, lid, http)
        db.commit()
        text = summary_text(db, result)
    assert result.summary["source"] == "правила" and result.summary["is_medical_design"]
    assert result.summary["recommendation"] in ("участвовать", "уточнить")
    assert "Рекомендация" in text and "tz.docx" in result.summary["documents_reviewed"]

    class OffNicheAI:
        name = "fake"

        def complete_json(self, prompt, document, schema):
            if schema is TenderSummary:
                return TenderSummary(is_medical_design=False, relevance_reason="только СМР по готовому проекту")
            return schema()

    lid2 = _listing_with_url()
    original = app.llm.get_provider
    app.llm.get_provider = lambda: OffNicheAI()
    try:
        with sync_session() as db:
            result = process_listing(db, lid2, http)
            db.commit()
            text = summary_text(db, result)
    finally:
        app.llm.get_provider = original
    with sync_session() as db:
        listing = db.get(Listing, lid2)
        assert listing.status == ListingStatus.EXCLUDED and "только СМР" in listing.exclusion_reason
    assert result.message_id is None and "Исключена" in text
