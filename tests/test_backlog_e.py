"""Группа E бэклога: один ответ на дописанное, повторы при сбоях, «нужен человек» держит ИИ в стороне,
нет двойных первых сообщений, дожимы не мешают живой переписке, отписка по смыслу, мелочи автопилота."""
from datetime import timedelta

from app import ai, autopilot, campaigns, db, delivery, inbox
from app.tg import tgm
from tests import test_autopilot as ta
from tests.conftest import FakeClient, add_rows, drain, make_account, make_campaign, make_lead, make_user, run
from tests.test_autopilot import incoming, job, make_due, send_due, setup

fake = ta.fake


def ai_sent(lid):
    return db.q("SELECT text FROM messages WHERE lead_id=%s AND source='ai' ORDER BY id", (lid,))


def interrupting_chat(monkeypatch, a, lid, times):
    """Поддельный ИИ: пока «пишет» ответ, клиент дописывает (первые times раз)."""
    real, n = ai.chat, {"i": 0}

    async def chat(messages, **kw):
        if not messages[0]["content"].startswith("Определи") and n["i"] < times:
            n["i"] += 1
            inbox.record(a, lid, "in", f"и ещё {n['i']}", 100 + n["i"], "incoming")
        return await real(messages, **kw)
    monkeypatch.setattr(ai, "chat", chat)


# ---------- E1 ----------
def test_e1_client_wrote_during_reply_one_answer(request, monkeypatch):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Сколько стоит лендинг?", 10)
    interrupting_chat(monkeypatch, a, lid, 1)
    make_due(lid)
    send_due()
    j = job(lid)
    assert j["status"] == "pending" and j["restarts"] == 1 and ai_sent(lid) == []     # черновик выброшен
    make_due(lid)
    send_due()
    assert job(lid)["status"] == "sent" and len(ai_sent(lid)) == 1                     # один ответ на всё
    assert db.val("SELECT COUNT(*) FROM ai_reply_jobs WHERE lead_id=%s", (lid,)) == 1


def test_e1_chatty_client_still_gets_answer(request, monkeypatch):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Привет", 10)
    interrupting_chat(monkeypatch, a, lid, 99)          # дописывает каждый раз
    for _ in range(autopilot.MAX_RESTARTS + 1):
        make_due(lid)
        send_due()
    assert len(ai_sent(lid)) == 1                        # после MAX_RESTARTS ответ всё-таки ушёл
    assert job(lid)["status"] == "pending"               # а остаток войдёт в следующий


def test_e1_typing_stops_when_client_writes(request, monkeypatch):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Сколько стоит лендинг?", 10)
    monkeypatch.setattr(autopilot, "typing_seconds", lambda text, rng=None: 5)
    monkeypatch.setattr(delivery, "TYPING_CHECK", 0.01)
    client = tgm.get(a).client

    class Act:
        async def __aenter__(self):
            inbox.record(a, lid, "in", "а сроки?", 11, "incoming")      # клиент пишет, пока мы «печатаем»

        async def __aexit__(self, *exc):
            return False
    monkeypatch.setattr(client, "action", lambda entity, act: Act())
    make_due(lid)
    send_due()                                           # не ждём 5 секунд «печатает» — прерываемся сразу
    assert job(lid)["status"] == "pending" and job(lid)["restarts"] == 1 and ai_sent(lid) == []


def test_e1_waiting_is_capped(request):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Привет", 10)
    db.ex("UPDATE ai_reply_jobs SET created_at=now() - interval '25 minutes', due_at=now() + interval '5 seconds'")
    due = job(lid)["due_at"]
    incoming(a, "ещё слово", 11)
    assert job(lid)["due_at"] == due                     # клиент пишет давно — дальше не откладываем


# ---------- E2 ----------
def test_e2_ai_down_retries_then_hands_off(request, monkeypatch):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Вопрос", 10)

    async def down(messages, **kw):
        raise ai.AIError("ИИ перегружен")
    monkeypatch.setattr(ai, "chat", down)
    for i in range(len(autopilot.AI_RETRY)):
        make_due(lid)
        send_due()
        assert job(lid)["status"] == "pending" and job(lid)["ai_attempts"] == i + 1
        assert not db.val("SELECT ai_handoff FROM leads WHERE id=%s", (lid,))
    make_due(lid)
    send_due()
    assert job(lid)["status"] == "handoff" and "ИИ недоступен" in db.val("SELECT ai_handoff FROM leads WHERE id=%s", (lid,))


