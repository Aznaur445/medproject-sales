"""Internet search (F1): Yandex Search API queries -> pages -> demand check -> listings.

Only the official API is used (no scraping of search result pages). Found pages are opened only when robots.txt
allows it; otherwise the search snippet is used. Every result passes a rule-based demand check and, when an AI
provider is configured, an AI check that tells a customer's request from a designer's advertising.
"""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from app.core.logging import get_logger
from app.models import Source
from app.services import runtime_config
from app.services.demand import check_demand
from app.services.gov_filter import government_reason
from app.services.web_search import SearchError, SearchResult, yandex_search
from app.sources.base import (
    ConfigField,
    Connector,
    FoundItem,
    SourceError,
    get_page,
    page_text,
    register,
    stable_id,
)

log = get_logger(__name__)

# Commercial tender platforms first: their procedure pages carry the ТЗ and documentation.
PLATFORM_QUERIES = [
    "site:bidzaar.com проектирование медицинского центра",
    "site:bidzaar.com проектирование клиники",
    "site:b2b-center.ru проектирование медицинского центра",
    "site:b2b-center.ru проектирование клиники",
    "site:tender.pro проектирование клиники",
    "site:fabrikant.ru проектирование медицинского центра",
    "site:etpgpb.ru проектирование клиники",
    "site:tektorg.ru проектирование медицинского центра",
    "site:roseltorg.ru коммерческая закупка проектирование клиники",
]
GENERAL_QUERIES = [
    "тендер на проектирование клиники",
    "требуется проектирование медицинского центра",
    "запрос коммерческих предложений проектирование медицинского центра",
    "техническое задание на проектирование медицинского центра",
    "конкурс на проектирование стоматологической клиники",
    "тендер проектирование лаборатории",
    "тендер проектирование диагностического центра МРТ КТ",
    "тендер проектирование реабилитационного центра",
    "ищем проектировщика для клиники",
    "приглашаем к участию в тендере проектирование клиники",
]
DEFAULT_QUERIES = PLATFORM_QUERIES + GENERAL_QUERIES
# The first default set: sources still using it are switched to the current one automatically.
DEFAULT_QUERIES_V1 = [
    "требуется проектирование медицинского центра",
    "тендер на проектирование клиники",
    "запрос коммерческих предложений проектирование медицинского центра",
    "ищем проектировщика для клиники",
    "конкурс на проектирование стоматологической клиники",
    "тендер проектирование стоматологии",
    "закупка проектные работы медицинский центр коммерческая",
    "техническое задание на проектирование медицинского центра",
    "тендер проектирование лаборатории ПЦР",
    "тендер проектирование диагностического центра МРТ КТ",
    "проектирование частной клиники тендер",
    "тендер проектирование реабилитационного центра",
    "приглашаем к участию в тендере проектирование клиники",
    "site:b2b-center.ru проектирование медицинского центра",
    "site:tender.pro проектирование клиники",
]
# Only requests and written information: video, social networks, job boards, maps, reviews and boards whose
# rules forbid automated collection are skipped without spending a page download or an AI call.
NON_TEXT_HOSTS = (
    "youtube.com",
    "youtu.be",
    "rutube.ru",
    "vk.com",
    "vkvideo.ru",
    "ok.ru",
    "dzen.ru",
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "pinterest.com",
    "pinterest.ru",
    "avito.ru",
    "hh.ru",
    "superjob.ru",
    "otzovik.com",
    "2gis.ru",
    "wikipedia.org",
)
NON_TEXT_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".mp4", ".avi", ".mov", ".mp3")
GOV_DOMAINS = ("zakupki.gov.ru", "torgi.gov.ru", "bus.gov.ru")
MAX_QUERIES = 40
MAX_LLM_TEXT = 8000


class SearchVerdict(BaseModel):
    is_customer_request: bool
    confidence: float = 0.0
    reason: str = ""
    customer_name: str | None = None
    region: str | None = None
    deadline: str | None = None
    is_government: bool = False


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def queries_of(source: Source) -> list[str]:
    raw = source.config.get("queries") or "\n".join(DEFAULT_QUERIES)
    if str(raw).strip() == "\n".join(DEFAULT_QUERIES_V1):
        raw = "\n".join(DEFAULT_QUERIES)
    seen, result = set(), []
    for line in str(raw).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.lower() not in seen:
            seen.add(line.lower())
            result.append(line)
    return result[:MAX_QUERIES]


def _is_non_text(url: str) -> bool:
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    if any(host == h or host.endswith("." + h) for h in NON_TEXT_HOSTS):
        return True
    return parts.path.lower().endswith(NON_TEXT_SUFFIXES)


def _is_gov_domain(url: str) -> bool:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host.endswith(".gov.ru") or any(host == d or host.endswith("." + d) for d in GOV_DOMAINS)


