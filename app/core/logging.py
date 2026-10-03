"""Structured JSON logging with redaction of secrets and personal data."""

import logging
import re
import sys
from typing import Any

import structlog

SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "authorization", "cookie", "totp")
SENSITIVE_KEYS_EXACT = {"code", "otp", "pin"}
EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
PHONE_RE = re.compile(r"(?<!\d)(\+?[78])[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*(\d{2})(?!\d)")
BOT_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def mask_text(text: str) -> str:
    text = BOT_TOKEN_RE.sub("***bot-token***", text)
    text = EMAIL_RE.sub(r"\1***@\2", text)
    return PHONE_RE.sub(r"\1*******\2", text)


def _redact(value: Any, key: str | None = None) -> Any:
    if key is not None:
        lowered = key.lower()
        if lowered in SENSITIVE_KEYS_EXACT or any(part in lowered for part in SENSITIVE_KEY_PARTS):
            return "***"
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


def redact_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if k in {"timestamp", "level", "logger"} else _redact(v, k)) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        redact_processor,
    ]
    renderer = structlog.processors.JSONRenderer(ensure_ascii=False) if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
