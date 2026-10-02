"""Аккаунты Telegram команды: подключение, лимиты и прогрев, пауза, импорт контактов."""
import re
from datetime import date, datetime

from fastapi import APIRouter, Form, Request
from telethon import errors

from .. import auth, db, worker
from ..tg import DuplicateAccount, configured, tgm
from .common import back, page, user

router = APIRouter(prefix="/accounts")

NOT_CONFIGURED = "Не заданы TG_API_ID / TG_API_HASH в .env — впишите их и перезапустите панель"
INT_FIELDS = {"warmup_start": "Лимит в 1-й день", "warmup_step": "Прибавка в день", "daily_max": "Потолок в день",
              "delay_min": "Пауза от", "delay_max": "Пауза до"}


def _get(request: Request, aid: int):
    acc = db.one("SELECT * FROM tg_accounts WHERE id=%s", (aid,))
    return acc if auth.can_use_account(user(request), acc) else None


@router.get("")
async def accounts_page(request: Request):
    u = user(request)
    rows = auth.user_accounts(u)
    for a in rows:
        a["client"] = tgm.get(a["id"])
        a["sent_today"] = worker.sent_today(a["id"])
        a["limit"] = worker.daily_limit(a)
        a["warmup_day"] = worker.warmup_day(a)
        a["wstate"] = worker.state.get(a["id"], {})
        a["paused"] = worker.paused_until(a)
        a["leads"] = db.val("SELECT COUNT(*) FROM leads WHERE owner_account_id=%s", (a["id"],))
        a["queued"] = db.val("SELECT COUNT(*) FROM campaign_leads WHERE account_id=%s AND state='queued'", (a["id"],))
    users = db.q("SELECT id, name FROM users WHERE active ORDER BY name") if auth.is_admin(u) else []
    return page(request, "accounts.html", rows=rows, users=users, configured=configured())


@router.post("/create")
async def accounts_create(request: Request, label: str = Form(""), owner: int = Form(0)):
    u = user(request)
    owner_id = owner if auth.is_admin(u) and owner else u["id"]
    aid = db.ex("INSERT INTO tg_accounts(user_id, label) VALUES (%s, %s) RETURNING id", (owner_id, label.strip()))
    return back(f"/accounts/{aid}/login")


@router.post("/{aid}/delete")
async def accounts_delete(request: Request, aid: int):
    acc = _get(request, aid)
    if not acc:
        return back("/accounts", err="Аккаунт не найден")
    if acc["status"] != "new":
        return back("/accounts", err="Удалить можно только ни разу не подключённый аккаунт. "
                                     "Подключённый — «Выйти»: его лиды и история остаются за ним")
    db.ex("DELETE FROM tg_accounts WHERE id=%s", (aid,))
    return back("/accounts", msg="Удалён")


# ---------- вход в Telegram ----------
@router.get("/{aid}/login")
async def login_page(request: Request, aid: int, step: str = "phone"):
    acc = _get(request, aid)
    if not acc:
        return back("/accounts", err="Аккаунт не найден")
    client = tgm.get(aid)
    if client.authorized:
        step = "done"
    return page(request, "account_login.html", acc=acc, step=step, configured=configured(), phone=client.phone,
                me=client.display())


@router.post("/{aid}/phone")
async def login_phone(request: Request, aid: int, phone: str = Form(...)):
    if not _get(request, aid):
        return back("/accounts", err="Аккаунт не найден")
    if not configured():
        return back(f"/accounts/{aid}/login", err=NOT_CONFIGURED)
    client = tgm.get(aid)
    try:
        where = await client.send_code(re.sub(r"[^\d+]", "", phone))
    except errors.SendCodeUnavailableError:
        if client.phone_code_hash:
            return back(f"/accounts/{aid}/login?step=code",
                        msg="Код уже был отправлен ранее — найдите его в чате «Telegram» и введите здесь")
        return back(f"/accounts/{aid}/login",
                    err="Telegram временно не отправляет коды на этот номер. Подождите 30–60 минут или войдите по QR")
    except errors.FloodWaitError as e:
        return back(f"/accounts/{aid}/login", err=f"Слишком много попыток. Повторите через {e.seconds // 60 + 1} мин.")
    except errors.RPCError as e:
        db.log(f"Ошибка отправки кода: {e}", "error", aid)
        return back(f"/accounts/{aid}/login", err=f"Не удалось отправить код: {e}")
    return back(f"/accounts/{aid}/login?step=code", msg=f"Код отправлен {where}")


