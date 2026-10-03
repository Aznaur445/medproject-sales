# МедПроект · Продажи

Круглосуточный сервис для ИП, который проектирует медицинские организации. Сервис:

- находит коммерческие заявки на проектирование клиник, поликлиник, стоматологий, лабораторий и диагностических центров (госзакупки исключены);
- оценивает их и считает стоимость;
- готовит КП по шаблону;
- после согласования владельцем отправляет КП с корпоративной почты и ведёт переписку и воронку.

**Главное правило:** ни одно сообщение клиенту не уходит без явного «Согласовано» владельца.

## Состав

| Сервис | Назначение |
|---|---|
| `api` | Веб-панель (FastAPI + Jinja2 + HTMX), `/health` |
| `worker` | Celery: фоновые задачи (поиск, разбор документов, отправка почты, бэкапы) |
| `scheduler` | Celery beat: расписание |
| `bot` | Telegram-бот (aiogram 3), доступ только по Telegram ID владельца |
| `postgres`, `redis` | База данных и очереди |
| `caddy` | HTTPS-прокси с автоматическими сертификатами |
| `uptime-kuma` | Внешний мониторинг и алерты в Telegram |

Отдельного сервиса `web` нет: панель рендерится на сервере тем же `api`. Это один образ и меньше движущихся частей (см. `docs/DECISIONS.md`).

## Документация

- [docs/DEPLOY.md](docs/DEPLOY.md): установка на VPS Timeweb Cloud пошагово
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md): инструкция для владельца
- [docs/RUNBOOK.md](docs/RUNBOOK.md): что делать, если что-то сломалось
- [docs/DECISIONS.md](docs/DECISIONS.md): принятые технические решения
- [docs/ROADMAP.md](docs/ROADMAP.md): этапы и статус

## Разработка

Нужны Python 3.12, [uv](https://docs.astral.sh/uv/), PostgreSQL и Redis.

```bash
uv sync
cp .env.example .env   # заполните SECRET_KEY, FERNET_KEY, DATABASE_URL, REDIS_URL, ENV=dev
uv run alembic upgrade head
uv run python -m app.cli create-user owner
uv run uvicorn app.main:app --reload
uv run celery -A app.worker.celery_app:celery worker -l INFO
uv run celery -A app.worker.celery_app:celery beat -l INFO
uv run python -m app.bot.main
```

Тесты (нужна база `medproject_test` и Redis):

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Секреты хранятся только в `.env` (он в `.gitignore`). Пароли приложений, которые сохраняются в БД, шифруются ключом `FERNET_KEY`.
