"""Бэкенд приглашения.

Шаг 4 — приём заявок и публичный список: POST /api/rsvp сохраняет ответ
гостя, GET /api/rsvp возвращает его собственную запись по токену,
GET /api/roster — список для всех, с сокращёнными именами.

Все маршруты живут под /api/*: Caddy отдаёт этот префикс сюда, а всё
остальное — статикой из /var/www/html.
"""
import csv
import hashlib
import io
import json
import math
import os
import secrets as pysecrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (HTMLResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

import db

# Документацию Swagger не публикуем: наружу смотрит открытый сайт,
# а схема API гостям ни к чему.
app = FastAPI(title="Str API", docs_url=None, redoc_url=None, openapi_url=None)

APP_VERSION = os.environ.get("APP_VERSION", "unknown")
STARTED_AT = time.time()

# Сколько заявок с одного адреса принимаем в час. Гостей десятки, так что
# порог щедрый для семьи за одним роутером и всё ещё бесполезен для бота.
RATE_LIMIT_PER_HOUR = 12

# Сколько раз пробуем отправить сообщение, прежде чем бросить.
MAX_ATTEMPTS = 5

FIELD_LABELS = {
    "name": "имя",
    "guests_count": "количество гостей",
    "drink": "напиток",
    "hype": "готовность угарать",
    "allergies": "аллергии",
    "message": "сообщение",
    "track": "трек",
}


ADMIN_USER = "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
BASE_DIR = Path(__file__).resolve().parent

basic = HTTPBasic(auto_error=False)


def admin_auth(cred: HTTPBasicCredentials | None = Depends(basic)) -> None:
    """Пароль приходит из секрета репозитория через systemd.

    Если он не задан, админка не открывается вообще — за ней полные имена
    гостей, аллергии и личные сообщения, и «временно без пароля» тут
    неприемлемо. Сравнение через compare_digest, чтобы время ответа не
    подсказывало подбирающему длину совпавшего префикса.
    """
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="Админка не настроена: не задан секрет ADMIN_PASSWORD.",
        )
    if cred is None:
        raise HTTPException(
            status_code=401, detail="Нужен пароль",
            headers={"WWW-Authenticate": 'Basic realm="Str admin"'},
        )
    ok = pysecrets.compare_digest(cred.username, ADMIN_USER) & pysecrets.compare_digest(
        cred.password, ADMIN_PASSWORD
    )
    if not ok:
        raise HTTPException(
            status_code=401, detail="Неверный пароль",
            headers={"WWW-Authenticate": 'Basic realm="Str admin"'},
        )


@app.on_event("startup")
def startup() -> None:
    db.init()


class RsvpIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    guests_count: int = Field(default=1, ge=1, le=10)
    drink: str = Field(min_length=1, max_length=40)
    hype: int = Field(default=7, ge=1, le=10)
    allergies: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=500)
    # Гость приносит свой напиток — его компанию не закладываем в закупку.
    brings_own: bool = False
    # Одна песня «на разминку». Длина с запасом на «Исполнитель — Название».
    track: str = Field(default="", max_length=120)
    # Токен своей записи, если гость уже отправлял форму с этого браузера.
    token: str | None = Field(default=None, max_length=64)
    # Ловушка для ботов: поле спрятано в вёрстке, человек его не заполнит.
    website: str = Field(default="", max_length=200)

    @field_validator("name", "drink", "allergies", "message", "track", mode="before")
    @classmethod
    def strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("пустое имя")
        return v


def client_ip(request: Request) -> str:
    """Между гостем и приложением стоит Caddy, поэтому реальный адрес
    приходит в X-Forwarded-For. Берём последний элемент: его дописывает
    сам Caddy, а всё, что левее, мог подставить клиент."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Ошибки валидации отдаём одной понятной строкой по-русски:
    страница показывает её гостю как есть."""
    first = exc.errors()[0] if exc.errors() else {}
    loc = [p for p in first.get("loc", []) if p != "body"]
    field = FIELD_LABELS.get(loc[0] if loc else "", "форма")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": f"Проверьте поле «{field}» — значение не подходит."},
    )


@app.get("/api/health")
def health():
    """Проверка живости — единственное, что нужно наружу.

    Версия остаётся: без неё не отличить «сервис отвечает» от «отвечает тот
    код, который выложили». Счётчики очереди и возраст бэкапа переехали под
    пароль в /api/admin/status — гостям они ни к чему.
    """
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
    }


@app.get("/api/admin/status")
def admin_status(_: None = Depends(admin_auth)):
    """Служебная сводка: что смотреть, если что-то встало.

    Возраст снимка тут не для красоты: молчаливо переставший работать бэкап
    обнаруживается только когда понадобился, а это поздно.
    """
    with db.get_conn() as conn:
        pending = conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE sent_at IS NULL AND attempts < ?",
            (MAX_ATTEMPTS,)).fetchone()["n"]
        last_sent = conn.execute(
            "SELECT sent_at FROM outbox WHERE sent_at IS NOT NULL"
            " ORDER BY sent_at DESC LIMIT 1").fetchone()
        last_poll = db.get_setting(conn, "last_poll_at")
        last_update = db.get_setting(conn, "last_webhook_at")

    backup_dir = Path(os.environ.get("STR_BACKUP_DIR", "/opt/str-api/backups"))
    snaps = sorted(backup_dir.glob("str-*.db.gz")) if backup_dir.exists() else []
    last_backup = None
    if snaps:
        last_backup = datetime.fromtimestamp(
            snaps[-1].stat().st_mtime, timezone.utc).isoformat(timespec="seconds")

    return {
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "backup": {"count": len(snaps), "last_at": last_backup},
        "telegram": {
            "bot": BOT_USERNAME,
            "last_poll_at": last_poll or None,
            "last_update_at": last_update or None,
            "queue_pending": pending,
            "last_sent_at": last_sent["sent_at"] if last_sent else None,
        },
    }


