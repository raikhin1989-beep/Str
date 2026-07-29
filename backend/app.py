"""Бэкенд приглашения.

Шаг 2 — только каркас: один эндпоинт здоровья, чтобы проверить связку
Caddy → uvicorn → systemd отдельно от какой-либо логики. База и приём
заявок появятся на шаге 3.

Все маршруты живут под /api/*: Caddy отдаёт этот префикс сюда, а всё
остальное — статикой из /var/www/html.
"""
import os
import time

from fastapi import FastAPI

# Документацию Swagger не публикуем: наружу смотрит открытый сайт,
# а схема API гостям ни к чему.
app = FastAPI(title="Str API", docs_url=None, redoc_url=None, openapi_url=None)

APP_VERSION = os.environ.get("APP_VERSION", "unknown")
STARTED_AT = time.time()


@app.get("/api/health")
def health():
    """Проверка живости. Возвращает версию, чтобы было видно,
    какой коммит реально крутится, а не только что сервис отвечает."""
    return {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
    }
