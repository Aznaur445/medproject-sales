import base64
import json

import httpx
import pytest
from sqlalchemy import select

from app.core.db import sync_session
from app.core.security import decrypt_secret
from app.models import Listing, Source
from app.models.enums import ListingStatus
from app.services import runtime_config
from app.services import settings_store as ss
from app.services.demand import check_demand
from app.services.runtime_config import SearchConfig
from app.services.source_runner import run_source
from app.services.web_search import SearchError, parse_xml, yandex_search
from app.sources import base as source_base
from app.sources import web_search as ws
from tests.test_web_sales import csrf_of, login

CFG = SearchConfig("key", "folder")


def xml_of(*docs: tuple[str, str, str], modtime: str = "20261001T100000") -> str:
    groups = "".join(
        f"<group><doc><url>{url}</url><domain>x</domain><title>{title}</title><modtime>{modtime}</modtime>"
        f"<passages><passage>{text}</passage></passages></doc></group>"
        for url, title, text in docs
    )
    body = f"<response><results><grouping>{groups}</grouping></results></response>"
    return f'<?xml version="1.0"?><yandexsearch>{body}</yandexsearch>'


def api_response(xml: str) -> dict:
    return {"rawData": base64.b64encode(xml.encode()).decode()}


REQUEST = (
    "https://clinic-example.ru/tender/1",
    "Тендер на проектирование медицинского центра",
    "ООО «Клиника Плюс» объявляет тендер: требуется проектирование медицинского центра 600 м², срок подачи до 20.10",
)
ADVERT = (
    "https://designer-example.ru/",
    "Проектирование медицинских центров под ключ",
    "Мы проектируем клиники. Наши услуги: проектирование медицинского центра. Стоимость проекта от 1500 руб/м². "
    "Портфолио. Бесплатная консультация.",
)
GOV = (
    "https://zakupki.gov.ru/epz/order/1",
    "Проектирование поликлиники",
    "ГБУЗ объявляет закупку по 44-ФЗ на проектирование клиники",
)


def test_parse_xml_and_no_results():
    results = parse_xml(xml_of(REQUEST))
    assert results[0].url == REQUEST[0] and "тендер" in results[0].snippet
    assert results[0].modified_at.year == 2026
    empty = '<yandexsearch><response><error code="15">Нет результатов</error></response></yandexsearch>'
    assert parse_xml(empty) == []
    with pytest.raises(SearchError):
        parse_xml('<yandexsearch><response><error code="32">Лимит</error></response></yandexsearch>')


def test_yandex_search_sync_and_async_fallback():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Api-Key key"
        if request.url.path.endswith("/web/search"):
            return httpx.Response(404)
        if request.url.path.endswith("/searchAsync"):
            body = json.loads(request.content)
            assert body["folderId"] == "folder" and body["query"]["queryText"] == "q"
            return httpx.Response(200, json={"id": "op1", "done": False})
        return httpx.Response(200, json={"id": "op1", "done": True, "response": api_response(xml_of(REQUEST))})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        results = yandex_search("q", CFG, http=http, poll_seconds=0)
    assert [r.url for r in results] == [REQUEST[0]]
    assert len(calls) == 3

    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as http:
        with pytest.raises(SearchError, match="доступ запрещён"):
            yandex_search("q", CFG, http=http)


def test_demand_check():
    assert check_demand(REQUEST[2]).is_request
    advert = check_demand(ADVERT[2])
    assert not advert.is_request and "реклам" in advert.reason
    assert not check_demand("Как выбрать помещение для медицинского центра: статья").is_request