def short_name(full: str) -> str:
    """«Мария Иванова» → «Мария И.» (решение Р4).

    Сокращение делает сервер, а не страница: иначе полные фамилии уезжали бы
    в браузер любому, кто откроет devtools, и публичность списка означала бы
    публикацию персональных данных.

    Первое слово считается именем — поле в форме так и подписано («Имя и
    фамилия»). Если гость всё же напишет фамилию первой, в списке окажется
    его фамилия целиком; поправить это можно в админке на шаге 5.
    """
    parts = [p for p in full.split() if p]
    if not parts:
        return "Гость"
    if len(parts) == 1:
        return parts[0]
    initials = " ".join(f"{p[0].upper()}." for p in parts[1:3])
    return f"{parts[0]} {initials}"


# Сколько первых подтвердившихся попадают в «стартовую пятёрку».
STARTERS = 5


@app.get("/api/roster")
def roster():
    """Публичный список. Отдаёт только то, что решено показывать всем:
    сокращённое имя, размер компании и градус готовности. Аллергии и
    личные сообщения не покидают админку."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name, guests_count, hype FROM guests ORDER BY created_at, id"
        ).fetchall()
    return {
        "entries": len(rows),
        "people": sum(r["guests_count"] for r in rows),
        "guests": [
            {
                "name": short_name(r["name"]),
                "guests_count": r["guests_count"],
                "hype": r["hype"],
                "starter": i < STARTERS,
            }
            for i, r in enumerate(rows)
        ],
    }


def _row_to_own(row) -> dict:
    """Своя запись отдаётся гостю целиком — это его собственные данные."""
    return {
        "token": row["token"],
        "name": row["name"],
        "guests_count": row["guests_count"],
        "drink": row["drink"],
        "hype": row["hype"],
        "allergies": row["allergies"],
        "message": row["message"],
        "brings_own": bool(row["brings_own"]),
        "track": row["track"],
        "link_code": row["link_code"],
        "tg_linked": bool(row["tg_chat_id"]),
    }


@app.get("/api/rsvp")
def get_own(token: str = ""):
    if not token:
        return JSONResponse(status_code=400, content={"error": "Не передан токен."})
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM guests WHERE token = ?", (token,)).fetchone()
        if row is not None and not row["link_code"]:
            # Заявки, созданные до появления бота, кода не имеют — выдаём его
            # при первом обращении, а не миграцией: так не нужно придумывать
            # уникальные значения пачкой в SQL.
            code = db.new_link_code()
            conn.execute("UPDATE guests SET link_code = ? WHERE id = ?",
                         (code, row["id"]))
            row = conn.execute("SELECT * FROM guests WHERE token = ?",
                               (token,)).fetchone()
    if row is None:
        # Токен из localStorage мог остаться от сброшенной базы — не 500,
        # а честное «такой записи нет», страница просто покажет пустую форму.
        return JSONResponse(status_code=404, content={"error": "Запись не найдена."})
    return _row_to_own(row)


@app.post("/api/rsvp")
def submit(data: RsvpIn, request: Request):
    if data.website:
        # Бот заполнил скрытое поле. Отвечаем как при успехе, чтобы не
        # подсказывать, что ловушка сработала, но ничего не сохраняем.
        return {"ok": True, "token": db.new_token(), "updated": False}

    ip = db.ip_hash(client_ip(request))
    ts = now_iso()

    with db.get_conn() as conn:
        existing = None
        if data.token:
            existing = conn.execute(
                "SELECT * FROM guests WHERE token = ?", (data.token,)
            ).fetchone()
        if existing is None:
            # Повторная отправка без токена (двойной клик, перезаход) не должна
            # плодить дубликаты: тот же адрес и то же имя — это тот же человек.
            existing = conn.execute(
                "SELECT * FROM guests WHERE ip_hash = ? AND lower(name) = lower(?)",
                (ip, data.name),
            ).fetchone()

        if existing is not None:
            conn.execute(
                "UPDATE guests SET name=?, guests_count=?, drink=?, hype=?,"
                " allergies=?, message=?, brings_own=?, track=?, updated_at=?"
                " WHERE id=?",
                (data.name, data.guests_count, data.drink, data.hype,
                 data.allergies, data.message, int(data.brings_own),
                 data.track, ts, existing["id"]),
            )
            return {"ok": True, "token": existing["token"], "updated": True}

        recent = conn.execute(
            "SELECT count(*) AS n FROM guests"
            " WHERE ip_hash = ? AND created_at > datetime('now', '-1 hour')",
            (ip,),
        ).fetchone()["n"]
        if recent >= RATE_LIMIT_PER_HOUR:
            return JSONResponse(
                status_code=429,
                content={"error": "Слишком много заявок с этого адреса. "
                                  "Попробуйте позже или напишите имениннику."},
            )

        token = db.new_token()
        conn.execute(
            "INSERT INTO guests(token, name, guests_count, drink, hype,"
            " allergies, message, brings_own, track, link_code, ip_hash,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, data.name, data.guests_count, data.drink, data.hype,
             data.allergies, data.message, int(data.brings_own), data.track,
             db.new_link_code(), ip, ts, ts),
        )

    # Уведомление имениннику. После коммита и вне транзакции: сеть может
    # тормозить, а заявка к этому моменту уже надёжно сохранена.
    notify_host(f"🏀 Новая заявка: <b>{data.name}</b>, гостей: "
                f"{data.guests_count}, напиток: {data.drink}"
                + (f"\nАллергии: {data.allergies}" if data.allergies else "")
                + (f"\nСообщение: {data.message}" if data.message else ""))
    return {"ok": True, "token": token, "updated": False}


# ── АДМИНКА ─────────────────────────────────────────────────────────────────
# Всё под /admin и /api/admin/* закрыто паролем. Здесь, в отличие от
# публичного ростера, отдаются полные данные: имя целиком, аллергии и
# личные сообщения — ради них поля и добавлялись.

ADMIN_FIELDS = [
    ("id", "id"),
    ("name", "Имя"),
    ("guests_count", "Гостей"),
    ("drink", "Напиток"),
    ("hype", "Готовность"),
    ("allergies", "Аллергии"),
    ("message", "Сообщение"),
    ("brings_own", "Своё"),
    ("track", "Трек"),
    ("created_at", "Записался"),
    ("updated_at", "Обновлено"),
]


class AdminPatch(BaseModel):
    """Правка имени: гость мог написать фамилию первой, и тогда в публичный
    список попадала бы именно она."""
    name: str = Field(min_length=2, max_length=80)

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("пустое имя")
        return v


def _all_guests():
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT * FROM guests ORDER BY created_at, id"
        ).fetchall()


@app.get("/admin", response_class=HTMLResponse)
def admin_page(_: None = Depends(admin_auth)):
    html = (BASE_DIR / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"X-Robots-Tag": "noindex"})


@app.get("/api/admin/guests")
def admin_guests(_: None = Depends(admin_auth)):
    rows = _all_guests()
    return {
        "entries": len(rows),
        "people": sum(r["guests_count"] for r in rows),
        "avg_hype": round(sum(r["hype"] for r in rows) / len(rows), 1) if rows else 0,
        "with_allergies": sum(1 for r in rows if r["allergies"].strip()),
        "guests": [{k: r[k] for k, _ in ADMIN_FIELDS} for r in rows],
        "public_preview": [short_name(r["name"]) for r in rows],
    }


@app.get("/api/admin/guests.csv")
def admin_csv(_: None = Depends(admin_auth)):
    buf = io.StringIO()
    # Точка с запятой и BOM — иначе русский Excel открывает файл одной
    # колонкой и ломает кириллицу.
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([title for _, title in ADMIN_FIELDS])
    for r in _all_guests():
        writer.writerow([r[k] for k, _ in ADMIN_FIELDS])
    data = "﻿" + buf.getvalue()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="guests-{stamp}.csv"'},
    )


@app.patch("/api/admin/guests/{guest_id}")
def admin_rename(guest_id: int, patch: AdminPatch, _: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        cur = conn.execute(
            "UPDATE guests SET name = ?, updated_at = ? WHERE id = ?",
            (patch.name, now_iso(), guest_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Запись не найдена")
    return {"ok": True, "name": patch.name, "public": short_name(patch.name)}


@app.delete("/api/admin/guests/{guest_id}")
def admin_delete(guest_id: int, _: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        # Фото удаляем в той же транзакции и файлы — сразу после: иначе на
        # диске остались бы картинки, на которые уже никто не ссылается.
        photo_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM photos WHERE guest_id = ?", (guest_id,))]
        cur = conn.execute("DELETE FROM guests WHERE id = ?", (guest_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        conn.execute("DELETE FROM photos WHERE guest_id = ?", (guest_id,))
    for pid in photo_ids:
        for f in photo_paths(pid):
            f.unlink(missing_ok=True)
    return {"ok": True, "photos_removed": len(photo_ids)}


# ── КАЛЬКУЛЯТОР БАРА ────────────────────────────────────────────────────────
# Нормы на одного человека на вечер примерно в пять часов. Это оценка, а не
# истина: цифры собраны в одном месте и показываются в админке, чтобы их
# можно было осознанно подкрутить, а не гадать, откуда взялось «4 бутылки».
BOTTLE = ("бутылка", "бутылки", "бутылок")
LITRE = ("литр", "литра", "литров")

BAR_NORMS = {
    "Виски":          {"ml": 300,  "bottle": 700,  "forms": BOTTLE, "size": "0,7 л"},
    "Вино":           {"ml": 500,  "bottle": 750,  "forms": BOTTLE, "size": "0,75 л"},
    "Пиво":           {"ml": 1500, "bottle": 500,  "forms": BOTTLE, "size": "0,5 л"},
    "Коктейль":       {"ml": 250,  "bottle": 700,  "forms": BOTTLE, "size": "крепкого 0,7 л"},
    "Безалкогольное": {"ml": 1500, "bottle": 1000, "forms": LITRE,  "size": ""},
}


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """Русское склонение: 1 бутылка, 2 бутылки, 5 бутылок."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def unit_label(amount: int, norm: dict) -> str:
    word = plural(amount, norm["forms"])
    return f"{word} {norm['size']}".strip()
