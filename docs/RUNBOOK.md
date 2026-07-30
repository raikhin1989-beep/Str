# Эксплуатация

Боевой адрес: https://raikhin.duckdns.org/ · админка `/admin`, логин `admin`,
пароль — секрет `ADMIN_PASSWORD`.

## Что где лежит

| Что | Где |
| --- | --- |
| Статика сайта | `/var/www/html` (зеркалит `site/`, чистится `--delete`) |
| Код бэкенда | `/opt/str-api/src` (зеркалит `backend/`) |
| Виртуальное окружение | `/opt/str-api/venv` |
| База гостей | `/opt/str-api/data/str.db` |
| Снимки базы | `/opt/str-api/backups/str-*.db.gz`, 7 последних |
| Фото гостей | `/opt/str-api/data/photos`, архивы `backups/photos-*.tar.gz`, 2 копии |
| Сервис | systemd `str-api`, слушает `127.0.0.1:8000` |
| Бэкапы | systemd `str-backup.timer`, ежедневно в 03:30 UTC |
| Веб-сервер | Caddy, `/etc/caddy/Caddyfile` (переписывается при каждом деплое) |

## Проверить состояние

```
curl https://raikhin.duckdns.org/api/health
```

Отдаёт версию задеплоенного коммита, возраст свежего снимка и сводку по
Telegram. На что смотреть:

- `version` не совпадает с `main` → деплой не доехал;
- `backup.last_at` старше суток → таймер бэкапов встал;
- `telegram.queue_pending` растёт и не убывает → задача рассылки не ходит;
- `telegram.last_poll_at` старше часа → то же самое.

## Деплой и откат

Деплой — пуш в `main`, если затронуты `site/`, `backend/` или сам workflow.
Отдельного действия не требуется.

Откат: вернуть коммит и запушить.

```
git revert <sha> && git push origin main
```

Данные при откате не страдают: `/opt/str-api/data` деплой не трогает.
Но **миграции назад не откатываются** — если откат отменяет миграцию,
восстанавливать придётся из снимка.

## Восстановить базу из снимка

```
systemctl stop str-api
ls -1 /opt/str-api/backups/                      # выбрать нужный
gunzip -c /opt/str-api/backups/str-20260729-193945.db.gz > /tmp/str.db
sqlite3 /tmp/str.db 'PRAGMA integrity_check'     # должно ответить ok
cp /opt/str-api/data/str.db /opt/str-api/data/str.db.before-restore
rm -f /opt/str-api/data/str.db-wal /opt/str-api/data/str.db-shm
mv /tmp/str.db /opt/str-api/data/str.db
chown strapi:strapi /opt/str-api/data/str.db
systemctl start str-api
curl -s https://raikhin.duckdns.org/api/health
```

Файлы `-wal` и `-shm` обязательно убрать: они относятся к прежней базе, и
SQLite, найдя их рядом с восстановленной, может применить чужие транзакции.

Снимок сделан через `sqlite3.Connection.backup()`, а не копированием файла:
при работающем WAL обычный `cp` уносит базу без части свежих транзакций.

## Восстановить фотоальбом

Фото не лежат в снимке базы — там только метаданные. Архив с файлами
складывается рядом, хранится 2 копии.

```
systemctl stop str-api
ls -1 /opt/str-api/backups/photos-*.tar.gz
tar -xzf /opt/str-api/backups/photos-20260730-044748.tar.gz -C /tmp
rm -rf /opt/str-api/data/photos
mv /tmp/photos /opt/str-api/data/photos
chown -R strapi:strapi /opt/str-api/data/photos
systemctl start str-api
```

Восстанавливать базу и фото нужно **из пары снимков одного времени**: иначе
в базе окажутся записи о фото, которых нет на диске, или наоборот.

## Сделать снимок прямо сейчас

```
systemctl start str-backup.service
ls -la /opt/str-api/backups/
```

## Разослать сообщения из очереди немедленно

Сервер не может писать в Telegram — провайдер блокирует (решения Р7, Р8).
Отправка и приём идут задачей GitHub Actions каждые 30 минут.

Чтобы не ждать: **Actions → Telegram outbox → Run workflow**. За один прогон
она забирает обновления, «созревает» напоминания и разгребает очередь.

## Если бэкенд лёг

Приглашение продолжает открываться: дата, место, дресс-код и кнопка
календаря — статика, они не зависят от бэкенда. Форма записи честно
показывает «Запись временно недоступна» и блокирует кнопку.

```
systemctl status str-api
journalctl -u str-api -n 50 --no-pager
systemctl restart str-api
```

## Изменить дату, место или продолжительность события

Правится в трёх местах, все три обязательны:

1. `site/index.html` — таймер (`target`) и плитки с датой;
2. `backend/app.py` — `EVENT_AT` (от неё считается напоминание за два дня);
3. `scripts/make_ics.py` — затем запустить и закоммитить `site/calendar.ics`.

## Секреты

| Секрет | Без него |
| --- | --- |
| `SERVER_HOST`, `SERVER_PASSWORD` | деплой не работает |
| `ADMIN_PASSWORD` | `/admin` отвечает 503, рассылка не может ходить в API |
| `TELEGRAM_BOT_TOKEN` | бот выключен, остальное работает |
| `DUCKDNS_SUBDOMAIN`, `DUCKDNS_TOKEN` | A-запись не обновляется автоматически |
| `ACME_EMAIL` | не приходят уведомления Let's Encrypt |

Пароль админки попадает в systemd-юнит, поэтому юнит стоит с правами `600`.
Смена пароля применяется **только с очередным деплоем** — юнит пересобирается
при выкате.
