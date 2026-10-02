"""Доставка сообщений: одна функция отправки для кампаний, дожимов, ответов из инбокса и автоответов.

deliver() находит адресата, при необходимости показывает «печатает…» и отправляет; ошибки Telegram
одинаково разбираются error_kind()/describe(), пауза аккаунта при ограничении — flood_pause().
"""
import asyncio
from datetime import timedelta

from telethon import errors

from . import campaigns, db, inbox
from . import leads as leads_mod
from .tg import AccountClient, tgm

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
TYPING_CHECK = 3          # сек: как часто во время «печатает…» проверяем, можно ли ещё отправлять
INTERRUPTED = "прервано во время отправки — проверьте в Telegram, дошло ли сообщение"


class Skip(Exception):
    """Отправлять нельзя (отписан, стоп-лист, менеджер вмешался) — не ошибка Telegram."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def error_kind(e: BaseException) -> str:
    """flood — ограничение аккаунта; transient — сеть (повторить позже); recipient — этому адресату нельзя;
    not_found — адресат не найден; rpc — прочая ошибка Telegram; other — непредвиденная."""
    if isinstance(e, (errors.FloodWaitError, errors.PeerFloodError)):
        return "flood"
    if isinstance(e, TRANSIENT_ERRORS):
        return "transient"
    if isinstance(e, RECIPIENT_ERRORS):
        return "recipient"
    if isinstance(e, ValueError):
        return "not_found"
    if isinstance(e, errors.RPCError):
        return "rpc"
    return "other"


def describe(e: BaseException) -> str:
    kind = error_kind(e)
    if kind == "recipient":
        return f"{type(e).__name__.replace('Error', '')}: {e}"
    if kind == "not_found":
        return f"получатель не найден: {e}"
    return f"{type(e).__name__}: {e}"


def flood_pause(account_id: int, e) -> str:
    """Ограничение Telegram: пауза только этого аккаунта (его лиды остаются за ним)."""
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
    from . import notify
    notify.admin(f"⚠️ PEER_FLOOD на аккаунте #{account_id}: отправка с него остановлена на 24 ч. "
                 "Проверьте аккаунт через @SpamBot и снизьте лимиты.", key=f"peerflood-{account_id}")
    return "peerflood"


def user_id_of(entity) -> int | None:
    """ID пользователя из того, что вернул resolve (User, InputPeerUser или просто id)."""
    if isinstance(entity, int):
        return entity if entity > 0 else None
    uid = getattr(entity, "user_id", None)
    if uid is None and type(entity).__name__ == "User":
        uid = entity.id
    return uid


def _reply_to(entity, topic_id: int | None) -> int | None:
    if not topic_id or topic_id <= 1:           # тема «_1» — General, общий поток
        return None
    if type(entity).__name__ == "InputPeerChat":  # обычная группа — тем (форума) не бывает
        return None
    return topic_id


async def deliver(client: AccountClient, lead: dict, text: str, *, topic_id: int | None = None, typing: float = 0,
                  check_resolved=None, check_before_send=None):
    """Найти адресата и отправить. check_resolved(entity) и check_before_send() могут бросить Skip.
    typing — сколько секунд показывать «печатает…» перед отправкой (между поиском и отправкой lock отпускаем)."""
    async with client.lock:
        entity = await client.resolve(lead)
        if check_resolved:
            check_resolved(entity)
        if not typing and not check_before_send:
            return await client.client.send_message(entity, text, reply_to=_reply_to(entity, topic_id), link_preview=True)
    if check_before_send:
        check_before_send()
    if typing:
        # «печатает…» гаснет сразу, как только проверка скажет «не отправлять» (клиент дописал, менеджер ответил)
        async with client.client.action(entity, "typing"):
            end = asyncio.get_running_loop().time() + typing
            while (left := end - asyncio.get_running_loop().time()) > 0:
                await asyncio.sleep(min(TYPING_CHECK, left))
                if check_before_send:
                    check_before_send()
    async with client.lock:
        return await client.client.send_message(entity, text, reply_to=_reply_to(entity, topic_id), link_preview=True)


# ---------- кампании ----------
def _fail(cl_id: int, err: str, st: str = "failed"):
    db.ex("UPDATE campaign_leads SET state=%s, error=%s WHERE id=%s", (st, err[:300], cl_id))


async def send_item(item: dict) -> str:
    """Первое сообщение кампании. item — строка campaign_leads с назначенным аккаунтом, прочитанная вызывающим;
    отправляем, только если её состояние с тех пор не изменилось (двойной клик или гонка с воркером → busy)."""
    prev, cl_id, account_id = item["state"], item["id"], item["account_id"]
    if not account_id:
        return "no_account"
    if not db.changed("UPDATE campaign_leads SET state='sending' WHERE id=%s AND state=%s", (cl_id, prev)):
        return "busy"
    lead = db.one("SELECT * FROM leads WHERE id=%s", (item["lead_id"],))
    if lead["opted_out_at"] or leads_mod.in_stoplist(lead["tg_id"]):
        _fail(cl_id, "лид отписался", "skipped")
        return "skipped"
    if lead["owner_account_id"] and lead["owner_account_id"] != account_id:
        _fail(cl_id, "лид закреплён за другим аккаунтом", "skipped")
        return "skipped"
    blocked = campaigns.recontact_block(lead, cl_id)
    if blocked:
        _fail(cl_id, blocked, "skipped")
        return "skipped"
    text = campaigns.text_for(item, lead)
    if not text:
        _fail(cl_id, "нет текста", "skipped")
        return "skipped"
    client: AccountClient = tgm.get(account_id)
    if not client.authorized:
        db.ex("UPDATE campaign_leads SET state=%s WHERE id=%s", (prev, cl_id))
        return "no_account"

    def learn_id(entity):
        # адресат был указан только username/телефоном: запоминаем id, чтобы ловить прочтения/ответы/отписку
        uid = user_id_of(entity) if lead["kind"] != "chat" else None
        if uid and uid != lead["tg_id"]:
            twin = db.one("SELECT id, opted_out_at FROM leads WHERE tg_id=%s AND id!=%s", (uid, lead["id"]))
            if (twin and twin["opted_out_at"]) or leads_mod.in_stoplist(uid):
                raise Skip("лид отписался")
            if not twin:
                db.ex("UPDATE leads SET tg_id=%s WHERE id=%s", (uid, lead["id"]))
    try:
        sent = await deliver(client, lead, text, topic_id=item["topic_id"], check_resolved=learn_id)
    except Skip as s:
        _fail(cl_id, s.reason, "skipped")
        return "skipped"
    except Exception as e:
        kind = error_kind(e)
        if kind in ("flood", "transient"):
            db.ex("UPDATE campaign_leads SET state=%s WHERE id=%s", (prev, cl_id))
            if kind == "transient":
                raise
            return flood_pause(account_id, e)
        _fail(cl_id, describe(e))        # иначе строка навсегда осталась бы первой в очереди
        if kind == "other":
            db.log(f"Строка #{cl_id}: непредвиденная ошибка {describe(e)}", "error", account_id)
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
    """Следующий шаг цепочки лиду, который не ответил. Захват строки атомарный — без дублей."""
    if not db.changed("""UPDATE campaign_leads SET next_step_at=NULL WHERE id=%s AND next_step_at=%s
                         AND state IN ('sent','read')""", (cl["id"], cl["next_step_at"])):
        return "busy"
    lead = db.one("SELECT * FROM leads WHERE id=%s", (cl["lead_id"],))
    reason = campaigns.stop_reason(cl, lead) or ("в стоп-листе" if leads_mod.in_stoplist(lead["tg_id"]) else None)
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

    def restore():
        db.ex("UPDATE campaign_leads SET next_step_at=%s WHERE id=%s", (cl["next_step_at"], cl["id"]))
    if not client.authorized:
        restore()
        return "no_account"
    try:
        sent = await deliver(client, lead, text, topic_id=cl["topic_id"])
    except Exception as e:
        kind = error_kind(e)
        if kind in ("flood", "transient"):
            restore()
            if kind == "transient":
                raise
            return flood_pause(cl["account_id"], e)
        # получатель недоступен и т.п. — первое сообщение дошло, просто прекращаем дожимы
        db.ex("UPDATE campaign_leads SET chain_note=%s WHERE id=%s", (f"дожим {pos} не отправлен: {describe(e)}"[:300], cl["id"]))
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
    from .worker import account, paused_until
    cl = db.one("SELECT * FROM campaign_leads WHERE id=%s", (cl_id,))
    if not cl:
        return "error", "строка не найдена"
    if cl["state"] not in ("new", "queued", "failed", "skipped"):
        return "error", "эта строка уже отправлена"
    if cl["state"] != "queued" or not cl["account_id"]:
        n, _ = campaigns.enqueue(cl["campaign_id"], {"ids": [cl_id]})     # аккаунт — по тем же правилам, что и очередь
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


# ---------- инбокс ----------
async def send_reply(lead_id: int, text: str, user_id: int) -> str:
    """Ответ из инбокса: от аккаунта, за которым закреплён лид, только в уже начатую переписку.
    Дневной лимит не тратит (это не рассылка), но паузу аккаунта после ограничения Telegram соблюдает.
    Возвращает пустую строку при успехе или текст ошибки."""
    from .worker import account, paused_until
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lead_id,))
    text = (text or "").strip()
    if not lead or not text:
        return "Пустое сообщение"
    if lead["opted_out_at"] or leads_mod.in_stoplist(lead["tg_id"]):
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
        sent = await deliver(client, lead, text)
    except Exception as e:
        kind = error_kind(e)
        if kind == "flood":
            flood_pause(acc["id"], e)
            return f"Telegram ограничил отправку: {type(e).__name__.replace('Error', '')}"
        if kind == "transient":
            return "Нет связи с Telegram — повторите через минуту"
        if kind == "other":
            db.log(f"Ответ из инбокса лиду #{lead_id}: {describe(e)}", "error", acc["id"])
        return f"Не отправлено: {describe(e)}"
    inbox.record(acc["id"], lead_id, "out", text, sent.id, "inbox", sender_user_id=user_id)
    inbox.mark_read(lead_id)
    return ""
