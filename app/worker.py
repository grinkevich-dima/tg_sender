"""Фоновая отправка: у каждого аккаунта своя очередь, прогрев, лимиты, рабочие часы и паузы.

Лиды закреплены за аккаунтами, поэтому ограничение одного аккаунта (FloodWait, PEER_FLOOD)
останавливает только его очередь — чужие аккаунты его лидов не подхватывают.
"""
import asyncio
import random
from datetime import date, datetime, time, timedelta

from telethon import errors

from . import autopilot, campaigns, db, inbox
from .tg import AccountClient, tgm

state: dict[int, dict] = {}                 # account_id → {"status", "next_send_at"}
_wake: dict[int, asyncio.Event] = {}
_tasks: dict[int, asyncio.Task] = {}

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
    errors.ChannelPrivateError,
    errors.ChatAdminRequiredError,
    errors.ChatRestrictedError,
    errors.SlowModeWaitError,
    errors.ChatSendPlainForbiddenError,
)
# Сетевые сбои: сообщение возвращаем в очередь и пробуем позже, а не помечаем ошибкой
TRANSIENT_ERRORS = (ConnectionError, OSError, asyncio.TimeoutError)
INTERRUPTED = "прервано во время отправки — проверьте в Telegram, дошло ли сообщение"


def wake(account_id: int | None = None) -> None:
    """Будим отправку аккаунта (или всех) после запуска кампании, смены настроек, входа."""
    for aid, ev in list(_wake.items()):
        if account_id is None or aid == account_id:
            ev.set()


# ---------- лимиты ----------
def account(account_id: int) -> dict | None:
    return db.one("SELECT * FROM tg_accounts WHERE id=%s", (account_id,))


def daily_limit(acc: dict, today: date | None = None) -> int:
    if not acc["warmup_enabled"]:
        return acc["daily_max"]
    today = today or db.today()
    start = acc["warmup_start_date"]
    day_n = (today - start).days if start else 0
    return min(acc["daily_max"], acc["warmup_start"] + acc["warmup_step"] * max(day_n, 0))


def warmup_day(acc: dict) -> int:
    start = acc["warmup_start_date"]
    return (db.today() - start).days + 1 if start else 0


def sent_today(account_id: int) -> int:
    """Сообщения кампаний за сегодня (первые и дожимы) — то, что расходует дневной лимит. Ответы из инбокса не считаются."""
    start = datetime.combine(db.today(), time.min, tzinfo=db.now_local().tzinfo)
    return db.val("""SELECT COUNT(*) FROM messages WHERE account_id=%s AND direction='out' AND source='campaign'
                     AND created_at >= %s AND created_at < %s""",
                  (account_id, start, start + timedelta(days=1))) or 0


def in_work_hours(acc: dict, now: datetime | None = None) -> bool:
    now = now or db.now_local()
    a, b, t = acc["work_start"], acc["work_end"], now.time()
    return a <= t < b if a <= b else (t >= a or t < b)


def paused_until(acc: dict) -> datetime | None:
    pu = acc["paused_until"]
    return pu if pu and pu > db.now_utc() else None


# ---------- основной цикл ----------
def recover_interrupted():
    """После падения/перезапуска посреди отправки неизвестно, ушло ли сообщение.
    Повторять нельзя (будет дубль) — помечаем ошибкой, решение за человеком."""
    n = db.changed("UPDATE campaign_leads SET state='failed', error=%s WHERE state='sending'", (INTERRUPTED,))
    if n:
        db.log(f"Найдено прерванных отправок: {n} — помечены ошибкой", "warn")


def finish_campaigns():
    for c in db.q("""SELECT id, name FROM campaigns c WHERE status='running' AND NOT EXISTS
                     (SELECT 1 FROM campaign_leads cl WHERE cl.campaign_id=c.id
                      AND (cl.state IN ('queued','sending') OR cl.next_step_at IS NOT NULL))"""):
        db.ex("UPDATE campaigns SET status='done', finished_at=now() WHERE id=%s", (c["id"],))
        db.log(f"Кампания «{c['name']}» отправлена полностью")


