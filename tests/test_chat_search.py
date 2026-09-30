"""Поиск групп: запросы, оценка, поиск с поддельным Telegram, ссылки, статусы, страница."""
import asyncio
import types
from datetime import timedelta

import pytest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, ChatPhotoEmpty

from app import chat_search as cs, db
from app.tg import tgm
from tests.conftest import FakeClient, make_account, make_user, run
from tests.test_web import browser


def test_queries_and_parsing():
    assert cs.build_queries(["бизнес клуб", "предприниматель"], ["Минск"]) == \
        ["бизнес клуб", "бизнес клуб Минск", "предприниматель", "предприниматель Минск"]
    assert len(cs.build_queries([f"w{i}" for i in range(30)], ["a", "b"])) == cs.MAX_QUERIES
    assert cs.split_words("a, b\nc;;") == ["a", "b", "c"]
    assert cs.parse_link("https://t.me/biz_minsk") == ("username", "biz_minsk")
    assert cs.parse_link("@biz_minsk") == ("username", "biz_minsk")
    assert cs.parse_link("https://t.me/+AbCdEf123") == ("invite", "AbCdEf123")
    assert cs.parse_link("t.me/joinchat/XyZ") == ("invite", "XyZ")
    assert cs.parse_link("просто текст") is None


def test_score():
    assert cs.score(3, 3, 10000, 20, 1, None) == 100
    assert cs.score(3, 3, 10000, 20, 1, "крипта") == 0                 # стоп-слово
    assert cs.score(3, 3, 10000, 20, 45, None) == 10                   # молчит больше месяца
    assert cs.score(0, 3, 100, 0, 2, None) < cs.score(2, 3, 100, 5, 2, None)
    assert cs.detect_lang(["Привет всем, обсуждаем маркетинг и продажи в Минске"]) == "кириллица"
    assert cs.detect_lang(["short"]) is None
    text = "Клуб предпринимателей Минска: собственники обсуждают бизнес"
    assert cs.count_matches(text, ["предприниматель", "собственник", "бизнес клуб", "маркетинг"]) == 3
    assert cs.count_matches(text, ["бизнес школа"]) == 0


def _channel(cid, title, *, megagroup=False, broadcast=False, username=None, members=None):
    return Channel(id=cid, title=title, photo=ChatPhotoEmpty(), date=None, megagroup=megagroup, broadcast=broadcast,
                   username=username, access_hash=1, participants_count=members)


class SearchClient(FakeClient):
    """Поиск отдаёт: живую группу, мёртвую группу, группу со стоп-словом и канал с группой обсуждения."""
    live = _channel(11, "Бизнес клуб Минск", megagroup=True, username="biz_minsk", members=5000)
    dead = _channel(12, "Старый чат предпринимателей", megagroup=True, username="old_chat", members=900)
    spam = _channel(13, "Крипта и бизнес", megagroup=True, username="crypto_biz", members=20000)
    chan = _channel(21, "Канал про бизнес", broadcast=True, username="biz_channel")
    discussion = _channel(22, "Обсуждение: бизнес", megagroup=True, username=None, members=300)
    nopublic = _channel(14, "Без username", megagroup=True)            # закрытая — не берём
    calls = []

    async def __call__(self, req):
        SearchClient.calls.append(type(req).__name__)
        if isinstance(req, SearchRequest):
            return types.SimpleNamespace(chats=[self.live, self.dead, self.spam, self.chan, self.nopublic])
        if isinstance(req, GetFullChannelRequest):
            ch = req.channel
            cid = getattr(ch, "channel_id", getattr(ch, "id", None))
            about = {11: "Клуб предпринимателей Минска", 13: "крипта, сигналы"}.get(cid, "")
            linked = 22 if cid == 21 else None
            return types.SimpleNamespace(full_chat=types.SimpleNamespace(about=about, participants_count=None,
                                                                         linked_chat_id=linked),
                                         chats=[self.discussion])
        raise AssertionError(req)

    async def get_messages(self, entity, limit=30):
        now = db.now_utc()
        cid = getattr(entity, "id", None)
        if cid == 12:
            return [types.SimpleNamespace(date=now - timedelta(days=90), message="давно тут никого")]
        return [types.SimpleNamespace(date=now - timedelta(hours=i * 3), message="обсуждаем бизнес и маркетинг в Минске")
                for i in range(30)]

    async def get_entity(self, x):
        return {"biz_minsk": self.live, "biz_channel": self.chan}.get(x) or (_ for _ in ()).throw(ValueError("нет"))


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(cs, "PAUSE", (0, 0))
    real = asyncio.sleep
    monkeypatch.setattr(cs.asyncio, "sleep", lambda s: real(0))