def _deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@register
class WebSearchConnector(Connector):
    name = "web_search"
    title = "Поиск в интернете (Яндекс)"
    kind = "web_page"
    help = (
        "Сервис сам ищет в интернете заявки на проектирование медицинских объектов (Bidzaar, B2B-Center, Tender.Pro "
        "и др. коммерческие площадки + общий поиск) через официальный Yandex Search API. Нужны ключ и каталог "
        "Yandex Cloud (Настройки → ИИ и поиск). Каждый запрос платный (тариф Yandex Cloud): 19 запросов 2 раза в "
        "день ≈ 1150 запросов в месяц. Реклама проектировщиков, статьи, видео, соцсети и госзакупки отсеиваются; "
        "по каждой подходящей заявке скачивается документация, разбирается ТЗ и готовится КП на согласование."
    )
    config_fields = [
        ConfigField(
            "queries",
            "Поисковые запросы (по одному в строке, # — комментарий)",
            required=False,
            multiline=True,
            default="\n".join(DEFAULT_QUERIES),
        ),
        ConfigField("days", "Только страницы не старше, дней", required=False, default="60"),
        ConfigField("max_pages", "Открывать новых страниц за проверку, не больше", required=False, default="25"),
        ConfigField("use_ai", "Проверять находки ИИ (1 — да, 0 — нет)", required=False, default="1"),
    ]

    # fetch/parse are kept for the plugin interface; run() is overridden to skip known results cheaply.
    def fetch(self, source: Source, http: httpx.Client) -> Iterator[SearchResult]:
        cfg = runtime_config.search_config()
        if not cfg.ready:
            raise SourceError("Не задан ключ Yandex Search API: Настройки → ИИ и поиск")
        seen: set[str] = set()
        errors: list[str] = []
        succeeded = 0
        for query in queries_of(source):
            try:
                results = yandex_search(query, cfg, http=http)
            except SearchError as exc:
                errors.append(str(exc))
                if not succeeded and len(errors) >= 3:  # key or quota problem: stop spending requests
                    raise SourceError(f"Поиск не работает: {errors[0]}") from exc
                continue
            succeeded += 1
            for result in results:
                if result.url not in seen:
                    seen.add(result.url)
                    yield result
            time.sleep(0.2)
        if errors and not succeeded:
            raise SourceError(f"Поиск не работает: {errors[0]}")

    def parse(self, raw: Any, source: Source) -> Iterator[FoundItem]:
        result: SearchResult = raw
        yield FoundItem(
            external_id=stable_id(result.url),
            title=result.title,
            text=result.snippet,
            url=result.url,
            published_at=result.modified_at,
            contact_source=result.url,
        )

    def run(self, source: Source, http: httpx.Client, known: frozenset[str] = frozenset()) -> Iterator[FoundItem]:
        days = _int(source.config.get("days"), 60, 1, 3650)
        max_pages = _int(source.config.get("max_pages"), 25, 0, 200)
        use_ai = str(source.config.get("use_ai", "1")).strip() != "0"
        oldest = datetime.now(UTC) - timedelta(days=days)
        provider = None
        if use_ai:
            from app.llm import get_provider

            provider = get_provider()
        opened = 0
        for result in self.fetch(source, http):
            if _is_gov_domain(result.url) or _is_non_text(result.url):
                continue
            if result.modified_at and result.modified_at < oldest:
                continue
            item = next(iter(self.parse(result, source)))
            if item.external_id in known:
                continue  # already a listing: no page download, no AI call
            if opened < max_pages:
                opened += 1
                try:
                    text = page_text(get_page(http, result.url))
                    if text:
                        item.text = f"{result.snippet}\n{text}"
                except (SourceError, httpx.HTTPError) as exc:
                    log.info("search_page_skipped", url=result.url, reason=str(exc)[:100])
            self._judge(item, provider)
            yield self.normalize(item)

    @staticmethod
    def _judge(item: FoundItem, provider: Any) -> None:
        full = f"{item.title}\n{item.text}"
        if government_reason(item.title, item.text, item.url):
            return  # ingest excludes it with the exact reason
        demand = check_demand(full)
        if not demand.is_request:
            item.exclude_reason = demand.reason
            return
        if provider is None:
            return
        from app.llm import LLMError

        try:
            verdict = provider.complete_json(
                "classify_search_result", f"Адрес: {item.url}\n{full[:MAX_LLM_TEXT]}", SearchVerdict
            )
        except LLMError as exc:
            log.warning("search_ai_failed", url=item.url, error=str(exc)[:200])
            return
        if verdict.is_government:
            item.exclude_reason = "исключена: госзаказ (по оценке ИИ)"
        elif not verdict.is_customer_request and verdict.confidence >= 0.6:
            item.exclude_reason = f"ИИ: не заявка заказчика — {verdict.reason}"[:300]
        else:
            item.customer_name = item.customer_name or verdict.customer_name
            item.region = item.region or verdict.region
            item.deadline_at = item.deadline_at or _deadline(verdict.deadline)
