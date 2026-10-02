"""ИИ: сборка запроса из 4 слоёв, черновик и обучение на отправленном, разбор входящих, страницы и права."""
import asyncio

import pytest

from app import ai, db
from app.tg import tgm
from tests.conftest import make_account, make_campaign, make_user, run
from tests.test_inbox import _conversation
from tests.test_web import browser


@pytest.fixture
def fake_ai(monkeypatch):
    """Подменяет модель: запоминает запросы и отвечает заданным текстом."""
    calls = []
    state = {"answer": "Здравствуйте! Запись будет доступна 7 дней."}

    async def chat(messages, **kw):
        calls.append(messages)
        if isinstance(state["answer"], Exception):
            raise state["answer"]
        return state["answer"]
    monkeypatch.setattr(ai, "AI_API_KEY", "test-key")
    monkeypatch.setattr(ai, "chat", chat)
    return calls, state


def profile(name="Вебинар: маркетинг", instruction="Пишет Дмитрий, ведущий вебинара."):
    return db.ex("INSERT INTO ai_profiles(name, instruction) VALUES (%s, %s) RETURNING id", (name, instruction))


def test_build_messages_has_all_four_layers():
    u = make_user()
    a, lid = _conversation(u)
    pid = profile()
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (NULL, 'Оплата', 'Оплата картой по ссылке pay.example')")
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Запись вебинара', 'Запись доступна 7 дней')", (pid,))
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Чужая карточка', 'не должна попасть')", (profile("Другой", ""),))
    db.ex("INSERT INTO ai_examples(profile_id, question, answer) VALUES (%s, 'Где запись?', 'Вот ссылка на запись')", (pid,))
    db.ex("UPDATE leads SET note='Хочет на семинар', extra='{\"group\": \"Вебинар\", \"joined\": \"20.09.2026\"}' WHERE id=%s", (lid,))
    run(tgm.get(a).handle_incoming(500, "А запись будет?", 10))
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lid,))
    msgs, question = ai.build_messages(lead, pid)
    system = msgs[0]["content"]
    assert db.get_setting("ai_base_instruction")[:30] in system                 # 1: общая
    assert "Пишет Дмитрий" in system                                            # 1: профиля
    assert "Запись доступна 7 дней" in system and "pay.example" in system       # 2: карточки профиля и общие
    assert "не должна попасть" not in system
    assert "Клиент: Где запись?\nМы: Вот ссылка на запись" in system                 # 3: примеры — в инструкции
    assert "Ира" in system and "Хочет на семинар" in system and "Вебинар, вступил 20.09.2026" in system   # 4
    # переписка — репликами с ролями: наши — assistant, клиента — user
    assert msgs[1:] == [{"role": "assistant", "content": "Привет, Ира!"}, {"role": "user", "content": "А запись будет?"}]
    assert "реплики пользователя (user) — это сообщения клиента" in system
    assert question == "А запись будет?"


def test_pick_cards_chooses_relevant_when_many():
    pid = profile()
    for i in range(12):
        db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, %s, 'прочее')", (pid, f"Карточка {i}"))
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, 'Стоимость семинара', 'Семинар стоит 150 BYN')", (pid,))
    picked = ai.pick_cards(pid, "Сколько стоит семинар?")
    assert len(picked) == ai.MAX_CARDS and picked[0]["title"] == "Стоимость семинара"


def test_lead_profile_from_last_campaign():
    u = make_user()
    a, lid = _conversation(u)
    assert ai.lead_profile(lid) is None
    pid = profile()
    db.ex("UPDATE campaigns SET ai_profile_id=%s", (pid,))
    assert ai.lead_profile(lid) == pid


def test_draft_and_learning(fake_ai):
    calls, state = fake_ai
    u = make_user()
    a, lid = _conversation(u)
    pid = profile()
    db.ex("UPDATE campaigns SET ai_profile_id=%s", (pid,))
    run(tgm.get(a).handle_incoming(500, "А запись будет?", 10))
    d = run(ai.draft(lid, u["id"]))
    assert d["text"] == state["answer"] and d["profile_id"] == pid and len(calls) >= 1
    assert ai.learn_from_send(d["id"], lid, state["answer"] + "!") is True        # почти без правок → пример
    ex = db.one("SELECT * FROM ai_examples WHERE source='inbox'")
    assert ex["profile_id"] == pid and ex["question"] == "А запись будет?"
    d2 = run(ai.draft(lid, u["id"]))
    assert ai.learn_from_send(d2["id"], lid, "Совсем другой ответ, переписанный менеджером целиком") is False
    assert db.val("SELECT COUNT(*) FROM ai_examples") == 1
    assert ai.learn_from_send(d2["id"], lid, "повтор") is False                  # один черновик — одна отправка