# Сок и тоник к коктейлям — считаются отдельной строкой.
MIXER_ML_PER_PERSON = 750


@app.get("/api/admin/bar")
def admin_bar(_: None = Depends(admin_auth)):
    """Закупочный список.

    Считаем по головам, а не по заявкам: если человек привёл компанию, весь
    его выбор умножается на размер компании. Что пьют спутники, форма не
    спрашивает, поэтому предположение сознательно щедрое — на празднике
    лучше остаться с лишней бутылкой, чем без.
    """
    rows = _all_guests()

    by_drink: dict[str, int] = {}
    bring_own_people = 0
    people_total = 0

    for r in rows:
        n = r["guests_count"]
        people_total += n
        if r["brings_own"]:
            bring_own_people += n
            continue
        by_drink[r["drink"]] = by_drink.get(r["drink"], 0) + n

    shopping, custom = [], []
    for drink, people in sorted(by_drink.items(), key=lambda kv: -kv[1]):
        norm = BAR_NORMS.get(drink)
        if norm is None:
            # Свой вариант гостя: нормы для произвольного текста нет,
            # поэтому такие позиции выносим списком, а не выдумываем объём.
            custom.append({"drink": drink, "people": people})
            continue
        volume = norm["ml"] * people
        amount = -(-volume // norm["bottle"])  # округление вверх
        shopping.append({
            "drink": drink,
            "people": people,
            "volume_l": round(volume / 1000, 1),
            "amount": amount,
            "label": unit_label(amount, norm),
        })

    cocktail_people = by_drink.get("Коктейль", 0)
    if cocktail_people:
        mixer = MIXER_ML_PER_PERSON * cocktail_people
        litres = -(-mixer // 1000)
        shopping.append({
            "drink": "Соки и тоники к коктейлям",
            "people": cocktail_people,
            "volume_l": round(mixer / 1000, 1),
            "amount": litres,
            "label": plural(litres, LITRE),
        })

    return {
        "people_total": people_total,
        "people_counted": people_total - bring_own_people,
        "people_bring_own": bring_own_people,
        "shopping": shopping,
        "custom": custom,
        "norms": [
            {"drink": d, "per_person_ml": n["ml"],
             "label": unit_label(2, n)}
            for d, n in BAR_NORMS.items()
        ],
    }


# ── TELEGRAM ────────────────────────────────────────────────────────────────
# Сервер физически не может обратиться к api.telegram.org: провайдер хостинга
# блокирует его (проверено — контрольные хосты отвечают за доли секунды,
# оба адреса Telegram молчат). Поэтому приложение НИКОГДА не ходит в Telegram
# само: оно кладёт сообщения в таблицу outbox, а рассылает их задача
# GitHub Actions, у раннеров доступ есть. Входящие от Telegram к нам приходят
# вебхуком — это встречное направление, оно не блокируется.

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Имя бота приходит из деплоя: спросить getMe отсюда нельзя.
BOT_USERNAME = os.environ.get("TG_BOT_USERNAME", "")
ADMIN_PASSWORD_FOR_HOOK = os.environ.get("ADMIN_PASSWORD", "")
SITE_URL = os.environ.get("SITE_URL", "https://raikhin.duckdns.org")


def webhook_secret() -> str:
    """Секрет вебхука выводится из пароля админки, а не хранится файлом:
    его должны одинаково вычислить и сервер, и раннер, который вызывает
    setWebhook. Общий секрет у них уже есть."""
    if not ADMIN_PASSWORD_FOR_HOOK:
        return ""
    return hashlib.sha256(
        ("tg-webhook:" + ADMIN_PASSWORD_FOR_HOOK).encode()).hexdigest()


def enqueue(chat_id: str, text: str, kind: str = "") -> None:
    """Поставить сообщение в очередь. Отправкой займётся GitHub Actions."""
    if not chat_id:
        return
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO outbox(chat_id, text, kind, created_at) VALUES (?,?,?,?)",
            (str(chat_id), text, kind, now_iso()),
        )


def notify_host(text: str) -> None:
    with db.get_conn() as conn:
        chat = db.get_setting(conn, "host_chat_id")
    enqueue(chat, text, "host")


@app.get("/api/telegram")
def telegram_public():
    """Публично отдаём только имя бота — оно и так видно всем в Telegram.
    Нужно странице, чтобы собрать ссылку «Подключить Telegram»."""
    return {"bot": BOT_USERNAME}


@app.post("/api/tg/webhook")
async def tg_webhook(request: Request):
    """Точка приёма обновлений. Путь публичный — его зовёт Telegram, — но
    закрыт секретным заголовком, который знают только Telegram и сервер."""
    expected = webhook_secret()
    got = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not expected or not pysecrets.compare_digest(got, expected):
        raise HTTPException(status_code=403, detail="forbidden")

    # Отметка нужна, чтобы отличить «Telegram до нас не достучался» от
    # «никто ничего не писал»: getWebhookInfo этого не различает.
    with db.get_conn() as conn:
        db.set_setting(conn, "last_webhook_at", now_iso())

    process_update(await request.json())
    return {"ok": True}


def process_update(update: dict) -> None:
    """Разбор одного обновления. Вызывается и вебхуком, и опросом с раннера —
    логика одна, различается только способ доставки."""
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    username = chat.get("username") or ""
    if not chat_id:
        return

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        _handle_start(chat_id, username, parts[1].strip() if len(parts) > 1 else "")
    elif text.startswith("/stop"):
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE guests SET tg_chat_id = NULL WHERE tg_chat_id = ?", (chat_id,))
        enqueue(chat_id, "Отключил напоминания. Вернуться можно той же кнопкой "
                         "на странице приглашения.", "stop")
    else:
        enqueue(chat_id, "Я бот приглашения на день рождения Александра.\n\n"
                         f"Чтобы получать напоминание, откройте {SITE_URL} "
                         "и нажмите «Подключить Telegram».", "help")


def _handle_start(chat_id: str, username: str, code: str) -> None:
    if not code:
        enqueue(chat_id, "Здравствуйте! Чтобы я напомнил о празднике за два дня, "
                         f"откройте {SITE_URL}, заполните форму и нажмите "
                         "«Подключить Telegram».", "start")
        return

    with db.get_conn() as conn:
        admin_code = db.get_setting(conn, "host_link_code")
        if admin_code and code == admin_code:
            db.set_setting(conn, "host_chat_id", chat_id)
            db.set_setting(conn, "host_link_code", "")  # код одноразовый
            enqueue(chat_id, "Готово — сюда будут приходить уведомления "
                             "о новых заявках.", "host-linked")
            return

        row = conn.execute(
            "SELECT * FROM guests WHERE link_code = ?", (code,)).fetchone()
        if row is None:
            enqueue(chat_id, "Не узнаю этот код. Откройте страницу приглашения "
                             "и нажмите «Подключить Telegram» ещё раз.", "unknown")
            return
        conn.execute(
            "UPDATE guests SET tg_chat_id = ?, tg_username = ? WHERE id = ?",
            (chat_id, username, row["id"]),
        )
        name = row["name"].split()[0] if row["name"].split() else "друг"

    enqueue(chat_id, f"{name}, вы в составе! 🏀\n\nНапомню за два дня до "
                     "праздника — 22 августа, 17:00, CleverLOFT у метро Тульская.\n\n"
                     "Отключить: /stop", "linked")


@app.get("/api/admin/telegram")
def admin_telegram(_: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        host = db.get_setting(conn, "host_chat_id")
        code = db.get_setting(conn, "host_link_code")
        linked = conn.execute(
            "SELECT count(*) AS n FROM guests WHERE tg_chat_id IS NOT NULL"
        ).fetchone()["n"]
        total = conn.execute("SELECT count(*) AS n FROM guests").fetchone()["n"]
        # Ждущими считаем только те, что ещё будут отправлены: исчерпавшие
        # попытки висели бы в счётчике вечно и выглядели как затор.
        pending = conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE sent_at IS NULL AND attempts < ?",
            (MAX_ATTEMPTS,)).fetchone()["n"]
        stuck = conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE sent_at IS NULL AND attempts >= ?",
            (MAX_ATTEMPTS,)).fetchone()["n"]
        sent = conn.execute(
            "SELECT count(*) AS n FROM outbox WHERE sent_at IS NOT NULL").fetchone()["n"]
        last = conn.execute(
            "SELECT sent_at FROM outbox WHERE sent_at IS NOT NULL"
            " ORDER BY sent_at DESC LIMIT 1").fetchone()
        last_hook = db.get_setting(conn, "last_webhook_at")
        last_poll = db.get_setting(conn, "last_poll_at")
        if not host and not code:
            code = db.new_link_code()
            db.set_setting(conn, "host_link_code", code)
    return {
        "configured": bool(TELEGRAM_TOKEN and BOT_USERNAME),
        "bot": BOT_USERNAME,
        "host_linked": bool(host),
        "host_link": f"https://t.me/{BOT_USERNAME}?start={code}" if BOT_USERNAME and code else "",
        "guests_linked": linked,
        "guests_total": total,
        "queue_pending": pending,
        "queue_stuck": stuck,
        "queue_sent": sent,
        "last_sent_at": last["sent_at"] if last else None,
        "last_webhook_at": last_hook or None,
        "last_poll_at": last_poll or None,
    }


