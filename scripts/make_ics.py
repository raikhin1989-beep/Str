#!/usr/bin/env python3
"""Генерирует site/calendar.ics.

Файл статический и лежит рядом с сайтом намеренно: календарь — самый
устойчивый канал напоминания, и было бы странно, если бы он переставал
скачиваться вместе с бэкендом. Caddy отдаёт его с принудительным
Content-Type: text/calendar.

Запускать вручную при изменении даты, места или продолжительности:
    python3 scripts/make_ics.py
Результат коммитить.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
EVENT_AT = datetime(2026, 8, 22, 16, 0, tzinfo=MSK)
EVENT_DURATION = timedelta(hours=6)
EVENT_TITLE = "Александру 37 — Матч всех звёзд"
EVENT_LOCATION = "Лофт в Сити, Москва"
SITE_URL = "https://raikhin.duckdns.org"
OUT = Path(__file__).resolve().parent.parent / "site" / "calendar.ics"


def ics_escape(value: str) -> str:
    """Обратная косая, точка с запятой, запятая и перенос строки —
    служебные символы iCalendar (RFC 5545)."""
    return (value.replace("\\", "\\\\")
                 .replace(";", "\\;")
                 .replace(",", "\\,")
                 .replace("\n", "\\n"))


def ics_fold(line: str) -> str:
    """Строка не длиннее 75 октетов. Режем по байтам: кириллица занимает
    два, и резка по символам порвала бы букву пополам."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        limit = 75 if not out else 74
        if len(chunk) + len(b) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    out.append(chunk.decode("utf-8"))
    return "\r\n ".join(out)


def build() -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    start = EVENT_AT.astimezone(timezone.utc)
    desc = ("Дресс-код: нарядно плюс один геройский акцент. "
            f"Подробности и список гостей: {SITE_URL}")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Str//Invitation//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:birthday-2026-08-22@{SITE_URL.split('//')[-1]}",
        # Фиксированный DTSTAMP: файл статический, и генерация «сейчас»
        # меняла бы его при каждом запуске без изменения самого события.
        f"DTSTAMP:{EVENT_AT.astimezone(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}",
        f"DTEND:{(start + EVENT_DURATION).strftime(fmt)}",
        f"SUMMARY:{ics_escape(EVENT_TITLE)}",
        f"LOCATION:{ics_escape(EVENT_LOCATION)}",
        f"DESCRIPTION:{ics_escape(desc)}",
        f"URL:{SITE_URL}",
        "STATUS:CONFIRMED",
        "BEGIN:VALARM",
        "TRIGGER:-P2D",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{ics_escape('Послезавтра день рождения Александра')}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(ics_fold(x) for x in lines) + "\r\n"


if __name__ == "__main__":
    OUT.write_bytes(build().encode("utf-8"))
    print(f"записано: {OUT} ({OUT.stat().st_size} байт)")