def _client(pages: dict[str, str], search_xml: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searchapi.api.cloud.yandex.net":
            return httpx.Response(200, json=api_response(search_xml))
        body = pages.get(str(request.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setattr(source_base, "robots_allowed", lambda url: True)
    monkeypatch.setattr(runtime_config, "search_config", lambda: CFG)
    monkeypatch.setattr(ws.time, "sleep", lambda s: None)


def _source(config: dict | None = None) -> int:
    with sync_session() as db:
        source = Source(
            name="search",
            kind="web_page",
            connector="web_search",
            config={"queries": "проектирование клиники тендер", "use_ai": "0", **(config or {})},
            enabled=True,
        )
        db.add(source)
        db.commit()
        return source.id


def _listings() -> dict[str, Listing]:
    with sync_session() as db:
        return {item.url: item for item in db.execute(select(Listing)).scalars()}


def test_search_source_keeps_requests_and_drops_adverts(search_on):
    page = (
        "<html><body><nav>меню</nav>"
        "<main>Техническое задание: проектирование клиники, разделы АР, ОВ.</main></body></html>"
    )
    sid = _source()
    with sync_session() as db:
        result = run_source(db, sid, http=_client({REQUEST[0]: page}, xml_of(REQUEST, ADVERT, GOV)))
    assert result.ok, result.error
    rows = _listings()
    assert GOV[0] not in rows  # government sites are not even stored
    assert rows[REQUEST[0]].status == ListingStatus.FOUND
    assert "Техническое задание" in rows[REQUEST[0]].description and "меню" not in rows[REQUEST[0]].description
    assert rows[ADVERT[0]].status == ListingStatus.EXCLUDED
    assert "реклам" in rows[ADVERT[0]].exclusion_reason

    # Second run: known results are skipped without opening pages again.
    opened = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "searchapi.api.cloud.yandex.net":
            return httpx.Response(200, json=api_response(xml_of(REQUEST, ADVERT)))
        opened.append(str(request.url))
        return httpx.Response(404)

    with sync_session() as db:
        result = run_source(db, sid, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert result.ok and result.stats.seen == 0 and opened == []


def test_search_source_old_pages_and_ai_verdict(search_on, monkeypatch):
    class FakeAI:
        name = "fake"

        def complete_json(self, prompt, document, schema):
            assert prompt == "classify_search_result" and "<" not in prompt
            return schema(is_customer_request=True, confidence=0.9, customer_name="ООО «Клиника Плюс»", region="Казань")

    import app.llm

    monkeypatch.setattr(app.llm, "get_provider", lambda: FakeAI())
    sid = _source({"use_ai": "1", "days": "30"})
    old = xml_of(("https://old-example.ru/t", "Тендер проектирование клиники", "тендер"), modtime="20200101T000000")
    fresh = xml_of(REQUEST)
    xml = old.replace("</grouping>", fresh.split("<grouping>")[1].split("</grouping>")[0] + "</grouping>")
    with sync_session() as db:
        assert run_source(db, sid, http=_client({}, xml)).ok
    rows = _listings()
    assert "https://old-example.ru/t" not in rows
    assert rows[REQUEST[0]].region == "Казань"


def test_search_source_without_key_fails_clearly(monkeypatch):
    monkeypatch.setattr(runtime_config, "search_config", lambda: SearchConfig(None, None))
    sid = _source()
    with sync_session() as db:
        result = run_source(db, sid, http=_client({}, xml_of()))
    assert not result.ok and "Yandex Search API" in result.error


async def test_quick_search_edit_and_ai_settings(client, monkeypatch):
    await login(client, monkeypatch)
    from app.worker import tasks_sales

    monkeypatch.setattr(tasks_sales.run_source_task, "delay", lambda *a: None)
    csrf = await csrf_of(client, "/sources")
    assert "Включить автопоиск" in (await client.get("/sources")).text
    await client.post("/sources/quick-search", data={"csrf_token": csrf})
    await client.post("/sources/quick-search", data={"csrf_token": csrf})  # idempotent
    with sync_session() as db:
        sources = db.execute(select(Source).where(Source.connector == "web_search")).scalars().all()
    assert len(sources) == 1 and sources[0].schedule_minutes == 20
    assert "тендер на проектирование клиники" in sources[0].config["queries"]
    assert "Включить автопоиск" not in (await client.get("/sources")).text

    page = await client.get(f"/sources/{sources[0].id}/edit")
    assert page.status_code == 200 and "Поисковые запросы" in page.text
    await client.post(
        f"/sources/{sources[0].id}/edit",
        data={"csrf_token": csrf, "queries": "один запрос\nдругой", "days": "14", "schedule_minutes": "1440"},
    )
    with sync_session() as db:
        source = db.get(Source, sources[0].id)
        assert source.config["queries"] == "один запрос\nдругой" and source.schedule_minutes == 1440
        assert ws.queries_of(source) == ["один запрос", "другой"]

    csrf = await csrf_of(client, "/settings")
    await client.post(
        "/settings/ai",
        data={
            "csrf_token": csrf,
            "llm_provider": "deepseek",
            "deepseek_api_key": "sk-test",
            "yandex_api_key": "AQVN-test",
            "yandex_folder_id": "b1gfolder",
        },
    )
    with sync_session() as db:
        data = ss.load_sync(db, ss.Integrations)
    assert data.llm_provider == "deepseek" and decrypt_secret(data.deepseek_api_key_enc) == "sk-test"
    runtime_config.reset_cache()
    assert runtime_config.llm_config().deepseek_key == "sk-test"
    assert runtime_config.search_config() == SearchConfig("AQVN-test", "b1gfolder")
    from app.llm import get_provider

    assert get_provider().name == "deepseek"
    page = await client.get("/settings")
    assert "sk-test" not in page.text and "ИИ и поиск" in page.text


def test_search_rotates_queries_and_respects_daily_limit(search_on):
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content)["query"]["queryText"])
        return httpx.Response(200, json=api_response(xml_of()))

    sid = _source({"queries": "q1\nq2\nq3", "per_run": "2", "daily_limit": "5"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(4):
        with sync_session() as db:
            assert run_source(db, sid, http=http).ok
    assert asked == ["q1", "q2", "q3", "q1", "q2"]  # circle q1..q3, then the daily limit (5) stops spending
