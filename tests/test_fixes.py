"""Защита панели, двойная отправка, стоп-слова, «застрявшая» очередь, проверка настроек."""
import asyncio, os, tempfile
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("TG_API_ID", "1"); os.environ.setdefault("TG_API_HASH", "x")
os.environ.setdefault("PANEL_PASSWORD", "")
import pytest
from fastapi.testclient import TestClient
from telethon.tl.types import InputPeerUser
from app import db, worker
from app.main import app
from app.tg import is_stop_message, tg

ME = type("Me", (), {"first_name": "Me", "last_name": None, "username": None, "phone": "1"})()
panel = TestClient(app, base_url="http://127.0.0.1:8000")   # без with — lifespan (Telegram, воркер) не стартует


class Fake:
    def __init__(self, fail=None): self.sent = []; self.fail = fail
    async def get_input_entity(self, x):
        if isinstance(x, int): return x
        raise ValueError("no")
    async def get_entity(self, x): return InputPeerUser(user_id=5005, access_hash=0)
    async def send_message(self, e, text, reply_to=None, link_preview=True):
        await asyncio.sleep(0.01)                 # даём второму запросу «влезть»
        if self.fail: raise self.fail
        self.sent.append(e); return type("M", (), {"id": len(self.sent)})()
    def is_connected(self): return True