@app.post("/api/admin/telegram/test")
def admin_telegram_test(_: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        chat = db.get_setting(conn, "host_chat_id")
    if not chat:
        raise HTTPException(status_code=400, detail="Чат именинника ещё не привязан")
    enqueue(chat, "Проверка связи 🏀 Очередь работает, напоминания дойдут.", "test")
    return {"ok": True, "queued": True}


# ── ОЧЕРЕДЬ ДЛЯ РАССЫЛЬЩИКА ─────────────────────────────────────────────────
# Эти два эндпоинта зовёт задача GitHub Actions под тем же паролем, что и
# админку: отдельный секрет ради одного потребителя усложнил бы настройку.

class OutboxAck(BaseModel):
    sent: list[int] = []
    failed: list[dict] = []


@app.get("/api/admin/outbox")
def outbox_pending(limit: int = 50, _: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, text FROM outbox"
            " WHERE sent_at IS NULL AND attempts < ?"
            " ORDER BY id LIMIT ?",
            (MAX_ATTEMPTS, max(1, min(limit, 200))),
        ).fetchall()
    return {"messages": [dict(r) for r in rows]}


@app.post("/api/admin/outbox/ack")
def outbox_ack(ack: OutboxAck, _: None = Depends(admin_auth)):
    ts = now_iso()
    with db.get_conn() as conn:
        for mid in ack.sent:
            conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (ts, mid))
        for item in ack.failed:
            # Попытки считаем, чтобы битое сообщение (например, гость
            # заблокировал бота) не крутилось в очереди вечно.
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?"
                " WHERE id = ?",
                (str(item.get("error", ""))[:200], item.get("id")),
            )
    return {"ok": True, "sent": len(ack.sent), "failed": len(ack.failed)}


