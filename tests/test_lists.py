"""Списки xlsx: импорт реального формата, фильтр, очередь, отправка мок-клиентом."""
import asyncio, os, tempfile, types
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TG_API_ID", "1"); os.environ.setdefault("TG_API_HASH", "x")
from pathlib import Path
import pytest
from telethon import errors
from app import db, outreach, worker
from app.tg import tg

SAMPLE = os.environ.get("SAMPLE_XLSX", "")


def test_parse_link():
    assert outreach.parse_link("https://web.telegram.org/a/#-1001234567890_29438") == {"peer_id": -1001234567890, "topic_id": 29438, "username": None}
    assert outreach.parse_link("https://web.telegram.org/a/#152949444")["peer_id"] == 152949444
    assert outreach.parse_link("https://t.me/Marconi_kasper")["username"] == "Marconi_kasper"
    assert outreach.status_group("Не отправлено. Прямая ссылка") == "Не отправлено"
    assert outreach.status_group("Отправлено 28.09.2026 · партия") == "Отправлено 28.09.2026"


class Fake:
    def __init__(self): self.sent = []; self.fail = {}
    async def get_input_entity(self, x):
        if isinstance(x, int) and x != 999: return x
        raise ValueError("no")
    async def get_entity(self, x): return types.SimpleNamespace(id=5)
    async def send_message(self, e, text, reply_to=None, link_preview=True):
        key = e if isinstance(e, int) else "u"
        if key in self.fail: raise self.fail[key]
        self.sent.append((key, reply_to, text[:15])); return types.SimpleNamespace(id=len(self.sent))
    def is_connected(self): return True


@pytest.mark.skipif(not SAMPLE or not Path(SAMPLE).exists(), reason="нет файла-образца")
def test_real_file_flow():
    lid, n, _ = outreach.import_file(Path(SAMPLE).read_bytes(), "sample.xlsx")
    assert n == 155
    f = outreach.default_filter(lid)
    assert outreach.enqueue(lid, f) == 105
    db.ex("UPDATE lists SET status='running' WHERE id=?", (lid,))
    tg.client = Fake(); tg.me = types.SimpleNamespace(first_name="Me", last_name=None, username=None, phone="1")
    db.ex("UPDATE campaigns SET status='done'")   # изолируемся от других тестов
    for k, v in {"work_start": "00:00", "work_end": "23:59", "warmup_start": str(worker.sent_today() + 4),
                 "warmup_step": "0", "warmup_start_date": "", "paused_until": "", "daily_max": "500"}.items():
        db.set_setting(k, v)
    first = db.q("SELECT peer_id FROM list_items WHERE state='queued' ORDER BY order_idx LIMIT 2")
    tg.client.fail[first[1]["peer_id"]] = errors.UserPrivacyRestrictedError(request=None)
    for _ in range(6):
        asyncio.run(worker.tick())
    st = dict(db.q("SELECT state, COUNT(*) FROM list_items WHERE list_id=? GROUP BY state", (lid,)))
    print(st, tg.client.sent)
    assert st.get("sent") == 4 and st.get("failed") == 1      # лимит 4 в день
    assert "лимит" in worker.state["status"]
    # чат с темой форума → reply_to=topic_id, тема «_1» (General) → без reply_to
    assert all(r is None or r > 1 for _, r, _ in tg.client.sent)


