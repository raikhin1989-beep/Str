"""Хранилище заявок: SQLite + простые миграции.

База лежит в STR_DATA_DIR (по умолчанию /opt/str-api/data) — вне вебрута,
который раздаётся наружу и чистится rsync --delete при каждом деплое.

Схема меняется только добавлением новой записи в MIGRATIONS: применённые
версии отмечаются в таблице schema_migrations, поэтому повторный запуск
деплоя безопасен, а редактировать уже применённую миграцию нельзя —
на сервере она не переиграется.
"""
import hashlib
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("STR_DATA_DIR", "/opt/str-api/data"))
DB_PATH = DATA_DIR / "str.db"
SALT_PATH = DATA_DIR / "ip_salt"

MIGRATIONS = [
    (
        "001_guests",
        """
        CREATE TABLE guests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            token         TEXT    NOT NULL UNIQUE,
            name          TEXT    NOT NULL,
            guests_count  INTEGER NOT NULL,
            drink         TEXT    NOT NULL,
            hype          INTEGER NOT NULL,
            allergies     TEXT    NOT NULL DEFAULT '',
            message       TEXT    NOT NULL DEFAULT '',
            ip_hash       TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL
        );
        CREATE INDEX idx_guests_ip ON guests(ip_hash);
        CREATE INDEX idx_guests_created ON guests(created_at);
        """,
    ),
    (
        "002_brings_own",
        # Гость может принести свой напиток — тогда его компанию не нужно
        # закладывать в закупку.
        "ALTER TABLE guests ADD COLUMN brings_own INTEGER NOT NULL DEFAULT 0;",
    ),
    (
        "003_telegram",
        # link_code — короткий код для диплинка t.me/<bot>?start=<code>.
        # Он отдельный от token: токен даёт полный доступ к правке записи и
        # ему не место в ссылке, которой гость делится с ботом.
        """
        ALTER TABLE guests ADD COLUMN link_code TEXT;
        ALTER TABLE guests ADD COLUMN tg_chat_id TEXT;
        ALTER TABLE guests ADD COLUMN tg_username TEXT;
        CREATE UNIQUE INDEX idx_guests_link_code ON guests(link_code);

        -- Мелкие настройки, которые нужно пережить перезапуск: чат
        -- именинника для уведомлений, одноразовый код его привязки.
        CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """,
    ),
    (
        "004_outbox",
        # Сервер не может достучаться до Telegram — провайдер блокирует.
        # Поэтому он только кладёт сообщения в очередь, а рассылает их
        # задача GitHub Actions, у которой доступ есть.
        """
        CREATE TABLE outbox (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    TEXT    NOT NULL,
            text       TEXT    NOT NULL,
            kind       TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL,
            sent_at    TEXT,
            attempts   INTEGER NOT NULL DEFAULT 0,
            last_error TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_outbox_pending ON outbox(sent_at, id);
        """,
    ),
    (
        "005_reminded",
        # Отметка, что напоминание уже уходило: защита от повторов, если
        # задача по расписанию отработает дважды.
        "ALTER TABLE guests ADD COLUMN reminded_at TEXT;",
    ),
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_conn():
    """Соединение на запрос. SQLite не любит долгоживущие соединения,
    делящиеся между потоками, а нагрузка здесь — десятки гостей."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ")"
        )
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for name, sql in MIGRATIONS:
            if name in done:
                continue
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (name,))


def ip_hash(ip: str) -> str:
    """IP не хранится в открытом виде: он нужен только чтобы отличать
    повторные отправки и ловить ботов, а само значение — лишние
    персональные данные. Соль своя у каждой установки."""
    if not SALT_PATH.exists():
        SALT_PATH.write_text(secrets.token_hex(32))
        SALT_PATH.chmod(0o600)
    salt = SALT_PATH.read_text().strip()
    return hashlib.sha256((salt + ip).encode()).hexdigest()[:32]


def new_token() -> str:
    return secrets.token_urlsafe(24)


def new_link_code() -> str:
    """Код для диплинка в Telegram. Короче основного токена: он попадает
    в ссылку и в переписку с ботом, поэтому прав на правку записи не даёт —
    только опознаёт гостя."""
    return secrets.token_urlsafe(9)


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
