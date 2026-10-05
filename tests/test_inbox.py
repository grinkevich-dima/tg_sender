"""Инбокс и воронка: автоэтапы, свои сообщения из Telegram, ответ из панели, права, этапы."""
from telethon import errors

from app import campaigns, db, delivery, inbox, worker
from app.tg import tgm
from tests.conftest import FakeClient, add_rows, drain, make_account, make_campaign, make_lead, make_user, run
from tests.test_web import browser


def stage_of(lead_id):
    return db.val("SELECT s.name FROM leads l LEFT JOIN funnel_stages s ON s.id=l.stage_id WHERE l.id=%s", (lead_id,))


def _conversation(u, client=None, tg_id=500):
    """Лид, которому кампания уже написала с аккаунта a."""
    a = make_account(u["id"], client=client or FakeClient())
    lid = make_lead(tg_id=tg_id, first_name="Ира")
    cid = make_campaign(u["id"], [a])
    add_rows(cid, [lid])
    campaigns.enqueue(cid, {"state": ["new"]})
    drain(a, 1)
    return a, lid


def test_auto_stages_only_move_forward():
    u = make_user()
    a, lid = _conversation(u)
    assert stage_of(lid) == "написали"
    run(tgm.get(a).handle_incoming(500, "Интересно!", 77))
    assert stage_of(lid) == "ответил"
    inbox.set_stage(lid, db.val("SELECT id FROM funnel_stages WHERE name='записался'"))
    run(tgm.get(a).handle_incoming(500, "Спасибо, буду", 78))       # новый ответ не откатывает ручной этап
    assert stage_of(lid) == "записался"
    assert db.val("SELECT COUNT(*) FROM messages WHERE lead_id=%s AND direction='in'", (lid,)) == 2


def test_own_messages_from_telegram_and_no_duplicates():
    u = make_user()
    a, lid = _conversation(u)
    acc = tgm.get(a)
    assert acc.handle_outgoing(500, "написал руками", 900) is True
    assert acc.handle_outgoing(500, "написал руками", 900) is False            # повтор события
    assert acc.handle_outgoing(999999, "личное, не лиду", 901) is False        # не лид — не собираем
    other = make_lead(tg_id=600, owner_account_id=make_account(u["id"], "B"))
    assert acc.handle_outgoing(600, "лиду коллеги", 902) is False              # лид чужого аккаунта
    # то же сообщение потом записала панель — строка уточняется, а не дублируется
    inbox.record(a, lid, "out", "написал руками", 900, "inbox", sender_user_id=u["id"])
    rows = db.q("SELECT source, sender_user_id FROM messages WHERE tg_message_id=900")
    assert rows == [{"source": "inbox", "sender_user_id": u["id"]}]
    assert other


def test_reply_from_panel():
    u = make_user()
    client = FakeClient()
    a, lid = _conversation(u, client)
    assert run(delivery.send_reply(lid, "Привет! Ссылка на запись: …", u["id"])) == ""
    assert client.sent[-1][1].startswith("Привет! Ссылка")
    m = db.one("SELECT * FROM messages WHERE source='inbox'")
    assert m["sender_user_id"] == u["id"] and m["direction"] == "out"
    assert worker.sent_today(a) == 1                        # ответы не тратят дневной лимит рассылки


def test_reply_refused_cases():
    u = make_user()
    client = FakeClient()
    a, lid = _conversation(u, client)
    fresh = make_lead(tg_id=501, owner_account_id=a)
    assert "первое сообщение" in run(delivery.send_reply(fresh, "привет", u["id"]))
    assert run(delivery.send_reply(lid, "   ", u["id"])) == "Пустое сообщение"
    client.fail[500] = errors.FloodWaitError(request=None, capture=60)
    assert "ограничил" in run(delivery.send_reply(lid, "ещё раз", u["id"]))
    assert "на паузе" in run(delivery.send_reply(lid, "и ещё", u["id"]))      # после FloodWait аккаунт на паузе
    db.ex("UPDATE tg_accounts SET paused_until=NULL")
    db.ex("UPDATE leads SET opted_out_at=now() WHERE id=%s", (lid,))
    assert "отписался" in run(delivery.send_reply(lid, "привет", u["id"]))


