#!/usr/bin/env bash
# Восстановление из бэкапа: ./scripts/restore.sh 20261003-033000
# Без аргумента показывает список бэкапов.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -lt 1 ]]; then
  docker compose run --rm --no-deps worker python -m app.ops.backup list
  echo "Укажите метку: ./scripts/restore.sh <метка>"
  exit 1
fi
echo "==> Останавливаю приложение (база и Redis продолжают работать)"
docker compose stop api worker scheduler bot
# Local database only when it is enabled (COMPOSE_PROFILES=localdb); a managed database is always running.
if grep -qE '^COMPOSE_PROFILES=.*localdb' .env; then
  docker compose up -d postgres redis
else
  docker compose up -d redis
fi
docker compose run --rm --no-deps worker python -m app.ops.backup restore "$1"
echo "==> Применяю миграции и запускаю"
docker compose up -d
docker compose ps
