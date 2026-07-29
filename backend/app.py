"""Бэкенд приглашения.

Шаг 3 — приём заявок: POST /api/rsvp сохраняет ответ гостя, GET /api/rsvp
возвращает его собственную запись по токену, чтобы страница после
перезагрузки помнила, что человек уже записан.

Публичный список гостей (/api/roster) появится на шаге 4 — и отдавать он
будет сокращённое имя, а не полное (решение Р4).

Все маршруты живут под /api/*: Caddy отдаёт этот префикс сюда, а всё
остальное — статикой из /var/www/html.
"""
import os
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
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

FIELD_LABELS = {
    "name": "имя",
    "guests_count": "количество гостей",
    "drink": "напиток",
    "hype": "готовность угарать",
    "allergies": "аллергии",
    "message": "сообщение",
}


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
    }


@app.get("/api/rsvp")
def get_own(token: str = ""):
    if not token:
        return JSONResponse(status_code=400, content={"error": "Не передан токен."})
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM guests WHERE token = ?", (token,)).fetchone()
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
                " allergies=?, message=?, updated_at=? WHERE id=?",
                (data.name, data.guests_count, data.drink, data.hype,
                 data.allergies, data.message, ts, existing["id"]),
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
            " allergies, message, ip_hash, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (token, data.name, data.guests_count, data.drink, data.hype,
             data.allergies, data.message, ip, ts, ts),
        )
    return {"ok": True, "token": token, "updated": False}
