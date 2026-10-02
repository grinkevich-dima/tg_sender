"""ИИ-помощник: черновики ответов и разбор входящих.

Запрос к модели собирается из четырёх слоёв:
  1. инструкция — общая команды + профиля кампании;
  2. база знаний — карточки (общие + профиля), подходящие к последним сообщениям;
  3. примеры — удачные ответы этого профиля;
  4. контекст человека — имя, этап, заметка, группа, история переписки.
Модель — любой OpenAI-совместимый API (по умолчанию Битрикс24 Вайбкод). ИИ ничего не отправляет сам.
"""
import asyncio
import json
import re
import urllib.error
import urllib.request
from difflib import SequenceMatcher

from . import db
from .config import AI_API_KEY, AI_BASE_URL, AI_MODEL

LABELS = {"interest": "интерес", "question": "вопрос", "later": "не сейчас", "refusal": "отказ", "stop": "просит не писать"}
LABEL_STAGE = {"interest": "интерес", "refusal": "отказ"}      # какой этап предложить по разбору
MAX_CARDS = 8
MAX_EXAMPLES = 8
HISTORY = 20
EXAMPLE_SIMILARITY = 0.9          # отправили почти без правок → удачный пример


class AIError(Exception):
    """Понятная пользователю ошибка ИИ (нет ключа, сервис недоступен и т.п.)."""


def configured() -> bool:
    return bool(AI_API_KEY)


