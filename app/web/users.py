"""Вход в панель, первый запуск, пользователи команды."""
from fastapi import APIRouter, Form, Request

from .. import auth, db
from .common import back, local_url, page, user

router = APIRouter()


@router.get("/setup")
async def setup_page(request: Request):
    if auth.has_users():
        return back("/login")
    return page(request, "setup.html")


@router.post("/setup")
async def setup(request: Request, login: str = Form(...), name: str = Form(...), password: str = Form(...)):
    if auth.has_users():
        return back("/login")
    if err := auth.validate_password(password):
        return back("/setup", err=err)
    uid = auth.create_user(login, name, password, "admin")
    aid = auth.adopt_legacy_session(uid)
    auth.login_session(request, db.one("SELECT * FROM users WHERE id=%s", (uid,)))
    if aid:
        from ..tg import tgm
        await tgm.get(aid).start()
        return back("/accounts", msg="Админ создан. Аккаунт Telegram из прежней версии подключён — входить заново не нужно")
    return back("/accounts", msg="Админ создан. Теперь подключите аккаунт Telegram")


@router.get("/login")
async def login_page(request: Request, next: str = "/"):
    if not auth.has_users():
        return back("/setup")
    return page(request, "login.html", next=local_url(next, "/"))


@router.post("/login")
async def login(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form("/")):
    u = auth.authenticate(login, password)
    if not u:
        return back(f"/login?next={local_url(next, '/')}", err="Неверный логин или пароль")
    auth.login_session(request, u)
    return back(local_url(next, "/"))


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return back("/login")


@router.get("/users")
async def users_page(request: Request):
    if not auth.is_admin(user(request)):
        return back("/", err="Раздел доступен только админу")
    rows = db.q("""SELECT u.*, (SELECT COUNT(*) FROM tg_accounts a WHERE a.user_id=u.id) accounts
                   FROM users u ORDER BY u.id""")
    return page(request, "users.html", rows=rows)


@router.post("/users/create")
async def users_create(request: Request, login: str = Form(...), name: str = Form(...), password: str = Form(...),
                       role: str = Form("manager")):
    if not auth.is_admin(user(request)):
        return back("/", err="Только админ")
    if role not in auth.ROLES:
        return back("/users", err="Неизвестная роль")
    if err := auth.validate_password(password):
        return back("/users", err=err)
    if db.one("SELECT 1 FROM users WHERE login=%s", (login.strip().lower(),)):
        return back("/users", err="Такой логин уже есть")
    auth.create_user(login, name, password, role)
    db.log(f"Добавлен пользователь {login} ({auth.ROLES[role]})")
    return back("/users", msg="Пользователь добавлен")


@router.post("/users/{uid}/toggle")
async def users_toggle(request: Request, uid: int):
    me = user(request)
    if not auth.is_admin(me):
        return back("/", err="Только админ")
    if uid == me["id"]:
        return back("/users", err="Нельзя отключить самого себя")
    db.ex("UPDATE users SET active = NOT active WHERE id=%s", (uid,))
    return back("/users", msg="Готово")


@router.post("/users/{uid}/password")
async def users_password(request: Request, uid: int, password: str = Form(...)):
    me = user(request)
    if not (auth.is_admin(me) or uid == me["id"]):
        return back("/", err="Нет прав")
    if err := auth.validate_password(password):
        return back("/users" if auth.is_admin(me) else "/", err=err)
    db.ex("UPDATE users SET password_hash=%s WHERE id=%s", (auth.hash_password(password), uid))
    if uid == me["id"]:
        auth.login_session(request, me)     # свой вход остаётся, остальные сессии завершаются
    return back("/users" if auth.is_admin(me) else "/", msg="Пароль изменён")
