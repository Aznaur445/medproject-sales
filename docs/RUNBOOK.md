# Что делать, если что-то сломалось

Все команды выполняются на сервере из каталога `/opt/medproject-sales`.

## Быстрая диагностика

```bash
docker compose ps                    # у всех сервисов должно быть Up (healthy)
docker compose logs --tail 100 api   # логи сервиса: api, worker, scheduler, bot, postgres, caddy
curl -s https://crm.ваш-домен/health # состояние компонентов
df -h /                              # свободное место на диске
```

## Типовые ситуации

### Пришло «⚠️ Сбой сервиса: обработчик задач: нет сигнала»
```bash
docker compose logs --tail 200 worker
docker compose restart worker
```
Если не помогло, посмотрите строки с `error` в логах и пришлите их разработчику.

### Панель не открывается
1. `docker compose ps`: работают ли `caddy` и `api`?
2. `docker compose logs --tail 100 caddy`. Если в логах ошибка сертификата, проверьте, что A-запись домена указывает на IP сервера.
3. `docker compose up -d` поднимет всё, что остановлено.

### Потерян телефон с аутентификатором
```bash
docker compose exec api python -m app.cli reset-2fa owner
```
При следующем входе появится новый QR-код.

### Забыт пароль
```bash
docker compose exec api python -m app.cli set-password owner
```
Все открытые сессии будут завершены.

### Закончилось место на диске
```bash
docker system prune -f           # удаляет старые образы (данные не трогает)
docker compose exec worker du -sh /data/backups /data/files
```
Локальные бэкапы старше 14 дней удаляются автоматически.

### «Нет свежего бэкапа» или «Бэкап не выполнен»
```bash
docker compose logs --tail 200 worker | grep -i backup
./scripts/backup.sh
```
Частые причины: кончилось место на диске или неверные ключи S3 в `.env`.

### Восстановление из бэкапа
```bash
./scripts/restore.sh            # показать список бэкапов
./scripts/restore.sh <метка>    # восстановить; текущие данные будут заменены
```

### Перенос на новый сервер
1. Установите сервис по `docs/DEPLOY.md`. На шаге 7 **сначала** положите старый `.env` в `/opt/medproject-sales/.env`: нужен тот же `FERNET_KEY`.
2. Выполните `./scripts/restore.sh <метка>`. Бэкап скачается из S3.

## Где лежат данные

| Что | Где |
|---|---|
| База данных | Docker-том `medproject_pgdata` |
| Файлы (КП, документы) | том `medproject_files` → `/data/files` |
| Локальные бэкапы | том `medproject_backups` → `/data/backups` |
| Копии бэкапов | S3-бакет, папка `backups/` |
| Настройки и секреты | `/opt/medproject-sales/.env` |
