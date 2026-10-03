"""Health checks used by /health, Docker healthchecks and the watchdog task."""

import time
from typing import Any

from sqlalchemy import text

from app.core.db import async_session
from app.core.redis import get_redis

HEARTBEAT_KEYS = {"worker": 180, "scheduler": 180, "bot": 180}  # component -> max age, seconds


async def check_db() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
        return {"ok": True, "ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


async def check_redis() -> dict[str, Any]:
    try:
        await get_redis().ping()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


async def check_heartbeats() -> dict[str, Any]:
    result: dict[str, Any] = {}
    now = time.time()
    try:
        redis = get_redis()
        for component, max_age in HEARTBEAT_KEYS.items():
            raw = await redis.get(f"heartbeat:{component}")
            age = None if raw is None else round(now - float(raw))
            result[component] = {"ok": age is not None and age <= max_age, "age_s": age}
    except Exception as exc:  # noqa: BLE001
        result = {c: {"ok": False, "error": type(exc).__name__} for c in HEARTBEAT_KEYS}
    return result


async def full_health() -> dict[str, Any]:
    db, redis, beats = await check_db(), await check_redis(), await check_heartbeats()
    ok = db["ok"] and redis["ok"] and all(b["ok"] for b in beats.values())
    return {"status": "ok" if ok else "degraded", "db": db, "redis": redis, "components": beats}


def full_health_sync() -> dict[str, Any]:
    """Same as full_health, for synchronous processes (Celery tasks)."""
    from app.core.db import get_sync_engine
    from app.core.redis import get_sync_redis

    try:
        with get_sync_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        db: dict[str, Any] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        db = {"ok": False, "error": type(exc).__name__}
    beats: dict[str, Any] = {}
    try:
        redis = get_sync_redis()
        redis.ping()
        redis_state: dict[str, Any] = {"ok": True}
        now = time.time()
        for component, max_age in HEARTBEAT_KEYS.items():
            raw = redis.get(f"heartbeat:{component}")
            age = None if raw is None else round(now - float(raw))
            beats[component] = {"ok": age is not None and age <= max_age, "age_s": age}
    except Exception as exc:  # noqa: BLE001
        redis_state = {"ok": False, "error": type(exc).__name__}
        beats = {c: {"ok": False} for c in HEARTBEAT_KEYS}
    ok = db["ok"] and redis_state["ok"] and all(b["ok"] for b in beats.values())
    return {"status": "ok" if ok else "degraded", "db": db, "redis": redis_state, "components": beats}
