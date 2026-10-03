"""Telegram bot (aiogram 3). Access only for whitelisted owner Telegram IDs.

Stage 1: skeleton with whitelist, /start, /health and heartbeat. Approval cards arrive in stage 2.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import Message, TelegramObject

from app.bot import approvals
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import get_redis
from app.services.health import full_health

log = get_logger(__name__)
router = Router()


class WhitelistMiddleware(BaseMiddleware):
    """Drops every update from users that are not in TELEGRAM_OWNER_IDS."""

    def __init__(self, allowed_ids: set[int]) -> None:
        self.allowed_ids = allowed_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in self.allowed_ids:
            log.warning("bot_access_denied", tg_user_id=getattr(user, "id", None))
            return None
        return await handler(event, data)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Бот МедПроект подключён.\n"
        "Сюда приходят КП на согласование, новые заявки и уведомления о сбоях.\n\n"
        "/today — что требует внимания сегодня\n"
        "/pipeline — воронка\n"
        "/stats — статистика за неделю\n"
        "/add — добавить заявку (ссылка или описание)\n"
        "/pause, /resume — остановить или возобновить всю отправку\n"
        "/health — состояние сервиса"
    )


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    health = await full_health()
    names = {"worker": "Обработчик задач", "scheduler": "Планировщик", "bot": "Бот"}
    lines = [
        "✅ Всё работает" if health["status"] == "ok" else "⚠️ Есть сбои",
        f"База данных: {'OK' if health['db']['ok'] else 'ошибка'}",
        f"Redis: {'OK' if health['redis']['ok'] else 'ошибка'}",
    ]
    lines += [f"{names[k]}: {'OK' if v['ok'] else 'нет сигнала'}" for k, v in health["components"].items()]
    await message.answer("\n".join(lines))


async def heartbeat_loop() -> None:
    while True:
        try:
            await get_redis().set("heartbeat:bot", time.time(), ex=3600)
        except Exception:  # noqa: BLE001
            log.warning("bot_heartbeat_failed")
        await asyncio.sleep(60)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "dev")
    if settings.telegram_bot_token is None or not settings.telegram_owner_ids:
        log.error("bot_not_configured", hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_IDS")
        # Keep the container alive and reporting so the rest of the stack works before the bot is set up.
        await heartbeat_loop()
        return
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    dp = Dispatcher(storage=RedisStorage.from_url(settings.redis_url))
    dp.update.outer_middleware(WhitelistMiddleware(set(settings.telegram_owner_ids)))
    dp.include_router(router)
    dp.include_router(approvals.router)
    hb = asyncio.create_task(heartbeat_loop())
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        hb.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
