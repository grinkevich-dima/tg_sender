"""Автопилот: ИИ сам отвечает людям из кампаний, где это включено.

Человеческий ритм: заметил (1–10 мин) → прочитал (по длине) → подумал (по сложности) → «печатает…» (по длине ответа).
Новые сообщения клиента до ответа переносят ответ, а дописанное во время «печатает…» — выбрасывает черновик:
ИИ отвечает один раз на всё. Ждём, пока клиент замолчит, но не дольше WAIT_MAX от первого сообщения и не больше
MAX_RESTARTS перезапусков — так «пишущий по слову» клиент всё равно получит ответ.
Зовёт человека (и не отвечает), если: отказ / «не пишите», ИИ сам не может ответить по базе знаний,
исчерпан дневной лимит автоответов (человеку или аккаунту), менеджер вмешался в диалог, уже ждёт человека,
сообщение пришло давно (панель была выключена), ИИ или Telegram не дают ответить дольше часа.
"""
import asyncio
import random
import re
from datetime import datetime, timedelta

from . import ai, db, inbox
from . import leads as leads_mod
from .tasks import spawn

NOTICE = (60, 600)                  # «заметил сообщение», сек
READ_WPM = 220                      # скорость чтения, слов в минуту
THINK = {"question": (20, 60), "interest": (10, 30), "later": (10, 30)}
THINK_DEFAULT = (15, 45)
TYPE_CPS = (3.0, 5.0)               # скорость набора, символов в секунду
TYPE_MAX = 90                       # «печатает…» не дольше, сек
TOTAL_MAX = 15 * 60                 # пауза до ответа не больше 15 минут
JITTER = 0.25
WAIT_MAX = 20 * 60                  # клиент пишет и пишет: ответ откладываем не дольше, чем на столько от первого сообщения
MAX_RESTARTS = 2                    # сколько раз выбрасываем черновик из-за дописанного; дальше — отправляем
AI_RETRY = (60, 180, 300)           # ИИ недоступен: повторы через столько секунд, потом — человеку
FAIL_MAX = timedelta(hours=1)       # Telegram не даёт отправить дольше часа — человеку
STALE = timedelta(hours=6)          # сообщение старше (панель была выключена) — не автоответ, а человеку
HANDOFF_LABELS = {"stop": "просит не писать", "refusal": "отказ"}
MANUAL_CHECK_AFTER = 5              # через сколько секунд решаем, что своё сообщение написано руками в Telegram
AUTOPILOT_RULE = (
    "Ты отвечаешь клиенту сам, без проверки менеджером. Если ответить по базе знаний и переписке нельзя "
    "или нужен человек (жалоба, торг или просьба о скидке, нестандартный или сложный вопрос, просьба позвать менеджера, "
    "раздражение) — не отвечай клиенту, а напиши строго одну строку: HANDOFF: <причина, до 10 слов>.")

# ИИ пообещал уточнить — значит, ответ за человеком: ставим «нужен человек», чтобы клиент не повис
PROMISE_RE = re.compile(r"\b(уточн\w*|узна\w* у|спрош\w* у|верн\w* с ответ\w*|сверюсь|передам (?:вопрос|коллег))", re.I)

# ---------- проверка ответа перед отправкой (защита от манипуляций текстом клиента) ----------
MAX_REPLY = 900
URL_RE = re.compile(r"(?:https?://|t\.me/|www\.)[^\s<>«»\"')]+", re.I)
MONEY_RE = re.compile(r"(\d[\d\s.,]*\d|\d)\s*(%|процент\w*|byn|бел\.?\s*руб\w*|руб\w*|р\.|₽|\$|€|usd|eur|долл\w*|евро)", re.I)
AI_TALK_RE = re.compile(r"(как (?:ии|искусственный интеллект|языковая модель|нейросеть)|я\s*[—-]?\s*(?:ии|бот|нейросеть|языковая модель)\b"
                        r"|мои (?:инструкции|правила)|систем\w* (?:промпт|инструкц)|prompt)", re.I)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def check_reply(text: str, profile_id: int | None) -> str | None:
    """Почему этот автоответ нельзя отправлять без человека (None — можно).
    Ссылки и суммы — только те, что есть в базе знаний: так клиент не «уговорит» ИИ на скидку или чужую ссылку."""
    if len(text) > MAX_REPLY:
        return f"слишком длинный ответ ({len(text)} символов)"
    if AI_TALK_RE.search(text):
        return "ответ про ИИ или инструкции"
    kb = " ".join(f"{c['title']} {c['body']}" for c in db.q(
        "SELECT title, body FROM ai_cards WHERE profile_id IS NULL OR profile_id=%s", (profile_id,)))
    kb_low = kb.lower()
    for url in URL_RE.findall(text):
        url = url.rstrip(".,;:!?")
        if url.lower() not in kb_low:
            return f"ссылка не из базы знаний: {url}"
    kb_numbers = {_digits(m.group(0)) for m in re.finditer(r"\d[\d\s.,]*\d|\d", kb)}
    for m in MONEY_RE.finditer(text):
        if _digits(m.group(1)) not in kb_numbers:
            return f"сумма или процент не из базы знаний: {m.group(0).strip()}"
    return None


