"""Тренажёр ИИ: диалог с ботом в песочнице. В Telegram ничего не уходит.

Клиента играет человек или ИИ («клиент-робот») по выбранному характеру. Бот отвечает так же,
как в инбоксе (те же 4 слоя). Удачный или исправленный ответ можно сохранить примером профиля.
"""
from . import ai, db

PERSONAS = {
    "interested": ("Заинтересован", "Тебе понравился вебинар, ты хочешь узнать подробности и, скорее всего, записаться."),
    "price": ("Сомневается в цене", "Тебе интересно, но дорого: ты торгуешься, спрашиваешь про скидки и рассрочку, сравниваешь."),
    "questions": ("Засыпает вопросами", "Ты задаёшь много уточняющих вопросов: время, место, формат, программа, кто ведёт, что если не смогу."),
    "busy": ("Занят, отвечает коротко", "Ты очень занят, отвечаешь одним-двумя словами, не сразу понимаешь, о чём речь."),
    "annoyed": ("Раздражён", "Тебя раздражают сообщения, ты не помнишь, что был на вебинаре, и можешь попросить больше не писать."),
    "custom": ("Свой сценарий", ""),
}
ROBOT_PROMPT = (
    "Ты играешь роль клиента: ты был на вебинаре или семинаре и сейчас переписываешься в Telegram с менеджером. "
    "Твой характер: {persona} "
    "Пиши ровно одно короткое сообщение (1–2 предложения), как в мессенджере: просто, без официоза, можно без приветствия. "
    "Никогда не говори, что ты ИИ или что это игра. Если тебе всё ясно и больше нечего сказать — коротко заверши разговор.")
MAX_ROBOT_TURNS = 5


def get(sid: int) -> dict | None:
    return db.one("""SELECT s.*, p.name AS profile_name FROM ai_sandboxes s LEFT JOIN ai_profiles p ON p.id=s.profile_id
                     WHERE s.id=%s""", (sid,))


def messages(sid: int) -> list[dict]:
    return db.q("SELECT * FROM ai_sandbox_messages WHERE sandbox_id=%s ORDER BY id", (sid,))


def _history(sid: int) -> list[dict]:
    return [{"direction": "in" if m["role"] == "client" else "out", "text": m["text"]} for m in messages(sid)]


def fake_lead(client: dict) -> dict:
    """Вымышленный клиент в том виде, в каком ИИ видит настоящих лидов."""
    stage_id = db.val("SELECT id FROM funnel_stages WHERE name=%s", (client.get("stage"),)) if client.get("stage") else None
    extra = {k: client[k] for k in ("group", "joined") if client.get(k)}
    return {"id": 0, "first_name": client.get("name") or "Клиент", "last_name": "", "title": None, "extra": extra,
            "stage_id": stage_id, "note": client.get("note") or ""}


def create(user_id: int, profile_id: int | None, client: dict, persona: str, persona_text: str, opening: str) -> int:
    sid = db.ex("""INSERT INTO ai_sandboxes(user_id, profile_id, client, persona, persona_text)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (user_id, profile_id, db.jsonb(client), persona if persona in PERSONAS else "interested", persona_text.strip()))
    if opening.strip():          # первое сообщение от нас, как в кампании
        db.ex("INSERT INTO ai_sandbox_messages(sandbox_id, role, text) VALUES (%s, 'bot', %s)", (sid, opening.strip()))
    return sid


async def bot_reply(sid: int) -> dict:
    """Ответ бота на текущую переписку + что он видел (debug)."""
    s = get(sid)
    debug: dict = {}
    msgs, _ = ai.build_messages(fake_lead(s["client"]), s["profile_id"], history=_history(sid), debug=debug)
    text = await ai.chat(msgs)
    if not text:
        raise ai.AIError("ИИ вернул пустой ответ — попробуйте ещё раз")
    mid = db.ex("INSERT INTO ai_sandbox_messages(sandbox_id, role, text, debug) VALUES (%s, 'bot', %s, %s) RETURNING id",
                (sid, text, db.jsonb(debug)))
    return {"id": mid, "text": text}


async def client_says(sid: int, text: str, by_robot: bool = False) -> dict:
    """Сообщение клиента → разбор → ответ бота."""
    mid = db.ex("""INSERT INTO ai_sandbox_messages(sandbox_id, role, text, by_robot) VALUES (%s, 'client', %s, %s)
                   RETURNING id""", (sid, text.strip(), by_robot))
    try:
        label, note = await ai.classify(_history(sid))
        if label:
            db.ex("UPDATE ai_sandbox_messages SET label=%s, note=%s WHERE id=%s", (label, note, mid))
    except ai.AIError:
        pass                     # разбор — подсказка; без него диалог продолжается
    return await bot_reply(sid)


async def robot_turn(sid: int) -> str | None:
    """Клиент-робот пишет следующее сообщение по своему характеру, бот отвечает. None — робот завершил разговор."""
    s = get(sid)
    persona = s["persona_text"] if s["persona"] == "custom" and s["persona_text"] else PERSONAS.get(s["persona"], PERSONAS["interested"])[1]
    client = s["client"] or {}
    who = f"Тебя зовут {client.get('name') or 'клиент'}." + (f" Ты был в группе «{client['group']}»." if client.get("group") else "")
    transcript = "\n".join(f"{'Вы' if m['role'] == 'client' else 'Менеджер'}: {m['text']}" for m in messages(sid))
    text = await ai.chat([{"role": "system", "content": ROBOT_PROMPT.format(persona=persona) + " " + who},
                          {"role": "user", "content": f"Переписка:\n{transcript or '(пока пусто — начни разговор сам)'}\n\n"
                                                      "Напиши следующее сообщение клиента."}],
                         temperature=0.8, max_tokens=120)
    text = text.strip().strip('"«»')
    if text.lower().startswith(("вы:", "клиент:")):
        text = text.split(":", 1)[1].strip()
    if not text:
        return None
    await client_says(sid, text, by_robot=True)
    return text


def save_example(sid: int, message_id: int, corrected: str | None = None) -> str:
    """Ответ бота (или исправленный) → пример профиля. Возвращает ошибку или пустую строку."""
    s = get(sid)
    if not s["profile_id"]:
        return "Диалог без профиля: примеры сохраняются в профиль — выберите профиль при старте"
    m = db.one("SELECT * FROM ai_sandbox_messages WHERE id=%s AND sandbox_id=%s AND role='bot'", (message_id, sid))
    if not m:
        return "Сообщение не найдено"
    question = db.val("""SELECT text FROM ai_sandbox_messages WHERE sandbox_id=%s AND role='client' AND id < %s
                         ORDER BY id DESC LIMIT 1""", (sid, message_id))
    if not question:
        return "Перед этим ответом нет сообщения клиента — пример не из чего составить"
    answer = (corrected or "").strip() or m["text"]
    if corrected and corrected.strip() and corrected.strip() != m["text"]:
        debug = dict(m["debug"] or {}, original=m["text"])
        db.ex("UPDATE ai_sandbox_messages SET text=%s, debug=%s WHERE id=%s", (answer, db.jsonb(debug), message_id))
    db.ex("INSERT INTO ai_examples(profile_id, question, answer, source) VALUES (%s, %s, %s, 'sandbox')",
          (s["profile_id"], question, answer))
    db.ex("UPDATE ai_sandbox_messages SET saved=true WHERE id=%s", (message_id,))
    return ""
