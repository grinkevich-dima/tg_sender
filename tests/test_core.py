"""Тесты без реального Telegram: мок-клиент. Запуск: python -m pytest tests -q"""
import asyncio, os, random, tempfile, types
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["TG_API_ID"] = "1"; os.environ["TG_API_HASH"] = "x"; os.environ["PANEL_PASSWORD"] = ""

from datetime import date, timedelta
from telethon import errors
from app import db, worker
from app.templating import render, variables_in
from app.tg import tg


def test_render():
    r = render("{Привет|Здравствуйте}, {first_name}! {Как {дела|жизнь}?|Рад видеть}", {"first_name": "Аня"}, random.Random(1))
    assert "Аня" in r and "{" not in r
    assert render("{Привет, {first_name}|Hi {first_name}}", {"first_name": "Ян"}).endswith("Ян")
    assert render("Привет, {first_name}!", {}) == "Привет!"
    assert variables_in("{a|b} {first_name} {company}") == ["company", "first_name"]


def test_warmup_limit():
    db.set_setting("warmup_start_date", "")
    assert worker.daily_limit() == 10
    db.set_setting("warmup_start_date", (date.today() - timedelta(days=3)).isoformat())
    assert worker.daily_limit() == 25
    db.set_setting("warmup_start_date", (date.today() - timedelta(days=30)).isoformat())
    assert worker.daily_limit() == 50
    db.set_setting("warmup_enabled", "0"); assert worker.daily_limit() == 50
    db.set_setting("warmup_enabled", "1"); db.set_setting("warmup_start_date", "")


class FakeClient:
    def __init__(self): self.sent = []; self.fail = {}
    async def get_input_entity(self, x):
        if isinstance(x, int): return x
        raise ValueError("no")
    async def get_entity(self, x):
        if x == "ghost": raise ValueError("No user has \"ghost\" as username")
        return types.SimpleNamespace(id=hash(x) % 10**6)
    async def send_message(self, entity, text):
        if isinstance(entity, int) and entity in self.fail: raise self.fail[entity]
        self.sent.append((entity, text))
        return types.SimpleNamespace(id=len(self.sent), chat_id=entity if isinstance(entity, int) else entity.id)
    def is_connected(self): return True


def test_send_flow():
    tg.client = FakeClient(); tg.me = types.SimpleNamespace(first_name="Me", last_name=None, username=None, phone="1")
    db.set_setting("work_start", "00:00"); db.set_setting("work_end", "23:59")
    db.set_setting("delay_min", "5"); db.set_setting("delay_max", "5")
    tid = db.ex("INSERT INTO templates(name, body) VALUES ('t', '{Привет|Хай}, {first_name}!')")
    ids = []
    for uid, un, fn in [(101, None, "Аня"), (102, None, "Боб"), (None, "ghost", "Призрак"), (103, None, "Вера"), (None, "real_user", "Гена")]:
        ids.append(db.ex("INSERT INTO contacts(tg_user_id, username, first_name) VALUES (?,?,?)", (uid, un, fn)))
    db.ex("UPDATE contacts SET opted_out=1 WHERE tg_user_id=103")
    tg.client.fail[102] = errors.UserPrivacyRestrictedError(request=None)
    cid = db.ex("INSERT INTO campaigns(name, template_id, status) VALUES ('c', ?, 'running')", (tid,))
    for i in ids: db.ex("INSERT INTO messages(campaign_id, contact_id) VALUES (?,?)", (cid, i))

    async def go():
        for _ in range(6): await worker.tick()
    asyncio.run(go())
    st = dict(db.q("SELECT status, COUNT(*) FROM messages GROUP BY status"))
    print(st, tg.client.sent)
    assert st == {"sent": 2, "failed": 2, "skipped": 1}
    assert db.one("SELECT status FROM campaigns WHERE id=?", (cid,))["status"] == "done"
    assert db.get_setting("warmup_start_date") == db.today()
    assert worker.sent_today() == 2

    # MessageRead → read
    db.ex("UPDATE messages SET status='read' WHERE peer_id=101 AND tg_message_id<=1")
    # FloodWait → пауза, сообщение остаётся в очереди
    cid2 = db.ex("INSERT INTO campaigns(name, template_id, status) VALUES ('c2', ?, 'running')", (tid,))
    db.ex("INSERT INTO messages(campaign_id, contact_id) VALUES (?,?)", (cid2, ids[0]))
    tg.client.fail[101] = errors.FloodWaitError(request=None, capture=120)
    asyncio.run(worker.tick())
    assert worker.paused_until() is not None
    assert db.one("SELECT status FROM messages WHERE campaign_id=?", (cid2,))["status"] == "queued"
    # PeerFlood → кампании на паузу
    db.set_setting("paused_until", "")
    tg.client.fail[101] = errors.PeerFloodError(request=None)
    asyncio.run(worker.tick())
    assert db.one("SELECT status FROM campaigns WHERE id=?", (cid2,))["status"] == "paused"


def test_daily_limit_blocks():
    db.set_setting("paused_until", "")
    db.set_setting("warmup_start", "2"); db.set_setting("warmup_start_date", "")
    db.ex("UPDATE campaigns SET status='running' WHERE name='c2'")
    tg.client.fail.clear()
    asyncio.run(worker.tick())
    assert "лимит" in worker.state["status"]
    db.set_setting("warmup_start", "10")