# заготовка вместо факта («[ссылка]», «[цена]») — у ИИ нет нужного факта в базе знаний; такое не отправляем
PLACEHOLDER_RE = re.compile(r"\[[^\[\]\n]{2,40}\]")



# ---------- человеческая задержка (чистые функции) ----------
def _jitter(x: float, rng=random) -> float:
    return x * rng.uniform(1 - JITTER, 1 + JITTER)


def reply_delay(text: str, label: str | None, rng=random, notice: bool = True) -> float:
    """Сколько ждать до начала ответа: заметить + прочитать + подумать."""
    words = len((text or "").split())
    read = words / READ_WPM * 60
    think = rng.uniform(*THINK.get(label, THINK_DEFAULT))
    total = (rng.uniform(*NOTICE) if notice else 0) + _jitter(read + think, rng)
    return min(total, TOTAL_MAX)


def typing_seconds(reply: str, rng=random) -> float:
    return min(len(reply or "") / rng.uniform(*TYPE_CPS), TYPE_MAX)


def next_work_time(acc: dict, now: datetime | None = None, rng=random) -> datetime:
    """Ближайшее начало рабочего дня аккаунта (+ несколько минут, как человек пришёл на работу)."""
    now = (now or db.now_utc()).astimezone(db.account_tz(acc))
    start = now.replace(hour=acc["work_start"].hour, minute=acc["work_start"].minute, second=0, microsecond=0)
    if start <= now:
        start += timedelta(days=1)
    return start + timedelta(minutes=rng.uniform(5, 40))


# ---------- решения ----------
def enabled() -> bool:
    return db.get_setting("ai_autopilot") == "1" and ai.configured()


def campaign_for_lead(lead_id: int) -> dict | None:
    """Кампания, из которой человеку писали последней (её настройки автоответа и профиль)."""
    return db.one("""SELECT c.*, cl.account_id AS cl_account_id FROM messages m
                     JOIN campaign_leads cl ON cl.id=m.campaign_lead_id JOIN campaigns c ON c.id=cl.campaign_id
                     WHERE m.lead_id=%s AND m.direction='out'
                     ORDER BY m.created_at DESC, m.id DESC LIMIT 1""", (lead_id,))


def handoff(lead_id: int, reason: str, account_id: int | None = None) -> None:
    """Передать диалог человеку: автоответ отменяется, в инбоксе — «нужен человек»."""
    db.ex("UPDATE leads SET ai_handoff=%s, ai_handoff_at=now() WHERE id=%s", (reason[:200], lead_id))
    db.ex("""UPDATE ai_reply_jobs SET status='handoff', reason=%s, done_at=now()
             WHERE lead_id=%s AND status IN ('pending','sending')""", (reason[:200], lead_id))
    db.log(f"Автопилот: лид #{lead_id} передан человеку — {reason}", "warn", account_id)


def manager_intervened(lead_id: int, how: str) -> None:
    """Менеджер сам написал человеку — дальше диалог ведёт он: автопилот для этого лида выключается."""
    camp = campaign_for_lead(lead_id)
    if not (camp and camp["ai_autoreply"]):
        return
    n = db.changed("UPDATE leads SET ai_paused=true, ai_handoff=NULL WHERE id=%s AND NOT ai_paused", (lead_id,))
    db.ex("""UPDATE ai_reply_jobs SET status='cancelled', reason=%s, done_at=now()
             WHERE lead_id=%s AND status IN ('pending','sending')""", (f"менеджер ответил {how}", lead_id))
    if n:
        db.log(f"Автопилот выключен для лида #{lead_id}: менеджер ответил {how}", "info")


