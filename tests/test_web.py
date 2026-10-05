"""Панель: первый запуск, вход, права ролей, защита форм, отправка «сейчас», настройки."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import campaigns, db, delivery, worker
from app.main import app
from tests.conftest import FakeClient, add_rows, cl_state, make_account, make_campaign, make_lead, make_user, run

BASE = "http://127.0.0.1:8000"


def browser(login=None) -> TestClient:
    """Клиент как браузер панели: без lifespan (Telegram и воркер не стартуют), свой Origin, вход по логину."""
    c = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    if login:
        r = c.post("/login", data={"login": login, "password": "password123"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", r.text
    return c


# ---- вход ----
def test_first_run_setup_creates_admin():
    c = browser()
    r = c.get("/", follow_redirects=False)
    assert r.headers["location"] == "/setup"
    r = c.post("/setup", data={"login": "Boss", "name": "Босс", "password": "short"}, follow_redirects=False)
    assert "err" in r.cookies.get("flash", "") or db.val("SELECT COUNT(*) FROM users") == 0
    r = c.post("/setup", data={"login": "Boss", "name": "Босс", "password": "longpassword"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.one("SELECT role, login FROM users") == {"role": "admin", "login": "boss"}
    assert c.get("/").status_code == 200                    # уже вошли
    assert browser().post("/setup", data={"login": "x", "name": "x", "password": "longpassword"},
                          follow_redirects=False).headers["location"] == "/login"   # второй раз нельзя


def test_login_required_and_wrong_password():
    make_user("admin")
    c = browser()
    assert c.get("/campaigns", follow_redirects=False).headers["location"].startswith("/login")
    r = c.post("/login", data={"login": "admin", "password": "wrong-pass"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login")
    assert c.post("/leads/import-csv", files={"file": ("a.csv", b"username\nx\n")}).status_code == 401


def test_password_change_ends_other_sessions():
    make_user("admin")
    c1, c2 = browser("admin"), browser("admin")
    c1.post("/users/1/password", data={"password": "newpassword1"})
    assert c1.get("/me").status_code == 200
    assert c2.get("/me", follow_redirects=False).status_code == 303


def test_all_pages_render():
    u = make_user("admin")
    a = make_account(u["id"])
    cid = make_campaign(u["id"], [a])
    add_rows(cid, [make_lead(tg_id=1, first_name="Аня")])
    db.ex("INSERT INTO templates(name, body) VALUES ('t', 'Привет')")
    c = browser("admin")
    for url in ["/", "/campaigns", f"/campaigns/{cid}", f"/campaigns/{cid}?show=new", "/leads", "/templates",
                "/accounts", f"/accounts/{a}", f"/accounts/{a}/login", "/users", "/settings", "/log", "/me",
                f"/campaigns/{cid}/export.csv"]:
        assert c.get(url).status_code == 200, url


# ---- роли ----
def test_manager_sees_only_own_things():
    admin = make_user("admin")
    m = make_user("anna", "manager")
    a_admin = make_account(admin["id"], "AdminAcc")
    a_m = make_account(m["id"], "AnnaAcc")
    c_admin = make_campaign(admin["id"], [a_admin])
    c = browser("anna")
    assert "AdminAcc" not in c.get("/accounts").text and "AnnaAcc" in c.get("/accounts").text
    assert c.get(f"/campaigns/{c_admin}", follow_redirects=False).status_code == 303
    assert c.get(f"/accounts/{a_admin}", follow_redirects=False).status_code == 303
    assert c.get("/users", follow_redirects=False).status_code == 303
    assert c.post("/settings", data={"stop_words": "x"}, follow_redirects=False).status_code == 303
    assert db.get_setting("stop_words") != "x"
    # в кампанию нельзя подставить чужой аккаунт
    db.ex("INSERT INTO templates(name, body) VALUES ('t', 'Привет')")
    c.post("/campaigns/create", data={"name": "x", "template_id": 1, "accounts": [a_admin]})
    assert db.val("SELECT COUNT(*) FROM campaigns WHERE created_by=%s", (m["id"],)) == 0
    c.post("/campaigns/create", data={"name": "x", "template_id": 1, "accounts": [a_m]})
    assert db.val("SELECT COUNT(*) FROM campaigns WHERE created_by=%s", (m["id"],)) == 1


def test_admin_adds_manager():
    make_user("admin")
    c = browser("admin")
    c.post("/users/create", data={"login": "Petr", "name": "Пётр", "password": "password123", "role": "manager"})
    assert db.val("SELECT role FROM users WHERE login='petr'") == "manager"
    assert browser("petr").get("/").status_code == 200


# ---- защита форм ----
def test_cross_site_post_rejected():
    make_user("admin")
    c = browser("admin")
    make_lead(username="keep_me")
    for origin in ("https://evil.example", "null"):
        r = c.post("/leads/delete", data={"confirm": "да"}, headers={"Origin": origin}, follow_redirects=False)
        assert r.status_code == 403
    assert db.one("SELECT 1 FROM leads WHERE username='keep_me'")


def test_foreign_host_rejected():
    assert TestClient(app, base_url="http://evil.example").get("/login").status_code == 400


# ---- отправка «сейчас» ----
def test_double_click_sends_once():
    u = make_user("admin")
    client = FakeClient()
    client.delay = 0.02
    a = make_account(u["id"], client=client)
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [make_lead(tg_id=77)])

    async def go():
        return await asyncio.gather(delivery.send_now(r), delivery.send_now(r))
    results = sorted(res for res, _ in run(go()))
    assert "sent" in results and len(client.sent) == 1
    assert cl_state(r)["state"] == "sent"


def test_send_now_redirect_stays_local():
    u = make_user("admin")
    a = make_account(u["id"])
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [make_lead(tg_id=78)], state="sent")
    res = browser("admin").post(f"/campaigns/row/{r}/send", headers={"Referer": f"{BASE}/campaigns/{cid}?show=new"},
                                follow_redirects=False)
    assert res.headers["location"] == f"/campaigns/{cid}?show=new"


def test_interrupted_send_not_repeated():
    u = make_user("admin")
    a = make_account(u["id"])
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [make_lead(tg_id=79)], state="sending", account_id=a)
    worker.recover_interrupted()
    st = cl_state(r)
    assert st["state"] == "failed" and "прервано" in st["error"]


def test_unexpected_error_marks_failed_and_network_error_restores():
    u = make_user("admin")
    client = FakeClient()
    a = make_account(u["id"], client=client)
    cid = make_campaign(u["id"], [a])
    r1, r2 = add_rows(cid, [make_lead(tg_id=80), make_lead(tg_id=81)])
    campaigns.enqueue(cid, {"state": ["new"]})
    client.fail[80] = TypeError("boom")
    client.fail[81] = ConnectionError("down")
    assert run(delivery.send_item(db.one("SELECT * FROM campaign_leads WHERE id=%s", (r1,)))) == "failed"
    with pytest.raises(ConnectionError):
        run(delivery.send_item(db.one("SELECT * FROM campaign_leads WHERE id=%s", (r2,))))
    assert cl_state(r2)["state"] == "queued"


def test_opted_out_twin_found_by_username_is_skipped():
    u = make_user("admin")
    client = FakeClient(known_usernames={"someone": 5005})
    a = make_account(u["id"], client=client)
    make_lead(tg_id=5005, opted_out_at=db.now_utc())
    cid = make_campaign(u["id"], [a])
    r, = add_rows(cid, [make_lead(username="someone")])
    assert run(delivery.send_now(r))[0] == "skipped"
    assert client.sent == []


def test_start_empty_campaign_keeps_status():
    u = make_user("admin")
    cid = make_campaign(u["id"], [make_account(u["id"])], status="draft")
    browser("admin").post(f"/campaigns/{cid}/start")
    assert db.val("SELECT status FROM campaigns WHERE id=%s", (cid,)) == "draft"


# ---- настройки аккаунта ----
@pytest.mark.parametrize("field,value", [("daily_max", "abc"), ("delay_min", "-5"), ("work_start", "10"),
                                         ("warmup_start_date", "вчера")])
def test_bad_account_settings_not_saved(field, value):
    u = make_user("admin")
    a = make_account(u["id"])
    before = worker.account(a)
    form = {"warmup_start": "10", "warmup_step": "5", "daily_max": "50", "delay_min": "45", "delay_max": "150",
            "work_start": "10:00", "work_end": "20:00", "warmup_enabled": "1", field: value}
    browser("admin").post(f"/accounts/{a}/settings", data=form)
    assert worker.account(a) == before


def test_good_account_settings_saved():
    u = make_user("admin")
    a = make_account(u["id"])
    form = {"label": "Продажи", "warmup_start": "5", "warmup_step": "2", "daily_max": "20", "delay_min": "60",
            "delay_max": "120", "work_start": "9:05", "work_end": "18:00"}
    browser("admin").post(f"/accounts/{a}/settings", data=form)
    acc = worker.account(a)
    assert (acc["label"], acc["daily_max"], acc["work_start"].strftime("%H:%M"), acc["warmup_enabled"]) == \
           ("Продажи", 20, "09:05", False)


def test_team_rules_validation():
    make_user("admin")
    c = browser("admin")
    c.post("/settings", data={"stop_words": " ", "recontact_days": "30"})
    assert db.get_setting("stop_words").startswith("стоп")
    c.post("/settings", data={"stop_words": "стоп, хватит", "recontact_days": "14"})
    assert (db.get_setting("stop_words"), db.get_setting("recontact_days")) == ("стоп,хватит", "14")


def test_cli_set_password(monkeypatch, capsys):
    from app import auth, cli
    make_user("boss")
    db.ex("UPDATE users SET active=false")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "brand-new-pass")
    assert cli.main(["set-password", "Boss"]) == 0
    assert auth.authenticate("boss", "brand-new-pass")
    assert cli.main(["set-password", "nobody"]) == 1
    assert cli.main(["users"]) == 0 and "boss" in capsys.readouterr().out


# ---- B2: перебор паролей ----
def test_login_throttled_after_many_failures(monkeypatch):
    make_user("admin")
    c = browser()
    for _ in range(8):
        c.post("/login", data={"login": "admin", "password": "wrong-pass"})
    r = c.post("/login", data={"login": "admin", "password": "password123"}, follow_redirects=False)
    assert r.headers["location"].startswith("/login")                       # даже верный пароль — подождать
    assert db.val("SELECT COUNT(*) FROM event_log WHERE text LIKE %s", ("Вход заблокирован%",)) >= 1
    from app import cli
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "password123")
    cli.main(["set-password", "admin"])                                       # админ сменил пароль — блокировка снята
    assert browser("admin").get("/me").status_code == 200


# ---- B3: размер загрузки ----
def test_upload_size_limited(monkeypatch):
    from app.web import common
    make_user("admin")
    c = browser("admin")
    monkeypatch.setattr(common, "MAX_UPLOAD", 1024)
    big = b"username\n" + b"user_x\n" * 500
    r = c.post("/leads/import-csv", files={"file": ("big.csv", big)}, follow_redirects=False)
    assert "%D0%B1%D0%BE%D0%BB%D1%8C%D1%88%D0%B5" in r.headers["set-cookie"]   # «больше … МБ»
    assert db.val("SELECT COUNT(*) FROM leads") == 0


def test_xlsx_bomb_rejected():
    import io
    import zipfile
    from app.xlsx import read_xlsx
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", b"0" * (101 * 1024 * 1024))           # сжимается в килобайты
    with pytest.raises(ValueError, match="слишком большой после распаковки"):
        read_xlsx(buf.getvalue())


def test_menu_highlights_current_section():
    """Пункт меню подсвечен на своей странице и на вложенных; «Дашборд» — только на главной."""
    import re
    make_user("admin")
    c = browser("admin")

    def current(url):
        menu = c.get(url).text.split('class="nav-menu"', 1)[1].split("</ul>", 1)[0]
        return re.findall(r'<a href="([^"]+)" aria-current="page"', menu)
    assert current("/") == ["/"]
    assert current("/ai/sandbox") == ["/ai"]
    assert current("/campaigns") == ["/campaigns"]
    assert current("/chat-search") == ["/chat-search"]
