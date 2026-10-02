"""Фоновая отправка: у каждого аккаунта своя очередь, прогрев, лимиты, рабочие часы и паузы.

Лиды закреплены за аккаунтами, поэтому ограничение одного аккаунта (FloodWait, PEER_FLOOD)
останавливает только его очередь — чужие аккаунты его лидов не подхватывают.
"""
import asyncio
import math
import random
from datetime import date, datetime, time, timedelta


from . import autopilot, db
from .delivery import INTERRUPTED, send_followup, send_item
from .tg import tgm

state: dict[int, dict] = {}                 # account_id → {"status", "next_send_at"}
_wake: dict[int, asyncio.Event] = {}
_tasks: dict[int, asyncio.Task] = {}



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


def sent_today_split(account_id: int) -> tuple[int, int]:
    """Сообщения кампаний за сегодня: (первые, дожимы). Вместе расходуют дневной лимит; ответы из инбокса не считаются.
    «Сегодня» — по часовому поясу аккаунта."""
    tz = db.account_tz(account(account_id))
    start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)
    r = db.one("""SELECT COUNT(*) FILTER (WHERE COALESCE(step, 1) = 1) first, COUNT(*) FILTER (WHERE step > 1) fu
                  FROM messages WHERE account_id=%s AND direction='out' AND source='campaign'
                  AND created_at >= %s AND created_at < %s""", (account_id, start, start + timedelta(days=1)))
    return r["first"] or 0, r["fu"] or 0


def sent_today(account_id: int) -> int:
    return sum(sent_today_split(account_id))


def followup_share() -> int:
    """Какую долю дневного лимита дожимы могут занять, пока ждут первые сообщения (%)."""
    v = db.get_setting("followup_share")
    return min(int(v), 100) if v.isdigit() else 50


def followup_quota_used(acc: dict) -> bool:
    """Дожимы уже израсходовали свою долю лимита — дальше очередь первых сообщений (если она есть)."""
    _, fu = sent_today_split(acc["id"])
    return fu >= math.ceil(daily_limit(acc) * followup_share() / 100)


def in_work_hours(acc: dict, now: datetime | None = None) -> bool:
    now = (now or db.now_utc()).astimezone(db.account_tz(acc))
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


def cleanup() -> None:
    """Раз в сутки: журнал старше log_keep_days, отработанные задания автоответа и старые черновики ИИ."""
    days = db.get_setting("log_keep_days")
    days = int(days) if days.isdigit() and int(days) > 0 else 90
    n = db.changed("DELETE FROM event_log WHERE ts < now() - make_interval(days => %s)", (days,))
    db.ex("DELETE FROM ai_reply_jobs WHERE status NOT IN ('pending','sending') AND created_at < now() - make_interval(days => %s)", (days,))
    db.ex("DELETE FROM ai_drafts WHERE created_at < now() - make_interval(days => %s)", (days,))
    if n:
        db.log(f"Чистка: удалено записей журнала старше {days} дн. — {n}")


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
    last_cleanup = last_handoff_check = datetime.min
    while True:
        try:
            if datetime.now() - last_cleanup > timedelta(days=1):
                last_cleanup = datetime.now()
                cleanup()
            if datetime.now() - last_handoff_check > timedelta(hours=1):
                last_handoff_check = datetime.now()
                from . import notify
                notify.check_handoffs()
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


SYNC_EVERY = timedelta(minutes=15)


async def account_loop(account_id: int):
    last_read_sync = datetime.min
    last_sync = datetime.min          # первая сверка переписки — сразу после старта (A3)
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
            if acc.authorized and datetime.now() - last_sync > SYNC_EVERY:
                last_sync = datetime.now()
                try:
                    await acc.sync_recent()
                except Exception as e:
                    db.log(f"Сверка переписки: {type(e).__name__}: {e}", "warn", account_id)
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


def followups_due(account_id: int) -> int:
    return db.val("""SELECT COUNT(*) FROM campaign_leads cl JOIN campaigns c ON c.id=cl.campaign_id
                     WHERE cl.account_id=%s AND cl.next_step_at <= now() AND cl.state IN ('sent','read')
                       AND c.status='running'""", (account_id,)) or 0


def due_followup(account_id: int) -> dict | None:
    """Дожим, время которого пришло (в запущенных кампаниях). Идут раньше новых первых сообщений,
    но не дальше своей доли дневного лимита, если первые сообщения ждут (followup_quota_used)."""
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

    followup, first = due_followup(account_id), next_item(account_id)
    if followup and first and followup_quota_used(acc):
        followup = None         # дожимы взяли свою долю лимита — теперь очередь первых сообщений
    item = followup or first
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
