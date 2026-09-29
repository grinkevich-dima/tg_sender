"""Фоновый воркер: отправка очереди с прогревом, лимитами и паузами."""
import asyncio
import random
from datetime import date, datetime, time, timedelta

from telethon import errors

from . import db
from .templating import render
from .tg import tg

wake = asyncio.Event()          # будим воркер при старте кампании / смене настроек
state = {"status": "запуск", "next_send_at": None}

# Ошибки, после которых конкретному получателю писать нельзя (помечаем failed и идём дальше)
RECIPIENT_ERRORS = (
    errors.UserIsBlockedError,
    errors.UserPrivacyRestrictedError,
    errors.InputUserDeactivatedError,
    errors.UserDeactivatedError,
    errors.UsernameNotOccupiedError,
    errors.UsernameInvalidError,
    errors.PeerIdInvalidError,
    errors.ChatWriteForbiddenError,
    errors.YouBlockedUserError,
)


# ---------- лимиты ----------
def daily_limit(today: date | None = None) -> int:
    s = db.all_settings()
    cap = int(s["daily_max"])
    if s["warmup_enabled"] != "1":
        return cap
    start = s["warmup_start_date"]
    today = today or db.now_local().date()
    day_n = (today - date.fromisoformat(start)).days if start else 0
    return min(cap, int(s["warmup_start"]) + int(s["warmup_step"]) * max(day_n, 0))


def sent_today() -> int:
    """Общий счётчик за день: кампании по шаблонам + списки из xlsx."""
    d = db.today()
    a = db.one("SELECT COUNT(*) AS n FROM messages WHERE sent_day=? AND status IN ('sent','read','replied')", (d,))["n"]
    b = db.one("SELECT COUNT(*) AS n FROM list_items WHERE sent_day=? AND state IN ('sent','read','replied')", (d,))["n"]
    return a + b


def warmup_day() -> int:
    start = db.get_setting("warmup_start_date")
    return (db.now_local().date() - date.fromisoformat(start)).days + 1 if start else 0


def in_work_hours(now: datetime | None = None) -> bool:
    now = now or db.now_local()
    a = time.fromisoformat(db.get_setting("work_start"))
    b = time.fromisoformat(db.get_setting("work_end"))
    t = now.time()
    return a <= t < b if a <= b else (t >= a or t < b)


def paused_until() -> datetime | None:
    v = db.get_setting("paused_until")
    if not v:
        return None
    dt = datetime.fromisoformat(v)
    return dt if dt > db.now_local() else None


