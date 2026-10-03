#!/usr/bin/env bash
# Обновление по команде владельца: бэкап -> git pull -> пересборка -> миграции -> перезапуск.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "==> Бэкап перед обновлением"
docker compose exec -T worker python -m app.ops.backup run
echo "==> Получаю новую версию"
git pull --ff-only
echo "==> Сборка и перезапуск"
docker compose up -d --build
docker compose ps
