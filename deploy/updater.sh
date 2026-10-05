#!/bin/bash
# Applies an owner-approved update. The panel (/updates) writes a commit SHA to $STATE_DIR/request; this script
# (systemd timer, every 2 minutes) downloads exactly that commit from GitHub, rebuilds the stack, checks health
# and rolls back to the previous code if the new version does not start. Nothing updates on its own.
set -uo pipefail
STATE_DIR=${STATE_DIR:-/var/lib/medproject-update}
APP_DIR=${APP_DIR:-/opt/medproject-sales}
UPDATE_REPO=${UPDATE_REPO:-Aznaur445/medproject-sales}

requested=$(tr -d '[:space:]' < "$STATE_DIR/request" 2>/dev/null) || exit 0
current=$(tr -d '[:space:]' < "$STATE_DIR/current" 2>/dev/null || true)
[[ "$requested" =~ ^[0-9a-f]{40}$ ]] || exit 0
[ "$requested" = "$current" ] && exit 0
exec 9>/run/medproject-update.lock
flock -n 9 || exit 0

LOG=/var/log/medproject-update.log
exec >> "$LOG" 2>&1

set_state() {  # set_state STATE MESSAGE
  python3 - "$1" "$requested" "${2:-}" "$STATE_DIR/status.json" <<'PY'
import datetime, json, os, sys
state, sha, message, path = sys.argv[1:5]
at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
with open(path + ".tmp", "w") as f:
    json.dump({"state": state, "sha": sha, "message": message[-1500:], "at": at}, f, ensure_ascii=False)
os.chmod(path + ".tmp", 0o644)
os.replace(path + ".tmp", path)
PY
}
healthy() {
  for _ in $(seq 1 40); do
    docker compose exec -T api python -m app.ops.healthcheck api >/dev/null 2>&1 && return 0
    sleep 10
  done
  return 1
}

echo "=== $(date -u) update ${current:-unknown} -> $requested"
set_state running "Скачиваю и собираю новую версию"
cd "$APP_DIR" || { set_state failed "Нет каталога $APP_DIR"; exit 1; }
backup_note=""
docker compose exec -T worker python -m app.ops.backup run || backup_note=" (резервная копия не создана, см. журнал)"

NEW="${APP_DIR}.new"
PREV="${APP_DIR}.prev"
rm -rf "$NEW" && mkdir -p "$NEW"
if ! { curl -sSL --fail --retry 3 -o /tmp/medproject-update.tar.gz \
         "https://codeload.github.com/${UPDATE_REPO}/tar.gz/${requested}" \
       && tar -xzf /tmp/medproject-update.tar.gz -C "$NEW" --strip-components=1 \
       && [ -f "$NEW/docker-compose.yml" ]; }; then
  rm -rf "$NEW"
  echo "$current" > "$STATE_DIR/request"
  set_state failed "Не удалось скачать версию ${requested:0:7} с GitHub"
  exit 1
fi
rm -f /tmp/medproject-update.tar.gz
cp -a "$APP_DIR/.env" "$NEW/.env"
echo "$requested" > "$NEW/.deployed_key"
rm -rf "$PREV" && mv "$APP_DIR" "$PREV" && mv "$NEW" "$APP_DIR"
cd "$APP_DIR" || exit 1

if docker compose up -d --build --remove-orphans && healthy; then
  echo "$requested" > "$STATE_DIR/current"
  install -m 0755 deploy/updater.sh /usr/local/bin/medproject-update
  set_state ok "Версия ${requested:0:7} установлена${backup_note}"
  echo "=== $(date -u) ok"
else
  failure=$(tail -n 30 "$LOG")
  rm -rf "${APP_DIR}.failed" && mv "$APP_DIR" "${APP_DIR}.failed" && mv "$PREV" "$APP_DIR"
  cd "$APP_DIR" && docker compose up -d --build --remove-orphans
  echo "$current" > "$STATE_DIR/request"  # do not retry the broken version every 2 minutes
  set_state rolled_back "Новая версия не запустилась, возвращена прежняя.${backup_note} ${failure}"
  echo "=== $(date -u) rolled back"
fi