def test_e2_telegram_failure_hands_off_after_an_hour(request):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Вопрос", 10)
    tgm.get(a).client.fail[500] = ConnectionError("нет сети")
    make_due(lid)
    send_due()
    assert job(lid)["status"] == "pending" and job(lid)["fail_since"]
    db.ex("UPDATE ai_reply_jobs SET fail_since=now() - interval '2 hours'")
    make_due(lid)
    send_due()
    assert job(lid)["status"] == "handoff" and "не ушёл за час" in db.val("SELECT ai_handoff FROM leads WHERE id=%s", (lid,))


def test_e2_night_is_not_a_failure(request):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    incoming(a, "Вопрос", 10)
    db.ex("UPDATE ai_reply_jobs SET fail_since=now() - interval '3 hours'")
    db.ex("UPDATE tg_accounts SET work_start=%s, work_end=%s", ("03:00", "03:01"))
    make_due(lid)
    send_due()
    assert job(lid)["status"] == "pending" and job(lid)["fail_since"] is None


# ---------- E3 ----------
def test_e3_no_autoreply_while_waiting_for_human(request):
    st = request.getfixturevalue("fake")
    u, a, lid = setup()
    st["answer"] = "HANDOFF: просит скидку"
    incoming(a, "Дайте скидку", 10)
    make_due(lid)
    send_due()
    assert db.val("SELECT ai_handoff FROM leads WHERE id=%s", (lid,))
    incoming(a, "Ну так что?", 11)
    assert job(lid)["status"] == "handoff"               # нового задания нет — ждём человека


# ---------- E4 ----------
def test_e4_two_campaigns_one_first_message():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    lid = make_lead(tg_id=77)
    c1, c2 = make_campaign(u["id"], [a]), make_campaign(u["id"], [a])
    add_rows(c1, [lid])
    r2, = add_rows(c2, [lid])
    assert campaigns.enqueue(c1, {"state": ["new"]}) == (1, 0)
    assert campaigns.enqueue(c2, {"state": ["new"]}) == (0, 1)
    assert "уже в очереди" in db.val("SELECT error FROM campaign_leads WHERE id=%s", (r2,))
    drain(a, 4)
    assert len(client.sent) == 1


def test_e4_recontact_rechecked_at_send_time():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    lid = make_lead(tg_id=77, owner_account_id=None)
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [lid])
    campaigns.enqueue(cid, {"state": ["new"]})
    inbox.record(a, lid, "out", "Написал лично, пока очередь стояла", 5, "telegram")
    drain(a, 2)
    assert client.sent == [] and "уже писали" in db.val("SELECT error FROM campaign_leads WHERE id=%s", (r,))


# ---------- E5 ----------
def test_e5_followup_stops_after_manager_wrote():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    lid = make_lead(tg_id=88)
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [lid])
    db.ex("INSERT INTO campaign_steps(campaign_id, position, body, delay_days, condition) VALUES (%s, 2, 'Напоминаю', 1, 'no_reply')", (cid,))
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)
    inbox.record(a, lid, "out", "Привет, это я лично", 999, "telegram")
    db.ex("UPDATE campaign_leads SET next_step_at=now() - interval '1 minute'")
    drain(a, 2)
    assert [t for _, t, _ in client.sent] == ["Привет!"]
    assert "писали отдельно" in db.val("SELECT chain_note FROM campaign_leads WHERE id=%s", (r,))


# ---------- E6 ----------
def test_e6_ai_detected_stop_opts_out(request):
    st = request.getfixturevalue("fake")
    u, a, lid = setup(autoreply=False)                   # даже без автоответов в кампании
    st["label"] = "stop"
    incoming(a, "Больше мне сюда не присылайте ничего", 10)
    assert db.val("SELECT opt_out_reason FROM leads WHERE id=%s", (lid,)) == "ИИ: просит не писать"


# ---------- E7 ----------
def test_e7_old_message_goes_to_human(request):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    old = db.now_utc() - timedelta(hours=8)
    run(tgm.get(a).handle_incoming(500, "Вопрос", 10, date=old))
    mid = db.val("SELECT id FROM messages WHERE tg_message_id=10 AND direction='in'")
    run(autopilot.after_incoming(mid, lid, a, sent_at=old))
    assert (job(lid) is None or job(lid)["status"] == "handoff") and "8 ч назад" in db.val("SELECT ai_handoff FROM leads WHERE id=%s", (lid,))


def test_e7_account_daily_cap(request):
    request.getfixturevalue("fake")
    u, a, lid = setup()
    db.set_setting("ai_autopilot_account_daily", "2")
    other = make_lead(tg_id=901)
    for i in range(2):
        inbox.record(a, other, "out", f"ответ {i}", 300 + i, "ai")
    incoming(a, "Вопрос", 10)
    make_due(lid)
    send_due()
    assert job(lid)["status"] == "handoff" and "лимит автоответов аккаунта" in db.val("SELECT ai_handoff FROM leads WHERE id=%s", (lid,))
