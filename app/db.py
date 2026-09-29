import json
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH, DEFAULT_SETTINGS, TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER,
    username TEXT,
    phone TEXT,
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    extra TEXT DEFAULT '{}',
    tags TEXT DEFAULT '',
    opted_out INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_uid ON contacts(tg_user_id) WHERE tg_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    template_id INTEGER NOT NULL,
    status TEXT DEFAULT 'draft',      -- draft | running | paused | done
    created_at TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL,
    status TEXT DEFAULT 'queued',     -- queued | sent | read | replied | failed | skipped
    text TEXT,
    tg_message_id INTEGER,
    peer_id INTEGER,
    error TEXT,
    sent_at TEXT,
    sent_day TEXT,
    read_at TEXT,
    replied_at TEXT,
    UNIQUE(campaign_id, contact_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS ix_messages_peer ON messages(peer_id);

-- Списки обращений, загруженные из xlsx (у каждой строки свой готовый текст)
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    filename TEXT,
    status TEXT DEFAULT 'draft',      -- draft | running | paused | done
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS list_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER NOT NULL,
    row_no TEXT,                      -- «Исх. №» из файла
    kind TEXT,                        -- chat | person | bot | other
    title TEXT,                       -- «Название / имя в Telegram»
    first_name TEXT, last_name TEXT,
    link TEXT,
    peer_id INTEGER,                  -- marked id (user > 0, группа/канал -100…)
    username TEXT,
    topic_id INTEGER,
    topic_title TEXT,                 -- «Куда»
    folder TEXT,
    dialog TEXT,                      -- Диалог есть | Диалога нет | Не применимо (из файла)
    real_dialog TEXT,                 -- то же по данным Telegram (после «Найти получателей»)
    address TEXT,                     -- ты | вы | Чат | Не писать
    plan TEXT,
    note TEXT,
    text_no TEXT,
    text TEXT,
    src_status TEXT,                  -- «Статус» из файла (полностью)
    src_group TEXT,                   -- нормализованная группа статуса для фильтра
    state TEXT DEFAULT 'new',         -- new | queued | sent | read | replied | failed | skipped
    order_idx INTEGER,
    error TEXT,
    tg_message_id INTEGER,
    sent_at TEXT, sent_day TEXT, read_at TEXT, replied_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_li_list ON list_items(list_id, state);
CREATE INDEX IF NOT EXISTS ix_li_peer ON list_items(peer_id);

-- Диалоги аккаунта (заполняется при «Найти получателей»): для поиска чатов по названию
CREATE TABLE IF NOT EXISTS tg_dialogs (
    peer_id INTEGER PRIMARY KEY,      -- marked id
    title TEXT,
    kind TEXT,                        -- user | group | channel
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    level TEXT,
    text TEXT
);
"""

_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        for col, typ in [("list_items", "real_dialog TEXT")]:   # миграции для уже созданных баз
            try:
                _conn.execute(f"ALTER TABLE {col} ADD COLUMN {typ}")
            except sqlite3.OperationalError:
                pass
        for k, v in DEFAULT_SETTINGS.items():
            _conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    return _conn


def q(sql: str, params=()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def one(sql: str, params=()):
    return conn().execute(sql, params).fetchone()


def ex(sql: str, params=()) -> int:
    cur = conn().execute(sql, params)
    return cur.lastrowid


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_local() -> datetime:
    return datetime.now(TZ)


def today() -> str:
    return now_local().date().isoformat()


# ---- settings ----
def get_setting(key: str) -> str:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str) -> None:
    ex("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


def all_settings() -> dict:
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}


# ---- log ----
def log(text: str, level: str = "info") -> None:
    ex("INSERT INTO event_log(ts, level, text) VALUES (?, ?, ?)", (now_local().isoformat(timespec="seconds"), level, text))
    print(f"[{level}] {text}", flush=True)


def contact_vars(c: sqlite3.Row) -> dict:
    extra = {}
    try:
        extra = json.loads(c["extra"] or "{}")
    except Exception:
        pass
    first = c["first_name"] or ""
    last = c["last_name"] or ""
    base = {
        "first_name": first,
        "last_name": last,
        "name": (first + " " + last).strip() or (c["username"] or ""),
        "username": c["username"] or "",
        "phone": c["phone"] or "",
    }
    base.update({k: str(v) for k, v in extra.items()})
    return base
