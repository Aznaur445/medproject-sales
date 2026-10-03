import time

from celery.signals import worker_ready

from app.core.logging import get_logger
from app.core.redis import get_sync_redis
from app.services.health import full_health_sync
from app.services.notify import alert
from app.worker.celery_app import celery

log = get_logger(__name__)

COMPONENT_NAMES = {"worker": "обработчик задач", "scheduler": "планировщик", "bot": "Telegram-бот"}


def beat(component: str) -> None:
    get_sync_redis().set(f"heartbeat:{component}", time.time(), ex=3600)


@celery.task
def worker_heartbeat() -> None:
    beat("worker")


@celery.task
def watchdog() -> None:
    """Alert the owner in Telegram when a component stops reporting.

    If the worker itself is down this task cannot run; that case is covered by Uptime Kuma,
    which polls /health (HTTP 503 when degraded) and alerts independently.
    """
    health = full_health_sync()
    problems = []
    if not health["db"]["ok"]:
        problems.append("база данных недоступна")
    for name, state in health["components"].items():
        if not state["ok"]:
            problems.append(f"{COMPONENT_NAMES.get(name, name)}: нет сигнала")
    if problems:
        log.warning("watchdog_problems", problems=problems)
        alert("Сбой сервиса", "\n".join(problems), dedup_key="watchdog:" + ",".join(sorted(problems)))


BACKUP_MAX_AGE_SECONDS = 26 * 3600


@celery.task(acks_late=False)  # a half-made backup must not be retried blindly
def daily_backup() -> None:
    from app.ops.backup import run_backup

    try:
        path = run_backup()
    except Exception as exc:
        log.exception("backup_failed")
        alert("Бэкап не выполнен", f"{type(exc).__name__}. Подробности в логах worker.", dedup_key="backup_failed")
        raise
    get_sync_redis().set("backup:last_success", time.time())
    log.info("backup_ok", path=str(path))


@celery.task
def backup_watchdog() -> None:
    raw = get_sync_redis().get("backup:last_success")
    if raw is None or time.time() - float(raw) > BACKUP_MAX_AGE_SECONDS:
        alert("Нет свежего бэкапа", "Последний успешный бэкап старше 26 часов.", dedup_key="backup_stale")


@worker_ready.connect
def _on_worker_ready(**_kwargs: object) -> None:
    beat("worker")  # report immediately instead of waiting for the first scheduled heartbeat
