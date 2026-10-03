#!/usr/bin/env bash
# Ручной бэкап (автоматический выполняется каждый день в 03:30 МСК).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T worker python -m app.ops.backup run
docker compose exec -T worker python -m app.ops.backup list