def _search(user_id, account_id, keywords=("бизнес", "предприниматель"), geo=("Минск",), stops=("крипта",)):
    return db.ex("""INSERT INTO chat_searches(name, keywords, geo, stop_words, account_id, created_by)
                    VALUES ('тест', %s, %s, %s, %s, %s) RETURNING id""", (list(keywords), list(geo), list(stops), account_id, user_id))


def test_search_finds_groups_scores_and_dedupes(fast):
    u = make_user()
    a = make_account(u["id"], client=SearchClient())
    sid = _search(u["id"], a)
    run(tgm.run_chat_search(sid, a))
    st = tgm.search_state[sid]
    assert st["step"] == "готово" and st["done"] == st["total"] == 4 and not st["running"]
    rows = {r["title"]: r for r in db.q("SELECT * FROM found_chats")}
    # группы и группа обсуждения канала; сам канал и группа без username — нет
    assert set(rows) == {"Бизнес клуб Минск", "Старый чат предпринимателей", "Крипта и бизнес", "Обсуждение: бизнес"}
    assert rows["Обсуждение: бизнес"]["via"] == "discussion"
    live, dead, spam = rows["Бизнес клуб Минск"], rows["Старый чат предпринимателей"], rows["Крипта и бизнес"]
    assert live["tg_id"] == -1000000000011 and live["msgs_per_day"] > 5 and live["lang"] == "кириллица"
    assert live["score"] > 60 and dead["score"] <= 10 and spam["score"] == 0 and spam["stop_hit"] == "крипта"
    # одна группа найдена 4 запросами — одна строка, 4 отметки
    assert db.val("SELECT COUNT(*) FROM found_chat_hits WHERE found_chat_id=%s", (live["id"],)) == 4
    assert db.one("SELECT status, step FROM chat_searches WHERE id=%s", (sid,)) == {"status": "done", "step": "найдено групп: 4"}


def test_rejected_not_rechecked_and_joined_marked(fast):
    u = make_user()
    a = make_account(u["id"], client=SearchClient())
    sid = _search(u["id"], a, keywords=("бизнес",), geo=())
    run(tgm.run_chat_search(sid, a))
    db.ex("UPDATE found_chats SET status='rejected', score=1, checked_at=now() - interval '30 days' WHERE username='old_chat'")
    db.ex("INSERT INTO tg_dialogs(account_id, peer_id, title, kind) VALUES (%s, -1000000000011, 'x', 'group')", (a,))
    run(tgm.run_chat_search(sid, a))
    assert db.one("SELECT status, score FROM found_chats WHERE username='old_chat'") == {"status": "rejected", "score": 1}
    assert db.val("SELECT status FROM found_chats WHERE username='biz_minsk'") == "joined"


def test_check_links(fast):
    u = make_user()
    a = make_account(u["id"], client=SearchClient())
    sid = _search(u["id"], a, geo=())
    run(tgm.run_chat_search(sid, a, links=["https://t.me/biz_minsk", "@biz_channel", "t.me/nobody_here", "мусор"]))
    step = tgm.search_state[sid]["step"]
    assert step.startswith("проверено ссылок: 1") and "это не группа" in step and "не похоже на ссылку" in step
    assert db.val("SELECT via FROM found_chats WHERE username='biz_minsk'") == "link"


def test_account_is_busy_during_search(fast):
    u = make_user()
    a = make_account(u["id"], client=SearchClient())
    tgm.search_state[99] = {"running": True, "account_id": a}
    assert tgm.preparing_account(a)
    from app import worker
    assert run(worker.tick(a)) == 15 and "поиск" in worker.state[a]["status"]


def test_page_and_statuses(fast):
    u = make_user("admin")
    a = make_account(u["id"], client=SearchClient())
    sid = _search(u["id"], a, keywords=("бизнес",), geo=())
    run(tgm.run_chat_search(sid, a))
    c = browser("admin")
    html = c.get("/chat-search").text
    assert "Бизнес клуб Минск" in html and "https://t.me/biz_minsk" in html and "https://web.telegram.org/a/#-1000000000011" in html
    fid = db.val("SELECT id FROM found_chats WHERE username='biz_minsk'")
    c.post(f"/chat-search/found/{fid}/rejected")
    assert "Бизнес клуб Минск" not in c.get("/chat-search").text               # «в работе» без отклонённых
    assert "Бизнес клуб Минск" in c.get("/chat-search?status=rejected").text
    for url in ["/chat-search?status=all&sort=members", f"/chat-search?search={sid}&min_score=50", "/chat-search/state"]:
        assert c.get(url).status_code == 200
    # чужой аккаунт для поиска использовать нельзя
    make_user("anna", "manager")
    browser("anna").post("/chat-search/create", data={"keywords": "x", "account_id": a})
    assert db.val("SELECT COUNT(*) FROM chat_searches") == 1