async def _sleep(seconds: float):
    """Сон, который можно прервать через wake.set()."""
    wake.clear()
    try:
        await asyncio.wait_for(wake.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# ---------- основной цикл ----------
async def run():
    last_read_sync = datetime.min
    while True:
        try:
            delay = await tick()
            if (datetime.now() - last_read_sync) > timedelta(minutes=30) and await tg.authorized():
                last_read_sync = datetime.now()
                try:
                    await tg.refresh_reads()
                except Exception as e:
                    db.log(f"Синхронизация прочтений: {e}", "warn")
        except Exception as e:
            db.log(f"Воркер: непредвиденная ошибка {type(e).__name__}: {e}", "error")
            delay = 30
        await _sleep(delay)


def _finish_empty_campaigns():
    for c in db.q("SELECT id, name FROM campaigns WHERE status='running'"):
        left = db.one("SELECT COUNT(*) AS n FROM messages WHERE campaign_id=? AND status='queued'", (c["id"],))["n"]
        if left == 0:
            db.ex("UPDATE campaigns SET status='done', finished_at=? WHERE id=?", (db.now_utc(), c["id"]))
            db.log(f"Кампания «{c['name']}» завершена")
    for l in db.q("SELECT id, name FROM lists WHERE status='running'"):
        left = db.one("SELECT COUNT(*) AS n FROM list_items WHERE list_id=? AND state='queued'", (l["id"],))["n"]
        if left == 0:
            db.ex("UPDATE lists SET status='done' WHERE id=?", (l["id"],))
            db.log(f"Список «{l['name']}» отправлен полностью")


async def tick() -> float:
    """Одна итерация. Возвращает, сколько секунд спать до следующей."""
    state["next_send_at"] = None
    if not await tg.authorized():
        state["status"] = "аккаунт не авторизован"
        return 15

    pu = paused_until()
    if pu:
        state["status"] = f"пауза до {pu:%d.%m %H:%M} (ограничение Telegram)"
        return min(60, (pu - db.now_local()).total_seconds() + 1)

    _finish_empty_campaigns()
    msg = db.one("""SELECT m.*, c.template_id FROM messages m
                    JOIN campaigns c ON c.id=m.campaign_id
                    WHERE m.status='queued' AND c.status='running'
                    ORDER BY c.id, m.id LIMIT 1""")
    item = None if msg else db.one("""SELECT li.* FROM list_items li JOIN lists l ON l.id=li.list_id
                    WHERE li.state='queued' AND l.status='running'
                    ORDER BY l.id, li.order_idx, li.id LIMIT 1""")
    if not msg and not item:
        state["status"] = "очередь пуста"
        return 30

    if not in_work_hours():
        state["status"] = f"вне рабочих часов ({db.get_setting('work_start')}–{db.get_setting('work_end')})"
        return 60

    limit, done = daily_limit(), sent_today()
    if done >= limit:
        state["status"] = f"дневной лимит исчерпан ({done}/{limit})"
        return 300

    if msg:
        await send_one(msg)
    else:
        await send_list_item(item)

    s = db.all_settings()
    lo, hi = sorted((int(s["delay_min"]), int(s["delay_max"])))
    delay = random.uniform(lo, hi)
    state["next_send_at"] = db.now_local() + timedelta(seconds=delay)
    state["status"] = "отправка"
    return delay


async def send_one(msg) -> str:
    contact = db.one("SELECT * FROM contacts WHERE id=?", (msg["contact_id"],))
    if not contact or contact["opted_out"]:
        db.ex("UPDATE messages SET status='skipped', error='отписан/удалён' WHERE id=?", (msg["id"],))
        return "skipped"
    tpl = db.one("SELECT body FROM templates WHERE id=?", (msg["template_id"],))
    text = render(tpl["body"], db.contact_vars(contact))

    try:
        async with tg.lock:
            entity = await tg.resolve(contact)
            sent = await tg.client.send_message(entity, text)
    except (errors.FloodWaitError, errors.PeerFloodError) as e:
        return _flood(e)
    except RECIPIENT_ERRORS as e:
        _fail(msg["id"], type(e).__name__.replace("Error", ""))
        return "failed"
    except ValueError as e:
        _fail(msg["id"], f"получатель не найден: {e}")
        return "failed"
    except errors.RPCError as e:
        _fail(msg["id"], f"{type(e).__name__}: {e}")
        return "failed"

    peer_id = getattr(sent, "chat_id", None) or getattr(getattr(sent, "peer_id", None), "user_id", None)
    db.ex("""UPDATE messages SET status='sent', text=?, tg_message_id=?, peer_id=?, sent_at=?, sent_day=?, error=NULL
             WHERE id=?""", (text, sent.id, peer_id, db.now_utc(), db.today(), msg["id"]))
    _mark_warmup_start()
    return "sent"


def _mark_warmup_start():
    if not db.get_setting("warmup_start_date"):
        db.set_setting("warmup_start_date", db.today())


def _flood(e) -> str:
    if isinstance(e, errors.FloodWaitError):
        until = db.now_local() + timedelta(seconds=e.seconds + 5)
        db.set_setting("paused_until", until.isoformat(timespec="seconds"))
        db.log(f"FloodWait {e.seconds} сек — пауза до {until:%H:%M:%S}", "warn")
        return "flood"
    until = db.now_local() + timedelta(hours=24)
    db.set_setting("paused_until", until.isoformat(timespec="seconds"))
    db.ex("UPDATE campaigns SET status='paused' WHERE status='running'")
    db.ex("UPDATE lists SET status='paused' WHERE status='running'")
    db.log("PEER_FLOOD: Telegram ограничил отправку. Всё поставлено на паузу на 24 ч. "
           "Проверьте аккаунт через @SpamBot и снизьте лимиты.", "error")
    return "peerflood"


def _fail(message_id: int, err: str):
    db.ex("UPDATE messages SET status='failed', error=? WHERE id=?", (err[:300], message_id))


# ---------- строки списков из xlsx ----------
def _item_fail(item_id: int, err: str, state: str = "failed"):
    db.ex("UPDATE list_items SET state=?, error=? WHERE id=?", (state, err[:300], item_id))


async def send_list_item(item) -> str:
    text = (item["text"] or "").strip()
    if not text:
        _item_fail(item["id"], "нет текста", "skipped")
        return "skipped"
    if item["kind"] == "person" and item["peer_id"]:
        if db.one("SELECT 1 FROM contacts WHERE tg_user_id=? AND opted_out=1", (item["peer_id"],)):
            _item_fail(item["id"], "контакт отписался", "skipped")
            return "skipped"
    try:
        async with tg.lock:
            entity = await tg.resolve_item(item)
            reply_to = item["topic_id"] if item["topic_id"] and item["topic_id"] > 1 else None
            if type(entity).__name__ == "InputPeerChat":   # обычная группа — тем (форума) не бывает
                reply_to = None
            sent = await tg.client.send_message(entity, text, reply_to=reply_to, link_preview=True)
    except (errors.FloodWaitError, errors.PeerFloodError) as e:
        return _flood(e)
    except RECIPIENT_ERRORS as e:
        _item_fail(item["id"], type(e).__name__.replace("Error", ""))
        return "failed"
    except (errors.ChannelPrivateError, errors.ChatAdminRequiredError, errors.ChatRestrictedError,
            errors.SlowModeWaitError, errors.ChatSendPlainForbiddenError, errors.ChatWriteForbiddenError) as e:
        _item_fail(item["id"], f"{type(e).__name__.replace('Error', '')}: {e}")
        return "failed"
    except ValueError as e:
        _item_fail(item["id"], f"получатель не найден: {e}")
        return "failed"
    except errors.RPCError as e:
        _item_fail(item["id"], f"{type(e).__name__}: {e}")
        return "failed"

    db.ex("""UPDATE list_items SET state='sent', tg_message_id=?, sent_at=?, sent_day=?, error=NULL WHERE id=?""",
          (sent.id, db.now_utc(), db.today(), item["id"]))
    _mark_warmup_start()
    return "sent"
