"""Отправка: шаблон, ошибки получателей, лимиты, FloodWait/PEER_FLOOD — у каждого аккаунта свои."""
import random
from datetime import timedelta

from telethon import errors

from app import campaigns, db, worker
from app.templating import render, variables_in
from tests.conftest import FakeClient, add_rows, cl_state, drain, make_account, make_campaign, make_lead, make_user


def test_render():
    r = render("{Привет|Здравствуйте}, {first_name}! {Как {дела|жизнь}?|Рад видеть}", {"first_name": "Аня"}, random.Random(1))
    assert "Аня" in r and "{" not in r
    assert render("{Привет, {first_name}|Hi {first_name}}", {"first_name": "Ян"}).endswith("Ян")
    assert render("Привет, {first_name}!", {}) == "Привет!"
    assert variables_in("{a|b} {first_name} {company}") == ["company", "first_name"]


def test_warmup_limit():
    u = make_user()
    aid = make_account(u["id"])
    acc = worker.account(aid)
    assert worker.daily_limit(acc) == 10
    db.ex("UPDATE tg_accounts SET warmup_start_date=%s WHERE id=%s", (db.today() - timedelta(days=3), aid))
    assert worker.daily_limit(worker.account(aid)) == 25
    db.ex("UPDATE tg_accounts SET warmup_start_date=%s WHERE id=%s", (db.today() - timedelta(days=30), aid))
    assert worker.daily_limit(worker.account(aid)) == 50
    db.ex("UPDATE tg_accounts SET warmup_enabled=false WHERE id=%s", (aid,))
    assert worker.daily_limit(worker.account(aid)) == 50


def test_send_flow():
    u = make_user()
    client = FakeClient(known_usernames={"real_user": 555})
    aid = make_account(u["id"], client=client)
    ok = make_lead(tg_id=101, first_name="Аня")
    priv = make_lead(tg_id=102, first_name="Боб")
    ghost = make_lead(username="ghost", first_name="Призрак")
    out = make_lead(tg_id=103, first_name="Вера", opted_out_at=db.now_utc())
    by_name = make_lead(username="real_user", first_name="Гена")
    client.fail[102] = errors.UserPrivacyRestrictedError(request=None)
    cid = make_campaign(u["id"], [aid], body="{Привет|Хай}, {first_name}!")
    rows = add_rows(cid, [ok, priv, ghost, out, by_name])
    queued, skipped = campaigns.enqueue(cid, {"state": ["new"]})
    assert (queued, skipped) == (4, 1)                     # отписавшийся не попал в очередь
    drain(aid, 8)
    st = [cl_state(r)["state"] for r in rows]
    assert st == ["sent", "failed", "failed", "skipped", "sent"]
    assert "получатель не найден" in cl_state(rows[2])["error"]
    assert [t for _, t, _ in client.sent][0].endswith("Аня!")
    worker.finish_campaigns()
    assert db.val("SELECT status FROM campaigns WHERE id=%s", (cid,)) == "done"
    acc = worker.account(aid)
    assert acc["warmup_start_date"] == db.today()
    assert worker.sent_today(aid) == 2
    # адресат по username получил tg_id — теперь ловим его ответы и прочтения
    assert db.val("SELECT tg_id FROM leads WHERE id=%s", (by_name,)) == 555
    # переписка сохранена, лиды закреплены за аккаунтом
    assert db.val("SELECT COUNT(*) FROM messages WHERE direction='out' AND account_id=%s", (aid,)) == 2
    assert db.val("SELECT owner_account_id FROM leads WHERE id=%s", (ok,)) == aid