async def run():
    """Следит, чтобы у каждого подключённого аккаунта работал свой цикл отправки."""
    recover_interrupted()
    autopilot.recover_interrupted()
    while True:
        try:
            await autopilot.process_due()
            for aid, acc in list(tgm.accounts.items()):
                if acc.authorized and (aid not in _tasks or _tasks[aid].done()):
                    _wake[aid] = asyncio.Event()
                    _tasks[aid] = asyncio.create_task(account_loop(aid))
            finish_campaigns()
        except Exception as e:
            db.log(f"Воркер: {type(e).__name__}: {e}", "error")
        await asyncio.sleep(10)


async def stop():
    for t in _tasks.values():
        t.cancel()
    _tasks.clear()


async def account_loop(account_id: int):
    last_read_sync = datetime.min
    while True:
        try:
            delay = await tick(account_id)
            acc = tgm.get(account_id)
            if acc.authorized and datetime.now() - last_read_sync > timedelta(minutes=30):
                last_read_sync = datetime.now()
                try:
                    await acc.refresh_reads()
                except Exception as e:
                    db.log(f"Синхронизация прочтений: {e}", "warn", account_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            db.log(f"Отправка: непредвиденная ошибка {type(e).__name__}: {e}", "error", account_id)
            delay = 30
        ev = _wake.setdefault(account_id, asyncio.Event())
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def _status(account_id: int, text: str, next_at=None) -> None:
    state[account_id] = {"status": text, "next_send_at": next_at}


def due_followup(account_id: int) -> dict | None:
    """Дожим, время которого пришло (в запущенных кампаниях). Идут раньше новых первых сообщений."""
    return db.one("""SELECT cl.* FROM campaign_leads cl JOIN campaigns c ON c.id=cl.campaign_id
                     WHERE cl.account_id=%s AND cl.next_step_at <= now() AND cl.state IN ('sent','read')
                       AND c.status='running'
                     ORDER BY cl.next_step_at, cl.id LIMIT 1""", (account_id,))


def next_item(account_id: int) -> dict | None:
    return db.one("""SELECT cl.* FROM campaign_leads cl JOIN campaigns c ON c.id=cl.campaign_id
                     WHERE cl.account_id=%s AND cl.state='queued' AND c.status='running'
                     ORDER BY c.id, cl.order_idx NULLS LAST, cl.id LIMIT 1""", (account_id,))


async def tick(account_id: int) -> float:
    """Одна итерация отправки аккаунта. Возвращает, сколько секунд спать до следующей."""
    acc = account(account_id)
    client = tgm.get(account_id)
    if not acc or acc["status"] == "logged_out" or not client.authorized:
        _status(account_id, "аккаунт не авторизован")
        return 30
    if acc["status"] == "paused":
        _status(account_id, "аккаунт на паузе (вручную)")
        return 60
    if tgm.preparing_account(account_id):
        _status(account_id, "идёт поиск получателей — отправка подождёт")
        return 15
    pu = paused_until(acc)
    if pu:
        _status(account_id, f"пауза до {pu.astimezone(db.now_local().tzinfo):%d.%m %H:%M} ({acc['pause_reason'] or 'ограничение Telegram'})")
        return min(60, (pu - db.now_utc()).total_seconds() + 1)

    followup = due_followup(account_id)
    item = followup or next_item(account_id)
    if not item:
        _status(account_id, "очередь пуста")
        return 30
    if not in_work_hours(acc):
        _status(account_id, f"вне рабочих часов ({acc['work_start']:%H:%M}–{acc['work_end']:%H:%M})")
        return 60
    limit, done = daily_limit(acc), sent_today(account_id)
    if done >= limit:
        _status(account_id, f"дневной лимит исчерпан ({done}/{limit})")
        return 300

    res = await (send_followup(item) if followup else send_item(item))
    if res in ("stopped", "skipped_step", "busy"):     # ничего не отправили — следующий без паузы
        return 1
    if res in ("flood", "peerflood"):
        acc = account(account_id)
        _status(account_id, f"пауза до {acc['paused_until'].astimezone(db.now_local().tzinfo):%d.%m %H:%M} ({acc['pause_reason']})")
        return 5
    lo, hi = sorted((acc["delay_min"], acc["delay_max"]))
    delay = random.uniform(lo, hi)
    _status(account_id, "отправка", db.now_local() + timedelta(seconds=delay))
    return delay


# ---------- отправка одного сообщения ----------
def _fail(cl_id: int, err: str, st: str = "failed"):
    db.ex("UPDATE campaign_leads SET state=%s, error=%s WHERE id=%s", (st, err[:300], cl_id))


def _user_id(entity) -> int | None:
    """ID пользователя из того, что вернул resolve (User, InputPeerUser или просто id)."""
    if isinstance(entity, int):
        return entity if entity > 0 else None
    uid = getattr(entity, "user_id", None)
    if uid is None and type(entity).__name__ == "User":
        uid = entity.id
    return uid


def _flood(account_id: int, e) -> str:
    if isinstance(e, errors.FloodWaitError):
        until = db.now_utc() + timedelta(seconds=e.seconds + 5)
        db.ex("UPDATE tg_accounts SET paused_until=%s, pause_reason=%s WHERE id=%s",
              (until, f"FloodWait {e.seconds} сек", account_id))
        db.log(f"FloodWait {e.seconds} сек — пауза аккаунта", "warn", account_id)
        return "flood"
    until = db.now_utc() + timedelta(hours=24)
    db.ex("UPDATE tg_accounts SET paused_until=%s, pause_reason='PEER_FLOOD' WHERE id=%s", (until, account_id))
    db.log("PEER_FLOOD: Telegram ограничил отправку с этого аккаунта. Его очередь на паузе 24 ч, "
           "лиды остаются за ним. Проверьте аккаунт через @SpamBot и снизьте лимиты.", "error", account_id)
    return "peerflood"


async def send_item(item: dict) -> str:
    """item — строка campaign_leads с назначенным аккаунтом, прочитанная вызывающим. Отправляем, только
    если её состояние с тех пор не изменилось (двойной клик или гонка с воркером → busy)."""
    prev, cl_id, account_id = item["state"], item["id"], item["account_id"]
    if not account_id:
        return "no_account"
    if not db.changed("UPDATE campaign_leads SET state='sending' WHERE id=%s AND state=%s", (cl_id, prev)):
        return "busy"
    lead = db.one("SELECT * FROM leads WHERE id=%s", (item["lead_id"],))
    if lead["opted_out_at"]:
        _fail(cl_id, "лид отписался", "skipped")
        return "skipped"
    if lead["owner_account_id"] and lead["owner_account_id"] != account_id:
        _fail(cl_id, "лид закреплён за другим аккаунтом", "skipped")
        return "skipped"
    text = campaigns.text_for(item, lead)
    if not text:
        _fail(cl_id, "нет текста", "skipped")
        return "skipped"
    client: AccountClient = tgm.get(account_id)
    if not client.authorized:
        db.ex("UPDATE campaign_leads SET state=%s WHERE id=%s", (prev, cl_id))
        return "no_account"
    is_person = lead["kind"] != "chat"
    try:
        async with client.lock:
            entity = await client.resolve(lead)
            uid = _user_id(entity) if is_person else None
            if uid and uid != lead["tg_id"]:
                # адресат был указан только username/телефоном: запоминаем id, чтобы ловить прочтения/ответы/отписку
                twin = db.one("SELECT id, opted_out_at FROM leads WHERE tg_id=%s AND id!=%s", (uid, lead["id"]))
                if twin and twin["opted_out_at"]:
                    _fail(cl_id, "лид отписался", "skipped")
                    return "skipped"
                if not twin:
                    db.ex("UPDATE leads SET tg_id=%s WHERE id=%s", (uid, lead["id"]))
            reply_to = item["topic_id"] if item["topic_id"] and item["topic_id"] > 1 else None
            if type(entity).__name__ == "InputPeerChat":   # обычная группа — тем (форума) не бывает
                reply_to = None
            sent = await client.client.send_message(entity, text, reply_to=reply_to, link_preview=True)
    except (errors.FloodWaitError, errors.PeerFloodError) as e:
        db.ex("UPDATE campaign_leads SET state=%s WHERE id=%s", (prev, cl_id))
        return _flood(account_id, e)
    except TRANSIENT_ERRORS:
        db.ex("UPDATE campaign_leads SET state=%s WHERE id=%s", (prev, cl_id))
        raise
    except RECIPIENT_ERRORS as e:
        _fail(cl_id, f"{type(e).__name__.replace('Error', '')}: {e}")
        return "failed"
    except ValueError as e:
        _fail(cl_id, f"получатель не найден: {e}")
        return "failed"
    except errors.RPCError as e:
        _fail(cl_id, f"{type(e).__name__}: {e}")
        return "failed"
    except Exception as e:   # иначе строка навсегда остаётся первой в очереди и блокирует её
        _fail(cl_id, f"{type(e).__name__}: {e}")
        db.log(f"Строка #{cl_id}: непредвиденная ошибка {type(e).__name__}: {e}", "error", account_id)
        return "failed"

    nxt = campaigns.next_step(item["campaign_id"], 1)
    with db.tx():
        db.ex("""UPDATE campaign_leads SET state='sent', tg_message_id=%s, sent_at=now(), sent_day=%s, error=NULL,
                 step=1, next_step_at=%s, chain_note=NULL WHERE id=%s""",
              (sent.id, db.today(), db.now_utc() + timedelta(days=nxt["delay_days"]) if nxt else None, cl_id))
        inbox.record(account_id, lead["id"], "out", text, sent.id, "campaign", campaign_lead_id=cl_id, step=1)
        inbox.auto_stage(lead["id"], "contacted")
        db.ex("UPDATE leads SET owner_account_id=%s WHERE id=%s AND owner_account_id IS NULL", (account_id, lead["id"]))
        db.ex("UPDATE tg_accounts SET warmup_start_date=%s WHERE id=%s AND warmup_start_date IS NULL",
              (db.today(), account_id))
    return "sent"


def _schedule_after(cl: dict, position: int, note: str | None) -> None:
    """Шаг position считается пройденным (отправлен или пропущен) — ставим время следующего."""
    nxt = campaigns.next_step(cl["campaign_id"], position)
    db.ex("UPDATE campaign_leads SET step=%s, next_step_at=%s, chain_note=%s WHERE id=%s",
          (position, db.now_utc() + timedelta(days=nxt["delay_days"]) if nxt else None, note, cl["id"]))


async def send_followup(cl: dict) -> str:
    """Отправляет следующий шаг цепочки лиду, который не ответил. Захват строки атомарный: второй воркер
    или повторный вызов получит busy, а не отправит дубль."""
    if not db.changed("""UPDATE campaign_leads SET next_step_at=NULL WHERE id=%s AND next_step_at=%s
                         AND state IN ('sent','read')""", (cl["id"], cl["next_step_at"])):
        return "busy"
    lead = db.one("SELECT * FROM leads WHERE id=%s", (cl["lead_id"],))
    reason = campaigns.stop_reason(cl, lead)
    if reason:
        db.ex("UPDATE campaign_leads SET chain_note=%s WHERE id=%s", (f"дожимы остановлены: {reason}", cl["id"]))
        return "stopped"
    step = campaigns.next_step(cl["campaign_id"], cl["step"])
    if not step:
        return "stopped"
    pos = step["position"]
    if step["condition"] == "read_no_reply" and cl["state"] != "read":
        _schedule_after(cl, pos, f"шаг {pos} пропущен: не прочитал")
        return "skipped_step"
    if step["condition"] == "unread" and cl["state"] == "read":
        _schedule_after(cl, pos, f"шаг {pos} пропущен: уже прочитал")
        return "skipped_step"
    text = campaigns.text_for(cl, lead, pos)
    if not text:
        _schedule_after(cl, pos, f"шаг {pos} пропущен: нет текста")
        return "skipped_step"
    client: AccountClient = tgm.get(cl["account_id"])
    restore = lambda: db.ex("UPDATE campaign_leads SET next_step_at=%s WHERE id=%s", (cl["next_step_at"], cl["id"]))
    if not client.authorized:
        restore()
        return "no_account"
    try:
        async with client.lock:
            entity = await client.resolve(lead)
            reply_to = cl["topic_id"] if cl["topic_id"] and cl["topic_id"] > 1 else None
            if type(entity).__name__ == "InputPeerChat":
                reply_to = None
            sent = await client.client.send_message(entity, text, reply_to=reply_to, link_preview=True)
    except (errors.FloodWaitError, errors.PeerFloodError) as e:
        restore()
        return _flood(cl["account_id"], e)
    except TRANSIENT_ERRORS:
        restore()
        raise
    except Exception as e:      # получатель недоступен и т.п. — первое сообщение дошло, просто прекращаем дожимы
        db.ex("UPDATE campaign_leads SET chain_note=%s WHERE id=%s",
              (f"дожим {pos} не отправлен: {type(e).__name__.replace('Error', '')}: {e}"[:300], cl["id"]))
        return "failed"
    nxt = campaigns.next_step(cl["campaign_id"], pos)
    with db.tx():
        db.ex("""UPDATE campaign_leads SET state='sent', tg_message_id=%s, sent_at=now(), sent_day=%s, step=%s,
                 next_step_at=%s, chain_note=NULL WHERE id=%s""",
              (sent.id, db.today(), pos, db.now_utc() + timedelta(days=nxt["delay_days"]) if nxt else None, cl["id"]))
        inbox.record(cl["account_id"], lead["id"], "out", text, sent.id, "campaign", campaign_lead_id=cl["id"], step=pos)
    return "sent"


async def send_now(cl_id: int) -> tuple[str, str]:
    """«▶ Отправить сейчас» для одной строки: мимо лимита и рабочих часов, но по общим правилам
    закрепления, отписки и паузы аккаунта. Возвращает (результат, пояснение)."""
    cl = db.one("SELECT * FROM campaign_leads WHERE id=%s", (cl_id,))
    if not cl:
        return "error", "строка не найдена"
    if cl["state"] not in ("new", "queued", "failed", "skipped"):
        return "error", "эта строка уже отправлена"
    if cl["state"] != "queued" or not cl["account_id"]:
        # назначаем аккаунт по тем же правилам, что и очередь
        n, _ = campaigns.enqueue(cl["campaign_id"], {"ids": [cl_id]})
        cl = db.one("SELECT * FROM campaign_leads WHERE id=%s", (cl_id,))
        if not n:
            return "skipped", cl["error"] or "строку нельзя поставить в очередь"
    acc = account(cl["account_id"])
    if not tgm.get(acc["id"]).authorized:
        return "error", f"аккаунт «{acc['label'] or acc['id']}» не авторизован"
    if tgm.preparing_account(acc["id"]):
        return "error", "идёт поиск получателей — отправьте, когда он закончится"
    pu = paused_until(acc)
    if pu:
        return "error", f"аккаунт на паузе до {pu.astimezone(db.now_local().tzinfo):%d.%m %H:%M} из-за ограничения Telegram"
    res = await send_item(cl)
    cl = db.one("SELECT * FROM campaign_leads WHERE id=%s", (cl_id,))
    return res, cl["error"] or ""


async def send_reply(lead_id: int, text: str, user_id: int) -> str:
    """Ответ из инбокса: от аккаунта, за которым закреплён лид, только в уже начатую переписку.
    Дневной лимит не тратит (это не рассылка), но паузу аккаунта после ограничения Telegram соблюдает.
    Возвращает пустую строку при успехе или текст ошибки."""
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lead_id,))
    text = (text or "").strip()
    if not lead or not text:
        return "Пустое сообщение"
    if lead["opted_out_at"]:
        return "Лид отписался — писать ему нельзя"
    if not lead["owner_account_id"]:
        return "Лид ни за кем не закреплён"
    if not db.one("SELECT 1 FROM messages WHERE lead_id=%s", (lead_id,)):
        return "Переписки ещё нет — первое сообщение отправляется через кампанию"
    acc = account(lead["owner_account_id"])
    client = tgm.get(acc["id"])
    if not client.authorized:
        return f"Аккаунт «{acc['label'] or acc['id']}» не подключён"
    if tgm.preparing_account(acc["id"]):
        return "Аккаунт занят поиском — отправьте через минуту"
    pu = paused_until(acc)
    if pu:
        return f"Аккаунт на паузе до {pu.astimezone(db.now_local().tzinfo):%d.%m %H:%M} из-за ограничения Telegram"
    try:
        async with client.lock:
            entity = await client.resolve(lead)
            sent = await client.client.send_message(entity, text, link_preview=True)
    except (errors.FloodWaitError, errors.PeerFloodError) as e:
        _flood(acc["id"], e)
        return f"Telegram ограничил отправку: {type(e).__name__.replace('Error', '')}"
    except RECIPIENT_ERRORS as e:
        return f"Не отправлено: {type(e).__name__.replace('Error', '')}"
    except (ValueError, errors.RPCError) as e:
        return f"Не отправлено: {e}"
    inbox.record(acc["id"], lead_id, "out", text, sent.id, "inbox", sender_user_id=user_id)
    db.ex("UPDATE leads SET inbox_read_at=now() WHERE id=%s", (lead_id,))
    return ""
