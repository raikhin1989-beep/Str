#!/usr/bin/env python3
"""Снимок базы гостей.

Запускается systemd-таймером раз в сутки. Снимок делается через
sqlite3.Connection.backup(), а не копированием файла: база работает в режиме
WAL, и обычный `cp` может унести файл без части свежих транзакций — то есть
бэкап окажется битым ровно тогда, когда понадобится.

Переменные окружения:
  STR_DATA_DIR    — где лежит str.db (по умолчанию /opt/str-api/data)
  STR_BACKUP_DIR  — куда складывать (по умолчанию /opt/str-api/backups)
  STR_BACKUP_KEEP — сколько снимков хранить (по умолчанию 7)
"""
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("STR_DATA_DIR", "/opt/str-api/data"))
DB_PATH = DATA_DIR / "str.db"
BACKUP_DIR = Path(os.environ.get("STR_BACKUP_DIR", "/opt/str-api/backups"))
KEEP = int(os.environ.get("STR_BACKUP_KEEP", "7"))


def make_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tmp = BACKUP_DIR / f"str-{stamp}.db"
    final = BACKUP_DIR / f"str-{stamp}.db.gz"

    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    with open(tmp, "rb") as fi, gzip.open(final, "wb") as fo:
        shutil.copyfileobj(fi, fo)
    tmp.unlink()
    final.chmod(0o600)
    return final


def rotate() -> list[Path]:
    """Оставляем KEEP свежих. Имена содержат дату в сортируемом виде,
    поэтому обычной сортировки достаточно."""
    snaps = sorted(BACKUP_DIR.glob("str-*.db.gz"))
    dropped = snaps[:-KEEP] if len(snaps) > KEEP else []
    for old in dropped:
        old.unlink()
    return dropped


def main() -> int:
    if not DB_PATH.exists():
        print(f"базы нет: {DB_PATH}")
        return 1
    path = make_backup()
    dropped = rotate()
    size = path.stat().st_size
    print(f"снимок: {path.name} ({size} байт)")
    if dropped:
        print(f"удалено старых: {', '.join(p.name for p in dropped)}")
    print(f"всего снимков: {len(sorted(BACKUP_DIR.glob('str-*.db.gz')))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
