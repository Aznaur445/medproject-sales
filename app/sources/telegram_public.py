"""Public Telegram channels through the official web preview t.me/s/<channel> (no account, robots.txt checked)."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.models import Source
from app.sources.base import ConfigField, Connector, FoundItem, clean, get_page, register


@register
class TelegramPublicConnector(Connector):
    name = "telegram_public"
    title = "Публичный Telegram-канал"
    kind = "telegram"
    help = "Читает публичные посты через веб-превью t.me/s/канал. Закрытые каналы и чаты не поддерживаются."
    config_fields = [ConfigField("channel", "Имя канала (без @)", placeholder="tenders_stroy")]

    def fetch(self, source: Source, http: httpx.Client) -> Iterable[Any]:
        channel = source.config["channel"].lstrip("@").strip().rsplit("/", 1)[-1]
        yield channel, get_page(http, f"https://t.me/s/{channel}")

    def parse(self, raw: Any, source: Source) -> Iterable[FoundItem]:
        channel, html = raw
        soup = BeautifulSoup(html, "html.parser")
        for message in soup.select(".tgme_widget_message[data-post]"):
            post = message["data-post"]
            text_el = message.select_one(".tgme_widget_message_text")
            if text_el is None:
                continue
            text = clean(text_el.get_text(" "))
            time_el = message.select_one("time[datetime]")
            published = None
            if time_el is not None:
                try:
                    published = datetime.fromisoformat(time_el["datetime"])
                except ValueError:
                    published = None
            first_line = text.split(". ")[0][:200]
            yield FoundItem(
                external_id=post,
                title=first_line,
                text=text,
                url=f"https://t.me/{post}",
                published_at=published,
                contact_source=f"https://t.me/{post}",
            )