# ── НАПОМИНАНИЕ ЗА ДВА ДНЯ ──────────────────────────────────────────────────
# Время события задано с явным смещением: Москва круглый год UTC+3, поэтому
# фиксированный сдвиг корректен и не тянет зависимость от базы часовых поясов.
MSK = timezone(timedelta(hours=3))
EVENT_AT = datetime(2026, 8, 22, 17, 0, tzinfo=MSK)
REMIND_BEFORE = timedelta(days=2)


def reminder_text(row) -> str:
    name = row["name"].split()[0] if row["name"].split() else "Друг"
    return (f"{name}, послезавтра играем! 🏀\n\n"
            "<b>22 августа, 17:00</b>, CleverLOFT — Холодильный пер., 3.\n"
            "2 минуты от метро Тульская.\n\n"
            f"Поменять ответ: {SITE_URL}")


def _pending_reminders(conn):
    return conn.execute(
        "SELECT * FROM guests"
        " WHERE tg_chat_id IS NOT NULL AND reminded_at IS NULL"
        " ORDER BY created_at, id"
    ).fetchall()


@app.get("/api/admin/reminders")
def reminders_status(_: None = Depends(admin_auth)):
    """Предпросмотр: кому уйдёт напоминание и когда. Ничего не отправляет —
    без такого режима проверить рассылку можно было бы только дождавшись
    20 августа."""
    now = datetime.now(timezone.utc)
    send_at = EVENT_AT - REMIND_BEFORE
    with db.get_conn() as conn:
        pending = _pending_reminders(conn)
        already = conn.execute(
            "SELECT count(*) AS n FROM guests WHERE reminded_at IS NOT NULL"
        ).fetchone()["n"]
        no_tg = conn.execute(
            "SELECT count(*) AS n FROM guests WHERE tg_chat_id IS NULL"
        ).fetchone()["n"]
    return {
        "send_at": send_at.isoformat(),
        "event_at": EVENT_AT.isoformat(),
        "now": now.astimezone(MSK).isoformat(),
        "window_open": send_at <= now < EVENT_AT,
        "recipients": [
            {"name": short_name(r["name"]), "tg": r["tg_username"] or r["tg_chat_id"]}
            for r in pending
        ],
        "already_reminded": already,
        "without_telegram": no_tg,
        "sample": reminder_text(pending[0]) if pending else
                  "Имя, послезавтра играем! 🏀 …",
    }


