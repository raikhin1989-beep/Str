#!/usr/bin/env python3
"""Скачивает woff2 из Google Fonts и генерирует локальный fonts.css.

Берём только подмножества cyrillic и latin — vietnamese/greek/latin-ext
странице не нужны и весят лишнее.
"""
import os
import re
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
OUT = "/home/user/Str/site/fonts"
KEEP = {"cyrillic", "latin"}

FAMILIES = [
    ("Oswald", "wght@700"),
    ("Russo+One", None),
    ("Bangers", None),
    ("Inter", "wght@400;600;800"),
    ("JetBrains+Mono", "wght@500;700"),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


os.makedirs(OUT, exist_ok=True)
css_parts = []

for fam, axis in FAMILIES:
    url = f"https://fonts.googleapis.com/css2?family={fam}"
    if axis:
        url += f":{axis}"
    url += "&display=swap"
    css = fetch(url).decode("utf-8")

    # Каждый @font-face предваряется комментарием с именем подмножества.
    blocks = re.findall(
        r"/\*\s*([a-z0-9-]+)\s*\*/\s*(@font-face\s*\{.*?\})", css, re.S)
    for subset, block in blocks:
        if subset not in KEEP:
            continue
        weight = re.search(r"font-weight:\s*(\d+)", block).group(1)
        src = re.search(r"url\((https://[^)]+\.woff2)\)", block).group(1)
        family = re.search(r"font-family:\s*'([^']+)'", block).group(1)

        name = f"{family.replace(' ', '')}-{weight}-{subset}.woff2"
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(fetch(src))

        local = block.replace(src, f"fonts/{name}")
        local = re.sub(r"\s+", " ", local).strip()
        css_parts.append(f"/* {family} {weight} {subset} */\n{local}")
        print(f"  {name}")

header = ("/* Шрифты захостены локально: страница не должна зависеть от\n"
          "   доступности fonts.googleapis.com и не должна мигать подменой\n"
          "   шрифта при загрузке. Сгенерировано scripts/getfonts.py. */\n")
with open(os.path.join(OUT, "fonts.css"), "w") as fh:
    fh.write(header + "\n" + "\n\n".join(css_parts) + "\n")
print(f"\nвсего файлов: {len(css_parts)}")
