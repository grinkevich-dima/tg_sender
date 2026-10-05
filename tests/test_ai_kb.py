"""Группа G: наполнение базы знаний ИИ — импорт, «нет ответа в базе», сбор из переписок, мастер, копирование, проверка."""
import io
import json
import zipfile

import pytest

from app import ai, ai_kb, autopilot, db, inbox, leads, sandbox
from tests import test_autopilot as ta
from tests.conftest import make_user, run
from tests.test_autopilot import incoming, make_due, send_due, setup
from tests.test_inbox import _conversation
from tests.test_web import browser


fake = ta.fake          # фикстура «поддельный ИИ» автопилота


@pytest.fixture
def kb_ai(monkeypatch):
    """Поддельный ИИ для наполнения: отвечает по началу системной инструкции."""
    calls = []

    async def chat(messages, **kw):
        sysmsg, user_msg = messages[0]["content"], messages[-1]["content"]
        calls.append(user_msg)
        if sysmsg.startswith("Ты помогаешь наполнить"):
            return json.dumps({"cards": [{"title": "Сколько стоит сайт", "body": "Лендинг от 1500 BYN"},
                                         {"title": "Сроки", "body": "2–3 недели"}]}, ensure_ascii=False)
        if sysmsg.startswith("Ты помогаешь обучить"):
            return json.dumps({"examples": [{"question": "Сколько стоит?", "answer": "От 1500, зависит от задачи"}],
                               "cards": [{"title": "Рассрочка", "body": "Есть, на 3 месяца"}]}, ensure_ascii=False)
        if sysmsg.startswith("Составь профиль"):
            return json.dumps({"instruction": "Кто пишет: Дмитрий.\nЦель: созвон.",
                               "cards": [{"title": "Как записаться", "body": "https://cal.example/dima"}]}, ensure_ascii=False)
        if sysmsg.startswith("Проверь базу"):
            return '{"issues": [{"type": "дубль", "text": "«Цена» и «Стоимость» — об одном"}]}'
        return "ok"
    monkeypatch.setattr(ai, "AI_API_KEY", "k")
    monkeypatch.setattr(ai, "chat", chat)
    return calls


def profile(name="Сайты", user_id=None) -> int:
    return db.ex("INSERT INTO ai_profiles(name, created_by) VALUES (%s, %s) RETURNING id", (name, user_id))


# ---------- G1: текст из файлов ----------
def test_g1_text_extraction():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:body><w:p><w:r><w:t>Цены</w:t></w:r></w:p><w:p><w:r><w:t>Лендинг — </w:t></w:r><w:r><w:t>1500</w:t></w:r></w:p>"
                   "</w:body></w:document>")
    assert ai_kb.file_text("прайс.docx", buf.getvalue()) == "Цены\nЛендинг — 1500"
    html = "<html><head><title>x</title><script>var a=1</script></head><body><h1>Услуги</h1><p>Сайты&nbsp;под ключ</p></body></html>"
    assert ai_kb.file_text("page.html", html.encode()) == "Услуги\n\nСайты под ключ"
    assert ai_kb.file_text("a.txt", "Привет".encode("cp1251")) == "Привет"
    with pytest.raises(ai_kb.KBError):
        ai_kb.file_text("old.doc", b"x")
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(100, 100)
    pdf = io.BytesIO()
    w.write(pdf)
    with pytest.raises(ai_kb.KBError, match="скан"):
        ai_kb.file_text("scan.pdf", pdf.getvalue())
    parts = ai_kb.chunks("а" * 100 + "\n" + "б" * 100, size=120)
    assert len(parts) == 2 and all(len(p) <= 121 for p in parts)


def test_g1_url_must_be_public():
    for url in ("ftp://x", "http://127.0.0.1:8000/", "http://localhost/", "http://192.168.1.1/"):
        with pytest.raises(ai_kb.KBError):
            ai_kb.fetch_url(url)


def test_g1_import_creates_suggestions_without_duplicates(kb_ai):
    pid = profile()
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Сроки', 'месяц')", (pid,))
    run(ai_kb.import_text(pid, None, "Наши услуги: сайты. Цены от 1500. Сроки — 2–3 недели." * 3, "вставленный текст"))
    s = ai_kb.suggestions(pid)
    assert [x["title"] for x in s] == ["Сколько стоит сайт"]            # «Сроки» уже есть в базе
    assert "Уже есть: " in kb_ai[0] and "сроки" in kb_ai[0]
    assert ai_kb.jobs[pid]["running"] is False and "1" in ai_kb.jobs[pid]["step"]


