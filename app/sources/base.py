"""Source connector plugin interface (F1): fetch() -> parse() -> normalize().

Connectors only read public pages allowed by robots.txt, official feeds or the owner's own mailbox.
Anything else is a «manual check» source: the owner gets a link instead of automated scraping.
"""

import hashlib
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, ClassVar
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.models import Source

USER_AGENT = "MedProjectBot/1.0 (+https://project-med.ru; design tenders search)"
SPACE_RE = re.compile(r"\s+")


class SourceError(Exception):
    """Expected source failure (blocked by robots.txt, login failed, page changed)."""


@dataclass
class FoundItem:
    external_id: str
    title: str
    text: str = ""
    url: str | None = None
    customer_name: str | None = None
    customer_inn: str | None = None
    region: str | None = None
    address: str | None = None
    budget: Decimal | None = None
    deadline_at: datetime | None = None
    published_at: datetime | None = None
    contact_email: str | None = None
    contact_source: str | None = None
    documents: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ConfigField:
    key: str
    label: str
    secret: bool = False
    required: bool = True
    placeholder: str = ""


def clean(text: str | None) -> str:
    return SPACE_RE.sub(" ", text or "").strip()


def stable_id(*parts: str | None) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:24]  # noqa: S324 - not security


class Connector(ABC):
    name: ClassVar[str]
    title: ClassVar[str]
    kind: ClassVar[str]
    help: ClassVar[str] = ""
    config_fields: ClassVar[list[ConfigField]] = []
    runnable: ClassVar[bool] = True

    @abstractmethod
    def fetch(self, source: Source, http: httpx.Client) -> Iterable[Any]:
        """Download raw payloads (pages, e-mails)."""

    @abstractmethod
    def parse(self, raw: Any, source: Source) -> Iterable[FoundItem]:
        """Turn one raw payload into items."""

    def normalize(self, item: FoundItem) -> FoundItem:
        item.title = clean(item.title)[:1000]
        item.text = clean(item.text)[:20000]
        item.customer_name = clean(item.customer_name) or None
        return item

    def run(self, source: Source, http: httpx.Client) -> Iterator[FoundItem]:
        for raw in self.fetch(source, http):
            for item in self.parse(raw, source):
                item = self.normalize(item)
                if item.title:
                    yield item

    def after_ingest(self, source: Source) -> None:  # noqa: B027 - optional hook, no-op by default
        """Hook called after items were stored (e.g. mark e-mails as read)."""


REGISTRY: dict[str, Connector] = {}


def register(cls: type[Connector]) -> type[Connector]:
    REGISTRY[cls.name] = cls()
    return cls


def get_connector(name: str) -> Connector:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise SourceError(f"Неизвестный тип источника: {name}") from exc


@lru_cache(maxsize=256)
def _robots_for(base: str) -> RobotFileParser | None:
    parser = RobotFileParser()
    try:
        resp = httpx.get(f"{base}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if resp.status_code >= 400:
        return None  # no robots.txt: allowed
    parser.parse(resp.text.splitlines())
    return parser


def robots_allowed(url: str) -> bool:
    parts = urlsplit(url)
    robots = _robots_for(f"{parts.scheme}://{parts.netloc}")
    return robots is None or robots.can_fetch(USER_AGENT, url)


def get_page(http: httpx.Client, url: str) -> str:
    if not robots_allowed(url):
        raise SourceError(f"robots.txt запрещает автоматический доступ к {url}: нужна ручная проверка")
    resp = http.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
    if resp.status_code in (401, 403, 429):
        raise SourceError(f"Сайт ограничивает доступ (HTTP {resp.status_code}): нужна ручная проверка")
    resp.raise_for_status()
    return resp.text
