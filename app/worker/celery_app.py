from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

celery = Celery("medproject", broker=settings.redis_url, backend=None, include=["app.worker.tasks"])
celery.conf.update(
    timezone=settings.timezone,
    enable_utc=True,
    task_acks_late=True,  # a task killed mid-way is redelivered; tasks are idempotent
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=15 * 60,
    task_soft_time_limit=14 * 60,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 20 * 60},
    worker_hijack_root_logger=False,
    beat_scheduler="app.worker.beat:HeartbeatScheduler",
    beat_max_loop_interval=60,  # beat wakes at least once a minute, so its heartbeat stays fresh
    beat_schedule={
        "worker-heartbeat": {"task": "app.worker.tasks.worker_heartbeat", "schedule": 60.0},
        "watchdog": {"task": "app.worker.tasks.watchdog", "schedule": 300.0},
        # Times are in settings.timezone (Europe/Moscow by default).
        "daily-backup": {"task": "app.worker.tasks.daily_backup", "schedule": crontab(hour=3, minute=30)},
        "backup-watchdog": {"task": "app.worker.tasks.backup_watchdog", "schedule": crontab(hour=9, minute=7)},
    },
)


@setup_logging.connect
def _configure_logging(**_kwargs: object) -> None:
    configure_logging(settings.log_level, json=settings.env != "dev")
