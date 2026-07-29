#!/usr/bin/env python3
"""Разгребает очередь сообщений и отправляет их в Telegram.

Запускается на раннере GitHub, а не на сервере: провайдер хостинга
блокирует api.telegram.org, а у раннеров доступ есть. Сервер только
складывает сообщения в outbox — см. backend/app.py.

Переменные окружения:
  SITE            — адрес приложения (https://raikhin.duckdns.org)
  ADMIN_PASSWORD  — пароль админки, им же закрыты эндпоинты очереди
  TELEGRAM_BOT_TOKEN
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = os.environ.get("SITE", "").rstrip("/")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BATCH = int(os.environ.get("BATCH", "50"))


def api(path: str, payload=None):
    """Запрос к нашему приложению под basic-авторизацией админки."""
    url = f"{SITE}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    cred = base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    req.add_header("Authorization", f"Basic {cred}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def send(chat_id: str, text: str):
    """Отправка одного сообщения. Возвращает (успех, описание ошибки)."""
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        with urllib.request.urlopen(url, data=data, timeout=25) as r:
            return json.loads(r.read()).get("ok", False), ""
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            # 403 — гость заблокировал бота, повторять бессмысленно, но
            # счётчик попыток всё равно доведёт сообщение до отсева.
            return False, body.get("description", f"HTTP {e.code}")
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def main() -> int:
    if not (SITE and PASSWORD and TOKEN):
        print("не задано что-то из SITE / ADMIN_PASSWORD / TELEGRAM_BOT_TOKEN")
        return 1

    queue = api(f"/api/admin/outbox?limit={BATCH}")["messages"]
    if not queue:
        print("очередь пуста")
        return 0

    print(f"в очереди сообщений: {len(queue)}")
    sent, failed = [], []
    for m in queue:
        ok, err = send(m["chat_id"], m["text"])
        if ok:
            sent.append(m["id"])
        else:
            failed.append({"id": m["id"], "error": err})
            print(f"  #{m['id']} -> ошибка: {err}")
        # Лимит Telegram — примерно 30 сообщений в секунду; на наших
        # объёмах хватает скромной паузы, чтобы не ловить 429.
        time.sleep(0.1)

    api("/api/admin/outbox/ack", {"sent": sent, "failed": failed})
    print(f"отправлено: {len(sent)}, не удалось: {len(failed)}")
    # Неудачи не валят задачу: следующий запуск повторит, а после пяти
    # попыток сообщение перестанет попадать в выборку.
    return 0


if __name__ == "__main__":
    sys.exit(main())
