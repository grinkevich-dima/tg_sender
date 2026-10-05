"""Тренажёр ИИ: диалог, клиент-робот, разбор, «что видел ИИ», сохранение и исправление примеров, права."""
import pytest

from app import ai, db, sandbox
from tests.conftest import make_campaign, make_account, make_user, run
from tests.test_web import browser


@pytest.fixture
def fake(monkeypatch):
    """Модель-заглушка: бот, клиент-робот и разбор отвечают по-своему."""
    calls = {"bot": 0, "robot": 0, "classify": 0}

    async def chat(messages, **kw):
        system = messages[0]["content"]
        if system.startswith("Определи, что означает"):
            calls["classify"] += 1
            return '{"label": "question", "note": "спрашивает про запись"}'
        if system.startswith("Ты играешь роль клиента"):
            calls["robot"] += 1
            return f"«Клиент: а запись будет? ({calls['robot']})»"
        calls["bot"] += 1
        return f"Ответ бота {calls['bot']}"
    monkeypatch.setattr(ai, "AI_API_KEY", "k")
    monkeypatch.setattr(ai, "chat", chat)
    return calls


def _profile(card=True):
    pid = db.ex("INSERT INTO ai_profiles(name, instruction) VALUES ('Сайты', 'Пишет Дмитрий') RETURNING id")
    if card:
        db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Запись', 'Запись доступна 7 дней')", (pid,))
    return pid


def test_dialog_with_debug(fake):
    u = make_user()
    pid = _profile()
    sid = sandbox.create(u["id"], pid, {"name": "Ольга", "group": "Клуб предпринимателей", "stage": "интерес"},
                         "interested", "", "Ольга, спасибо, что вступили в клуб!")
    run(sandbox.client_says(sid, "А запись будет?"))
    msgs = sandbox.messages(sid)
    assert [(m["role"], m["text"]) for m in msgs] == [("bot", "Ольга, спасибо, что вступили в клуб!"),
                                                      ("client", "А запись будет?"), ("bot", "Ответ бота 1")]
    assert msgs[1]["label"] == "question" and msgs[1]["note"] == "спрашивает про запись"
    dbg = msgs[2]["debug"]
    assert dbg["profile"] == "Сайты" and dbg["cards"] == ["Запись"] and "Пишет Дмитрий" in dbg["system"]
    assert "Ольга" in dbg["person"] and "Этап воронки: интерес" in dbg["person"]


def test_robot_turns(fake):
    u = make_user()
    sid = sandbox.create(u["id"], None, {"name": "Пётр"}, "price", "", "")
    for _ in range(3):
        run(sandbox.robot_turn(sid))
    msgs = sandbox.messages(sid)
    assert [m["role"] for m in msgs] == ["client", "bot"] * 3
    assert msgs[0]["text"] == "а запись будет? (1)" and msgs[0]["by_robot"]     # кавычки и «Клиент:» срезаны
    assert fake["robot"] == 3 and fake["bot"] == 3 and fake["classify"] == 3


def test_save_and_fix_examples(fake):
    u = make_user()
    pid = _profile()
    sid = sandbox.create(u["id"], pid, {"name": "Ирина"}, "interested", "", "")
    run(sandbox.client_says(sid, "Сколько стоит?"))
    run(sandbox.client_says(sid, "А рассрочка есть?"))
    b1, b2 = [m["id"] for m in sandbox.messages(sid) if m["role"] == "bot"]
    assert sandbox.save_example(sid, b1) == ""
    assert sandbox.save_example(sid, b2, "Да, можно разбить на 3 платежа.") == ""
    ex = db.q("SELECT question, answer, source FROM ai_examples WHERE profile_id=%s ORDER BY id", (pid,))
    assert ex == [{"question": "Сколько стоит?", "answer": "Ответ бота 1", "source": "sandbox"},
                  {"question": "А рассрочка есть?", "answer": "Да, можно разбить на 3 платежа.", "source": "sandbox"}]
    fixed = db.one("SELECT text, debug, saved FROM ai_sandbox_messages WHERE id=%s", (b2,))
    assert fixed["text"] == "Да, можно разбить на 3 платежа." and fixed["debug"]["original"] == "Ответ бота 2" and fixed["saved"]
    # без профиля примеры некуда сохранять
    sid2 = sandbox.create(u["id"], None, {}, "interested", "", "")
    run(sandbox.client_says(sid2, "Привет"))
    bot = [m["id"] for m in sandbox.messages(sid2) if m["role"] == "bot"][0]
    assert "без профиля" in sandbox.save_example(sid2, bot)


