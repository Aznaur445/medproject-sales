"""Alerts and notifications to the owner's Telegram via Bot API (plain HTTP, usable from any process)."""

import time
from pathlib import Path

import httpx

from app.core.logging import get_logger
from app.core.redis import get_sync_redis
from app.services.runtime_config import telegram_config

log = get_logger(__name__)


def send_owner_message(text: str, *, dedup_key: str | None = None, dedup_seconds: int = 1800) -> bool:
    """Send a message to every owner Telegram id. Returns True if delivered at least once.

    `dedup_key` suppresses repeats of the same alert (e.g. a source that keeps failing).
    """
    tg = telegram_config()
    if not tg.ready:
        log.warning("telegram_not_configured", text_preview=text[:80])
        return False
    if dedup_key:
        try:
            if not get_sync_redis().set(f"alert:{dedup_key}", int(time.time()), nx=True, ex=dedup_seconds):
                return False
        except Exception:  # noqa: BLE001 - alerting must not fail because Redis is down
            log.warning("alert_dedup_unavailable")
    url = f"https://api.telegram.org/bot{tg.token}/sendMessage"
    delivered = False
    for chat_id in tg.owner_ids:
        try:
            resp = httpx.post(
                url, json={"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}, timeout=10
            )
            resp.raise_for_status()
            delivered = True
        except httpx.HTTPError as exc:
            log.error("telegram_send_failed", error=type(exc).__name__)
    return delivered


def telegram_call(method: str, payload: dict) -> list[dict]:
    """Call a Bot API method for every owner chat (chat_id is filled in). Returns successful results."""
    tg = telegram_config()
    if not tg.ready:
        log.warning("telegram_not_configured", method=method)
        return []
    url = f"https://api.telegram.org/bot{tg.token}/{method}"
    results = []
    for chat_id in tg.owner_ids:
        try:
            resp = httpx.post(url, json={**payload, "chat_id": chat_id}, timeout=15)
            resp.raise_for_status()
            results.append(resp.json().get("result", {}))
        except httpx.HTTPError as exc:
            log.error("telegram_call_failed", method=method, error=type(exc).__name__)
    return results


def telegram_send_document(path: Path, filename: str, caption: str = "") -> bool:
    tg = telegram_config()
    if not tg.ready:
        return False
    url = f"https://api.telegram.org/bot{tg.token}/sendDocument"
    ok = False
    for chat_id in tg.owner_ids:
        try:
            with path.open("rb") as fh:
                resp = httpx.post(
                    url,
                    data={"chat_id": str(chat_id), "caption": caption[:1000]},
                    files={"document": (filename, fh)},
                    timeout=60,
                )
            resp.raise_for_status()
            ok = True
        except (httpx.HTTPError, OSError) as exc:
            log.error("telegram_document_failed", error=type(exc).__name__)
    return ok


def alert(title: str, details: str = "", *, dedup_key: str | None = None) -> bool:
    return send_owner_message(f"⚠️ {title}\n{details}".strip(), dedup_key=dedup_key)