@app.post("/api/admin/reminders/run")
def reminders_run(force: bool = False, _: None = Depends(admin_auth)):
    """Складывает напоминания в очередь. Вызывается задачей по расписанию;
    вне окна ничего не делает. force=true — отправить принудительно, чтобы
    можно было проверить рассылку заранее."""
    now = datetime.now(timezone.utc)
    send_at = EVENT_AT - REMIND_BEFORE
    in_window = send_at <= now < EVENT_AT
    if not in_window and not force:
        return {"queued": 0, "window_open": False,
                "reason": f"окно откроется {send_at.astimezone(MSK):%d.%m %H:%M} МСК"}

    ts = now_iso()
    queued = 0
    with db.get_conn() as conn:
        for row in _pending_reminders(conn):
            conn.execute(
                "INSERT INTO outbox(chat_id, text, kind, created_at) VALUES (?,?,?,?)",
                (row["tg_chat_id"], reminder_text(row), "reminder", ts),
            )
            # Отметку ставим в той же транзакции, что и постановку в очередь:
            # иначе сбой между ними разослал бы напоминание дважды.
            conn.execute("UPDATE guests SET reminded_at = ? WHERE id = ?",
                         (ts, row["id"]))
            queued += 1
    return {"queued": queued, "window_open": in_window, "forced": force}


# ── ОПРОС ВМЕСТО ВЕБХУКА ────────────────────────────────────────────────────
# Блокировка провайдера оказалась двусторонней: Telegram не может доставить
# обновление на сервер (getWebhookInfo показывал Connection timed out, а
# отметки о принятых вебхуках так и не появилось). Поэтому обновления
# забирает раннер через getUpdates и приносит сюда.

class TgUpdates(BaseModel):
    updates: list[dict] = []


@app.get("/api/admin/tg/offset")
def tg_offset(_: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        return {"offset": int(db.get_setting(conn, "tg_offset", "0") or 0)}


@app.post("/api/admin/tg/updates")
def tg_updates(payload: TgUpdates, _: None = Depends(admin_auth)):
    """Принимает пачку обновлений от раннера и двигает offset.

    Offset хранится здесь, а не на раннере: у задачи нет своего состояния
    между запусками, и после перезапуска она перечитала бы всё заново.
    """
    processed = 0
    max_id = 0
    for u in payload.updates:
        try:
            process_update(u)
        except Exception as e:  # одно битое обновление не должно ронять пачку
            print(f"не удалось обработать обновление {u.get('update_id')}: {e}")
        max_id = max(max_id, int(u.get("update_id", 0)))
        processed += 1

    with db.get_conn() as conn:
        if max_id:
            db.set_setting(conn, "tg_offset", str(max_id + 1))
        # Факт опроса отмечаем всегда, а не только когда что-то пришло.
        # Пустой ответ — норма: гости жмут «Старт» редко, и если засчитывать
        # опросом лишь непустые пачки, отметка застынет на неделю и админка
        # начнёт врать, что приём обновлений встал.
        db.set_setting(conn, "last_poll_at", now_iso())
        if processed:
            db.set_setting(conn, "last_webhook_at", now_iso())
    return {"processed": processed, "next_offset": max_id + 1 if max_id else None}


# ── КАЛЕНДАРЬ ───────────────────────────────────────────────────────────────
# Самый устойчивый канал напоминания: файл уезжает в телефон гостя, и
# будильник сработает, даже если у нас всё отвалится и Telegram останется
# заблокирован. Поэтому напоминание за два дня зашито прямо в событие.

# Календарь отдаётся статикой из site/calendar.ics, а не отсюда: это самый
# устойчивый канал напоминания, и он не должен падать вместе с бэкендом.
# Генератор — scripts/make_ics.py, Content-Type принудительно задан в Caddy.


@app.get("/api/admin/playlist")
def admin_playlist(_: None = Depends(admin_auth)):
    """Плейлист с полными именами плюс готовая к копированию простыня —
    её удобно вставить в поиск музыкального сервиса одним куском."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name, track FROM guests WHERE trim(track) != ''"
            " ORDER BY created_at, id"
        ).fetchall()
    return {
        "count": len(rows),
        "items": [{"name": r["name"], "track": r["track"]} for r in rows],
        "plain": "\n".join(r["track"] for r in rows),
    }


@app.get("/api/admin/allergies")
def admin_allergies(_: None = Depends(admin_auth)):
    """Ограничения в еде отдельным списком — с ним идут к кейтерингу,
    и выуживать их из общей таблицы неудобно."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name, guests_count, allergies FROM guests"
            " WHERE trim(allergies) != '' ORDER BY created_at, id"
        ).fetchall()
    return {
        "count": len(rows),
        "items": [{"name": r["name"], "guests_count": r["guests_count"],
                   "allergies": r["allergies"]} for r in rows],
    }


