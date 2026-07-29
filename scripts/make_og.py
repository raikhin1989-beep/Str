#!/usr/bin/env python3
"""Рендерит site/og.png — картинку для превью ссылки в мессенджерах.

Гости получат приглашение репостом, и без картинки вместо карточки будет
голый URL. Размер 1200×630 — то, что ожидают Telegram, WhatsApp и VK.

Картинка собирается из scripts/og_template.html, а не рисуется руками:
шаблон использует те же локальные шрифты и цвета, что и сайт, поэтому
превью не разъедется с приглашением при правках.

Запускать при изменении даты, места или заголовка:
    python3 scripts/make_og.py
Результат коммитить.
"""
import http.server
import shutil
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "og_template.html"
OUT = ROOT / "site" / "og.png"
PORT = 8765
CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def serve():
    """Шаблон отдаём по http: через file:// браузер не подхватит шрифты
    из соседнего каталога."""
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(ROOT), **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("нужен playwright: pip install playwright")
        return 1
    if not TEMPLATE.exists():
        print(f"нет шаблона: {TEMPLATE}")
        return 1

    httpd = serve()
    try:
        with sync_playwright() as p:
            exe = CHROMIUM if Path(CHROMIUM).exists() else None
            browser = p.chromium.launch(executable_path=exe)
            page = browser.new_page(viewport={"width": 1200, "height": 630})
            page.goto(f"http://127.0.0.1:{PORT}/scripts/og_template.html",
                      wait_until="networkidle")
            page.wait_for_timeout(700)   # даём шрифтам примениться
            page.screenshot(path=str(OUT))
            browser.close()
    finally:
        httpd.shutdown()

    print(f"записано: {OUT} ({OUT.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
