"""Sources that may not be scraped (Avito, closed platforms): the owner gets a reminder with the link."""

from collections.abc import Iterable
from typing import Any

import httpx

from app.models import Source
from app.sources.base import ConfigField, Connector, FoundItem, register


@register
class ManualLinkConnector(Connector):
    name = "manual_link"
    title = "Ручная проверка (ссылка-напоминание)"
    kind = "board"
    help = (
        "Для площадок, где автоматический сбор запрещён правилами (например, Avito) или нужен вход: сервис раз "
        "в период присылает ссылку, вы просматриваете сами и добавляете подходящее через /add."
    )
    config_fields = [ConfigField("url", "Ссылка на поиск / раздел", placeholder="https://www.avito.ru/...")]
    runnable = False

    def fetch(self, source: Source, http: httpx.Client) -> Iterable[Any]:
        return []

    def parse(self, raw: Any, source: Source) -> Iterable[FoundItem]:
        return []