@app.get("/api/playlist")
def playlist():
    """Плейлист «на разминку» — публичный: половина удовольствия в том,
    чтобы увидеть, кто что заказал. Имя сокращённое, как и в ростере."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT name, track FROM guests WHERE trim(track) != ''"
            " ORDER BY created_at, id"
        ).fetchall()
    return {
        "count": len(rows),
        "tracks": [{"name": short_name(r["name"]), "track": r["track"]} for r in rows],
    }


# ── ФОТОАЛЬБОМ ──────────────────────────────────────────────────────────────
# Гость приносит любимое или совместное фото с именинником. Файлы лежат на
# диске рядом с базой, в SQLite только метаданные.
#
# Каждое фото пересохраняется через Pillow, и это не косметика:
#   * доказывает, что файл действительно картинка, а не переименованный zip
#     с заявленным image/jpeg — заголовку из формы верить нельзя;
#   * уменьшает до разумного размера, иначе снимок с телефона на 12 Мп
#     будет грузиться у гостей минуту;
#   * попутно срезает EXIF, в котором у телефонных фото лежат GPS-координаты
#     места съёмки — этому в открытом альбоме точно не место.

PHOTO_DIR = Path(os.environ.get("STR_PHOTO_DIR",
                                str(db.DATA_DIR / "photos")))
MAX_UPLOAD_BYTES = 12 * 1024 * 1024      # 12 МБ — снимок с любого телефона влезает
MAX_SIDE = 2000                          # длинная сторона готового фото
THUMB_SIDE = 480
PHOTOS_PER_GUEST = 5
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO", "GIF"}


def photo_paths(photo_id: int) -> tuple[Path, Path]:
    return (PHOTO_DIR / f"{photo_id}.jpg", PHOTO_DIR / f"{photo_id}_thumb.jpg")


def _guest_by_token(conn, token: str):
    if not token:
        return None
    return conn.execute("SELECT * FROM guests WHERE token = ?", (token,)).fetchone()


@app.get("/api/photos")
def photos_list(token: str = ""):
    """Публичный альбом: id, кто принёс (сокращённо) и подпись.

    С токеном заявки дополнительно помечает флагом `mine` фото самого
    гостя — только его собственные. Без этого страница не знает, у какой
    карточки рисовать «Удалить»: снимок мог приехать с другого устройства,
    и списка своих загрузок в браузере может не быть.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.id, p.caption, p.created_at, p.guest_id, g.name"
            " FROM photos p JOIN guests g ON g.id = p.guest_id"
            " ORDER BY p.created_at DESC, p.id DESC"
        ).fetchall()
        me = _guest_by_token(conn, token)
    my_id = me["id"] if me else None
    return {
        "count": len(rows),
        "photos": [{"id": r["id"], "name": short_name(r["name"]),
                    "caption": r["caption"],
                    "mine": my_id is not None and r["guest_id"] == my_id}
                   for r in rows],
    }


