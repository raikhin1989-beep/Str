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
import os
import secrets as pysecrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
    # Токен своей записи, если гость уже отправлял форму с этого браузера.
    token: str | None = Field(default=None, max_length=64)
    # Ловушка для ботов: поле спрятано в вёрстке, человек его не заполнит.
    website: str = Field(default="", max_length=200)

    @field_validator("name", "drink", "allergies", "message", mode="before")
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
    """Проверка живости. Возвращает версию, чтобы было видно,
    какой коммит реально крутится, а не только что сервис отвечает."""
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
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
                " allergies=?, message=?, brings_own=?, updated_at=? WHERE id=?",
                (data.name, data.guests_count, data.drink, data.hype,
                 data.allergies, data.message, int(data.brings_own), ts,
                 existing["id"]),
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
            " allergies, message, brings_own, link_code, ip_hash,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, data.name, data.guests_count, data.drink, data.hype,
             data.allergies, data.message, int(data.brings_own),
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
        cur = conn.execute("DELETE FROM guests WHERE id = ?", (guest_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Запись не найдена")
    return {"ok": True}


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

    update = await request.json()
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    username = chat.get("username") or ""
    if not chat_id:
        return {"ok": True}

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
    return {"ok": True}


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
                     "праздника — 22 августа, 16:00, лофт в Сити.\n\n"
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
