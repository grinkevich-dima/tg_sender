"""Дожимы: расписание, условия, остановка при ответе/этапе/отписке, лимит, приоритет, правка цепочки."""
from datetime import timedelta

from telethon import errors

from app import campaigns, db, inbox, leads, worker
from app.tg import tgm
from tests.conftest import FakeClient, add_rows, cl_state, drain, make_account, make_campaign, make_lead, make_user, run
from tests.test_web import browser


def add_step(cid, pos, body, days=3, cond="no_reply"):
    return db.ex("""INSERT INTO campaign_steps(campaign_id, position, body, delay_days, condition)
                    VALUES (%s,%s,%s,%s,%s) RETURNING id""", (cid, pos, body, days, cond))


def make_due(rid):
    db.ex("UPDATE campaign_leads SET next_step_at=now() - interval '1 minute' WHERE id=%s", (rid,))


def row(rid):
    return db.one("SELECT state, step, next_step_at, chain_note FROM campaign_leads WHERE id=%s", (rid,))


def setup_chain(client=None, steps=((3, "no_reply"),), tg_id=100):
    u = make_user(f"user{tg_id}")
    client = client or FakeClient()
    a = make_account(u["id"], client=client)
    cid = make_campaign(u["id"], [a], body="Привет, {first_name}!")
    for i, (days, cond) in enumerate(steps, start=2):
        add_step(cid, i, f"Дожим {i}, {{first_name}}", days, cond)
    rid, = add_rows(cid, [make_lead(tg_id=tg_id, first_name="Ира")])
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)
    return u, a, cid, rid, client


def test_followup_scheduled_and_sent():
    u, a, cid, rid, client = setup_chain(steps=((3, "no_reply"), (5, "no_reply")))
    r = row(rid)
    assert r["step"] == 1 and abs((r["next_step_at"] - db.now_utc()) - timedelta(days=3)) < timedelta(minutes=1)
    drain(a, 1)
    assert len(client.sent) == 1                                  # ещё рано
    make_due(rid)
    drain(a, 1)
    assert [t for _, t, _ in client.sent] == ["Привет, Ира!", "Дожим 2, Ира"]
    r = row(rid)
    assert r["step"] == 2 and abs((r["next_step_at"] - db.now_utc()) - timedelta(days=5)) < timedelta(minutes=1)
    assert db.val("SELECT step FROM messages WHERE text='Дожим 2, Ира'") == 2
    make_due(rid)
    drain(a, 1)
    assert row(rid)["step"] == 3 and row(rid)["next_step_at"] is None   # цепочка закончилась
    worker.finish_campaigns()
    assert db.val("SELECT status FROM campaigns WHERE id=%s", (cid,)) == "done"


def test_campaign_not_done_while_followup_pending():
    u, a, cid, rid, client = setup_chain()
    worker.finish_campaigns()
    assert db.val("SELECT status FROM campaigns WHERE id=%s", (cid,)) == "running"


def test_reply_and_optout_stop_chain():
    u, a, cid, rid, client = setup_chain()
    run(tgm.get(a).handle_incoming(100, "Спасибо!", 50))
    r = row(rid)
    assert r["state"] == "replied" and r["next_step_at"] is None and "ответил" in r["chain_note"]
    u2, a2, cid2, rid2, _ = setup_chain(tg_id=101)
    leads.opt_out(db.val("SELECT lead_id FROM campaign_leads WHERE id=%s", (rid2,)), "тест")
    assert row(rid2)["next_step_at"] is None and "отписался" in row(rid2)["chain_note"]


def test_manual_stage_stops_chain():
    u, a, cid, rid, client = setup_chain()
    lid = db.val("SELECT lead_id FROM campaign_leads WHERE id=%s", (rid,))
    inbox.set_stage(lid, db.val("SELECT id FROM funnel_stages WHERE name='записался'"))
    make_due(rid)
    drain(a, 1)
    assert len(client.sent) == 1 and "этап «записался»" in row(rid)["chain_note"]


