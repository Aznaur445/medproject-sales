#!/bin/bash
# First boot of a Timeweb server (passed as cloud-init user data with the variables below filled in).
# Installs Docker, firewall and fail2ban, downloads the code (embedded archive, CODE_URL such as a GitHub
# tarball of a pinned commit, or the private S3 bucket), writes .env,
# starts the stack and installs the update timer. Progress is reported as marker objects in
# s3://$S3_BUCKET/deploy/status/ so the setup can be followed through the Timeweb API without SSH.
set -uo pipefail
exec > >(tee -a /var/log/medproject-bootstrap.log) 2>&1

S3_ENDPOINT="__S3_ENDPOINT__"
S3_REGION="__S3_REGION__"
S3_BUCKET="__S3_BUCKET__"
S3_ACCESS="__S3_ACCESS__"
S3_SECRET="__S3_SECRET__"
CODE_KEY="__CODE_KEY__"
CODE_URL="__CODE_URL__"  # optional tar.gz URL: presigned S3 URL or https://codeload.github.com/OWNER/REPO/tar.gz/SHA
STATUS_TOKEN="__STATUS_TOKEN__"  # private path of the progress page https://DOMAIN/STATUS_TOKEN/log.txt
DOMAIN="__DOMAIN__"
STATUS_DOMAIN="__STATUS_DOMAIN__"
DATABASE_URL="__DATABASE_URL__"
DB_SCHEMA="__DB_SCHEMA__"
SETUP_TOKEN="__SETUP_TOKEN__"
RESET_SCHEMA="__RESET_SCHEMA__"  # "1": drop DB_SCHEMA before migrations (reinstall whose old .env keys are lost)
APP_DIR=/opt/medproject-sales

s3() {  # s3 METHOD KEY [curl args...]
  local method=$1 key=$2; shift 2
  curl -sS --fail --retry 5 --retry-delay 5 --aws-sigv4 "aws:amz:${S3_REGION}:s3" --user "${S3_ACCESS}:${S3_SECRET}" \
    -X "$method" "$@" "${S3_ENDPOINT}/${S3_BUCKET}/${key}"
}
report() {
  echo "### STEP $1 $(date -u +%H:%M:%S)"
  s3 PUT "deploy/status/$(date -u +%Y%m%dT%H%M%SZ)-$1" --data-binary "${2:-}" -o /dev/null 2>/dev/null || true
  publish_log
}
publish_log() {
  [ -d /var/lib/mp-status ] || return 0
  mkdir -p "/var/lib/mp-status/${STATUS_TOKEN}"
  tail -n 300 /var/log/medproject-bootstrap.log > "/var/lib/mp-status/${STATUS_TOKEN}/log.txt" 2>/dev/null || true
}
status_page_up() {  # temporary HTTPS page with the install log until the real stack takes ports 80/443
  mkdir -p /var/lib/mp-status
  docker rm -f mp-status >/dev/null 2>&1
  docker run -d --name mp-status --restart unless-stopped -p 80:80 -p 443:443 -v /var/lib/mp-status:/srv:ro \
    caddy:2-alpine caddy file-server --domain "$DOMAIN" --root /srv >/dev/null 2>&1 || echo "status page failed"
}
fail() {
  report "FAILED-$1" "$(tail -n 60 /var/log/medproject-bootstrap.log)"
  status_page_up; publish_log
  exit 1
}

# A public IPv4 may be attached a few minutes after the first boot: wait for internet access (up to an hour).
for _ in $(seq 1 120); do
  curl -sS -o /dev/null --max-time 10 https://download.docker.com && break
  sleep 30
done
report "01-start"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq ca-certificates curl ufw fail2ban python3 >/dev/null || fail "apt"

if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc || fail "docker-key"
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq && apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null \
    || fail "docker-install"
fi
systemctl enable --now docker
status_page_up
( while sleep 15; do publish_log; done ) &
LOG_PUBLISHER=$!
report "02-docker-ok"

ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
printf '[sshd]\nenabled = true\nmaxretry = 5\nfindtime = 10m\nbantime = 1h\n' > /etc/fail2ban/jail.d/sshd.local
systemctl enable --now fail2ban && systemctl restart fail2ban
report "03-firewall-ok"

mkdir -p "$APP_DIR"
if [ -s /root/medproject-code.tar.xz.b64 ]; then
  base64 -d /root/medproject-code.tar.xz.b64 | tar -xJf - -C "$APP_DIR" || fail "unpack-embedded-code"
elif [ -n "$CODE_URL" ]; then
  curl -sSL --fail --retry 5 -o /tmp/medproject.tar.gz "$CODE_URL" || fail "download-code-url"
  # GitHub tarballs wrap the tree in a single top-level directory: strip it.
  STRIP=0; tar -tzf /tmp/medproject.tar.gz | grep -qx 'docker-compose.yml' || STRIP=1
  tar -xzf /tmp/medproject.tar.gz -C "$APP_DIR" --strip-components="$STRIP" || fail "unpack-code"
  [ -f "$APP_DIR/docker-compose.yml" ] || fail "unpack-code-layout"