def test_g1_import_route_validation():
    u = make_user("admin")
    pid = profile(user_id=u["id"])
    make_user("anna", "manager")
    assert "автор профиля" in browser("anna").post(f"/ai/profiles/{pid}/import", data={"text": "x"}).text
    r = browser("admin").post(f"/ai/profiles/{pid}/import", files={"file": ("a.doc", b"x")}, data={})
    assert "не настроен" in r.text or "DOCX" in r.text


# ---------- предложения ----------
def test_suggestions_accept_edit_reject(kb_ai):
    u = make_user("admin")
    pid = profile(user_id=u["id"])
    ai_kb.add_suggestions(pid, u["id"], "мастер", cards=[{"title": "Цена", "body": "1500"}, {"title": "Лишнее", "body": "-"}],
                          examples=[{"question": "Сколько?", "answer": "1500"}], instruction="Новая инструкция")
    ids = {s["title"] or s["kind"]: s["id"] for s in ai_kb.suggestions(pid)}
    c = browser("admin")
    c.post(f"/ai/profiles/{pid}/suggestions", data={"action": "accept", "pick": [ids["Цена"], ids["Сколько?"], ids["instruction"]],
                                                    f"body_{ids['Цена']}": "Лендинг — 1500 BYN"})
    assert db.val("SELECT body FROM ai_cards WHERE profile_id=%s", (pid,)) == "Лендинг — 1500 BYN"     # с правкой
    assert db.val("SELECT source FROM ai_examples WHERE profile_id=%s", (pid,)) == "dialogs"
    assert db.val("SELECT instruction FROM ai_profiles WHERE id=%s", (pid,)) == "Новая инструкция"
    assert [s["title"] for s in ai_kb.suggestions(pid)] == ["Лишнее"]
    c.post(f"/ai/profiles/{pid}/suggestions", data={"action": "reject_all"})
    assert ai_kb.suggestions(pid) == []


# ---------- G2: нет ответа в базе ----------
def test_g2_gap_dedup_and_answer():
    make_user("admin")
    pid = profile()
    ai_kb.note_gap(pid, "А рассрочка есть?", "заготовка [условия]", "draft")
    ai_kb.note_gap(pid, "а рассрочка есть?", "ИИ пообещал уточнить", "sandbox")
    g, = ai_kb.gaps(pid)
    assert g["hits"] == 2
    browser("admin").post(f"/ai/gaps/{pid}/{g['id']}/answer", data={"title": "Рассрочка", "body": "Да, на 3 месяца"})
    assert ai_kb.gaps(pid) == [] and db.val("SELECT body FROM ai_cards WHERE profile_id=%s", (pid,)) == "Да, на 3 месяца"
    ai_kb.note_gap(None, "Как оплатить?", "", "draft")                       # без профиля — в общую базу
    assert "Как оплатить?" in browser("admin").get("/ai").text


def test_g2_gaps_recorded_from_autopilot_draft_sandbox(request, monkeypatch):
    st = request.getfixturevalue("fake")
    u, a, lid = setup()
    pid = profile(user_id=u["id"])
    db.ex("UPDATE campaigns SET ai_profile_id=%s", (pid,))
    st["answer"] = "HANDOFF: нет данных о рассрочке"
    incoming(a, "А рассрочка есть?", 10)
    make_due(lid)
    send_due()
    assert ai_kb.gaps(pid)[0]["question"] == "А рассрочка есть?" and ai_kb.gaps(pid)[0]["source"] == "autopilot"
    st["answer"] = "Покажу примеры: [ссылка]"                                  # черновик с заготовкой
    incoming(a, "Покажите работы", 11)
    run(ai.draft(lid, u["id"]))
    assert any(g["question"] == "Покажите работы" and g["source"] == "draft" for g in ai_kb.gaps(pid))
    sid = sandbox.create(u["id"], pid, {"name": "Ира"}, "interested", "", "Здравствуйте!")
    st["answer"] = "Уточню у коллег и вернусь с ответом"
    run(sandbox.client_says(sid, "Есть ли гарантия?"))
    assert any(g["question"] == "Есть ли гарантия?" and g["source"] == "sandbox" for g in ai_kb.gaps(pid))
    assert autopilot.PLACEHOLDER_RE is ai.PLACEHOLDER_RE