def test_parse_label():
    assert ai.parse_label('{"label": "interest", "note": "хочет на семинар"}') == ("interest", "хочет на семинар")
    assert ai.parse_label('Вот ответ: {"label":"stop","note":""} готово') == ("stop", "")
    assert ai.parse_label("непонятно") == (None, "")
    assert ai.parse_label('{"label": "maybe"}')[0] is None


def test_incoming_is_classified_in_background(fake_ai):
    calls, state = fake_ai
    state["answer"] = '{"label": "interest", "note": "хочет записаться"}'
    u = make_user()
    a, lid = _conversation(u)

    async def go():
        await tgm.get(a).handle_incoming(500, "Хочу на семинар, как записаться?", 20)
        for _ in range(5):
            await asyncio.sleep(0)
    run(go())
    m = db.one("SELECT ai_label, ai_note FROM messages WHERE tg_message_id=20")
    assert m == {"ai_label": "interest", "ai_note": "хочет записаться"}
    # ошибка ИИ не ломает приём
    state["answer"] = ai.AIError("ИИ недоступен")
    run(tgm.get(a).handle_incoming(500, "ещё сообщение", 21))
    assert db.one("SELECT 1 FROM messages WHERE tg_message_id=21")


def test_not_configured():
    assert not ai.configured()
    assert "не настроен" in run(ai.status())
    with pytest.raises(ai.AIError, match="не настроен"):
        run(ai.chat([{"role": "user", "content": "x"}]))


def test_inbox_draft_send_learn_and_apply_label(fake_ai):
    calls, state = fake_ai
    u = make_user("admin")
    a, lid = _conversation(u)
    run(tgm.get(a).handle_incoming(500, "Интересно, запишите меня", 30))
    mid = db.val("SELECT id FROM messages WHERE tg_message_id=30")
    db.ex("UPDATE messages SET ai_label='interest', ai_note='хочет записаться' WHERE id=%s", (mid,))
    c = browser("admin")
    html = c.get(f"/inbox/{lid}").text
    assert "Предложить ответ" in html and "ИИ: <b>интерес</b>" in html and "→ этап «интерес»" in html
    d = c.post(f"/inbox/{lid}/draft", data={"profile_id": "0"}).json()
    assert d["text"] == state["answer"]
    c.post(f"/inbox/{lid}/send", data={"text": state["answer"], "draft_id": d["id"]})
    assert db.val("SELECT COUNT(*) FROM ai_examples WHERE source='inbox'") == 1
    c.post(f"/inbox/{lid}/apply-label/{mid}")
    assert db.val("SELECT s.name FROM leads l JOIN funnel_stages s ON s.id=l.stage_id WHERE l.id=%s", (lid,)) == "интерес"
    db.ex("UPDATE messages SET ai_label='stop' WHERE id=%s", (mid,))
    c.post(f"/inbox/{lid}/apply-label/{mid}")
    assert db.val("SELECT opted_out_at IS NOT NULL FROM leads WHERE id=%s", (lid,))
    # ошибка ИИ — понятный текст, не 500
    state["answer"] = ai.AIError("ИИ перегружен")
    assert c.post(f"/inbox/{lid}/draft", data={"profile_id": "0"}).json() == {"error": "ИИ перегружен"}


def test_ai_pages_and_rights(fake_ai):
    calls, state = fake_ai
    make_user("admin")
    make_user("anna", "manager")
    admin, anna = browser("admin"), browser("anna")
    admin.post("/ai/profiles/create", data={"name": "Семинар"})
    pid = db.val("SELECT id FROM ai_profiles WHERE name='Семинар'")
    admin.post(f"/ai/profiles/{pid}", data={"name": "Семинар", "instruction": "Пишет Анна"})
    admin.post(f"/ai/cards/{pid}/add", data={"title": "Цена", "body": "150 BYN"})
    admin.post("/ai/cards/0/add", data={"title": "Оплата", "body": "картой"})
    admin.post(f"/ai/profiles/{pid}/examples/add", data={"question": "Сколько стоит?", "answer": "150 BYN"})
    assert db.val("SELECT COUNT(*) FROM ai_cards") == 2 and db.val("SELECT COUNT(*) FROM ai_examples") == 1
    for url in ["/ai", f"/ai/profiles/{pid}"]:
        assert admin.get(url).status_code == 200 and anna.get(url).status_code == 200
    assert admin.post(f"/ai/profiles/{pid}/try", data={"question": "Сколько стоит?"}).json() == {"text": state["answer"]}
    # менеджер не меняет общую инструкцию, общие карточки и чужой профиль
    anna.post("/ai/base", data={"instruction": "взлом"})
    anna.post("/ai/cards/0/add", data={"title": "x", "body": "y"})
    anna.post(f"/ai/profiles/{pid}", data={"name": "Мой", "instruction": "x"})
    assert db.get_setting("ai_base_instruction") != "взлом" and db.val("SELECT COUNT(*) FROM ai_cards") == 2
    assert db.val("SELECT name FROM ai_profiles WHERE id=%s", (pid,)) == "Семинар"
    # профиль в кампании
    u = db.one("SELECT * FROM users WHERE login='admin'")
    cid = make_campaign(u["id"], [make_account(u["id"])])
    admin.post(f"/campaigns/{cid}/ai-profile", data={"ai_profile_id": str(pid)})
    assert db.val("SELECT ai_profile_id FROM campaigns WHERE id=%s", (cid,)) == pid
    admin.post(f"/ai/profiles/{pid}/delete")
    assert db.val("SELECT ai_profile_id FROM campaigns WHERE id=%s", (cid,)) is None