def test_flood_pauses_only_that_account():
    u = make_user()
    ca, cb = FakeClient(), FakeClient()
    a = make_account(u["id"], "A", client=ca)
    b = make_account(u["id"], "B", client=cb)
    la, lb = make_lead(tg_id=201, owner_account_id=a), make_lead(tg_id=202, owner_account_id=b)
    cid = make_campaign(u["id"], [a, b])
    ra, rb = add_rows(cid, [la, lb])
    campaigns.enqueue(cid, {"state": ["new"]})
    ca.fail[201] = errors.FloodWaitError(request=None, capture=120)
    drain(a, 1)
    drain(b, 1)
    assert worker.paused_until(worker.account(a)) is not None
    assert cl_state(ra)["state"] == "queued" and cl_state(ra)["account_id"] == a   # остался за A
    assert cl_state(rb)["state"] == "sent"                                          # B не затронут
    assert worker.paused_until(worker.account(b)) is None
    assert "пауза" in worker.state[a]["status"]


def test_peer_flood_pauses_account_for_a_day():
    u = make_user()
    c = FakeClient()
    a = make_account(u["id"], client=c)
    lid = make_lead(tg_id=301)
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [lid])
    campaigns.enqueue(cid, {"state": ["new"]})
    c.fail[301] = errors.PeerFloodError(request=None)
    drain(a, 1)
    acc = worker.account(a)
    assert acc["pause_reason"] == "PEER_FLOOD"
    assert acc["paused_until"] - db.now_utc() > timedelta(hours=23)
    assert cl_state(r)["state"] == "queued"
    assert db.val("SELECT status FROM campaigns WHERE id=%s", (cid,)) == "running"


def test_daily_limit_and_work_hours():
    u = make_user()
    a = make_account(u["id"], warmup_start=1, warmup_step=0)
    cid = make_campaign(u["id"], [a])
    add_rows(cid, [make_lead(tg_id=400 + i) for i in range(3)])
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 3)
    assert worker.sent_today(a) == 1
    assert "лимит" in worker.state[a]["status"]
    db.ex("UPDATE tg_accounts SET warmup_enabled=false, work_start='03:00', work_end='03:01' WHERE id=%s", (a,))
    drain(a, 1)
    assert "вне рабочих часов" in worker.state[a]["status"]


def test_paused_account_does_not_send():
    u = make_user()
    a = make_account(u["id"])
    cid = make_campaign(u["id"], [a])
    add_rows(cid, [make_lead(tg_id=501)])
    campaigns.enqueue(cid, {"state": ["new"]})
    db.ex("UPDATE tg_accounts SET status='paused' WHERE id=%s", (a,))
    drain(a, 2)
    assert worker.sent_today(a) == 0 and "на паузе" in worker.state[a]["status"]


def test_forum_topic_reply_to():
    u = make_user()
    c = FakeClient()
    a = make_account(u["id"], client=c)
    chat = make_lead(kind="chat", tg_id=-1001234567890, title="Форум")
    cid = make_campaign(u["id"], [a])
    add_rows(cid, [chat], custom_text="Пост в тему", topic_id=29438)
    chat2 = make_lead(kind="chat", tg_id=-1009999999999, title="Общий")
    add_rows(cid, [chat2], custom_text="В General", topic_id=1)
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 3)
    assert sorted(r for _, _, r in c.sent if r) == [29438]
    assert len(c.sent) == 2


def test_work_hours_edges():
    """Через полночь, круглосуточно и последняя минута суток (раньше 00:00–23:59 «не работал» в 23:59)."""
    from datetime import datetime, time
    tz = db.now_local().tzinfo

    def at(h, m, s=0):
        return datetime(2026, 10, 5, h, m, s, tzinfo=tz)
    acc = {"work_start": time(10, 0), "work_end": time(20, 0), "tz": None}
    assert worker.in_work_hours(acc, at(10, 0)) and not worker.in_work_hours(acc, at(20, 0))
    night = {"work_start": time(22, 0), "work_end": time(6, 0), "tz": None}
    assert worker.in_work_hours(night, at(23, 30)) and worker.in_work_hours(night, at(5, 59))
    assert not worker.in_work_hours(night, at(12, 0))
    always = {"work_start": time(0, 0), "work_end": time(0, 0), "tz": None}
    assert all(worker.in_work_hours(always, at(h, m, s)) for h, m, s in [(0, 0, 0), (12, 0, 0), (23, 59, 30)])