def test_g2_purge_removes_gaps():
    u = make_user("admin")
    a, lid = _conversation(u)
    ai_kb.note_gap(None, "личный вопрос", "", "draft", lid)
    leads.purge(lid)
    assert ai_kb.gaps(None) == []


# ---------- G3: из переписок ----------
def test_g3_dialog_pairs_only_human_answers():
    u = make_user()
    a, lid = _conversation(u)                         # рассылка «Привет!» — не пример
    for d, text, src, mid in [("in", "Сколько стоит?", "incoming", 20), ("in", "И сроки?", "incoming", 21),
                              ("out", "От 1500, 2 недели", "inbox", 22), ("in", "Спасибо", "incoming", 23),
                              ("out", "ответ ИИ", "ai", 24), ("in", "ещё вопрос", "incoming", 25)]:
        inbox.record(a, lid, d, text, mid, src)
    pid = profile()
    pairs = ai_kb.dialog_pairs(pid, 30, "all")
    assert pairs == [{"question": "Сколько стоит?\nИ сроки?", "answer": "От 1500, 2 недели"}]
    assert ai_kb.dialog_pairs(pid, 30, "profile") == []                       # кампания не с этим профилем


def test_g3_mine_dialogs(kb_ai):
    u = make_user()
    a, lid = _conversation(u)
    inbox.record(a, lid, "in", "Сколько стоит?", 20, "incoming")
    inbox.record(a, lid, "out", "От 1500", 21, "telegram")
    pid = profile()
    run(ai_kb.mine_dialogs(pid, u["id"], 90, "all"))
    kinds = sorted(s["kind"] for s in ai_kb.suggestions(pid))
    assert kinds == ["card", "example"] and "КЛИЕНТ: Сколько стоит?" in kb_ai[-1]


def test_g3_nothing_to_mine(kb_ai):
    pid = profile()
    run(ai_kb.mine_dialogs(pid, None, 30, "profile"))
    assert "нет диалогов" in ai_kb.jobs[pid]["error"]


# ---------- G4: мастер ----------
def test_g4_wizard(kb_ai):
    u = make_user("admin")
    c = browser("admin")
    r = c.post("/ai/wizard/create", data={"name": "Сайты"}, follow_redirects=False)
    pid = db.val("SELECT id FROM ai_profiles WHERE name='Сайты'")
    assert r.headers["location"] == f"/ai/profiles/{pid}/wizard"
    html = c.get(f"/ai/profiles/{pid}/wizard").text
    assert "Кто пишет клиентам?" in html and html.count("wiz-step") >= 12
    run(ai_kb.wizard(pid, u["id"], {"who": "Дмитрий", "goal": "созвон", "links": "https://cal.example/dima"}))
    kinds = sorted(s["kind"] for s in ai_kb.suggestions(pid))
    assert kinds == ["card", "instruction"] and "Кто пишет клиентам?\nДмитрий" in kb_ai[-1]
    assert "Предложения на проверку" in c.get(f"/ai/profiles/{pid}").text


# ---------- G5 ----------
def test_g5_copy_cards():
    u = make_user("admin")
    p1, p2 = profile("A", u["id"]), profile("B", u["id"])
    c1 = db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Цена', '1500') RETURNING id", (p1,))
    c2 = db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Сроки', '2 нед') RETURNING id", (p1,))
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'цена', 'старое')", (p2,))
    browser("admin").post(f"/ai/profiles/{p1}/cards-copy", data={"target": p2, "card": [c1, c2]})
    assert sorted(r["title"] for r in db.q("SELECT title FROM ai_cards WHERE profile_id=%s", (p2,))) == ["Сроки", "цена"]
    make_user("anna", "manager")                                              # в чужой профиль — нельзя
    browser("anna").post(f"/ai/profiles/{p1}/cards-copy", data={"target": p2, "card": [c2]})
    assert db.val("SELECT COUNT(*) FROM ai_cards WHERE profile_id=%s", (p2,)) == 2


def test_g5_check(kb_ai):
    make_user("admin")
    pid = profile()
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Всё о нас', %s)", (pid, "x" * 900))
    issues = browser("admin").post(f"/ai/profiles/{pid}/check").json()["issues"]
    assert [i["type"] for i in issues] == ["длинная карточка", "дубль"]
