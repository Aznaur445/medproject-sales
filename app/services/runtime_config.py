"""Effective Telegram/mail configuration: values from .env win; otherwise what the owner entered in the panel.

Read from the database with a short cache so that alerts still work (from .env or the last cached value)
when the database is unavailable.
"""

import time
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_secret

log = get_logger(__name__)
CACHE_SECONDS = 30
_cache: tuple[float, object] | None = None


@dataclass(frozen=True)
class TelegramConfig:
    token: str | None
    owner_ids: tuple[int, ...]

    @property
    def ready(self) -> bool:
        return bool(self.token and self.owner_ids)


@dataclass(frozen=True)
class MailConfig:
    user: str | None
    password: str | None
    from_name: str

    @property
    def ready(self) -> bool:
        return bool(self.user and self.password)


def _integrations():
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    from app.core.db import sync_session
    from app.services import settings_store as ss

    try:
        with sync_session() as db:
            value = ss.load_sync(db, ss.Integrations)
    except Exception:  # noqa: BLE001 - database down: keep the previous value
        log.warning("integrations_unavailable")
        return _cache[1] if _cache else ss.Integrations()
    _cache = (now, value)
    return value


def reset_cache() -> None:
    global _cache
    _cache = None


def _decrypt(value: str) -> str | None:
    if not value:
        return None
    try:
        return decrypt_secret(value)
    except ValueError:
        log.error("integration_secret_undecryptable")
        return None


def telegram_config() -> TelegramConfig:
    settings = get_settings()
    if settings.telegram_bot_token is not None:
        return TelegramConfig(settings.telegram_bot_token.get_secret_value(), tuple(settings.telegram_owner_ids))
    data = _integrations()
    ids = tuple(settings.telegram_owner_ids) or tuple(data.telegram_owner_ids)
    return TelegramConfig(_decrypt(data.telegram_bot_token_enc), ids)


def mail_config() -> MailConfig:
    settings = get_settings()
    data = _integrations()
    user = settings.mail_user or data.mail_user or None
    if settings.mail_app_password is not None:
        password = settings.mail_app_password.get_secret_value()
    else:
        password = _decrypt(data.mail_app_password_enc)
    return MailConfig(user, password, data.mail_from_name or settings.mail_from_name)