def test_inbox_pages_unread_and_rights():
    admin = make_user("admin")
    anna = make_user("anna", "manager")
    a, lid = _conversation(admin)
    b, lid_b = _conversation(anna, tg_id=700)
    run(tgm.get(a).handle_incoming(500, "Расскажите подробнее", 10))
    run(tgm.get(b).handle_incoming(700, "Анне ответ", 11))
    c = browser("admin")
    html = c.get("/inbox").text
    assert "Расскажите подробнее" in html and "Анне ответ" in html             # админ видит всех
    assert c.get("/inbox/unread").json() == {"unread": 2}
    assert ">2</span>" in c.get("/").text                                      # счётчик в меню
    page = c.get(f"/inbox/{lid}").text                                          # открыли — прочитано
    assert "Расскажите подробнее" in page and "Привет, Ира!" in page
    assert c.get("/inbox/unread").json() == {"unread": 1}
    assert "Расскажите подробнее" not in c.get("/inbox?show=unread").text
    assert "Расскажите подробнее" in c.get("/inbox?show=waiting").text
    # менеджер видит только свои диалоги и не может отвечать в чужие
    m = browser("anna")
    assert "Анне ответ" in m.get("/inbox").text and "Расскажите подробнее" not in m.get("/inbox").text
    assert m.get(f"/inbox/{lid}", follow_redirects=False).status_code == 303
    m.post(f"/inbox/{lid}/send", data={"text": "чужому"})
    assert not db.one("SELECT 1 FROM messages WHERE text='чужому'")
    # подгрузка новых сообщений
    last = db.val("SELECT MAX(id) FROM messages WHERE lead_id=%s", (lid,))
    run(tgm.get(a).handle_incoming(500, "И ещё вопрос", 12))
    new = c.get(f"/inbox/{lid}/messages?after={last}").json()["messages"]
    assert [x["text"] for x in new] == ["И ещё вопрос"]


def test_stage_note_optout_via_panel():
    u = make_user("admin")
    a, lid = _conversation(u)
    c = browser("admin")
    interest = db.val("SELECT id FROM funnel_stages WHERE name='интерес'")
    c.post(f"/inbox/{lid}/stage", data={"stage_id": interest})
    c.post(f"/inbox/{lid}/note", data={"note": "Хочет консультацию в ноябре"})
    assert stage_of(lid) == "интерес" and db.val("SELECT note FROM leads WHERE id=%s", (lid,)) == "Хочет консультацию в ноябре"
    funnel = {s["name"]: s["n"] for s in inbox.funnel()}
    assert funnel["интерес"] == 1 and funnel["написали"] == 0
    c.post(f"/inbox/{lid}/optout")
    assert db.val("SELECT opted_out_at IS NOT NULL FROM leads WHERE id=%s", (lid,))


def test_stages_admin():
    make_user("admin")
    make_user("anna", "manager")
    c = browser("admin")
    c.post("/settings/stages/add", data={"name": "на консультации"})
    st = db.one("SELECT * FROM funnel_stages WHERE name='на консультации'")
    assert st and st["position"] == 6 and db.val("SELECT position FROM funnel_stages WHERE name='отказ'") == 7
    c.post(f"/settings/stages/{st['id']}", data={"name": "консультация", "position": "4", "is_goal": "1"})
    assert db.one("SELECT name, is_goal FROM funnel_stages WHERE id=%s", (st["id"],)) == {"name": "консультация", "is_goal": True}
    auto = db.val("SELECT id FROM funnel_stages WHERE auto='replied'")
    c.post(f"/settings/stages/{auto}/delete")
    assert db.one("SELECT 1 FROM funnel_stages WHERE id=%s", (auto,))               # автоэтап не удаляется
    c.post(f"/settings/stages/{st['id']}/delete")
    assert not db.one("SELECT 1 FROM funnel_stages WHERE id=%s", (st["id"],))
    browser("anna").post("/settings/stages/add", data={"name": "менеджерский"})
    assert not db.one("SELECT 1 FROM funnel_stages WHERE name='менеджерский'")
    assert c.get("/settings").status_code == 200


def test_deleted_in_telegram_is_marked_not_removed():
    """Сообщение удалили в Telegram: остаётся в инбоксе с пометкой, ИИ его не видит, пометка приходит и без перезагрузки."""
    from app import ai
    u = make_user()
    a, lid = _conversation(u)
    run(tgm.get(a).handle_incoming(500, "Сколько стоит?", 77))
    run(tgm.get(a).handle_incoming(500, "ой, не туда", 78))
    other = make_account(u["id"], label="B")
    assert tgm.get(other).mark_deleted([78]) == 0                  # id сообщений — свои у каждого аккаунта
    assert tgm.get(a).mark_deleted([78, 999]) == 1
    texts = [r["text"] for r in ai._history(lid)]
    assert "Сколько стоит?" in texts and "ой, не туда" not in texts
    c = browser("admin")
    html = c.get(f"/inbox/{lid}").text
    assert 'class="msg in deleted"' in html
    mid = db.val("SELECT id FROM messages WHERE tg_message_id=78")
    assert mid in c.get(f"/inbox/{lid}/messages?after=0").json()["deleted"]
