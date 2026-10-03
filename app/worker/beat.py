"""Celery beat scheduler that reports its own heartbeat (used by /health and the watchdog)."""

import time

from celery.beat import PersistentScheduler

from app.core.redis import get_sync_redis


class HeartbeatScheduler(PersistentScheduler):
    def tick(self, *args, **kwargs):
        try:
            get_sync_redis().set("heartbeat:scheduler", time.time(), ex=3600)
        except Exception:  # noqa: BLE001,S110 - beat must keep scheduling even if Redis blips
            pass
        return super().tick(*args, **kwargs)
