"""Командная работа: закрепление лидов, распределение, правило повторных сообщений, отписка, ответы."""
import types

import pytest

from app import campaigns, db, leads
from app.tg import DuplicateAccount, tgm
from tests.conftest import (FakeClient, add_rows, cl_state, drain, make_account, make_campaign, make_lead, make_user, run,
                            snapshot)


def test_new_leads_are_spread_and_pinned():
    u = make_user()
    a, b = make_account(u["id"], "A"), make_account(u["id"], "B")
    snapshot(a, b)
    cid = make_campaign(u["id"], [a, b])
    rows = add_rows(cid, [make_lead(tg_id=1000 + i) for i in range(10)])
    assert campaigns.enqueue(cid, {"state": ["new"]}) == (10, 0)
    owners = [cl_state(r)["account_id"] for r in rows]
    assert owners.count(a) == 5 and owners.count(b) == 5
    # закрепление записано в лиде
    assert db.val("SELECT COUNT(*) FROM leads WHERE owner_account_id IS NOT NULL") == 10


def test_warm_lead_goes_to_account_that_knows_him():
    u = make_user()
    a, b = make_account(u["id"], "A"), make_account(u["id"], "B")
    db.ex("INSERT INTO tg_dialogs(account_id, peer_id, title, kind, has_private) VALUES (%s, 777, 'x', 'user', true)", (b,))
    snapshot(a)
    cid = make_campaign(u["id"], [a, b])
    r, = add_rows(cid, [make_lead(tg_id=777)])
    campaigns.enqueue(cid, {"state": ["new"]})
    assert cl_state(r)["account_id"] == b


def test_lead_of_other_manager_is_not_taken():
    admin = make_user()
    m2 = make_user("anna", "manager")
    a = make_account(admin["id"], "A")
    b = make_account(m2["id"], "B")
    lid = make_lead(tg_id=888, owner_account_id=a)
    cid = make_campaign(m2["id"], [b])
    r, = add_rows(cid, [lid])
    assert campaigns.enqueue(cid, {"state": ["new"]}) == (0, 1)
    st = cl_state(r)
    assert st["state"] == "skipped" and "закреплён за другим аккаунтом" in st["error"]


def test_recontact_rule_across_campaigns():
    u = make_user()
    a = make_account(u["id"])
    lid = make_lead(tg_id=999)
    c1 = make_campaign(u["id"], [a])
    add_rows(c1, [lid])
    campaigns.enqueue(c1, {"state": ["new"]})
    drain(a, 1)
    c2 = make_campaign(u["id"], [a])
    r2, = add_rows(c2, [lid])
    assert campaigns.enqueue(c2, {"state": ["new"]}) == (0, 1)
    assert "уже писали" in cl_state(r2)["error"]
    # правило настраивается: 0 дней — можно писать снова
    db.set_setting("recontact_days", "0")
    assert campaigns.enqueue(c2, {"state": ["skipped"]}) == (1, 0)
    # давнее сообщение правилу не мешает
    db.set_setting("recontact_days", "30")
    db.ex("UPDATE messages SET created_at=now() - interval '40 days'")
    db.ex("UPDATE campaign_leads SET state='new' WHERE id=%s", (r2,))
    assert campaigns.enqueue(c2, {"state": ["new"]}) == (1, 0)


def test_chats_are_not_limited_by_recontact_rule():
    u = make_user()
    a = make_account(u["id"])
    chat = make_lead(kind="chat", tg_id=-1001)
    for _ in range(2):
        cid = make_campaign(u["id"], [a])
        add_rows(cid, [chat], custom_text="пост")
        assert campaigns.enqueue(cid, {"state": ["new"]}) == (1, 0)
        drain(a, 1)


def test_campaign_without_accounts_refuses_to_queue():
    u = make_user()
    cid = make_campaign(u["id"], [])
    add_rows(cid, [make_lead(tg_id=5)])
    with pytest.raises(ValueError, match="нет подключённых аккаунтов"):
        campaigns.enqueue(cid, {"state": ["new"]})


# ---- входящие ----
def test_reply_marks_replied_and_is_stored():
    u = make_user()
    a = make_account(u["id"])
    lid = make_lead(tg_id=1234)
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [lid])
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)
    run(tgm.get(a).handle_incoming(1234, "Интересно, расскажите", 77))
    assert cl_state(r)["state"] == "replied"
    m = db.one("SELECT * FROM messages WHERE direction='in'")
    assert m["text"] == "Интересно, расскажите" and m["account_id"] == a


