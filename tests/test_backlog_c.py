"""Группа C бэклога: чистка журнала, полное удаление человека и стоп-лист, кэш счётчика, часовой пояс аккаунта."""
from datetime import datetime, timezone

from app import campaigns, db, inbox, leads, worker
from app.tg import tgm
from tests.conftest import add_rows, drain, make_account, make_campaign, make_lead, make_user, run
from tests.test_inbox import _conversation
from tests.test_web import browser


def test_c1_cleanup_old_log():
    db.ex("INSERT INTO event_log(ts, level, text) VALUES (now() - interval '200 days', 'info', 'старое'), (now(), 'info', 'свежее')")
    db.set_setting("log_keep_days", "90")
    worker.cleanup()
    texts = [r["text"] for r in db.q("SELECT text FROM event_log")]
    assert "старое" not in texts and "свежее" in texts


def test_c2_purge_removes_everything_and_keeps_stoplist():
    u = make_user("admin")
    a, lid = _conversation(u)
    run(tgm.get(a).handle_incoming(500, "Удалите мои данные", 10))
    sid = db.ex("INSERT INTO segments(name) VALUES ('s') RETURNING id")
    db.ex("INSERT INTO segment_leads(segment_id, lead_id) VALUES (%s, %s)", (sid, lid))
    db.ex("INSERT INTO ai_examples(question, answer, lead_id, source) VALUES ('его слова', 'наш ответ', %s, 'inbox')", (lid,))
    c = browser("admin")
    c.post(f"/leads/{lid}/purge", data={"confirm": "нет"})
    assert db.one("SELECT 1 FROM leads WHERE id=%s", (lid,))                  # без подтверждения — ничего
    c.post(f"/leads/{lid}/purge", data={"confirm": "удалить", "keep_stoplist": "1"})
    for table, col in [("leads", "id"), ("messages", "lead_id"), ("campaign_leads", "lead_id"),
                       ("segment_leads", "lead_id"), ("ai_examples", "lead_id")]:
        assert db.val(f"SELECT COUNT(*) FROM {table} WHERE {col}=%s", (lid,)) == 0, table
    assert leads.in_stoplist(500)
    # повторный импорт того же человека — сразу «не писать», в кампанию не попадает
    new_id, _ = leads.upsert_lead({"tg_id": 500, "first_name": "Ира"})
    assert db.val("SELECT opted_out_at IS NOT NULL FROM leads WHERE id=%s", (new_id,))
    cid = make_campaign(u["id"], [a])
    add_rows(cid, [new_id])
    assert campaigns.enqueue(cid, {"state": ["new"]}) == (0, 1)


def test_c2_stoplist_blocks_username_only_lead():
    u = make_user()
    from tests.conftest import FakeClient
    client = FakeClient(known_usernames={"ira": 700})
    a = make_account(u["id"], client=client)
    db.ex("INSERT INTO do_not_contact(tg_id) VALUES (700)")
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [make_lead(username="ira")])
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)
    assert client.sent == [] and db.val("SELECT state FROM campaign_leads WHERE id=%s", (r,)) == "skipped"


def test_c2_only_admin_purges():
    admin = make_user("admin")
    make_user("anna", "manager")
    lid = make_lead(tg_id=1)
    browser("anna").post(f"/leads/{lid}/purge", data={"confirm": "удалить"})
    assert db.one("SELECT 1 FROM leads WHERE id=%s", (lid,)) and admin


def test_c3_unread_cache_invalidated():
    u = make_user()
    a, lid = _conversation(u)
    assert inbox.unread_count(None) == 0
    run(tgm.get(a).handle_incoming(500, "Новый ответ", 10))
    assert inbox.unread_count(None) == 1                                     # новое входящее сбрасывает кэш
    inbox.mark_read(lid)
    assert inbox.unread_count(None) == 0


def test_c4_account_timezone():
    u = make_user("admin")
    a = make_account(u["id"], work_start="09:00", work_end="18:00")
    acc = worker.account(a)
    utc_noon = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    db.ex("UPDATE tg_accounts SET tz='Asia/Almaty' WHERE id=%s", (a,))      # 12:00 UTC = 17:00 в Алматы
    assert worker.in_work_hours(worker.account(a), utc_noon)
    db.ex("UPDATE tg_accounts SET tz='America/New_York' WHERE id=%s", (a,))  # 08:00 в Нью-Йорке — ещё рано
    assert not worker.in_work_hours(worker.account(a), utc_noon)
    assert acc["tz"] is None
    form = {"warmup_start": "10", "warmup_step": "5", "daily_max": "50", "delay_min": "45", "delay_max": "150",
            "work_start": "10:00", "work_end": "20:00", "tz": "Mars/Base"}
    browser("admin").post(f"/accounts/{a}/settings", data=form)
    assert worker.account(a)["tz"] == "America/New_York"                     # неверный пояс не сохраняется
    form["tz"] = "Europe/Moscow"
    browser("admin").post(f"/accounts/{a}/settings", data=form)
    assert worker.account(a)["tz"] == "Europe/Moscow"
