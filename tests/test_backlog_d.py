"""Группа D бэклога: единая доставка, уведомления админу, расход ИИ, меню на телефоне."""
import asyncio

import pytest
from telethon import errors

from app import ai, db, delivery, notify
from app.tg import tgm
from tests.conftest import FakeClient, make_account, make_lead, make_user, run
from tests.test_inbox import _conversation
from tests.test_web import browser


# ---------- D1: доставка ----------
def test_d1_error_kind():
    assert delivery.error_kind(errors.FloodWaitError(None, capture=5)) == "flood"
    assert delivery.error_kind(ConnectionError()) == "transient"
    assert delivery.error_kind(errors.UserPrivacyRestrictedError(None)) == "recipient"
    assert delivery.error_kind(ValueError("нет такого")) == "not_found"
    assert delivery.error_kind(RuntimeError()) == "other"
    assert delivery.describe(ValueError("x")).startswith("получатель не найден")


def test_d1_skip_before_send_sends_nothing():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    lead = db.one("SELECT * FROM leads WHERE id=%s", (make_lead(tg_id=42),))

    def stop():
        raise delivery.Skip("менеджер ответил сам")

    with pytest.raises(delivery.Skip):
        run(delivery.deliver(tgm.get(a), lead, "привет", typing=0.001, check_before_send=stop))
    assert client.sent == [] and client.actions == [(42, "typing")]


def test_d1_inbox_reply_respects_stoplist():
    u = make_user()
    client = FakeClient()
    a, lid = _conversation(u, client)
    db.ex("INSERT INTO do_not_contact(tg_id) VALUES (500)")
    assert "отписался" in run(delivery.send_reply(lid, "ещё раз привет", u["id"]))
    assert len(client.sent) == 1 and a


def test_d1_inbox_reply_unexpected_error_is_reported_not_raised():
    u = make_user()
    client = FakeClient()
    a, lid = _conversation(u, client)
    client.fail[500] = RuntimeError("сломалось")
    err = run(delivery.send_reply(lid, "ответ", u["id"]))
    assert err.startswith("Не отправлено") and "сломалось" in err
    assert db.one("SELECT 1 FROM event_log WHERE level='error' AND text LIKE %s", ("%сломалось%",))


# ---------- D3: уведомления ----------
def _flush():
    async def go(*fns):
        for f in fns:
            f()
        await asyncio.sleep(0.05)
    return go


def test_d3_notify_goes_to_saved_messages_once():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    assert run(_flush()(lambda: notify.admin("не настроено"))) is None and client.sent == []
    db.set_setting("notify_account_id", str(a))
    run(_flush()(lambda: notify.admin("PEER_FLOOD", key="k"), lambda: notify.admin("PEER_FLOOD", key="k")))
    assert [(to, t.splitlines()[-1]) for to, t, _ in client.sent] == [("me", "PEER_FLOOD")]


def test_d3_handoffs_threshold():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    db.set_setting("notify_account_id", str(a))
    for i in range(5):
        make_lead(tg_id=100 + i, ai_handoff="вопрос о цене", ai_handoff_at=db.now_utc())
    run(_flush()(notify.check_handoffs))
    assert len(client.sent) == 1 and "5 диалогов" in client.sent[0][1]


def test_d3_notify_test_route():
    make_user("admin")
    c = browser("admin")
    assert "Не отправлено" in c.post("/settings/notify-test").text      # аккаунт не выбран — понятная ошибка


# ---------- D4: расход ИИ ----------
def test_d4_usage_on_ai_page(monkeypatch):
    make_user("admin")
    monkeypatch.setattr(ai, "configured", lambda: True)

    def fake_post(path, body, timeout=60):
        if path.startswith("/ai/usage"):
            return {"data": {"totals": {"calls": 12, "totalTokens": 34567, "cost": None},
                             "byModel": [{"modelId": ai.AI_MODEL, "calls": 12, "totalTokens": 34567}]}}
        return {"data": [{"id": ai.AI_MODEL}]}
    monkeypatch.setattr(ai, "_post", fake_post)
    assert run(ai.usage(7))["totals"]["calls"] == 12
    html = browser("admin").get("/ai").text
    assert "Расход ИИ" in html and "34 567" in html and "0 / 0" in html   # нет разбивки по токенам — не падаем


def test_d4_usage_unavailable(monkeypatch):
    monkeypatch.setattr(ai, "configured", lambda: True)

    def broken(*a, **k):
        raise ai.AIError("нет связи")
    monkeypatch.setattr(ai, "_post", broken)
    assert run(ai.usage(30)) is None


# ---------- D5: меню ----------
def test_d5_burger_menu_rendered():
    make_user("admin")
    html = browser("admin").get("/").text
    assert 'class="nav-toggle"' in html and "nav-burger" in html and "nav-menu" in html