def test_pages_rights_and_opening_from_campaign(fake):
    admin = make_user("admin")
    make_user("anna", "manager")
    pid = _profile()
    cid = make_campaign(admin["id"], [make_account(admin["id"])], body="{first_name}, спасибо за интерес!")
    db.ex("UPDATE campaigns SET ai_profile_id=%s WHERE id=%s", (pid, cid))
    a, m = browser("admin"), browser("anna")
    assert a.get("/ai/sandbox").status_code == 200
    r = m.post("/ai/sandbox/create", data={"profile_id": pid, "persona": "busy", "name": "Олег", "campaign_opening": "1"},
               follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    assert sandbox.messages(sid)[0]["text"] == "Олег, спасибо за интерес!"        # первое сообщение кампании
    assert m.post(f"/ai/sandbox/{sid}/say", data={"text": "кто это?"}).json() == {"ok": True}
    assert m.post(f"/ai/sandbox/{sid}/robot", data={"turns": "2"}).json() == {"ok": True, "turns": 2}
    html = m.get(f"/ai/sandbox/{sid}").text
    assert "кто это?" in html and "что видел ИИ" in html and "сохранить пример" not in html   # менеджер не правит чужой профиль
    last = db.val("SELECT MAX(id) FROM ai_sandbox_messages WHERE sandbox_id=%s", (sid,))
    assert m.post(f"/ai/sandbox/{sid}/retry/{last}").json() == {"ok": True}
    assert db.val("SELECT text FROM ai_sandbox_messages WHERE sandbox_id=%s ORDER BY id DESC LIMIT 1", (sid,)).startswith("Ответ бота")
    bot = db.val("SELECT id FROM ai_sandbox_messages WHERE sandbox_id=%s AND role='bot' AND debug IS NOT NULL ORDER BY id LIMIT 1", (sid,))
    m.post(f"/ai/sandbox/{sid}/save/{bot}")
    assert db.val("SELECT COUNT(*) FROM ai_examples") == 0
    a.post(f"/ai/sandbox/{sid}/save/{bot}", data={"corrected": "Это Дмитрий, мы знакомы по клубу 🙂"})
    assert db.val("SELECT answer FROM ai_examples") == "Это Дмитрий, мы знакомы по клубу 🙂"
    # чужой диалог менеджер не видит, админ видит
    sid_admin = sandbox.create(admin["id"], None, {}, "interested", "", "")
    assert m.get(f"/ai/sandbox/{sid_admin}", follow_redirects=False).status_code == 303
    assert a.get(f"/ai/sandbox/{sid}").status_code == 200
    m.post(f"/ai/sandbox/{sid}/delete")
    assert sandbox.get(sid) is None and db.val("SELECT COUNT(*) FROM ai_examples") == 1


def test_dialog_always_starts_with_bot(fake, monkeypatch):
    make_user("admin")
    pid = _profile()
    seen = []
    real = ai.chat

    async def spy(messages, **kw):
        seen.append(messages[-1]["content"])
        return await real(messages, **kw)
    monkeypatch.setattr(ai, "chat", spy)
    c = browser("admin")
    r = c.post("/ai/sandbox/create", data={"profile_id": pid, "persona": "interested", "name": "Ольга",
                                           "group": "Клуб предпринимателей", "campaign_opening": "1"}, follow_redirects=False)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    first, = sandbox.messages(sid)
    assert first["role"] == "bot" and first["text"] == "Ответ бота 1" and first["debug"]["profile"] == "Сайты"
    assert "[Служебно, не от клиента]: переписки ещё нет. Напиши наше первое сообщение" in seen[0]
    # дальше отвечаю как клиент
    c.post(f"/ai/sandbox/{sid}/say", data={"text": "Здравствуйте! А что за клуб?"})
    assert [m["role"] for m in sandbox.messages(sid)] == ["bot", "client", "bot"]
    # начало можно перегенерировать
    sid2 = int(c.post("/ai/sandbox/create", data={"profile_id": pid}, follow_redirects=False).headers["location"].rsplit("/", 1)[1])
    first2 = sandbox.messages(sid2)[0]
    assert c.post(f"/ai/sandbox/{sid2}/retry/{first2['id']}").json() == {"ok": True}
    assert len(sandbox.messages(sid2)) == 1
    # ошибка ИИ при старте — диалог создан, понятное сообщение
    async def broken(messages, **kw):
        raise ai.AIError("ИИ перегружен")
    monkeypatch.setattr(ai, "chat", broken)
    r = c.post("/ai/sandbox/create", data={"profile_id": pid}, follow_redirects=False)
    assert r.status_code == 303 and "/ai/sandbox/" in r.headers["location"]