def _item(state="new", **kw):
    lid = db.ex("INSERT INTO lists(name) VALUES ('fix')")
    cols = {"list_id": lid, "kind": "person", "peer_id": 4004, "text": "привет", "state": state, **kw}
    iid = db.ex(f"INSERT INTO list_items({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(cols.values()))
    return db.one("SELECT * FROM list_items WHERE id=?", (iid,))


def _state(iid):
    return db.one("SELECT state, error FROM list_items WHERE id=?", (iid,))


# ---- 1. CSRF / DNS-rebinding ----
def test_cross_site_post_rejected():
    db.ex("INSERT INTO contacts(username) VALUES ('keep_me')")
    r = panel.post("/contacts/delete", data={"confirm": "да"}, headers={"Origin": "https://evil.example"},
                   follow_redirects=False)
    assert r.status_code == 403
    r = panel.post("/contacts/delete", data={"confirm": "да"}, headers={"Origin": "null"}, follow_redirects=False)
    assert r.status_code == 403
    assert db.one("SELECT 1 FROM contacts WHERE username='keep_me'")


def test_same_origin_post_allowed():
    r = panel.post("/contacts/delete", data={"confirm": "нет"}, headers={"Origin": "http://127.0.0.1:8000"},
                   follow_redirects=False)
    assert r.status_code == 303


def test_foreign_host_rejected():
    r = TestClient(app, base_url="http://evil.example").get("/log")
    assert r.status_code == 400


def test_send_now_redirect_stays_local():
    tg.client, tg.me = Fake(), ME
    it = _item(state="sent")
    r = panel.post(f"/lists/item/{it['id']}/send", headers={"Referer": "http://127.0.0.1:8000/lists/1?show=new"},
                   follow_redirects=False)
    assert r.headers["location"] == "/lists/1?show=new"


# ---- 2. двойная отправка ----
def test_double_click_sends_once():
    tg.client, tg.me = Fake(), ME
    it = _item()

    async def go():
        return await asyncio.gather(worker.send_list_item(it), worker.send_list_item(it))
    assert sorted(asyncio.run(go())) == ["busy", "sent"]
    assert len(tg.client.sent) == 1
    assert _state(it["id"])["state"] == "sent"


def test_interrupted_send_not_repeated():
    it = _item(state="sending")
    worker.recover_interrupted()
    st = _state(it["id"])
    assert st["state"] == "failed" and "прервано" in st["error"]


# ---- 3. стоп-слова ----
@pytest.mark.parametrize("text,stop", [("Стоп!", True), ("stop please", True), ("  не пишите мне", True),
                                       ("стопудово интересно", False), ("stopping by", False), ("", False)])
def test_stop_words(text, stop):
    assert is_stop_message(text, "стоп,stop,не пишите") is stop


# ---- 4. непредвиденная ошибка не блокирует очередь; сетевой сбой — вернуть как было ----
def test_unexpected_error_marks_failed():
    tg.client = Fake(fail=TypeError("boom"))
    it = _item(state="queued")
    assert asyncio.run(worker.send_list_item(it)) == "failed"
    assert _state(it["id"])["state"] == "failed"


def test_network_error_restores_state():
    tg.client = Fake(fail=ConnectionError("down"))
    it = _item(state="queued")
    with pytest.raises(ConnectionError):
        asyncio.run(worker.send_list_item(it))
    assert _state(it["id"])["state"] == "queued"


# ---- 5. отписка человека, указанного только username ----
def test_opt_out_by_username():
    tg.client = Fake()
    db.ex("INSERT INTO contacts(tg_user_id, opted_out) VALUES (5005, 1)")
    it = _item(peer_id=None, username="someone")
    assert asyncio.run(worker.send_list_item(it)) == "skipped"
    assert tg.client.sent == []
    db.ex("DELETE FROM contacts WHERE tg_user_id=5005")
    it = _item(peer_id=None, username="someone")
    assert asyncio.run(worker.send_list_item(it)) == "sent"
    assert db.one("SELECT peer_id FROM list_items WHERE id=?", (it["id"],))["peer_id"] == 5005   # теперь ловим ответы


# ---- 6. проверка настроек ----
@pytest.mark.parametrize("field,value", [("daily_max", "abc"), ("delay_min", "-5"), ("work_start", "10"),
                                         ("warmup_start_date", "вчера")])
def test_bad_settings_not_saved(field, value):
    before = db.all_settings()
    r = panel.post("/settings", data={field: value, "warmup_enabled": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.all_settings() == before
    worker.daily_limit(); worker.in_work_hours()     # и ничего не падает


def test_good_settings_saved():
    old = db.get_setting("work_start")
    panel.post("/settings", data={"work_start": "9:05", "warmup_enabled": "1"}, follow_redirects=False)
    assert db.get_setting("work_start") == "09:05"
    db.set_setting("work_start", old)


# ---- поиск получателей не мешает отправке ----
def test_worker_waits_while_preparing():
    tg.client, tg.me = Fake(), ME
    tg.prepare_state[-1] = {"running": True}
    try:
        assert asyncio.run(worker.tick()) == 15
        assert "поиск" in worker.state["status"]
        it = _item()
        r = panel.post(f"/lists/item/{it['id']}/send", follow_redirects=False)
        assert r.status_code == 303 and _state(it["id"])["state"] == "new"
    finally:
        del tg.prepare_state[-1]


# ---- панель без TG_API_ID/HASH не падает ----
def test_auth_without_config(monkeypatch):
    monkeypatch.setattr(type(tg), "configured", property(lambda self: False))
    for url, data in [("/auth/phone", {"phone": "+1"}), ("/auth/code", {"code": "1"}), ("/auth/password", {"password": "x"})]:
        r = panel.post(url, data=data, follow_redirects=False)
        assert r.status_code == 303, url


# ---- запуск с пустой очередью не завершает список ----
def test_start_empty_list_keeps_status():
    lid = db.ex("INSERT INTO lists(name) VALUES ('empty')")
    panel.post(f"/lists/{lid}/start", follow_redirects=False)
    assert db.one("SELECT status FROM lists WHERE id=?", (lid,))["status"] == "draft"


# ---- отчёт кампании не теряет удалённые контакты ----
def test_campaign_report_keeps_deleted_contacts():
    tid = db.ex("INSERT INTO templates(name, body) VALUES ('t', 'x')")
    cid = db.ex("INSERT INTO campaigns(name, template_id, status) VALUES ('del', ?, 'done')", (tid,))
    ct = db.ex("INSERT INTO contacts(username) VALUES ('gone')")
    db.ex("INSERT INTO messages(campaign_id, contact_id, status) VALUES (?,?,'sent')", (cid, ct))
    db.ex("DELETE FROM contacts WHERE id=?", (ct,))
    r = panel.get(f"/campaigns/{cid}")
    assert "контакт удалён" in r.text
    assert panel.get(f"/campaigns/{cid}/export.csv").text.count("\n") == 2


# ---- транзакция откатывает пачку целиком ----
def test_tx_rollback():
    with pytest.raises(RuntimeError):
        with db.tx():
            db.ex("INSERT INTO contacts(username) VALUES ('tx_probe')")
            raise RuntimeError
    assert not db.one("SELECT 1 FROM contacts WHERE username='tx_probe'")


# ---- xlsx без атрибутов r у ячеек и строк ----
def test_xlsx_without_cell_refs():
    import io, zipfile
    from app.xlsx import read_xlsx
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="S" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>'
                   '<row><c t="inlineStr"><is><t>Тип</t></is></c><c t="inlineStr"><is><t>Ссылка</t></is></c></row>'
                   '<row><c t="inlineStr"><is><t>Чат</t></is></c><c><v>5</v></c></row></sheetData></worksheet>')
    assert read_xlsx(buf.getvalue()) == {"S": [["Тип", "Ссылка"], ["Чат", 5]]}