def test_stop_word_opts_out_for_whole_team():
    u = make_user()
    a, b = make_account(u["id"], "A"), make_account(u["id"], "B")
    lid = make_lead(tg_id=4321, owner_account_id=a)
    c1, c2 = make_campaign(u["id"], [a]), make_campaign(u["id"], [a])
    r1, = add_rows(c1, [lid])
    r2, = add_rows(c2, [lid])
    campaigns.enqueue(c1, {"state": ["new"]})
    db.set_setting("recontact_days", "0")
    campaigns.enqueue(c2, {"state": ["new"]})
    run(tgm.get(a).handle_incoming(4321, "Стоп!", 1))
    assert db.val("SELECT opted_out_at IS NOT NULL FROM leads WHERE id=%s", (lid,))
    assert cl_state(r1)["state"] == cl_state(r2)["state"] == "skipped"
    # незнакомец со стоп-словом тоже попадает в стоп-лист
    run(tgm.get(b).handle_incoming(5555, "stop please", 2))
    assert db.val("SELECT opted_out_at IS NOT NULL FROM leads WHERE tg_id=5555")


@pytest.mark.parametrize("text,stop", [("Стоп!", True), ("stop please", True), ("  не пишите мне", True),
                                       ("стопудово интересно", False), ("stopping by", False), ("", False)])
def test_stop_words(text, stop):
    assert leads.is_stop_message(text, "стоп,stop,не пишите") is stop


def test_read_receipt_only_for_own_account():
    u = make_user()
    a, b = make_account(u["id"], "A"), make_account(u["id"], "B")
    lid = make_lead(tg_id=2222)
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [lid])
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)
    tgm.get(b)._mark_read(2222, 100)
    assert cl_state(r)["state"] == "sent"
    tgm.get(a)._mark_read(2222, 100)
    assert cl_state(r)["state"] == "read"


def test_same_telegram_account_cannot_be_connected_twice():
    u = make_user()
    make_account(u["id"], "A", tg_user_id=42)
    b = db.ex("INSERT INTO tg_accounts(user_id, label) VALUES (%s, 'B') RETURNING id", (u["id"],))
    acc = tgm.get(b)

    class C(FakeClient):
        logged_out = False
        async def get_me(self):
            return types.SimpleNamespace(id=42, first_name="Same", last_name=None, username=None, phone="1")
        async def log_out(self):
            C.logged_out = True
    acc.client = C()
    with pytest.raises(DuplicateAccount):
        run(acc._on_login("вход"))
    assert C.logged_out and acc.me is None
    assert db.val("SELECT tg_user_id FROM tg_accounts WHERE id=%s", (b,)) is None


# ---- лиды ----
def test_upsert_merges_by_any_identifier():
    lid, created = leads.upsert_lead({"username": "Ivan_P", "first_name": "Иван"}, ["a"])
    assert created
    same, created = leads.upsert_lead({"tg_id": 55, "username": "ivan_p", "phone": "+375291"}, ["b"])
    assert same == lid and not created
    row = db.one("SELECT * FROM leads WHERE id=%s", (lid,))
    assert row["tg_id"] == 55 and row["phone"] == "+375291" and row["first_name"] == "Иван"
    assert sorted(row["tags"]) == ["a", "b"]
    assert leads.upsert_lead({"phone": "+375291"})[0] == lid


def test_csv_import():
    raw = "username;phone;first_name;company\n@petr;;Пётр;ООО Ромашка\n;375291112233;Ольга;\n;;;\n".encode()
    assert leads.import_csv(raw, "csv") == (2, 0, 1)
    row = db.one("SELECT * FROM leads WHERE username='petr'")
    assert row["extra"] == {"company": "ООО Ромашка"} and row["tags"] == ["csv"]
    assert db.val("SELECT phone FROM leads WHERE first_name='Ольга'") == "+375291112233"


def test_tx_rollback():
    with pytest.raises(RuntimeError):
        with db.tx():
            make_lead(username="tx_probe")
            raise RuntimeError
    assert not db.one("SELECT 1 FROM leads WHERE username='tx_probe'")


def test_multi_account_needs_dialog_snapshot_first():
    u = make_user()
    a, b = make_account(u["id"], "A"), make_account(u["id"], "B")
    cid = make_campaign(u["id"], [a, b])
    add_rows(cid, [make_lead(tg_id=31), make_lead(tg_id=32)])
    with pytest.raises(ValueError, match="Найти получателей"):
        campaigns.enqueue(cid, {"state": ["new"]})
    for acc in (a, b):
        db.ex("INSERT INTO tg_dialogs(account_id, peer_id, kind, has_private) VALUES (%s, %s, 'user', true)", (acc, 31 if acc == b else 1))
    assert campaigns.enqueue(cid, {"state": ["new"]}) == (2, 0)
    assert db.val("SELECT account_id FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id WHERE l.tg_id=31") == b
    # один аккаунт или только чаты — снимок не нужен
    c2 = make_campaign(u["id"], [a])
    add_rows(c2, [make_lead(tg_id=33)])
    assert campaigns.enqueue(c2, {"state": ["new"]}) == (1, 0)