@router.post("/{aid}/code")
async def login_code(request: Request, aid: int, code: str = Form(...)):
    if not _get(request, aid):
        return back("/accounts", err="Аккаунт не найден")
    client = tgm.get(aid)
    if not (configured() and client.phone_code_hash):
        return back(f"/accounts/{aid}/login", err=NOT_CONFIGURED if not configured() else "Сначала запросите код")
    try:
        r = await client.sign_in_code(code)
    except DuplicateAccount as e:
        return back("/accounts", err=str(e))
    except errors.RPCError as e:
        return back(f"/accounts/{aid}/login?step=code", err=f"Ошибка: {e}")
    if r == "password":
        return back(f"/accounts/{aid}/login?step=password", msg="Включена двухэтапная защита — введите облачный пароль")
    worker.wake(aid)
    return back("/accounts", msg="Аккаунт подключён")


@router.post("/{aid}/password")
async def login_password(request: Request, aid: int, password: str = Form(...)):
    if not _get(request, aid):
        return back("/accounts", err="Аккаунт не найден")
    if not configured():
        return back(f"/accounts/{aid}/login", err=NOT_CONFIGURED)
    try:
        await tgm.get(aid).sign_in_password(password)
    except DuplicateAccount as e:
        return back("/accounts", err=str(e))
    except errors.RPCError as e:
        return back(f"/accounts/{aid}/login?step=password", err=f"Ошибка: {e}")
    worker.wake(aid)
    return back("/accounts", msg="Аккаунт подключён")


def _qr_svg(url: str) -> str:
    import qrcode
    import qrcode.image.svg
    return qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12).to_string(encoding="unicode")


@router.get("/{aid}/qr")
async def login_qr(request: Request, aid: int):
    acc = _get(request, aid)
    if not acc:
        return back("/accounts", err="Аккаунт не найден")
    client = tgm.get(aid)
    if client.authorized:
        return back("/accounts", msg="Аккаунт уже подключён")
    if not configured():
        return back(f"/accounts/{aid}/login", err=NOT_CONFIGURED)
    try:
        url = await client.qr_start()
    except errors.RPCError as e:
        return back(f"/accounts/{aid}/login", err=f"Не удалось получить QR: {e}")
    return page(request, "account_qr.html", acc=acc, svg=_qr_svg(url), v=client.qr_version)


@router.get("/{aid}/qr/status")
async def login_qr_status(request: Request, aid: int, v: int = 0):
    if not _get(request, aid):
        return {"status": "error: нет доступа", "v": 0}
    client = tgm.get(aid)
    res = {"status": client.qr_status, "v": client.qr_version}
    if client.qr_status == "ok":
        worker.wake(aid)
    elif client.qr_status == "waiting" and res["v"] != v:
        res["svg"] = _qr_svg(client.qr.url)   # токен обновился — отдаём новую картинку
    return res


@router.get("/{aid}/qr/done")
async def login_qr_done(request: Request, aid: int):
    if not _get(request, aid):
        return back("/accounts", err="Аккаунт не найден")
    client = tgm.get(aid)
    if client.qr_status == "password":
        return back(f"/accounts/{aid}/login?step=password", msg="QR принят. Введите облачный пароль (2FA)")
    return back("/accounts", msg="Аккаунт подключён") if client.authorized else back(f"/accounts/{aid}/login", err="Вход не завершён")


@router.post("/{aid}/logout")
async def logout(request: Request, aid: int):
    if not _get(request, aid):
        return back("/accounts", err="Аккаунт не найден")
    await tgm.get(aid).logout()
    return back("/accounts", msg="Сессия завершена. Лиды и история остаются за аккаунтом — войдите снова, чтобы продолжить")


