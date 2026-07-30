#!/usr/bin/env python3
"""Готовит локальные шрифты для site/fonts.

Скачивает woff2 из Google Fonts и **урезает их до нужных символов**.
Почему не берём готовые подмножества Google: их «latin» тянет уйму глифов,
которых на странице нет, и выходило 283 КБ на загрузку — по мобильному
интернету это заметно, а часть гостей откроет приглашение с телефона в дороге.

Латиница и кириллица сливаются в один файл на начертание: так вдвое меньше
запросов и нет `unicode-range`, из-за которого браузер тянул оба подмножества
(цифры и латиница на русской странице есть всегда).

Диапазоны заданы с запасом на **имена гостей** — их мы не знаем заранее,
поэтому кириллица берётся целиком, включая украинские и казахские буквы.

Запускать при смене состава шрифтов:
    pip install fonttools brotli
    python3 scripts/getfonts.py
Результат коммитить.
"""
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Нарочно старый User-Agent — и именно этот. Современному браузеру Google
# отдаёт CSS, разбитый на подмножества, и КАЖДЫЙ файл содержит только свой
# набор глифов: взяв «первый попавшийся» и вырезав из него кириллицу, получишь
# валидный шрифт на четыре глифа. Старым UA отдаётся один файл с полным
# шрифтом, но формат зависит от UA: MSIE 6 получает EOT (fontTools его не
# читает), а вот этот Safari — обычный TTF.
UA = ("Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10_6_8) "
      "AppleWebKit/533.20.25 (KHTML, like Gecko) Version/5.0.4 Safari/533.20.27")
OUT = Path(__file__).resolve().parent.parent / "site" / "fonts"

# Что оставляем в шрифте.
UNICODES = ",".join([
    "U+0020-007E",          # базовая латиница, цифры, знаки
    "U+00A0-00FF",          # ° § и прочее из latin-1
    "U+0400-045F",          # кириллица
    "U+0490-0491",          # украинская ґ
    "U+04B0-04B1",          # казахская ұ
    "U+2116",               # №
    "U+2010-2015",          # дефисы и тире
    "U+2018-201F",          # кавычки, включая «лапки»
    "U+2022", "U+00B7",     # • и ·
    "U+2026",               # …
    "U+2192",               # →
    "U+2713",               # ✓ — им помечаем «подключено»
])

# Начертания ровно те, что использует вёрстка. Лишнее начертание — это
# лишние килобайты: Inter в трёх начертаниях весил 196 КБ.
#
# Последнее поле — что обязано остаться после урезания. Набор разный не для
# красоты: у Bangers кириллицы нет вовсе (ровно поэтому он стоит только на
# латинском SPORT HERO), а галочку ✓ несёт лишь основной текст.
CYR = "АБЯабяёЁ"
LAT = "ABZabz0123456789"
PUNCT = "«»—·"
FAMILIES = [
    ("Oswald", "wght@700", [700], CYR + LAT + PUNCT),
    ("Russo+One", None, [400], CYR + LAT + PUNCT),
    ("Bangers", None, [400], LAT),
    ("Inter", "wght@400;700", [400, 700], CYR + LAT + PUNCT + "✓…"),
    ("JetBrains+Mono", "wght@700", [700], CYR + LAT + "·"),
]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.woff2"):
        old.unlink()

    css_parts = []
    total = 0
    for fam, axis, weights, required in FAMILIES:
        url = f"https://fonts.googleapis.com/css2?family={fam}"
        if axis:
            url += f":{axis}"
        url += "&display=swap"
        css = fetch(url).decode("utf-8")
        family = re.search(r"font-family:\s*'([^']+)'", css).group(1)
        blocks = re.findall(r"@font-face\s*\{.*?\}", css, re.S)

        for weight in weights:
            src = None
            for block in blocks:
                if re.search(rf"font-weight:\s*{weight}\b", block):
                    m = re.search(r"url\((https://[^)]+)\)", block)
                    if m:
                        src = m.group(1)
                    break
            if src is None:
                print(f"не нашёл {family} {weight}")
                return 1

            name = f"{family.replace(' ', '')}-{weight}.woff2"
            dest = OUT / name
            with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as tmp:
                tmp.write(fetch(src))
                tmp_path = tmp.name
            try:
                subprocess.run([
                    sys.executable, "-m", "fontTools.subset", tmp_path,
                    f"--unicodes={UNICODES}",
                    "--layout-features=kern,liga",
                    "--flavor=woff2",
                    f"--output-file={dest}",
                ], check=True, capture_output=True)
            finally:
                os.unlink(tmp_path)

            # Проверяем покрытие, а не только размер: подмена исходника на
            # неполный файл даёт валидный шрифт без кириллицы, и заметно это
            # станет только на живой странице.
            from fontTools.ttLib import TTFont
            cmap = TTFont(dest).getBestCmap()
            missing = [ch for ch in required if ord(ch) not in cmap]
            if missing:
                print(f"  {name}: НЕ ХВАТАЕТ глифов: {''.join(missing)}")
                return 1

            size = dest.stat().st_size
            total += size
            css_parts.append(
                f"/* {family} {weight} */\n"
                f"@font-face {{ font-family: '{family}'; font-style: normal;"
                f" font-weight: {weight}; font-display: swap;"
                f" src: url({name}) format('woff2'); }}"
            )
            print(f"  {name}: {size/1024:.1f} КБ, глифов {len(cmap)}")

    header = ("/* Шрифты захостены локально и урезаны до нужных символов:\n"
              "   готовые подмножества Google тянули 283 КБ на загрузку.\n"
              "   Латиница и кириллица в одном файле — вдвое меньше запросов.\n"
              "   Сгенерировано scripts/getfonts.py, править руками нет смысла. */\n")
    (OUT / "fonts.css").write_text(header + "\n" + "\n\n".join(css_parts) + "\n")
    print(f"\nвсего: {total/1024:.0f} КБ в {len(css_parts)} файлах")
    return 0


if __name__ == "__main__":
    sys.exit(main())