def test_conditions_skip_step():
    # «прочитал, но не ответил» — а человек не прочитал: шаг пропущен, следующий запланирован
    u, a, cid, rid, client = setup_chain(steps=((2, "read_no_reply"), (4, "unread")))
    make_due(rid)
    drain(a, 1)
    r = row(rid)
    assert len(client.sent) == 1 and r["step"] == 2 and "пропущен: не прочитал" in r["chain_note"] and r["next_step_at"]
    make_due(rid)
    drain(a, 1)
    assert [t for _, t, _ in client.sent][-1] == "Дожим 3, Ира"        # «не прочитал» — отправлен
    # «не прочитал» у прочитавшего — пропуск
    u2, a2, cid2, rid2, client2 = setup_chain(steps=((1, "unread"),), tg_id=102)
    tgm.get(a2)._mark_read(102, 10**6)
    make_due(rid2)
    drain(a2, 1)
    assert len(client2.sent) == 1 and "уже прочитал" in row(rid2)["chain_note"]


def test_followups_count_in_limit_and_go_first():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client, warmup_start=2, warmup_step=0)
    cid = make_campaign(u["id"], [a])
    add_step(cid, 2, "Дожим")
    rows = add_rows(cid, [make_lead(tg_id=200 + i, first_name=f"Л{i}") for i in range(3)])
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)                                       # первое сообщение Л0
    make_due(rows[0])
    drain(a, 3)                                       # лимит 2: дожим Л0 раньше первого сообщения Л1
    assert [t for _, t, _ in client.sent] == ["Привет, Л0!", "Дожим"]
    assert worker.sent_today(a) == 2 and "лимит" in worker.state[a]["status"]
    assert cl_state(rows[1])["state"] == "queued"


def test_flood_on_followup_keeps_it_scheduled():
    u, a, cid, rid, client = setup_chain()
    make_due(rid)
    due = row(rid)["next_step_at"]
    client.fail[100] = errors.FloodWaitError(request=None, capture=30)
    drain(a, 1)
    assert row(rid)["next_step_at"] == due and worker.paused_until(worker.account(a))


def test_added_step_scheduled_for_already_sent():
    u, a, cid, rid, client = setup_chain(steps=())
    assert row(rid)["next_step_at"] is None
    add_step(cid, 2, "Новый дожим", 2)
    assert campaigns.reschedule(cid) == 1
    assert abs((row(rid)["next_step_at"] - db.now_utc()) - timedelta(days=2)) < timedelta(minutes=1)


def test_xlsx_first_text_then_template_followup():
    u = make_user()
    client = FakeClient()
    a = make_account(u["id"], client=client)
    cid = db.ex("INSERT INTO campaigns(name, source, created_by, status) VALUES ('x', 'xlsx', %s, 'running') RETURNING id", (u["id"],))
    campaigns.set_accounts(cid, [a])
    add_step(cid, 2, "{first_name}, напоминаю про запись")
    rid, = add_rows(cid, [make_lead(tg_id=300, first_name="Олег")], custom_text="Свой текст из файла")
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)
    make_due(rid)
    drain(a, 1)
    assert [t for _, t, _ in client.sent] == ["Свой текст из файла", "Олег, напоминаю про запись"]


def test_chain_editing_via_panel():
    u = make_user("admin")
    a = make_account(u["id"])
    cid = make_campaign(u["id"], [a])
    c = browser("admin")
    for bad in [{"body": "", "delay_days": "3"}, {"body": "x", "delay_days": "0"}, {"body": "x", "delay_days": "3", "condition": "??"}]:
        c.post(f"/campaigns/{cid}/steps/add", data=bad)
    assert campaigns.followups(cid) == []
    for i in range(campaigns.MAX_FOLLOWUPS + 1):
        c.post(f"/campaigns/{cid}/steps/add", data={"body": f"дожим {i}", "delay_days": "3", "condition": "read_no_reply"})
    steps = campaigns.followups(cid)
    assert [s["position"] for s in steps] == [2, 3, 4]                  # больше максимума не добавить
    c.post(f"/campaigns/{cid}/steps/{steps[0]['id']}", data={"body": "новый текст", "delay_days": "7", "condition": "no_reply"})
    assert db.one("SELECT body, delay_days, condition FROM campaign_steps WHERE id=%s", (steps[0]["id"],)) == \
        {"body": "новый текст", "delay_days": 7, "condition": "no_reply"}
    c.post(f"/campaigns/{cid}/steps/{steps[1]['id']}/delete")
    assert len(campaigns.followups(cid)) == 2
    html = c.get(f"/campaigns/{cid}").text
    assert "Цепочка" in html and "новый текст" in html and "Дожим 1" in html