def _send_photo(photo_id: int, thumb: bool):
    full, small = photo_paths(photo_id)
    path = small if thumb else full
    if not path.exists():
        raise HTTPException(status_code=404, detail="Фото не найдено")
    # Тип задаём сами, а не берём из запроса: всё сохранённое — JPEG,
    # потому что мы его пересохранили.
    return Response(content=path.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/photos/{photo_id}")
def photo_full(photo_id: int):
    return _send_photo(photo_id, thumb=False)


@app.get("/api/photos/{photo_id}/thumb")
def photo_thumb(photo_id: int):
    return _send_photo(photo_id, thumb=True)


@app.post("/api/photos")
async def photo_upload(request: Request):
    """Загрузка фото. Гость опознаётся токеном своей заявки: альбом собирают
    те, кто идёт, а не любой прохожий со ссылкой."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    form = await request.form()
    token = str(form.get("token") or "")
    caption = str(form.get("caption") or "").strip()[:100]
    upload = form.get("file")

    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="Файл не передан.")

    with db.get_conn() as conn:
        guest = _guest_by_token(conn, token)
        if guest is None:
            raise HTTPException(
                status_code=403,
                detail="Сначала запишитесь в состав — фото привязывается к заявке.")
        mine = conn.execute(
            "SELECT count(*) AS n FROM photos WHERE guest_id = ?",
            (guest["id"],)).fetchone()["n"]
    if mine >= PHOTOS_PER_GUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Больше {PHOTOS_PER_GUEST} фото от одного гостя не принимаем.")

    raw = await upload.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Файл больше {MAX_UPLOAD_BYTES // (1024*1024)} МБ.")
    if not raw:
        raise HTTPException(status_code=400, detail="Файл пустой.")

    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
        img.load()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Не удалось прочитать картинку. Если это HEIC с iPhone, "
                   "сохраните её как JPEG и попробуйте снова.")
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400,
                            detail=f"Формат {fmt} не поддерживается.")

    # exif_transpose разворачивает снимок по ориентации из EXIF: без этого
    # фото с телефона легло бы набок, ведь сам EXIF мы дальше отбрасываем.
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    ts = now_iso()
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO photos(guest_id, caption, width, height, created_at)"
            " VALUES (?,?,?,?,?)",
            (guest["id"], caption, img.width, img.height, ts))
        photo_id = cur.lastrowid

    full, small = photo_paths(photo_id)
    big = img.copy()
    big.thumbnail((MAX_SIDE, MAX_SIDE))
    big.save(full, "JPEG", quality=85, optimize=True)
    thumb = img.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE))
    thumb.save(small, "JPEG", quality=80, optimize=True)

    notify_host(f"📸 <b>{guest['name']}</b> добавил фото в альбом"
                + (f": {caption}" if caption else ""))
    return {"ok": True, "id": photo_id, "mine": mine + 1,
            "limit": PHOTOS_PER_GUEST}


@app.delete("/api/photos/{photo_id}")
def photo_delete_own(photo_id: int, token: str = ""):
    """Своё фото гость может убрать сам — по токену заявки."""
    with db.get_conn() as conn:
        guest = _guest_by_token(conn, token)
        if guest is None:
            raise HTTPException(status_code=403, detail="Нужен токен заявки.")
        row = conn.execute(
            "SELECT * FROM photos WHERE id = ? AND guest_id = ?",
            (photo_id, guest["id"])).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Это не ваше фото.")
        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    for p in photo_paths(photo_id):
        p.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/admin/photos")
def admin_photos(_: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT p.id, p.caption, p.created_at, p.width, p.height, g.name"
            " FROM photos p JOIN guests g ON g.id = p.guest_id"
            " ORDER BY p.created_at DESC, p.id DESC"
        ).fetchall()
    total = sum(sum(f.stat().st_size for f in photo_paths(r["id"]) if f.exists())
                for r in rows)
    return {
        "count": len(rows),
        "disk_bytes": total,
        "photos": [dict(r) for r in rows],
    }


@app.delete("/api/admin/photos/{photo_id}")
def admin_photo_delete(photo_id: int, _: None = Depends(admin_auth)):
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Фото не найдено")
    for p in photo_paths(photo_id):
        p.unlink(missing_ok=True)
    return {"ok": True}


def _photo_rows():
    """Фото в порядке загрузки. Порядок по id, а не по времени: два фото из
    одной пачки приходят в одну секунду, и сортировка по created_at
    переставляла бы их от запроса к запросу — коллаж выходил бы каждый раз
    другим."""
    with db.get_conn() as conn:
        return conn.execute(
            "SELECT p.id, p.caption, p.created_at, p.width, p.height, g.name"
            " FROM photos p JOIN guests g ON g.id = p.guest_id"
            " ORDER BY p.id"
        ).fetchall()


def safe_part(s: str, limit: int = 40) -> str:
    """Кусок имени файла: убираем всё, что ломает распаковку или путь.

    Кириллицу оставляем — zip хранит имена в UTF-8, и «Иван-Петров.jpg»
    читается глазами, в отличие от «7.jpg»."""
    s = "".join(ch for ch in s if ch.isprintable() and ch not in '\\/:*?"<>|')
    s = "-".join(s.split())
    return s.strip(". -")[:limit] or "без-имени"


@app.get("/api/admin/photos.zip")
def admin_photos_zip(_: None = Depends(admin_auth)):
    """Скачать альбом целиком — чтобы он жил не только на сервере.

    Внутри лежат полноразмерные файлы с человеческими именами и `photos.csv`
    со всеми метаданными: подпись при переименовании файла потерялась бы,
    а она часто и есть самое ценное."""
    import tempfile
    import zipfile

    rows = _photo_rows()
    if not rows:
        raise HTTPException(status_code=404, detail="Фото пока не приносили.")

    # Собираем не в память: полсотни снимков по 2000 px — это десятки
    # мегабайт, а сервер маленький. До 8 МБ файл живёт в памяти, дальше
    # сам утекает на диск.
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    manifest = io.StringIO()
    writer = csv.writer(manifest, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["файл", "id", "гость", "подпись", "загружено", "ширина", "высота"])

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        # ZIP_STORED, а не DEFLATE: JPEG уже сжат, повторное сжатие даёт
        # проценты и стоит процессорного времени на каждой картинке.
        for n, r in enumerate(rows, 1):
            full, _ = photo_paths(r["id"])
            if not full.exists():
                continue
            name = f"{n:03d}-{safe_part(r['name'])}"
            if r["caption"]:
                name += f"-{safe_part(r['caption'], 60)}"
            name += ".jpg"
            z.write(full, name)
            writer.writerow([name, r["id"], r["name"], r["caption"],
                             r["created_at"], r["width"], r["height"]])
        z.writestr("photos.csv", ("﻿" + manifest.getvalue()).encode("utf-8"))

    size = tmp.tell()
    tmp.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def stream():
        try:
            while chunk := tmp.read(64 * 1024):
                yield chunk
        finally:
            tmp.close()

    return StreamingResponse(
        stream(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="photos-{stamp}.zip"',
                 "Content-Length": str(size)})


COLLAGE_MAX_SIDE = 8000      # дальше JPEG становится неудобным для просмотра


@app.get("/api/admin/photos/collage.jpg")
def admin_photos_collage(cell: int = 500, cols: int = 0,
                         _: None = Depends(admin_auth)):
    """Коллаж из всех фото — сеткой, на фоне цвета сайта.

    Ячейки квадратные: кадры приходят и вертикальные, и горизонтальные, и
    без обрезки сетка превращается в решето из пустот. Обрезаем по центру,
    смещённому чуть вверх (0.4) — на вертикальных снимках лица выше середины,
    и ровно по центру им срезает макушки.

    Надписей на коллаже нет намеренно: шрифты сайта лежат в woff2, который
    Pillow не читает, а какой TTF найдётся на сервере — не гарантировано.
    Подписать чужим шрифтом или получить квадратики вместо букв хуже, чем
    не подписывать; имена и подписи есть в zip-архиве.
    """
    from PIL import Image, ImageOps

    rows = [r for r in _photo_rows() if photo_paths(r["id"])[0].exists()]
    if not rows:
        raise HTTPException(status_code=404, detail="Фото пока не приносили.")

    cell = max(120, min(cell, 1000))
    n = len(rows)
    if cols <= 0:
        cols = math.ceil(math.sqrt(n))       # сетка как можно ближе к квадрату
    cols = max(1, min(cols, n))
    rows_n = math.ceil(n / cols)

    gap, pad = max(4, cell // 40), max(8, cell // 20)
    width = pad * 2 + cols * cell + (cols - 1) * gap
    height = pad * 2 + rows_n * cell + (rows_n - 1) * gap
    if max(width, height) > COLLAGE_MAX_SIDE:
        raise HTTPException(
            status_code=400,
            detail=f"Коллаж вышел бы {width}×{height} — уменьшите размер ячейки.")

    # Последний ряд обычно неполный; центрируем его, иначе коллаж выглядит
    # обрезанным справа, как будто чего-то не хватает.
    tail = n % cols
    tail_shift = ((cols - tail) * (cell + gap)) // 2 if tail else 0

    canvas = Image.new("RGB", (width, height), (12, 27, 61))   # --navy сайта
    for i, r in enumerate(rows):
        full, _ = photo_paths(r["id"])
        with Image.open(full) as im:
            tile = ImageOps.fit(im.convert("RGB"), (cell, cell),
                                method=Image.LANCZOS, centering=(0.5, 0.4))
        row_i, col_i = divmod(i, cols)
        x = pad + col_i * (cell + gap) + (tail_shift if row_i == rows_n - 1 else 0)
        y = pad + row_i * (cell + gap)
        canvas.paste(tile, (x, y))

    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=88, optimize=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=buf.getvalue(), media_type="image/jpeg",
        headers={"Content-Disposition":
                 f'attachment; filename="collage-{stamp}.jpg"'})
