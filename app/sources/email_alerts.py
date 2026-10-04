"""E-mail alerts from trading platforms (B2B-Center, Bicotender, Fabrikant, Tenderpro…) in a dedicated mailbox.

The owner subscribes the mailbox to the platforms' commercial-tender alerts; the service reads unread letters
over IMAP, turns each tender link into a candidate and marks the letters as read after they are stored.
"""

import email
import imaplib
from collections.abc import Iterable
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.core.security import decrypt_secret
from app.models import Source
from app.sources.base import ConfigField, Connector, FoundItem, SourceError, clean, register, stable_id

MAX_MESSAGES = 50
SKIP_LINK_WORDS = (
    "отписаться",
    "unsubscribe",
    "настройки рассылки",
    "политика",
    "privacy",
    "личный кабинет",
    "мобильное приложение",
    "app store",
    "google play",
    "служба поддержки",
)


def _connect(config: dict) -> imaplib.IMAP4_SSL:
    password = decrypt_secret(config["password_enc"]) if config.get("password_enc") else ""
    try:
        client = imaplib.IMAP4_SSL(
            config.get("imap_host") or "imap.mail.ru", int(config.get("imap_port") or 993), timeout=60
        )
        client.login(config["user"], password)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise SourceError(f"Не удалось войти в почтовый ящик {config.get('user')}: {type(exc).__name__}") from exc
    return client


def html_of(message: EmailMessage) -> str | None:
    part = message.get_body(preferencelist=("html",))
    return part.get_content() if part is not None else None


def text_of(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    return BeautifulSoup(content, "html.parser").get_text(" ") if part.get_content_subtype() == "html" else content


def items_from_message(message: EmailMessage, sender_note: str = "") -> list[FoundItem]:
    message_id = str(message.get("Message-ID") or stable_id(str(message.get("Subject")), str(message.get("Date"))))
    subject = clean(str(message.get("Subject") or ""))
    source_note = f"рассылка от {sender_note or message.get('From')}"
    items: list[FoundItem] = []
    html = html_of(message)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        for link in soup.find_all("a", href=True):
            href = link["href"]
            title = clean(link.get_text(" "))
            if not href.startswith("http") or len(title) < 25 or any(w in title.lower() for w in SKIP_LINK_WORDS):
                continue
            container = link.find_parent(["tr", "li", "p", "div"]) or link
            context = clean(container.get_text(" "))
            key = stable_id(message_id, href)
            if key in seen:
                continue
            seen.add(key)
            items.append(FoundItem(external_id=key, title=title, text=context, url=href, contact_source=source_note))
    if not items:
        items.append(
            FoundItem(
                external_id=stable_id(message_id),
                title=subject,
                text=clean(text_of(message)),
                contact_source=source_note,
            )
        )
    return items


@register
class EmailAlertsConnector(Connector):
    name = "email_alerts"
    title = "Рассылки площадок (почтовый ящик)"
    kind = "email_alert"
    help = (
        "Подпишите отдельный ящик на рассылки коммерческих закупок площадок (B2B-Center, Bicotender, Фабрикант, "
        "Tenderpro…). Сервис читает непрочитанные письма и разбирает ссылки на закупки. Нужен пароль приложения."
    )
    config_fields = [
        ConfigField("user", "Адрес ящика", placeholder="tenders@project-med.ru"),
        ConfigField("password", "Пароль приложения", secret=True),
        ConfigField("imap_host", "IMAP-сервер", required=False, placeholder="imap.mail.ru"),
        ConfigField("folder", "Папка", required=False, placeholder="INBOX"),
        ConfigField(
            "senders",
            "Только от этих доменов (через запятую)",
            required=False,
            placeholder="b2b-center.ru, bicotender.ru",
        ),
    ]

    def __init__(self) -> None:
        self._pending: dict[int, list[bytes]] = {}

    def fetch(self, source: Source, http: httpx.Client) -> Iterable[Any]:
        client = _connect(source.config)
        try:
            folder = source.config.get("folder") or "INBOX"
            status, _ = client.select(folder)
            if status != "OK":
                raise SourceError(f"Папка «{folder}» не найдена")
            _, data = client.uid("search", None, "UNSEEN")
            uids = (data[0] or b"").split()[-MAX_MESSAGES:]
            senders = [s.strip().lower() for s in (source.config.get("senders") or "").split(",") if s.strip()]
            self._pending[source.id] = []
            for uid in uids:
                _, msg_data = client.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
                if raw is None:
                    continue
                message = email.message_from_bytes(raw, policy=default_policy)
                sender = str(message.get("From") or "").lower()
                if senders and not any(s in sender for s in senders):
                    continue
                self._pending[source.id].append(uid)
                yield message
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    def parse(self, raw: Any, source: Source) -> Iterable[FoundItem]:
        return items_from_message(raw)

    def after_ingest(self, source: Source) -> None:
        uids = self._pending.pop(source.id, [])
        if not uids:
            return
        client = _connect(source.config)
        try:
            client.select(source.config.get("folder") or "INBOX")
            for uid in uids:
                client.uid("store", uid, "+FLAGS", r"(\Seen)")
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
