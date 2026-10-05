import time

from app.core.redis import get_sync_redis
from app.services.health import full_health_sync
from app.worker import tasks


async def test_live(client):
    assert (await client.get("/health/live")).json()["status"] == "ok"


async def test_health_degraded_without_heartbeats(client):
    resp = await client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["db"]["ok"] and resp.json()["redis"]["ok"]


async def test_health_ok_with_heartbeats(client):
    for component in ("worker", "scheduler", "bot"):
        get_sync_redis().set(f"heartbeat:{component}", time.time())
    resp = await client.get("/health")
    assert resp.status_code == 200, resp.json()


def test_watchdog_alerts_on_stale_component(monkeypatch):
    sent = []
    monkeypatch.setattr(tasks, "alert", lambda title, details, dedup_key=None: sent.append(details))
    get_sync_redis().set("heartbeat:worker", time.time())
    get_sync_redis().set("heartbeat:scheduler", time.time())
    get_sync_redis().set("heartbeat:bot", time.time() - 3600)
    tasks.watchdog()
    assert sent and "Telegram-бот" in sent[0] and "планировщик" not in sent[0]


def test_watchdog_silent_when_healthy(monkeypatch):
    sent = []
    monkeypatch.setattr(tasks, "alert", lambda *a, **k: sent.append(a))
    for component in ("worker", "scheduler", "bot"):
        tasks.beat(component)
    assert full_health_sync()["status"] == "ok"
    tasks.watchdog()
    assert sent == []