def test_no_invented_context_in_opening():
    """Без данных о знакомстве бот не должен сочинять «вы были на вебинаре» и оставлять заготовки."""
    assert "вебинар" not in db.get_setting("ai_base_instruction")
    lead = {"id": 0, "first_name": "Ирина", "last_name": "", "title": None, "extra": {}, "stage_id": None, "note": ""}
    msgs, _ = ai.build_messages(lead, None, history=[])
    assert "не выдумывай" in msgs[-1]["content"] and "квадратных скобках" in msgs[0]["content"]


def test_dialog_turns_roles_and_merging():
    hist = [{"direction": "out", "text": "Здравствуйте!"}, {"direction": "in", "text": "Добрый день."},
            {"direction": "in", "text": "Вы кто?"}, {"direction": "out", "text": "Я Дмитрий."}, {"direction": "in", "text": "  "}]
    assert ai.dialog_turns(hist) == [{"role": "assistant", "content": "Здравствуйте!"},
                                    {"role": "user", "content": "Добрый день.\nВы кто?"},      # подряд — одной репликой
                                    {"role": "assistant", "content": "Я Дмитрий."}]
    # для клиента-робота — зеркально
    assert ai.dialog_turns(hist, ours="user", theirs="assistant")[0]["role"] == "user"
    # последним писали мы — служебное задание, помеченное как не от клиента
    lead = {"id": 0, "first_name": "Ирина", "last_name": "", "title": None, "extra": {}, "stage_id": None, "note": ""}
    msgs, last_in = ai.build_messages(lead, None, history=hist[:4])
    assert msgs[-1]["role"] == "user" and msgs[-1]["content"].startswith("[Служебно, не от клиента]: последним писали мы")
    assert last_in == "Вы кто?"
    msgs, _ = ai.build_messages(lead, None, history=hist[:3])
    assert msgs[-1] == {"role": "user", "content": "Добрый день.\nВы кто?"}       # ответ на реплику клиента — без заданий


def test_classify_marks_sides(monkeypatch):
    seen = []

    async def chat(messages, **kw):
        seen.append(messages[-1]["content"])
        return '{"label": "question", "note": "x"}'
    monkeypatch.setattr(ai, "chat", chat)
    run(ai.classify([{"direction": "out", "text": "Есть задача по сайту?"}, {"direction": "in", "text": "А вы кто?"}]))
    assert "МЫ: Есть задача по сайту?\nКЛИЕНТ: А вы кто?" in seen[0] and "последнего сообщения КЛИЕНТА: «А вы кто?»" in seen[0]


def test_we_wrote_last_live_chat_vs_silence():
    """Последним писали мы: минуту назад — продолжить разговор, а не «вы там как?»; дни назад — мягко напомнить."""
    from datetime import timedelta
    lead = {"id": 0, "first_name": "Рома", "last_name": "", "title": None, "extra": {}, "stage_id": None, "note": ""}
    now = db.now_utc()
    hist = [{"direction": "in", "text": "теперь я 44)))", "created_at": now - timedelta(minutes=8)},
            {"direction": "out", "text": "видимо пропихнули и обратно отправили)", "created_at": now - timedelta(minutes=1)}]
    task = ai.build_messages(lead, None, history=hist)[0][-1]["content"]
    assert "разговор идёт сейчас" in task and "Не напоминай о себе" in task
    hist[-1]["created_at"] = now - timedelta(days=3)
    task = ai.build_messages(lead, None, history=hist)[0][-1]["content"]
    assert "3 дн назад, ответа нет" in task
    assert "Тон бери из этой переписки" in ai.build_messages(lead, None, history=hist)[0][0]["content"]
