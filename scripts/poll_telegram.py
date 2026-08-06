#!/usr/bin/env python3
"""Забирает обновления у Telegram и приносит их в приложение.

Нужен потому, что блокировка провайдера двусторонняя: сервер не может
позвонить в Telegram, а Telegram не может доставить вебхук на сервер
(getWebhookInfo показывал Connection timed out, отметки о принятых
вебхуках так и не появилось). Раннеру GitHub доступны обе стороны.

Offset хранится в приложении, а не здесь: у задачи нет состояния между
запусками, и иначе она перечитывала бы историю заново.

Переменные окружения: SITE, ADMIN_PASSWORD, TELEGRAM_BOT_TOKEN.
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SITE = os.environ.get("SITE", "").rstrip("/")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def api(path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{SITE}{path}", data=data,
                                 method="POST" if data else "GET")
    cred = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get_updates(offset: int):
    params = urllib.parse.urlencode({
        "offset": offset, "timeout": 0, "limit": 100,
        "allowed_updates": json.dumps(["message"]),
    })
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?{params}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read()) if e.headers else {}
    if not body.get("ok"):
        # 409 приходит, если у бота остался вебхук: getUpdates и вебхук
        # взаимоисключающи, поэтому деплой его снимает.
        print(f"getUpdates отказал: {body.get('description', body)}")
        return None
    return body.get("result", [])


def main() -> int:
    if not (SITE and PASSWORD and TOKEN):
        print("не задано что-то из SITE / ADMIN_PASSWORD / TELEGRAM_BOT_TOKEN")
        return 1

    offset = api("/api/admin/tg/offset")["offset"]
    updates = get_updates(offset)
    if updates is None:
        return 1
    # Пустую пачку тоже отправляем: приложению нужен факт «опрос состоялся».
    # Раньше на пустом ответе задача молча завершалась, и отметка опроса
    # застывала на дате последнего гостя, нажавшего «Старт», — админка после
    # этого показывала, что приём обновлений встал, хотя всё работало.
    res = api("/api/admin/tg/updates", {"updates": updates})
    if not updates:
        print(f"новых обновлений нет (offset={offset})")
        return 0

    print(f"обработано обновлений: {res['processed']}, "
          f"следующий offset: {res['next_offset']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
