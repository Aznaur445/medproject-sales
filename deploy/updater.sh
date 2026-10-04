#!/bin/bash
# Applies an update only when s3://$S3_BUCKET/deploy/update-request contains a code key that differs from the
# deployed one. The request is written on the owner's command; nothing updates automatically otherwise.
set -uo pipefail
s3() {
  local method=$1 key=$2; shift 2
  curl -sS --fail --retry 3 --retry-delay 5 --aws-sigv4 "aws:amz:${S3_REGION}:s3" --user "${S3_ACCESS}:${S3_SECRET}" \
    -X "$method" "$@" "${S3_ENDPOINT}/${S3_BUCKET}/${key}"
}
report() { s3 PUT "deploy/status/$(date -u +%Y%m%dT%H%M%SZ)-$1" --data-binary "${2:-}" -o /dev/null || true; }

requested=$(s3 GET deploy/update-request 2>/dev/null | tr -d '[:space:]') || exit 0
current=$(cat "$APP_DIR/.deployed_key" 2>/dev/null || true)
[ -z "$requested" ] || [ "$requested" = "$current" ] && exit 0

exec >> /var/log/medproject-update.log 2>&1
echo "=== $(date -u) update $current -> $requested"
report "update-start-$(basename "$requested")"
cd "$APP_DIR" || exit 1
docker compose exec -T worker python -m app.ops.backup run || { report "update-FAILED-backup"; exit 1; }
tmp=$(mktemp -d)
s3 GET "$requested" -o "$tmp/code.tar.gz" && tar -xzf "$tmp/code.tar.gz" -C "$tmp" \
  || { report "update-FAILED-download"; rm -rf "$tmp"; exit 1; }
rm -f "$tmp/code.tar.gz"
# Replace code, keep .env and runtime data (volumes are outside the directory).
cp -a "$tmp"/. "$APP_DIR"/ && rm -rf "$tmp"
docker compose up -d --build || { report "update-FAILED-compose" "$(tail -n 60 /var/log/medproject-update.log)"; exit 1; }
echo "$requested" > "$APP_DIR/.deployed_key"
sleep 30
docker compose ps > /tmp/ps.txt 2>&1
report "update-ok-$(basename "$requested")" "$(cat /tmp/ps.txt)"
