"""Вход в панель: пользователи команды, роли, защита форм от чужих сайтов."""
import hashlib
import hmac
import secrets
import shutil
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from . import db
from .config import LEGACY_SESSION, SESSIONS_DIR

ROLES = {"admin": "Админ", "manager": "Менеджер"}
PUBLIC_PATHS = ("/login", "/setup", "/static/")


# ---------- пароли (scrypt из стандартной библиотеки) ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, salt, dk = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(got.hex(), dk)


def validate_password(password: str) -> str:
    return "" if len(password) >= 8 else "Пароль — не короче 8 символов"


def create_user(login: str, name: str, password: str, role: str) -> int:
    return db.ex("INSERT INTO users(login, name, password_hash, role) VALUES (%s, %s, %s, %s) RETURNING id",
                 (login.strip().lower(), name.strip(), hash_password(password), role))


def authenticate(login: str, password: str) -> dict | None:
    u = db.one("SELECT * FROM users WHERE login=%s AND active", (login.strip().lower(),))
    return u if u and check_password(password, u["password_hash"]) else None


def has_users() -> bool:
    return bool(db.val("SELECT EXISTS(SELECT 1 FROM users)"))


def adopt_legacy_session(user_id: int) -> int | None:
    """Однопользовательская версия хранила сессию в data/account.session — подключаем её
    первым аккаунтом админа, чтобы не входить в Telegram заново."""
    if not LEGACY_SESSION.exists() or db.val("SELECT EXISTS(SELECT 1 FROM tg_accounts)"):
        return None
    aid = db.ex("INSERT INTO tg_accounts(user_id, label, status) VALUES (%s, 'Основной', 'active') RETURNING id", (user_id,))
    shutil.copy2(LEGACY_SESSION, SESSIONS_DIR / f"acc_{aid}.session")
    db.log("Сессия прежней версии подключена как первый аккаунт", account_id=aid)
    return aid


# ---------- текущий пользователь ----------
def current_user(request: Request) -> dict | None:
    return getattr(request.state, "user", None)


def is_admin(user: dict | None) -> bool:
    return bool(user and user["role"] == "admin")


def can_use_account(user: dict, account: dict | None) -> bool:
    """Менеджер работает только со своими аккаунтами, админ — со всеми."""
    return bool(account) and (is_admin(user) or account["user_id"] == user["id"])


def can_edit_campaign(user: dict, campaign: dict | None) -> bool:
    return bool(campaign) and (is_admin(user) or campaign["created_by"] == user["id"])


def user_accounts(user: dict, only_active: bool = False) -> list[dict]:
    sql = """SELECT a.*, u.name AS user_name FROM tg_accounts a JOIN users u ON u.id=a.user_id
             WHERE (%s OR a.user_id=%s)"""
    if only_active:
        sql += " AND a.status IN ('active','paused')"
    return db.q(sql + " ORDER BY a.id", (is_admin(user), user["id"]))


# ---------- защита форм ----------
def same_origin(request: Request) -> bool:
    """POST принимаем только со страниц самой панели: иначе любой сайт, открытый в браузере,
    может отправить форму на 127.0.0.1 и запустить рассылку (CSRF). Браузер всегда шлёт Origin
    или Referer; запросы без них (curl, скрипты) — не из браузера, их пропускаем."""
    src = request.headers.get("origin") or request.headers.get("referer")
    return src is None or urlsplit(src).netloc == request.headers.get("host", "")


async def auth_middleware(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
        return Response("Запрос с чужого сайта отклонён", 403)
    path = request.url.path
    request.state.user = None
    uid = request.session.get("uid")
    if uid:
        u = db.one("SELECT id, login, name, role FROM users WHERE id=%s AND active", (uid,))
        if u and request.session.get("ver") == _session_version(u["id"]):
            request.state.user = u
        else:
            request.session.clear()
    if request.state.user is None and not path.startswith(PUBLIC_PATHS):
        if not has_users():
            return RedirectResponse("/setup", 303)
        if request.method == "GET":
            return RedirectResponse(f"/login?next={path}", 303)
        return Response("Нужно войти в панель", 401)
    return await call_next(request)


def _session_version(user_id: int) -> str:
    """Смена пароля или отключение пользователя завершает его старые входы."""
    h = db.val("SELECT password_hash FROM users WHERE id=%s", (user_id,)) or ""
    return hashlib.sha256(h.encode()).hexdigest()[:16]


def login_session(request: Request, user: dict) -> None:
    request.session.clear()
    request.session["uid"] = user["id"]
    request.session["ver"] = _session_version(user["id"])