# ---------- вызов модели ----------
def _post(path: str, payload: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{AI_BASE_URL}{path}", data=data, method="POST" if data else "GET",
                                 headers={"X-Api-Key": AI_API_KEY, "Authorization": f"Bearer {AI_API_KEY}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        if e.code == 429:
            raise AIError(f"ИИ перегружен (лимит запросов), повторите через {e.headers.get('Retry-After', 'минуту')} сек")
        if e.code in (401, 403):
            raise AIError("Ключ ИИ не подходит или у него нет доступа vibe:ai — проверьте VIBECODE_AI_API_KEY в .env")
        raise AIError(f"ИИ ответил ошибкой {e.code}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AIError(f"ИИ недоступен: {e}")


async def chat(messages: list[dict], *, temperature: float = 0.4, max_tokens: int = 500) -> str:
    if not configured():
        raise AIError("ИИ не настроен: впишите VIBECODE_AI_API_KEY в .env и перезапустите панель")
    d = await asyncio.to_thread(_post, "/chat/completions", {"model": AI_MODEL, "messages": messages,
                                                             "temperature": temperature, "max_tokens": max_tokens})
    try:
        return (d["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        raise AIError("ИИ вернул ответ в неожиданном формате")


async def status() -> str:
    """Проверка связи для страницы «ИИ»: пустая строка — всё в порядке."""
    if not configured():
        return "не настроен: нет VIBECODE_AI_API_KEY в .env"
    try:
        d = await asyncio.to_thread(_post, "/models", None, 15)
    except AIError as e:
        return str(e)
    ids = [m.get("id") for m in d.get("data", [])]
    return "" if AI_MODEL in ids else f"модели {AI_MODEL} нет среди доступных"


# ---------- слой 2: база знаний ----------
def _stems(text: str) -> set[str]:
    words = re.findall(r"[а-яёa-z0-9]{4,}", (text or "").lower())
    return {w[:6] for w in words}          # грубая основа: первые 6 букв


def pick_cards(profile_id: int | None, query: str) -> list[dict]:
    """Общие карточки и карточки профиля. Если их немного — все; иначе самые близкие к последним сообщениям."""
    cards = db.q("""SELECT * FROM ai_cards WHERE profile_id IS NULL OR profile_id=%s
                    ORDER BY profile_id NULLS FIRST, id""", (profile_id,))
    if len(cards) <= MAX_CARDS:
        return cards
    q = _stems(query)
    scored = sorted(cards, key=lambda c: -len(q & _stems(c["title"] + " " + c["body"])))
    return scored[:MAX_CARDS]


# ---------- слой 3: примеры ----------
def pick_examples(profile_id: int | None) -> list[dict]:
    return db.q("""SELECT question, answer FROM ai_examples WHERE active AND profile_id IS NOT DISTINCT FROM %s
                   ORDER BY source='manual' DESC, created_at DESC LIMIT %s""", (profile_id, MAX_EXAMPLES))


# ---------- слой 4: контекст человека ----------
def lead_profile(lead_id: int) -> int | None:
    """Профиль кампании, из которой человеку писали последней."""
    return db.val("""SELECT c.ai_profile_id FROM messages m JOIN campaign_leads cl ON cl.id=m.campaign_lead_id
                     JOIN campaigns c ON c.id=cl.campaign_id
                     WHERE m.lead_id=%s AND m.direction='out' AND c.ai_profile_id IS NOT NULL
                     ORDER BY m.created_at DESC, m.id DESC LIMIT 1""", (lead_id,))


def _history(lead_id: int) -> list[dict]:
    rows = db.q("""SELECT direction, text FROM messages WHERE lead_id=%s AND COALESCE(text,'') != ''
                   ORDER BY created_at DESC, id DESC LIMIT %s""", (lead_id, HISTORY))
    return list(reversed(rows))


def _person(lead: dict) -> str:
    extra = lead.get("extra") or {}
    stage = db.val("SELECT name FROM funnel_stages WHERE id=%s", (lead["stage_id"],)) if lead.get("stage_id") else None
    parts = [f"Имя: {' '.join(filter(None, [lead['first_name'], lead['last_name']])) or lead.get('title') or 'неизвестно'}"]
    if extra.get("group"):
        parts.append(f"Группа: {extra['group']}" + (f", вступил {extra['joined']}" if extra.get("joined") else ""))
    if stage:
        parts.append(f"Этап воронки: {stage}")
    if lead.get("note"):
        parts.append(f"Заметка менеджера: {lead['note']}")
    return "\n".join(parts)


SERVICE = "[Служебно, не от клиента]"


def dialog_turns(history: list[dict], ours: str = "assistant", theirs: str = "user") -> list[dict]:
    """Переписка → реплики с ролями: клиент — user, мы — assistant (для клиента-робота роли зеркальные).
    Несколько сообщений подряд от одной стороны склеиваются в одну реплику."""
    turns: list[dict] = []
    for r in history:
        role = theirs if r["direction"] == "in" else ours
        text = (r.get("text") or "").strip()
        if not text:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})
    return turns


def build_messages(lead: dict, profile_id: int | None, history: list[dict] | None = None,
                   debug: dict | None = None) -> tuple[list[dict], str]:
    """Сообщения для модели и последний вопрос клиента (для примеров).
    Переписка передаётся репликами с ролями: клиент — user, мы — assistant; служебные задания помечены.
    history — своя переписка вместо сохранённой (тренажёр); debug — сюда кладём, что попало в запрос."""
    base = db.get_setting("ai_base_instruction")
    profile = db.one("SELECT * FROM ai_profiles WHERE id=%s", (profile_id,)) if profile_id else None
    if history is None:
        history = _history(lead["id"])
    last_in = next((r["text"] for r in reversed(history) if r["direction"] == "in"), "")
    recent = " ".join(r["text"] for r in history[-4:])
    cards = pick_cards(profile_id, recent)
    examples = pick_examples(profile_id)
    name = (lead.get("first_name") or lead.get("title") or "клиент").strip()
    system = [base]
    if profile and profile["instruction"].strip():
        system.append(profile["instruction"].strip())
    system.append(f"Как устроена переписка ниже: реплики пользователя (user) — это сообщения клиента; клиента зовут {name}, "
                  f"обращайся к нему только так. Твои реплики (assistant) — это наши сообщения, их писали мы от имени автора "
                  "из инструкции; имя автора — не имя клиента. Ты пишешь следующее сообщение от нас клиенту. "
                  f"Сообщения с пометкой «{SERVICE}» — задания для тебя от системы, клиент их не писал и не видит.")
    system.append("Отвечай только готовым текстом сообщения для клиента — без кавычек, пометок, вариантов и заготовок "
                  "в квадратных скобках вроде [тема] или [имя]. Не придумывай ссылки, цены, даты и факты о клиенте: "
                  "используй только базу знаний, переписку и данные о клиенте.")
    system.append(f"Данные о клиенте:\n{_person(lead)}")
    if cards:
        system.append("База знаний:\n" + "\n\n".join(f"### {c['title']}\n{c['body']}" for c in cards))
    if examples:                                                  # слой 3: как мы обычно отвечаем
        system.append("Примеры наших ответов (только для стиля, это не текущая переписка):\n" + "\n\n".join(
            f"Клиент: {ex['question']}\nМы: {ex['answer']}" for ex in examples))
    msgs = [{"role": "system", "content": "\n\n".join(system)}]
    turns = dialog_turns(history)
    msgs += turns
    if not turns:   # переписки ещё нет — первое сообщение от нас
        msgs.append({"role": "user", "content": f"{SERVICE}: переписки ещё нет. Напиши наше первое сообщение этому человеку: "
                     "обратись по имени и мягко подведи к цели из инструкции. Где и как мы познакомились, упоминай только "
                     "если это есть в данных о клиенте (группа, заметка) — не выдумывай. Не больше 3–4 предложений."})
    elif turns[-1]["role"] == "assistant":   # последним писали мы — клиент ещё не ответил
        msgs.append({"role": "user", "content": f"{SERVICE}: клиент ещё не ответил на наше последнее сообщение. "
                     "Напиши следующее сообщение от нас — не повторяй уже сказанное."})
    if debug is not None:
        debug.update(profile=profile["name"] if profile else None, cards=[c["title"] for c in cards],
                     examples=len(examples), system=msgs[0]["content"], person=_person(lead),
                     turns=[{"role": m["role"], "text": m["content"]} for m in msgs[1:]])
    return msgs, last_in


async def draft(lead_id: int, user_id: int, profile_id: int | None = None) -> dict:
    """Черновик ответа для инбокса. Сохраняется, чтобы потом сравнить с отправленным."""
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lead_id,))
    if profile_id is None:
        profile_id = lead_profile(lead_id)
    msgs, question = build_messages(lead, profile_id)
    text = await chat(msgs)
    if not text:
        raise AIError("ИИ вернул пустой ответ — попробуйте ещё раз")
    did = db.ex("""INSERT INTO ai_drafts(lead_id, profile_id, user_id, question, draft) VALUES (%s,%s,%s,%s,%s)
                   RETURNING id""", (lead_id, profile_id, user_id, question, text))
    return {"id": did, "text": text, "profile_id": profile_id}


def learn_from_send(draft_id: int, lead_id: int, final: str) -> bool:
    """Ответ ушёл: сравниваем с черновиком. Почти без правок — удачный пример для профиля. True — добавлен пример."""
    d = db.one("SELECT * FROM ai_drafts WHERE id=%s AND lead_id=%s AND sent_at IS NULL", (draft_id, lead_id))
    if not d:
        return False
    sim = SequenceMatcher(None, d["draft"], final).ratio()
    db.ex("UPDATE ai_drafts SET final=%s, similarity=%s, sent_at=now() WHERE id=%s", (final, sim, draft_id))
    if sim >= EXAMPLE_SIMILARITY and d["question"]:
        db.ex("""INSERT INTO ai_examples(profile_id, question, answer, source, lead_id) VALUES (%s,%s,%s,'inbox',%s)""",
              (d["profile_id"], d["question"], final, lead_id))
        return True
    return False


# ---------- разбор входящего ----------
CLASSIFY_PROMPT = (
    "Определи, что означает последнее сообщение клиента в переписке. Ответь строго JSON без пояснений: "
    '{"label": "<interest|question|later|refusal|stop>", "note": "<до 10 слов: суть сообщения>"}. '
    "interest — проявил интерес, хочет участвовать/купить; question — задал вопрос; later — не сейчас, позже; "
    "refusal — отказывается, не интересно; stop — просит больше не писать.")


def parse_label(text: str) -> tuple[str | None, str]:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    label = d.get("label") if d.get("label") in LABELS else None
    return label, str(d.get("note") or "")[:200]


def labeled_transcript(rows: list[dict]) -> str:
    """Переписка с явной разметкой сторон — для разбора и подобных задач."""
    return "\n".join(f"{'КЛИЕНТ' if r['direction'] == 'in' else 'МЫ'}: {r['text']}" for r in rows if (r.get("text") or "").strip())


async def classify(rows: list[dict]) -> tuple[str | None, str]:
    """Разбор последнего сообщения клиента в переписке rows ({direction, text}, по времени)."""
    rows = rows[-8:]
    last = next((r["text"] for r in reversed(rows) if r["direction"] == "in"), "")
    answer = await chat([{"role": "system", "content": CLASSIFY_PROMPT},
                         {"role": "user", "content": f"Переписка (КЛИЕНТ — сообщения клиента, МЫ — наши):\n"
                                                     f"{labeled_transcript(rows)}\n\nОпредели смысл последнего сообщения "
                                                     f"КЛИЕНТА: «{last}»"}], temperature=0, max_tokens=80)
    return parse_label(answer)


async def classify_message(message_id: int) -> str | None:
    """Размечает входящее сообщение и сохраняет метку. Ошибки ИИ не мешают приёму сообщений."""
    m = db.one("SELECT * FROM messages WHERE id=%s AND direction='in'", (message_id,))
    if not m or not configured():
        return None
    rows = db.q("""SELECT direction, text FROM messages WHERE lead_id=%s AND id <= %s AND COALESCE(text,'') != ''
                   ORDER BY created_at DESC, id DESC LIMIT 8""", (m["lead_id"], message_id))
    try:
        label, note = await classify(list(reversed(rows)))
    except AIError as e:
        db.log(f"Разбор входящего: {e}", "warn")
        return None
    if label:
        db.ex("UPDATE messages SET ai_label=%s, ai_note=%s WHERE id=%s", (label, note, message_id))
    return label
