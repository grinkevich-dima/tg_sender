"""Общее для страниц: шаблоны, одноразовые сообщения после редиректа, проверка прав."""
import json
from datetime import date, datetime
from urllib.parse import quote, unquote, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth, db
from ..campaigns import KIND_RU
from ..config import BASE_DIR, TZ

STATUS_RU = {"draft": "черновик", "running": "идёт", "paused": "пауза", "done": "завершена"}
STATE_RU = {"new": "новая", "queued": "в очереди", "sending": "отправляется", "sent": "доставлено",
            "read": "прочитано", "replied": "ответил", "failed": "ошибка", "skipped": "пропущено"}
ACCOUNT_STATUS_RU = {"new": "не подключён", "active": "работает", "paused": "пауза", "logged_out": "вышел"}

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals.update(STATUS_RU=STATUS_RU, STATE_RU=STATE_RU, KIND_RU=KIND_RU,
                             ACCOUNT_STATUS_RU=ACCOUNT_STATUS_RU, ROLES=auth.ROLES)


def _dt(v, fmt="%d.%m %H:%M"):
    if not v:
        return ""
    if isinstance(v, datetime):
        return (v.astimezone(TZ) if v.tzinfo else v).strftime(fmt)
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    return str(v)


templates.env.filters["dt"] = _dt


def tg_app_link(peer_id: int | None, username: str | None, message_id: int | None) -> str | None:
    """Ссылка, которая открывает группу в приложении Telegram (Desktop, мобильное).
    Публичная — по username; без username — через номер сообщения (иначе приложение чат не откроет)."""
    if username:
        return f"tg://resolve?domain={username}"
    if peer_id and message_id and peer_id < -10**12:          # супергруппа: marked id = -(10^12 + id канала)
        return f"tg://privatepost?channel={-peer_id - 10**12}&post={message_id}"
    return None


templates.env.globals["tg_app_link"] = tg_app_link

FLASH_COOKIE = "flash"


def page(request: Request, name: str, **ctx):
    # одноразовое сообщение после редиректа: читаем из cookie и сразу удаляем
    flash = {}
    raw = request.cookies.get(FLASH_COOKIE)
    if raw:
        try:
            flash = json.loads(unquote(raw))
        except ValueError:
            flash = {}
    user = auth.current_user(request)
    unread = 0
    if user:
        from .. import inbox
        unread = inbox.unread_count(None if auth.is_admin(user) else [a["id"] for a in auth.user_accounts(user)])
    resp = templates.TemplateResponse(request, name, {"user": user, "is_admin": auth.is_admin(user),
                                                      "flash": flash, "menu_unread": unread, **ctx})
    if raw:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def back(url: str, msg: str = "", err: str = ""):
    """Редирект после POST (PRG). Сообщение передаём через cookie, а не в адресной строке."""
    resp = RedirectResponse(url, 303)
    data = {k: v for k, v in {"msg": msg, "err": err}.items() if v}
    if data:
        resp.set_cookie(FLASH_COOKIE, quote(json.dumps(data, ensure_ascii=False)), max_age=60,
                        path="/", httponly=True, samesite="lax")
    return resp


def local_url(url: str | None, default: str) -> str:
    """Только путь внутри панели — чтобы редирект по Referer/next не уводил на чужой сайт."""
    if not url:
        return default
    u = urlsplit(url)
    path = u.path if u.path.startswith("/") and not u.path.startswith("//") else default
    return path + (f"?{u.query}" if u.query else "")


def user(request: Request) -> dict:
    return auth.current_user(request)


def eta_days(queued: int, per_day: int) -> int:
    return -(-queued // max(per_day, 1)) if queued else 0


def log_rows(limit: int, account_ids: list[int] | None = None):
    if account_ids is None:
        return db.q("""SELECT e.*, a.label FROM event_log e LEFT JOIN tg_accounts a ON a.id=e.account_id
                       ORDER BY e.id DESC LIMIT %s""", (limit,))
    return db.q("""SELECT e.*, a.label FROM event_log e LEFT JOIN tg_accounts a ON a.id=e.account_id
                   WHERE e.account_id IS NULL OR e.account_id = ANY(%s) ORDER BY e.id DESC LIMIT %s""",
                (account_ids, limit))