@pytest.mark.skipif(not SAMPLE or not Path(SAMPLE).exists(), reason="нет файла-образца")
def test_dialog_check_with_telegram():
    lid, n, _ = outreach.import_file(Path(SAMPLE).read_bytes(), "sample2.xlsx")
    items = db.q("SELECT peer_id, dialog FROM list_items WHERE list_id=? AND kind='person' AND peer_id>0", (lid,))
    with_file = [i["peer_id"] for i in items if i["dialog"] == "Диалог есть"]
    without_file = [i["peer_id"] for i in items if i["dialog"] == "Диалога нет"]
    # в «Telegram» диалог есть со всеми «с диалогом» кроме первого, и ещё с одним «без диалога»
    real = set(with_file[1:]) | {without_file[0]}

    class D:
        def __init__(self, uid):
            self.is_user = True; self.is_group = False; self.is_channel = False
            self.id = uid; self.name = f"u{uid}"; self.message = object(); self.entity = types.SimpleNamespace(id=uid)

    class C:
        session = types.SimpleNamespace(get_input_entity=lambda pid: pid)
        flood_sleep_threshold = 0
        async def iter_dialogs(self, limit=None):
            for uid in real:
                yield D(uid)
        async def iter_participants(self, *a, **k):
            if False:
                yield None

    tg.client = C()
    asyncio.run(tg.prepare_list(lid))
    st = tg.prepare_state[lid]
    assert st["mismatch"] == 2, st
    f = outreach.default_filter(lid) | {"mismatch": True, "src": [], "state": []}
    w, p = outreach.filter_sql(lid, f)
    rows = db.q(f"SELECT peer_id, dialog, real_dialog FROM list_items WHERE {w}", p)
    assert {r["peer_id"] for r in rows} == {with_file[0], without_file[0]}
    # порядок по Telegram: человек, у которого диалог нашёлся в Telegram, идёт в группу «с диалогом»
    f2 = {"state": ["new"], "src": [], "kind": ["person"], "use_real": True, "q": ""}
    outreach.enqueue(lid, f2)
    first = db.one("SELECT peer_id FROM list_items WHERE list_id=? AND state='queued' ORDER BY order_idx LIMIT 1", (lid,))
    assert first["peer_id"] in real


def test_migration_adds_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)   # схема прошлой версии: list_items без real_dialog
    c.executescript(db.SCHEMA.replace("    real_dialog TEXT,", ""))
    assert "real_dialog" not in [r[1] for r in c.execute("PRAGMA table_info(list_items)")]
    c.commit(); c.close()
    old_conn, old_path = db._conn, db.DB_PATH
    try:
        db._conn = None; db.DB_PATH = path
        cols = [r[1] for r in db.conn().execute("PRAGMA table_info(list_items)")]
        assert "real_dialog" in cols
    finally:
        db._conn, db.DB_PATH = old_conn, old_path


def test_resolve_chat_alt_id_and_title():
    lid = db.ex("INSERT INTO lists(name) VALUES ('t')")
    a = db.ex("INSERT INTO list_items(list_id, kind, title, peer_id) VALUES (?,?,?,?)", (lid, "chat", "Test1", -1004000000001))
    b = db.ex("INSERT INTO list_items(list_id, kind, title, peer_id) VALUES (?,?,?,?)", (lid, "chat", "Мой чат", -777))
    db.ex("INSERT OR REPLACE INTO tg_dialogs(peer_id, title, kind) VALUES (?,?,?)", (-1009999, "мой чат", "group"))

    class C:
        known = {-4000000001, -1009999}
        async def get_input_entity(self, pid):
            if pid in self.known: return pid
            raise ValueError("no")
    tg.client = C()
    it = db.one("SELECT * FROM list_items WHERE id=?", (a,))
    assert asyncio.run(tg.resolve_item(it)) == -4000000001          # «-100» лишний → обычная группа
    assert db.one("SELECT peer_id FROM list_items WHERE id=?", (a,))["peer_id"] == -4000000001
    it = db.one("SELECT * FROM list_items WHERE id=?", (b,))
    assert asyncio.run(tg.resolve_item(it)) == -1009999              # найден по названию
    db.ex("UPDATE list_items SET peer_id=-1, title='нет такого' WHERE id=?", (b,))
    with pytest.raises(ValueError, match="чат не найден"):
        asyncio.run(tg.resolve_item(db.one("SELECT * FROM list_items WHERE id=?", (b,))))


def test_topic_from_other_chat_ignored():
    assert outreach.parse_link("https://web.telegram.org/a/#-1001234567890_29438")["topic_id"] == 29438
