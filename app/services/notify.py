"""Alerts and notifications to the owner's Telegram via Bot API (plain HTTP, usable from any process)."""

import time

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.redis import get_sync_redis

log = get_logger(__name__)


def send_owner_message(text: str, *, dedup_key: str | None = None, dedup_seconds: int = 1800) -> bool:
    """Send a message to every owner Telegram id. Returns True if delivered at least once.

    `dedup_key` suppresses repeats of the same alert (e.g. a source that keeps failing).
    """
    settings = get_settings()
    if settings.telegram_bot_token is None or not settings.telegram_owner_ids:
        log.warning("telegram_not_configured", text_preview=text[:80])
        return False
    if dedup_key:
        try:
            if not get_sync_redis().set(f"alert:{dedup_key}", int(time.time()), nx=True, ex=dedup_seconds):
                return False
        except Exception:  # noqa: BLE001 - alerting must not fail because Redis is down
            log.warning("alert_dedup_unavailable")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}/sendMessage"
    delivered = False
    for chat_id in settings.telegram_owner_ids:
        try:
            resp = httpx.post(
                url, json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}, timeout=10
            )
            resp.raise_for_status()
            delivered = True
        except httpx.HTTPError as exc:
            log.error("telegram_send_failed", error=type(exc).__name__)
    return delivered


def alert(title: str, details: str = "", *, dedup_key: str | None = None) -> bool:
    return send_owner_message(f"⚠️ {title}\n{details}".strip(), dedup_key=dedup_key)