def schedule(lead: dict, account_id: int, campaign: dict, text: str, label: str | None) -> int | None:
    """Поставить или перенести автоответ. Возвращает id задания."""
    now = db.now_utc()
    job = db.one("SELECT * FROM ai_reply_jobs WHERE lead_id=%s AND status='pending'", (lead["id"],))
    if job:      # клиент дописал — ответим на всё сразу, когда «дочитаем» новое, но не позже предела ожидания
        due = max(job["due_at"], now + timedelta(seconds=reply_delay(text, label, notice=False)))
        due = min(due, max(job["due_at"], job["created_at"] + timedelta(seconds=WAIT_MAX)))
        db.ex("UPDATE ai_reply_jobs SET due_at=%s WHERE id=%s", (due, job["id"]))
        return job["id"]
    due = now + timedelta(seconds=reply_delay(text, label))
    return db.ex("""INSERT INTO ai_reply_jobs(lead_id, account_id, campaign_id, profile_id, due_at)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id""",
                 (lead["id"], account_id, campaign["id"], campaign["ai_profile_id"], due))


async def after_incoming(message_id: int, lead_id: int, account_id: int, sent_at: datetime | None = None) -> None:
    """После входящего: разбор ИИ, затем — запланировать автоответ или позвать человека."""
    label = await ai.classify_message(message_id)
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lead_id,))
    if label == "stop" and lead and not lead["opted_out_at"]:
        # «не пишите мне» своими словами — отписка на всю команду, как стоп-слово (вернуть можно в «Лидах»)
        leads_mod.opt_out(lead_id, "ИИ: просит не писать")
        db.log(f"Лид #{lead_id} отписан: ИИ распознал просьбу не писать", "warn", account_id)
    if not enabled():
        return
    camp = campaign_for_lead(lead_id)
    if not (lead and camp and camp["ai_autoreply"]) or lead["ai_paused"]:
        return
    if lead["owner_account_id"] != account_id:
        return
    if label in HANDOFF_LABELS:
        handoff(lead_id, HANDOFF_LABELS[label], account_id)
        return
    if lead["opted_out_at"] or lead["ai_handoff"]:
        return          # отписан или уже ждёт человека — ИИ в этот диалог не вмешивается
    if sent_at and db.now_utc() - sent_at > STALE:
        hours = int((db.now_utc() - sent_at).total_seconds() // 3600)
        handoff(lead_id, f"сообщение пришло {hours} ч назад, пока панель не работала — ответьте сами", account_id)
        return
    text = db.val("SELECT text FROM messages WHERE id=%s", (message_id,)) or ""
    schedule(lead, account_id, camp, text, label)


# ---------- отправка ----------
def recover_interrupted() -> None:
    n = db.changed("""UPDATE ai_reply_jobs SET status='failed', reason='прервано при перезапуске', done_at=now()
                      WHERE status='sending'""")
    for j in db.q("SELECT DISTINCT lead_id FROM ai_reply_jobs WHERE reason='прервано при перезапуске' AND done_at > now() - interval '1 minute'"):
        db.ex("UPDATE leads SET ai_handoff='автоответ прерван перезапуском — проверьте диалог', ai_handoff_at=now() WHERE id=%s",
              (j["lead_id"],))
    if n:
        db.log(f"Автопилот: прервано автоответов при перезапуске — {n}, переданы человеку", "warn")


def _sent_today(lead_id: int) -> int:
    return db.val("""SELECT COUNT(*) FROM messages WHERE lead_id=%s AND source='ai'
                     AND created_at > now() - interval '24 hours'""", (lead_id,)) or 0


def _account_sent_today(account_id: int) -> int:
    return db.val("""SELECT COUNT(*) FROM messages WHERE account_id=%s AND source='ai'
                     AND created_at > now() - interval '24 hours'""", (account_id,)) or 0


def _retry_later(job: dict, until: datetime, reason: str) -> str:
    """Telegram не даёт ответить (сеть, ограничение, аккаунт не подключён): повторим позже,
    но если не получается дольше FAIL_MAX — человеку, чтобы клиент не ждал часами."""
    since = job.get("fail_since") or db.now_utc()
    if until - since > FAIL_MAX:
        handoff(job["lead_id"], f"автоответ не ушёл за час: {reason}"[:200], job["account_id"])
        return "handoff"
    db.ex("UPDATE ai_reply_jobs SET status='pending', due_at=%s, fail_since=%s, reason=%s WHERE id=%s",
          (until, since, reason[:200], job["id"]))
    return "postponed"


def _finish(job_id: int, status: str, reason: str | None = None, text: str | None = None) -> None:
    db.ex("UPDATE ai_reply_jobs SET status=%s, reason=%s, text=%s, done_at=now() WHERE id=%s",
          (status, reason, text, job_id))


async def process_due() -> int:
    """Запустить отправку автоответов, время которых пришло. Каждый — своей задачей (там «печатает…»)."""
    started = 0
    for job in db.q("SELECT * FROM ai_reply_jobs WHERE status='pending' AND due_at <= now() ORDER BY due_at LIMIT 20"):
        if not db.changed("UPDATE ai_reply_jobs SET status='sending' WHERE id=%s AND status='pending'", (job["id"],)):
            continue
        spawn(send_job(job), f"автоответ лиду #{job['lead_id']}")
        started += 1
    return started


async def send_job(job: dict) -> str:
    """Автоответ по заданию. Любая неожиданная ошибка — задание failed и «нужен человек», а не вечное «отправляется»."""
    try:
        return await _send_job(job)
    except Exception as e:
        _finish(job["id"], "failed", f"{type(e).__name__}: {e}"[:300])
        handoff(job["lead_id"], f"сбой автоответа: {type(e).__name__}", job["account_id"])
        db.log(f"Автоответ лиду #{job['lead_id']}: {type(e).__name__}: {e}", "error", job["account_id"])
        return "failed"


async def _send_job(job: dict) -> str:
    """Проверить, что отвечать всё ещё можно, написать ответ, «печатать» и отправить."""
    from .tg import tgm
    from .delivery import Skip, deliver, describe, error_kind, flood_pause
    from .worker import account, in_work_hours, paused_until
    jid, lead_id, acc_id = job["id"], job["lead_id"], job["account_id"]
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lead_id,))
    camp = db.one("SELECT * FROM campaigns WHERE id=%s", (job["campaign_id"],)) if job["campaign_id"] else None
    if not enabled() or not camp or not camp["ai_autoreply"] or not lead or lead["ai_paused"] or lead["opted_out_at"]:
        _finish(jid, "cancelled", "автоответ выключен или лид отписан")
        return "cancelled"
    manual = db.one("""SELECT 1 FROM messages WHERE lead_id=%s AND direction='out' AND source IN ('inbox','telegram')
                       AND created_at > %s""", (lead_id, job["created_at"]))
    if manual:
        manager_intervened(lead_id, "сам")
        return "cancelled"
    acc = account(acc_id)
    client = tgm.get(acc_id)
    pu = paused_until(acc) if acc else None
    if not acc or not client.authorized or acc["status"] != "active" or tgm.preparing_account(acc_id) or pu:
        if pu:
            return _retry_later(job, pu, f"аккаунт на паузе ({acc['pause_reason'] or 'ограничение Telegram'})")
        return _retry_later(job, db.now_utc() + timedelta(minutes=5), "аккаунт не подключён или занят")
    if not in_work_hours(acc):     # ночь — не сбой: отсчёт «не удаётся отправить» начинаем заново утром
        db.ex("UPDATE ai_reply_jobs SET status='pending', due_at=%s, fail_since=NULL WHERE id=%s", (next_work_time(acc), jid))
        return "postponed"
    cap = int(db.get_setting("ai_autopilot_daily") or 5)
    if _sent_today(lead_id) >= cap:
        handoff(lead_id, f"лимит автоответов ({cap} в сутки)", acc_id)
        return "handoff"
    acc_cap = int(db.get_setting("ai_autopilot_account_daily") or 50)
    if _account_sent_today(acc_id) >= acc_cap:
        handoff(lead_id, f"лимит автоответов аккаунта ({acc_cap} в сутки)", acc_id)
        return "handoff"

    started = db.now_utc()          # всё, что клиент пришлёт после этого момента, войдёт в следующий ответ (A1)
    msgs, _ = ai.build_messages(lead, job["profile_id"])
    msgs[0]["content"] += "\n\n" + AUTOPILOT_RULE
    try:
        text = (await ai.chat(msgs)).strip()
    except ai.AIError as e:
        attempt = (job.get("ai_attempts") or 0) + 1
        if attempt <= len(AI_RETRY):        # перегрузка и таймауты обычно проходят за минуту-другую
            db.ex("""UPDATE ai_reply_jobs SET status='pending', due_at=%s, ai_attempts=%s, reason=%s WHERE id=%s""",
                  (db.now_utc() + timedelta(seconds=AI_RETRY[attempt - 1]), attempt, f"ИИ: {e}"[:200], jid))
            return "retry"
        handoff(lead_id, f"ИИ недоступен: {e}"[:200], acc_id)
        from . import notify
        notify.admin(f"🤖 ИИ недоступен — автоответы передаются людям: {e}"[:400], key="ai-down")
        return "handoff"
    if not text or text.upper().startswith("HANDOFF"):
        handoff(lead_id, text.split(":", 1)[1].strip() if ":" in text else "ИИ не смог ответить", acc_id)
        return "handoff"
    hole = PLACEHOLDER_RE.search(text)
    if hole:
        handoff(lead_id, f"в базе знаний нет нужного факта: ИИ написал {hole.group(0)}", acc_id)
        return "handoff"
    problem = check_reply(text, job["profile_id"])
    if problem:
        handoff(lead_id, f"ответ ИИ не отправлен — {problem}", acc_id)
        return "handoff"
    can_restart = (job.get("restarts") or 0) < MAX_RESTARTS and \
        db.now_utc() - job["created_at"] < timedelta(seconds=WAIT_MAX)

    def still_ok():
        # пока сочиняли и «печатали»: менеджер мог ответить сам, клиент — дописать
        if db.one("""SELECT 1 FROM messages WHERE lead_id=%s AND direction='out' AND source IN ('inbox','telegram')
                     AND created_at > %s""", (lead_id, job["created_at"])):
            raise Skip("менеджер ответил сам")
        if can_restart and db.one("""SELECT 1 FROM messages WHERE lead_id=%s AND direction='in' AND created_at > %s""",
                                  (lead_id, started)):
            raise Skip("клиент дописал")
    try:
        # «печатает…» столько, сколько набирал бы человек
        sent = await deliver(client, lead, text, typing=typing_seconds(text), check_before_send=still_ok)
    except Skip as s:
        if s.reason == "клиент дописал":
            # черновик выбрасываем: ответим на всё сразу, когда «дочитаем» новое
            newer = db.val("""SELECT text FROM messages WHERE lead_id=%s AND direction='in'
                              ORDER BY created_at DESC, id DESC LIMIT 1""", (lead_id,)) or ""
            due = db.now_utc() + timedelta(seconds=reply_delay(newer, None, notice=False))
            db.ex("""UPDATE ai_reply_jobs SET status='pending', due_at=%s, restarts=restarts+1,
                     reason='клиент дописал — ответ переписывается' WHERE id=%s""", (due, jid))
            return "restarted"
        manager_intervened(lead_id, "сам")
        return "cancelled"
    except Exception as e:
        kind = error_kind(e)
        if kind in ("flood", "transient"):
            if kind == "flood":
                flood_pause(acc_id, e)
                acc = account(acc_id)
                return _retry_later(job, max(acc["paused_until"] or db.now_utc(), db.now_utc() + timedelta(minutes=10)),
                                    f"ограничение Telegram ({acc['pause_reason']})")
            return _retry_later(job, db.now_utc() + timedelta(minutes=2), "нет связи с Telegram")
        _finish(jid, "failed", describe(e)[:300])
        handoff(lead_id, f"не удалось отправить автоответ: {type(e).__name__}", acc_id)
        return "failed"
    inbox.record(acc_id, lead_id, "out", text, sent.id, "ai")
    _finish(jid, "sent", None, text)
    if PROMISE_RE.search(text):
        db.ex("UPDATE leads SET ai_handoff=%s, ai_handoff_at=now() WHERE id=%s",
              ("ИИ пообещал уточнить — нужен ответ человека", lead_id))
        return "sent"
    # клиент дописал, а перезапуски исчерпаны: это не вошло в ответ — планируем следующий
    newer = db.one("""SELECT text FROM messages WHERE lead_id=%s AND direction='in' AND created_at > %s
                      ORDER BY created_at DESC, id DESC LIMIT 1""", (lead_id, started))
    if newer:
        schedule(db.one("SELECT * FROM leads WHERE id=%s", (lead_id,)), acc_id, camp, newer["text"] or "", None)
    return "sent"


async def check_manual(account_id: int, lead_id: int, tg_message_id: int) -> None:
    """Своё сообщение пришло событием Telegram. Если через несколько секунд панель не узнала в нём своё
    (кампания, инбокс, автоответ) — значит, менеджер написал руками: выключаем автопилот для лида."""
    await asyncio.sleep(MANUAL_CHECK_AFTER)
    src = db.val("""SELECT source FROM messages WHERE account_id=%s AND lead_id=%s AND direction='out'
                    AND tg_message_id=%s""", (account_id, lead_id, tg_message_id))
    if src == "telegram":
        manager_intervened(lead_id, "в Telegram")