else
  s3 GET "$CODE_KEY" -o /tmp/medproject.tar.gz || fail "download-code"
  tar -xzf /tmp/medproject.tar.gz -C "$APP_DIR" || fail "unpack-code"
fi
echo "${CODE_KEY:-$CODE_URL}" > "$APP_DIR/.deployed_key"
report "04-code-ok"

cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
  FERNET=$(openssl rand -base64 32 | tr '+/' '-_')
  python3 - "$FERNET" <<PY || fail "env"
import re, sys
values = {
    "ENV": "prod", "PUBLIC_URL": "https://${DOMAIN}", "DOMAIN": "${DOMAIN}", "STATUS_DOMAIN": "${STATUS_DOMAIN}",
    "ACME_EMAIL": "", "SECRET_KEY": "$(openssl rand -hex 48)", "FERNET_KEY": sys.argv[1],
    "POSTGRES_PASSWORD": "$(openssl rand -hex 24)", "COMPOSE_PROFILES": "", "DATABASE_URL": "${DATABASE_URL}",
    "DB_SCHEMA": "${DB_SCHEMA}", "SETUP_TOKEN": "${SETUP_TOKEN}",
    "S3_ENDPOINT_URL": "${S3_ENDPOINT}", "S3_REGION": "${S3_REGION}", "S3_BUCKET": "${S3_BUCKET}",
    "S3_ACCESS_KEY": "${S3_ACCESS}", "S3_SECRET_KEY": "${S3_SECRET}",
}
text = open(".env").read()
for key, value in values.items():
    line = f"{key}={value}"
    text, n = re.subn(rf"^#?\s*{key}=.*$", lambda _m: line, text, count=1, flags=re.M)
    if not n:
        text += "\n" + line
open(".env", "w").write(text + "\n")
PY
  chmod 600 .env
  # Off-server copy of the keys (private bucket): needed to restore backups on a new server.
  s3 PUT "deploy/secrets/env" --data-binary @.env -o /dev/null || true
fi
report "05-env-ok"

docker compose build || fail "compose-build"
if [ "$RESET_SCHEMA" = "1" ] && [ -n "$DB_SCHEMA" ]; then
  # Data encrypted with the lost FERNET_KEY of the previous install cannot be read: start from a clean schema.
  docker compose run --rm --no-deps -T --entrypoint python migrate -c "
import os, sqlalchemy as sa
schema = os.environ['DB_SCHEMA']
assert schema.isidentifier()
with sa.create_engine(os.environ['DATABASE_URL']).begin() as conn:
    conn.execute(sa.text(f'DROP SCHEMA IF EXISTS \"{schema}\" CASCADE'))
print('schema dropped:', schema)
" || fail "reset-schema"
fi
docker rm -f mp-status >/dev/null 2>&1  # free ports 80/443 for the real stack
docker compose up -d || fail "compose-up"
report "06-started"
for _ in $(seq 1 60); do
  if docker compose exec -T api python -m app.ops.healthcheck api >/dev/null 2>&1; then report "07-healthy"; break; fi
  sleep 10
done
docker compose ps > /tmp/ps.txt 2>&1; cat /tmp/ps.txt; report "08-ps" "$(cat /tmp/ps.txt)"

# Owner-approved updates from the panel (/updates): the API writes a commit SHA, this timer applies it.
mkdir -p /var/lib/medproject-update
CODE_SHA=$(basename "${CODE_URL:-}")
[[ "$CODE_SHA" =~ ^[0-9a-f]{40}$ ]] && echo "$CODE_SHA" > /var/lib/medproject-update/current
chown 1000:1000 /var/lib/medproject-update  # uid of the "app" user in the image
install -m 0755 deploy/updater.sh /usr/local/bin/medproject-update
cat > /etc/medproject-update.env <<CONF
APP_DIR=${APP_DIR}
STATE_DIR=/var/lib/medproject-update
UPDATE_REPO=__UPDATE_REPO__
CONF
cat > /etc/systemd/system/medproject-update.service <<UNIT
[Unit]
Description=Apply an owner-approved MedProject update
[Service]
Type=oneshot
EnvironmentFile=/etc/medproject-update.env
ExecStart=/usr/local/bin/medproject-update
UNIT
cat > /etc/systemd/system/medproject-update.timer <<UNIT
[Unit]
Description=Check for an owner-approved MedProject update every 2 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload && systemctl enable --now medproject-update.timer
report "09-done"
kill "$LOG_PUBLISHER" 2>/dev/null || true
