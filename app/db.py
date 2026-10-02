"""Postgres: пул соединений, короткие помощники для запросов, миграции.

Запросы синхронные (psycopg 3): быстрые локальные запросы не мешают циклу asyncio, а код
остаётся простым и одинаково работает из обработчиков, воркера и тестов.
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL, DEFAULT_SETTINGS, TZ

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_pool: ConnectionPool | None = None
_tx_conn: ContextVar = ContextVar("tx_conn", default=None)


def init(url: str = DATABASE_URL) -> None:
    global _pool
    if _pool is not None:
        return
    _pool = ConnectionPool(url, min_size=1, max_size=10, kwargs={"autocommit": True, "row_factory": dict_row},
                           open=True)
    _pool.wait(timeout=30)
    migrate()
    for k, v in DEFAULT_SETTINGS.items():
        ex("INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT DO NOTHING", (k, v))


# ---- одна рабочая копия ----
LEADER_LOCK = 0x7465_6C65   # произвольный номер блокировки панели
_leader_conn = None


def acquire_leader(url: str = DATABASE_URL) -> bool:
    """Telegram и отправку запускает только одна копия панели: две копии на одних сессиях Telegram
    могут привести к принудительному завершению сессий. Блокировка держится, пока жива копия (её соединение)."""
    global _leader_conn
    if _leader_conn is not None:
        return True
    conn = psycopg.connect(url, autocommit=True)
    if conn.execute("SELECT pg_try_advisory_lock(%s)", (LEADER_LOCK,)).fetchone()[0]:
        _leader_conn = conn
        return True
    conn.close()
    return False


def release_leader() -> None:
    global _leader_conn
    if _leader_conn is not None:
        _leader_conn.close()       # блокировка снимается вместе с соединением
        _leader_conn = None


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def _conn():
    c = _tx_conn.get()
    if c is not None:
        yield c
        return
    if _pool is None:
        init()
    with _pool.connection() as c:
        yield c


def q(sql: str, params=()) -> list[dict]:
    with _conn() as c:
        return c.execute(sql, params).fetchall()


def one(sql: str, params=()) -> dict | None:
    with _conn() as c:
        return c.execute(sql, params).fetchone()


def val(sql: str, params=()):
    row = one(sql, params)
    return next(iter(row.values())) if row else None


def ex(sql: str, params=()):
    """INSERT/UPDATE/DELETE. Если в запросе RETURNING — вернёт первое значение первой строки."""
    with _conn() as c:
        cur = c.execute(sql, params)
        if cur.description:
            row = cur.fetchone()
            return next(iter(row.values())) if row else None
        return None


def changed(sql: str, params=()) -> int:
    """UPDATE/DELETE → сколько строк затронуто."""
    with _conn() as c:
        return c.execute(sql, params).rowcount


@contextmanager
def tx():
    """Одна транзакция: все q/one/ex внутри блока идут через одно соединение.
    Внутри не должно быть await — иначе другие задачи asyncio попадут в эту транзакцию.
    Вложенный tx — точка сохранения: ошибка внутри откатывает только его."""
    outer = _tx_conn.get()
    if outer is not None:
        with outer.transaction():
            yield
        return
    if _pool is None:
        init()
    with _pool.connection() as c:
        with c.transaction():
            token = _tx_conn.set(c)
            try:
                yield
            finally:
                _tx_conn.reset(token)


def jsonb(value) -> Jsonb:
    return Jsonb(value)


# ---- миграции ----
def migrate() -> list[str]:
    """Применяет app/migrations/NNN_*.sql по порядку, каждую в своей транзакции."""
    applied = []
    with _pool.connection() as c:
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r["version"] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            with c.transaction():
                c.execute(path.read_text())
                c.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (path.stem,))
            applied.append(path.stem)
    return applied


# ---- время ----
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return datetime.now(TZ)


def account_tz(acc: dict | None):
    """Часовой пояс аккаунта (рабочие часы, граница суток) или общий TZ_NAME."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = (acc or {}).get("tz")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return TZ


def today():
    return now_local().date()


# ---- общие настройки ----
def get_setting(key: str) -> str:
    v = val("SELECT value FROM settings WHERE key=%s", (key,))
    return v if v is not None else DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str) -> None:
    ex("INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


# ---- журнал ----
def log(text: str, level: str = "info", account_id: int | None = None) -> None:
    ex("INSERT INTO event_log(level, text, account_id) VALUES (%s, %s, %s)", (level, text, account_id))
    print(f"[{level}]{f' acc#{account_id}' if account_id else ''} {text}", flush=True)


def lead_vars(lead: dict) -> dict:
    """Переменные шаблона для лида: имя, username, телефон + колонки из импорта (extra)."""
    extra = lead.get("extra") or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = {}
    first = lead.get("first_name") or ""
    last = lead.get("last_name") or ""
    base = {
        "first_name": first,
        "last_name": last,
        "name": (first + " " + last).strip() or (lead.get("username") or lead.get("title") or ""),
        "username": lead.get("username") or "",
        "phone": lead.get("phone") or "",
    }
    base.update({k: str(v) for k, v in extra.items()})
    return base
