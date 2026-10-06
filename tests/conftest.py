"""Тесты с настоящим Postgres и поддельным клиентом Telegram.
Запуск: scripts/test.sh   (или TEST_DATABASE_URL=postgresql://… python -m pytest tests)"""
import asyncio
import os
import tempfile
import types

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
if not URL:
    raise RuntimeError("Задайте TEST_DATABASE_URL (отдельная база, будет очищена) или запустите scripts/test.sh")
os.environ["DATABASE_URL"] = URL
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["TG_API_ID"] = "1"
os.environ["TG_API_HASH"] = "x"

import psycopg  # noqa: E402
from telethon.tl.types import InputPeerUser  # noqa: E402

from app import auth, db, worker  # noqa: E402
from app.tg import tgm  # noqa: E402

TABLES = ["ai_gaps", "ai_suggestions", "do_not_contact", "login_failures", "ai_reply_jobs", "ai_sandbox_messages", "ai_sandboxes", "ai_drafts", "ai_examples", "ai_cards", "ai_profiles", "found_chat_hits", "found_chats", "chat_searches", "segment_leads", "segments", "messages", "campaign_leads", "campaign_steps", "campaign_accounts", "campaigns", "tg_dialogs",
          "leads", "templates", "event_log", "tg_accounts", "users", "settings"]


def _recreate_database():
    base, name = URL.rsplit("/", 1)
    with psycopg.connect(f"{base}/postgres", autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')


@pytest.fixture(scope="session", autouse=True)
def database():
    _recreate_database()
    db.init(URL)
    yield
    db.close()


@pytest.fixture(autouse=True)
def clean():
    db.ex(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
    db.ex("""DELETE FROM funnel_stages WHERE id NOT IN (SELECT id FROM funnel_stages ORDER BY id LIMIT 6);
             UPDATE funnel_stages SET name=v.name, position=v.pos, is_goal=v.goal, is_lost=v.lost FROM (VALUES
               (1,'написали',1,false,false),(2,'ответил',2,false,false),(3,'интерес',3,false,false),
               (4,'записался',4,false,false),(5,'пришёл / купил',5,true,false),(6,'отказ',6,false,true)) v(id,name,pos,goal,lost)
             WHERE funnel_stages.id=v.id""")
    for k, v in db.DEFAULT_SETTINGS.items():
        db.set_setting(k, v)
    from app import inbox, notify
    inbox.invalidate_unread()
    notify.reset()
    tgm.accounts.clear()
    tgm.prepare_state.clear()
    tgm.groups_state.clear()
    tgm.search_state.clear()
    worker.state.clear()
    yield


class FakeClient:
    """Минимум Telethon: get_input_entity по int, get_entity по username, send_message."""

    def __init__(self, known_usernames=None):
        self.sent = []          # (entity, text, reply_to)
        self.fail = {}          # entity/username → исключение
        self.delay = 0
        self.known = dict(known_usernames or {})   # username → user_id
        self.flood_sleep_threshold = 0
        self.session = types.SimpleNamespace(get_input_entity=lambda pid: pid)

    def is_connected(self):
        return True

    async def get_input_entity(self, x):
        if isinstance(x, int):
            return x
        raise ValueError("no")

    async def get_entity(self, x):
        if x in self.fail:
            raise self.fail[x]
        if x not in self.known:
            raise ValueError(f'No user has "{x}" as username')
        return InputPeerUser(user_id=self.known[x], access_hash=0)

    def action(self, entity, act):
        """«печатает…»: запоминаем, кому и что показывали."""
        client = self

        class _Act:
            async def __aenter__(self):
                client.actions = getattr(client, "actions", []) + [(entity, act)]

            async def __aexit__(self, *exc):
                return False
        return _Act()

    async def send_message(self, entity, text, reply_to=None, link_preview=True):
        if self.delay:
            await asyncio.sleep(self.delay)
        key = entity if isinstance(entity, int) else getattr(entity, "user_id", entity)
        if key in self.fail:
            raise self.fail[key]
        self.sent.append((key, text, reply_to))
        return types.SimpleNamespace(id=len(self.sent))


def make_user(login="admin", role="admin", name=None) -> dict:
    uid = auth.create_user(login, name or login.title(), "password123", role)
    return db.one("SELECT * FROM users WHERE id=%s", (uid,))


def make_account(user_id: int, label="A", tg_user_id=None, client=None, **settings) -> int:
    cols = {"user_id": user_id, "label": label, "status": "active", "tg_user_id": tg_user_id,
            "first_name": label, "work_start": "00:00", "work_end": "00:00", "delay_min": 0, "delay_max": 0,
            **settings}
    aid = db.ex(f"INSERT INTO tg_accounts({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                tuple(cols.values()))
    acc = tgm.get(aid)
    acc.client = client or FakeClient()
    acc.me = types.SimpleNamespace(id=tg_user_id or 9000 + aid, first_name=label, last_name=None, username=None, phone="1")
    return aid


def make_lead(**kw) -> int:
    cols = {"kind": "person", "first_name": "", **kw}
    return db.ex(f"INSERT INTO leads({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                 tuple(cols.values()))


def snapshot(*account_ids: int) -> None:
    """Как будто «Найти получателей» уже загрузил диалоги этих аккаунтов (пусто, без переписки)."""
    for aid in account_ids:
        db.ex("INSERT INTO tg_dialogs(account_id, peer_id, kind) VALUES (%s, 0, 'user') ON CONFLICT DO NOTHING", (aid,))


def make_campaign(user_id: int, account_ids: list[int], body="Привет, {first_name}!", status="running") -> int:
    from app import campaigns
    cid = db.ex("INSERT INTO campaigns(name, created_by, status) VALUES ('c', %s, %s) RETURNING id", (user_id, status))
    db.ex("INSERT INTO campaign_steps(campaign_id, position, body) VALUES (%s, 1, %s)", (cid, body))
    campaigns.set_accounts(cid, account_ids)
    return cid


def add_rows(cid: int, lead_ids: list[int], **kw) -> list[int]:
    out = []
    for lid in lead_ids:
        cols = {"campaign_id": cid, "lead_id": lid, **kw}
        out.append(db.ex(f"INSERT INTO campaign_leads({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                         tuple(cols.values())))
    return out


def run(coro):
    return asyncio.run(coro)


def drain(account_id: int, n: int = 10):
    """Несколько итераций отправки аккаунта."""
    async def go():
        for _ in range(n):
            await worker.tick(account_id)
    run(go())


def cl_state(rid: int) -> dict:
    return db.one("SELECT state, error, account_id FROM campaign_leads WHERE id=%s", (rid,))
