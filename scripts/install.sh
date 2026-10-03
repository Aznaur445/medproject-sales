#!/usr/bin/env bash
# Установка сервиса на чистый VPS Ubuntu 22.04/24.04 одной командой:
#   sudo ./scripts/install.sh
# Скрипт можно запускать повторно: уже сделанные шаги пропускаются.
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
ask()  { local prompt="$1" default="${2:-}" answer; read -r -p "$prompt${default:+ [$default]}: " answer; echo "${answer:-$default}"; }

[[ $EUID -eq 0 ]] || { echo "Запустите через sudo: sudo ./scripts/install.sh"; exit 1; }
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || warn "Скрипт проверялся на Ubuntu, у вас ${PRETTY_NAME:-unknown}. Продолжаю."

say "1/7 Системные пакеты"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl git ufw fail2ban openssl >/dev/null

say "2/7 Docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null
docker compose version

say "3/7 Файрвол: наружу открыты только SSH, 80 и 443"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null
ufw status | sed -n '1,12p'

say "4/7 fail2ban для SSH"
cat > /etc/fail2ban/jail.d/sshd.local <<'JAIL'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
JAIL
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban

if grep -qsE '^\s*PasswordAuthentication\s+yes' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null \
   || ! grep -qsE '^\s*PasswordAuthentication\s+no' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null; then
  if [[ -s /root/.ssh/authorized_keys ]] || ls /home/*/.ssh/authorized_keys >/dev/null 2>&1; then
    if [[ "$(ask 'SSH-ключ найден. Отключить вход по паролю (рекомендуется)? yes/no' 'yes')" == "yes" ]]; then
      echo "PasswordAuthentication no" > /etc/ssh/sshd_config.d/60-medproject.conf
      systemctl reload ssh || systemctl reload sshd
      echo "Вход по паролю отключён. Проверьте вход по ключу в НОВОМ окне, прежде чем закрывать это."
    fi
  else
    warn "SSH-ключ не найден, вход по паролю оставлен. Добавьте ключ по docs/DEPLOY.md и запустите скрипт снова."
  fi
fi

say "5/7 Настройки (.env)"
if [[ ! -f .env ]]; then
  cp .env.example .env
  domain=$(ask "Домен панели (например crm.project-med.ru)")
  status_domain=$(ask "Домен страницы мониторинга" "status.${domain#*.}")
  acme_email=$(ask "E-mail для сертификатов HTTPS")
  tg_token=$(ask "Токен Telegram-бота (можно оставить пустым и заполнить позже)")
  tg_id=$(ask "Ваш Telegram ID (можно позже)")
  fernet=$(openssl rand -base64 32 | tr '+/' '-_')
  db_mode=$(ask "База данных: 1 — в Docker на этом сервере, 2 — управляемая база Timeweb Cloud" "1")
  sed -i \
    -e "s|^PUBLIC_URL=.*|PUBLIC_URL=https://${domain}|" \
    -e "s|^DOMAIN=.*|DOMAIN=${domain}|" \
    -e "s|^STATUS_DOMAIN=.*|STATUS_DOMAIN=${status_domain}|" \
    -e "s|^ACME_EMAIL=.*|ACME_EMAIL=${acme_email}|" \
    -e "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 48)|" \
    -e "s|^FERNET_KEY=.*|FERNET_KEY=${fernet}|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" \
    -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=${tg_token}|" \
    -e "s|^TELEGRAM_OWNER_IDS=.*|TELEGRAM_OWNER_IDS=${tg_id}|" \
    .env
  if [[ "$db_mode" == "2" ]]; then
    echo "Данные подключения смотрите в панели Timeweb: Базы данных → ваша база → Подключение."
    db_host=$(ask "Хост базы (лучше приватный IP, если сервер и база в одной сети)")
    db_port=$(ask "Порт" "5432")
    db_name=$(ask "Имя базы (создайте отдельную базу для сервиса, например medproject)" "medproject")
    db_user=$(ask "Пользователь")
    read -r -s -p "Пароль пользователя базы: " db_pass; echo
    db_url=$(DB_USER="$db_user" DB_PASS="$db_pass" DB_HOST="$db_host" DB_PORT="$db_port" DB_NAME="$db_name" \
      python3 - <<'PY'
import os
from urllib.parse import quote

env = os.environ
user, password, name = (quote(env[k], safe="") for k in ("DB_USER", "DB_PASS", "DB_NAME"))
print(f"postgresql+psycopg://{user}:{password}@{env['DB_HOST']}:{env['DB_PORT']}/{name}?sslmode=require")
PY
)
    sed -i -e "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=|" .env
    sed -i -e "/^# DATABASE_URL=/d" .env
    echo "DATABASE_URL=${db_url}" >> .env
  fi
  chmod 600 .env
  echo "Файл .env создан. Ключи сгенерированы. Сохраните копию .env в надёжном месте:"
  echo "без FERNET_KEY нельзя расшифровать сохранённые пароли из бэкапа."
else
  echo ".env уже существует, не трогаю."
  # Installs made before COMPOSE_PROFILES existed used the local database.
  if ! grep -q '^COMPOSE_PROFILES=' .env && ! grep -q '^DATABASE_URL=' .env; then
    echo "COMPOSE_PROFILES=localdb" >> .env
  fi
fi

say "6/7 Сборка и запуск"
docker compose up -d --build
echo -n "Жду готовности API"
for _ in $(seq 1 60); do
  if docker compose exec -T api python -m app.ops.healthcheck api >/dev/null 2>&1; then echo " готово"; break; fi
  echo -n "."; sleep 3
done
docker compose ps

say "7/7 Пользователь панели"
if [[ "$(ask 'Создать владельца панели сейчас? yes/no' 'yes')" == "yes" ]]; then
  login=$(ask "Логин" "owner")
  docker compose exec api python -m app.cli create-user "$login"
fi

say "Готово"
. ./.env
cat <<DONE
Панель:      https://${DOMAIN}
Мониторинг:  https://${STATUS_DOMAIN}  (при первом входе создайте админа Uptime Kuma)
Каталог:     ${PROJECT_DIR}

Дальше: docs/DEPLOY.md, раздел «После установки».
DONE
