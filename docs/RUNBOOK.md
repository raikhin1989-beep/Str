# Эксплуатация

Боевой адрес: https://raikhin.duckdns.org/
Резервный, если основной не открывается: http://91.212.150.225/ — без DNS
вообще. Это открытый HTTP, годится «чтобы хоть посмотреть». · админка `/admin`, логин `admin`,
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
curl https://raikhin.duckdns.org/api/health          # публично: жив и версия
curl -u admin:ПАРОЛЬ https://raikhin.duckdns.org/api/admin/status
```

Публичный `health` отдаёт только `status`, `version` и аптайм — счётчики
гостям ни к чему. Подробности под паролем, они же показаны первой строкой
в админке.

На что смотреть:

- `version` не совпадает с `main` → деплой не доехал. **Метка пишется
  последним шагом**, так что свежий SHA означает «деплой дошёл до конца»;
- `backup.last_at` старше суток → таймер бэкапов встал (в админке подсветится
  красным после 30 часов);
- `telegram.queue_pending` растёт и не убывает → задача рассылки не ходит;
- `telegram.last_poll_at` старше часа → то же самое, привязка гостей встала.

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

## Переехать на свой домен

1. Направить A-запись домена на `91.212.150.225`.
2. Поменять `SITE_DOMAIN_FIXED` в `.github/workflows/deploy.yml`.
3. Обновить `SITE_URL` там же не нужно — он собирается из домена, но
   **поправить вручную придётся** `og:url` в `site/index.html` и `SITE_URL`
   в `scripts/make_ics.py`, затем перегенерировать `site/calendar.ics`.
4. Запушить. Caddy выпустит сертификат сам.

## Обновить шрифты

```
pip install fonttools brotli
python3 scripts/getfonts.py     # урезает до нужных символов и проверяет глифы
```

Скрипт падает, если после урезания в шрифте не осталось нужных букв — это
защита от молчаливой поломки, когда страница уезжает в системный шрифт.

## Изменить дату, место или продолжительность события

Дата и время не лежат в одном месте — сайт статический, а часть строк
уезжает в Telegram и в превью ссылки. Править нужно **все пять**, иначе
расхождение всплывёт не сразу и не у вас: в карточке репоста или в тексте
напоминания.

1. `site/index.html` — таймер (`target`, абсолютный офсет `+03:00`), плитка
   времени, тексты подтверждения формы **и три meta-строки** сверху
   (`description`, `og:description`, `og:image:alt`);
2. `backend/app.py` — `EVENT_AT` (от неё считается напоминание за два дня)
   и тексты сообщений бота — там время написано словами;
3. `scripts/make_ics.py` — затем `python3 scripts/make_ics.py` и закоммитить
   `site/calendar.ics`;
4. `scripts/og_template.html` — плитка времени в превью; затем
   `python3 scripts/make_og.py` и закоммитить `site/og.png`;
5. `docs/PLAN.md` и `docs/VERIFY.md` — иначе приёмка будет сверяться со
   старым временем и «пройдёт» на неверном.

Проверка, что ничего не забыли — поиском по старому значению:

```
grep -rn '16:00' site/ backend/ scripts/ docs/    # должно быть пусто
```

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
