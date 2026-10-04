"""Public procurement / «стать подрядчиком» pages of clinic chains and boards that allow robots."""

from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.models import Source
from app.sources.base import ConfigField, Connector, FoundItem, clean, get_page, register, stable_id

MIN_TEXT = 25


@register
class WebPageConnector(Connector):
    name = "web_page"
    title = "Страница закупок на сайте"
    kind = "web_page"
    help = (
        "Публичная страница закупок / тендеров / «стать подрядчиком». Каждая ссылка с описанием становится "
        "кандидатом, дальше работают фильтры. robots.txt соблюдается."
    )
    config_fields = [
        ConfigField("url", "Адрес страницы", placeholder="https://clinic.ru/tenders"),
        ConfigField(
            "item_selector", "CSS-селектор карточки заявки (необязательно)", required=False, placeholder=".tender-item"
        ),
        ConfigField("customer_name", "Заказчик (если страница одной сети)", required=False),
        ConfigField("region", "Регион по умолчанию", required=False),
    ]

    def fetch(self, source: Source, http: httpx.Client) -> Iterable[Any]:
        url = source.config["url"]
        yield url, get_page(http, url)

    def parse(self, raw: Any, source: Source) -> Iterable[FoundItem]:
        page_url, html = raw
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        selector = (source.config.get("item_selector") or "").strip()
        seen: set[str] = set()
        blocks = soup.select(selector) if selector else [a for a in soup.find_all("a", href=True)]
        for block in blocks:
            link = block if block.name == "a" else block.find("a", href=True)
            href = urljoin(page_url, link["href"]) if link is not None and link.get("href") else page_url
            if href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            title = clean(link.get_text(" ") if link is not None else block.get_text(" "))
            context = clean((block if selector else (block.parent or block)).get_text(" "))
            if len(title) < MIN_TEXT and len(context) < MIN_TEXT * 2:
                continue
            key = stable_id(href, title)
            if key in seen:
                continue
            seen.add(key)
            yield FoundItem(
                external_id=key,
                title=title or context[:200],
                text=context,
                url=href,
                customer_name=source.config.get("customer_name") or None,
                region=source.config.get("region") or None,
            )
