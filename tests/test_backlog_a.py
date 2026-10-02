"""Группа A бэклога: ответы не теряются, ошибки фоновых задач видны, сверка переписки, одна рабочая копия."""
import asyncio
import types

from app import ai, autopilot, db, inbox, tasks
from app.tg import tgm
from tests.conftest import URL, run
from tests import test_autopilot as ta
from tests.test_autopilot import incoming, job, make_due, send_due, setup

fake = ta.fake          # фикстура «поддельный ИИ» из тестов автопилота


def test_a1_message_during_typing_gets_its_own_reply(request, monkeypatch):
    """Перезапуски исчерпаны (E1) — ответ уходит, а на дописанное планируется следующий."""
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Сколько стоит лендинг?", 10)
    db.ex("UPDATE ai_reply_jobs SET restarts=%s", (autopilot.MAX_RESTARTS,))
    real = ai.chat

    async def chat_with_interrupt(messages, **kw):
        # пока ИИ пишет ответ, клиент дописывает
        if not messages[0]["content"].startswith("Определи"):
            inbox.record(a, lid, "in", "И сколько по времени?", 11, "incoming")
        return await real(messages, **kw)
    monkeypatch.setattr(ai, "chat", chat_with_interrupt)
    make_due(lid)
    send_due()
    jobs = db.q("SELECT status FROM ai_reply_jobs WHERE lead_id=%s ORDER BY id", (lid,))
    assert [j["status"] for j in jobs] == ["sent", "pending"]           # на дописанное — свой ответ


def test_a2_unexpected_error_hands_off(request, monkeypatch):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Вопрос", 10)
    monkeypatch.setattr(ai, "build_messages", lambda *a, **k: (_ for _ in ()).throw(KeyError("сломалось")))
    make_due(lid)
    send_due()
    assert job(lid)["status"] == "failed" and "сбой автоответа" in db.val("SELECT ai_handoff FROM leads WHERE id=%s", (lid,))


def test_a2_background_errors_are_logged():
    async def boom():
        raise RuntimeError("тест")
    seen = []

    async def go():
        tasks.spawn(boom(), "проверка", on_error=seen.append)
        await asyncio.sleep(0.01)
    run(go())
    assert db.val("SELECT COUNT(*) FROM event_log WHERE text LIKE %s", ("Фоновая задача «проверка» упала: RuntimeError%",)) == 1
    assert isinstance(seen[0], RuntimeError)


def test_a3_sync_recent_adds_missed_messages(request):
    request.getfixturevalue("fake")
    u, a, lid = setup()                                 # переписка: наше первое сообщение (tg id 1)
    acc = tgm.get(a)
    missed = [types.SimpleNamespace(id=5, message="Написал руками, пока панель лежала", out=True),
              types.SimpleNamespace(id=4, message="Ответ клиента, пока панель лежала", out=False)]   # новые сверху

    async def iter_dialogs(limit=None):
        yield types.SimpleNamespace(is_user=True, id=500, message=types.SimpleNamespace(id=5), entity=500)
        yield types.SimpleNamespace(is_user=True, id=999, message=types.SimpleNamespace(id=9), entity=999)  # не лид

    async def get_messages(entity, min_id=0, limit=30):
        return [m for m in missed if m.id > min_id]
    acc.client.iter_dialogs = iter_dialogs
    acc.client.get_messages = get_messages

    async def go():
        n = await acc.sync_recent()
        await asyncio.sleep(0.01)
        return n
    assert run(go()) == 2
    rows = db.q("SELECT direction, text, source FROM messages WHERE lead_id=%s ORDER BY tg_message_id", (lid,))
    assert [(r["direction"], r["source"]) for r in rows] == [("out", "campaign"), ("in", "incoming"), ("out", "telegram")]
    assert db.val("SELECT state FROM campaign_leads WHERE lead_id=%s", (lid,)) == "replied"
    assert run(acc.sync_recent()) == 0                  # повторная сверка ничего не дублирует


def test_a5_only_one_working_copy():
    assert db.acquire_leader(URL)
    first = db._leader_conn
    db._leader_conn = None
    try:
        assert db.acquire_leader(URL) is False          # «вторая копия» не получает Telegram и отправку
    finally:
        db._leader_conn = first
        db.release_leader()
    assert db.acquire_leader(URL)                        # после остановки первой — можно
    db.release_leader()


def test_a6_index_exists():
    assert db.val("SELECT COUNT(*) FROM pg_indexes WHERE indexname='ix_messages_account_out'") == 1