# ---------- настройки аккаунта ----------
@router.get("/{aid}")
async def account_page(request: Request, aid: int):
    acc = _get(request, aid)
    if not acc:
        return back("/accounts", err="Аккаунт не найден")
    return page(request, "account.html", acc=acc, client=tgm.get(aid), limit=worker.daily_limit(acc),
                warmup_day=worker.warmup_day(acc), sent_today=worker.sent_today(aid),
                paused=worker.paused_until(acc), wstate=worker.state.get(aid, {}),
                owner=db.one("SELECT name FROM users WHERE id=%s", (acc["user_id"],)))


def validate_settings(form) -> tuple[dict, str]:
    """Проверяем всё до сохранения: кривое значение ломает отправку аккаунта."""
    vals = {}
    for k, title in INT_FIELDS.items():
        v = str(form.get(k, "")).strip()
        if not (v.isdigit() and int(v) <= 100000):
            return {}, f"«{title}»: нужно целое число от 0"
        vals[k] = int(v)
    for k in ("work_start", "work_end"):
        try:
            vals[k] = datetime.strptime(str(form.get(k, "")).strip(), "%H:%M").time()
        except ValueError:
            return {}, "Рабочие часы: формат ЧЧ:ММ, например 10:00"
    wd = str(form.get("warmup_start_date", "")).strip()
    try:
        vals["warmup_start_date"] = date.fromisoformat(wd) if wd else None
    except ValueError:
        return {}, "Дата начала прогрева: формат ГГГГ-ММ-ДД"
    vals["warmup_enabled"] = bool(form.get("warmup_enabled"))
    tz = str(form.get("tz", "")).strip()
    if tz:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            return {}, "Часовой пояс: например Europe/Minsk, Europe/Moscow, Asia/Almaty"
    vals["tz"] = tz or None
    vals["label"] = str(form.get("label", "")).strip()[:60]
    return vals, ""


@router.post("/{aid}/settings")
async def account_settings(request: Request, aid: int):
    if not _get(request, aid):
        return back("/accounts", err="Аккаунт не найден")
    form = await request.form()
    vals, err = validate_settings(form)
    if err:
        return back(f"/accounts/{aid}", err=f"Не сохранено. {err}")
    db.ex(f"UPDATE tg_accounts SET {', '.join(f'{k}=%s' for k in vals)} WHERE id=%s", (*vals.values(), aid))
    if form.get("clear_pause"):
        db.ex("UPDATE tg_accounts SET paused_until=NULL, pause_reason=NULL WHERE id=%s", (aid,))
        db.log("Пауза снята вручную", "warn", aid)
    worker.wake(aid)
    return back(f"/accounts/{aid}", msg="Сохранено")


@router.post("/{aid}/import")
async def account_import(request: Request, aid: int, source: str = Form("contacts"), tag: str = Form("")):
    if not _get(request, aid):
        return back("/leads", err="Аккаунт не найден")
    client = tgm.get(aid)
    if not client.authorized:
        return back("/leads", err="Аккаунт не авторизован")
    try:
        n = await (client.import_dialogs(tag) if source == "dialogs" else client.import_contacts(tag))
    except errors.RPCError as e:
        return back("/leads", err=str(e))
    db.log(f"Импорт из Telegram ({source}): новых лидов {n}", account_id=aid)
    return back("/leads", msg=f"Импортировано новых лидов: {n}. Они закреплены за этим аккаунтом")


@router.post("/{aid}/{action}")
async def account_action(request: Request, aid: int, action: str):
    acc = _get(request, aid)
    if not acc:
        return back("/accounts", err="Аккаунт не найден")
    if action in ("pause", "resume"):
        if acc["status"] not in ("active", "paused"):
            return back("/accounts", err="Аккаунт не подключён")
        db.ex("UPDATE tg_accounts SET status=%s WHERE id=%s", ("paused" if action == "pause" else "active", aid))
        worker.wake(aid)
        return back("/accounts", msg="Отправка с аккаунта на паузе" if action == "pause" else "Отправка возобновлена")
    return back("/accounts", err="Неизвестное действие")
