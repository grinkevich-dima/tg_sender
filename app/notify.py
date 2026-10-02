"""Уведомления админу в Telegram: сообщение в «Избранное» аккаунта, выбранного в «Правилах».

Важное, что раньше было видно только в журнале: PEER_FLOOD, ИИ недоступен, упала фоновая задача,
накопились «нужен человек». Одинаковые уведомления — не чаще раза в REPEAT."""
import asyncio
import time

from . import db

REPEAT = 30 * 60          # сек
_last: dict[str, float] = {}


def account_id() -> int | None:
    v = db.get_setting("notify_account_id")
    return int(v) if v.isdigit() else None


def admin(text: str, key: str | None = None) -> bool:
    """Поставить уведомление в очередь. False — не отправляем (не настроено, повтор, нет цикла asyncio)."""
    aid = account_id()
    key = key or text[:60]
    if not aid or time.monotonic() - _last.get(key, -REPEAT) < REPEAT:
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    _last[key] = time.monotonic()
    from .tasks import spawn
    spawn(_send(aid, text), "уведомление админу", notify=False)
    return True


async def _send(aid: int, text: str) -> None:
    from .tg import tgm
    client = tgm.get(aid)
    if not client.authorized:
        return
    async with client.lock:
        await client.client.send_message("me", "🔔 TG Sender\n" + text)


def check_handoffs(threshold: int = 5) -> bool:
    """Раз в час: если за сутки скопилось много «нужен человек» — напомнить."""
    n = db.val("SELECT COUNT(*) FROM leads WHERE ai_handoff IS NOT NULL AND ai_handoff_at > now() - interval '24 hours'") or 0
    if n >= threshold:
        return admin(f"🙋 Ждут человека: {n} диалогов за сутки. Инбокс → фильтр «Нужен человек».", key="handoffs")
    return False


def reset() -> None:
    _last.clear()
