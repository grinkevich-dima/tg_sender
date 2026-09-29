import asyncio
import base64
import csv
import io
import json
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.templating import Jinja2Templates
from telethon import errors

from . import db, worker
from .config import ALLOWED_HOSTS, BASE_DIR, PANEL_PASSWORD, PANEL_USER
from .templating import render, variables_in
from .tg import tg

STATUS_RU = {"queued": "в очереди", "sent": "доставлено", "read": "прочитано", "replied": "ответил",
             "failed": "ошибка", "skipped": "пропущено", "sending": "отправляется", "draft": "черновик", "running": "идёт",
             "paused": "пауза", "done": "завершена"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    await tg.start()
    task = asyncio.create_task(worker.run())
    yield
    task.cancel()
    await tg.stop()


app = FastAPI(lifespan=lifespan, title="TG Sender")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals["STATUS_RU"] = STATUS_RU


# ---------- защита панели ----------
def same_origin(request: Request) -> bool:
    """POST принимаем только со страниц самой панели: иначе любой сайт, открытый в браузере,
    может отправить форму на 127.0.0.1 и запустить рассылку (CSRF). Браузер всегда шлёт Origin
    или Referer; запросы без них (curl, скрипты) — не из браузера, их пропускаем."""
    src = request.headers.get("origin") or request.headers.get("referer")
    return src is None or urlsplit(src).netloc == request.headers.get("host", "")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
        return Response("Запрос с чужого сайта отклонён", 403)
    if PANEL_PASSWORD:
        ok = False
        h = request.headers.get("authorization", "")
        if h.startswith("Basic "):
            try:
                u, _, p = base64.b64decode(h[6:]).decode().partition(":")
                ok = secrets.compare_digest(u, PANEL_USER) and secrets.compare_digest(p, PANEL_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="tg-sender"'})
    return await call_next(request)


app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


def local_url(url: str | None, default: str) -> str:
    """Только путь внутри панели — чтобы редирект по Referer не уводил на чужой сайт."""
    if not url:
        return default
    u = urlsplit(url)
    return (u.path or default) + (f"?{u.query}" if u.query else "")


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
    resp = templates.TemplateResponse(request, name, {"me": tg.display_me(), "authorized": bool(tg.me),
                                                      "flash": flash, **ctx})
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


# ---------- дашборд ----------
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    camps = db.q("""SELECT c.*, t.name AS tpl,
        SUM(m.status='queued') q, SUM(m.status IN ('sent','read','replied')) s,
        SUM(m.status IN ('read','replied')) r, SUM(m.status='replied') rep, SUM(m.status='failed') f, COUNT(m.id) total
        FROM campaigns c LEFT JOIN templates t ON t.id=c.template_id LEFT JOIN messages m ON m.campaign_id=c.id
        GROUP BY c.id ORDER BY c.id DESC LIMIT 10""")
    return page(request, "dashboard.html",
                sent_today=worker.sent_today(), limit=worker.daily_limit(), warmup_day=worker.warmup_day(),
                wstate=worker.state, camps=camps, configured=tg.configured,
                contacts_n=db.one("SELECT COUNT(*) n FROM contacts")["n"],
                log=db.q("SELECT * FROM event_log ORDER BY id DESC LIMIT 15"))


# ---------- авторизация аккаунта ----------
@app.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    step = "done" if tg.me else request.query_params.get("step", "phone")
    return page(request, "auth.html", step=step, configured=tg.configured, phone=tg.phone)


@app.post("/auth/phone")
async def auth_phone(phone: str = Form(...)):
    try:
        where = await tg.send_code(re.sub(r"[^\d+]", "", phone))
    except errors.SendCodeUnavailableError:
        if tg.phone_code_hash:
            return back("/auth?step=code", msg="Код уже был отправлен ранее — найдите его в чате «Telegram» и введите здесь")
        return back("/auth", err="Telegram временно не отправляет коды на этот номер. Подождите 30–60 минут и попробуйте снова")
    except errors.FloodWaitError as e:
        return back("/auth", err=f"Слишком много попыток. Повторите через {e.seconds // 60 + 1} мин.")
    except errors.RPCError as e:
        db.log(f"Ошибка отправки кода: {e}", "error")
        return back("/auth", err=f"Не удалось отправить код: {e}")
    return back("/auth?step=code", msg=f"Код отправлен {where}")


def _qr_svg(url: str) -> str:
    import qrcode, qrcode.image.svg
    return qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=12).to_string(encoding="unicode")


@app.get("/auth/qr", response_class=HTMLResponse)
async def auth_qr(request: Request):
    if tg.me:
        return back("/", msg="Аккаунт уже авторизован")
    try:
        url = await tg.qr_start()
    except errors.RPCError as e:
        return back("/auth", err=f"Не удалось получить QR: {e}")
    return page(request, "auth_qr.html", svg=_qr_svg(url), v=tg.qr_version)


@app.get("/auth/qr/status")
async def auth_qr_status(v: int = 0):
    st = getattr(tg, "qr_status", "none")
    res = {"status": st, "v": getattr(tg, "qr_version", 0)}
    if st == "ok":
        worker.wake.set()
    elif st == "waiting" and res["v"] != v:
        res["svg"] = _qr_svg(tg.qr.url)   # токен обновился — отдаём новую картинку
    return res


@app.get("/auth/qr/done")
async def auth_qr_done():
    if getattr(tg, "qr_status", "") == "password":
        return back("/auth?step=password", msg="QR принят. Введите облачный пароль (2FA)")
    return back("/", msg="Аккаунт авторизован") if tg.me else back("/auth", err="Вход не завершён")


@app.post("/auth/code")
async def auth_code(code: str = Form(...)):
    try:
        r = await tg.sign_in_code(code)
    except errors.RPCError as e:
        return back("/auth?step=code", err=f"Ошибка: {e}")
    if r == "password":
        return back("/auth?step=password", msg="Включена двухэтапная защита — введите облачный пароль")
    worker.wake.set()
    return back("/", msg="Аккаунт авторизован")


@app.post("/auth/password")
async def auth_password(password: str = Form(...)):
    try:
        await tg.sign_in_password(password)
    except errors.RPCError as e:
        return back("/auth?step=password", err=f"Ошибка: {e}")
    worker.wake.set()
    return back("/", msg="Аккаунт авторизован")


@app.post("/auth/logout")
async def auth_logout():
    await tg.logout()
    return back("/auth", msg="Сессия завершена")


# ---------- контакты ----------
@app.get("/contacts", response_class=HTMLResponse)
async def contacts(request: Request, tag: str = "", s: str = "", p: int = 1):
    where, params = ["1=1"], []
    if tag:
        where.append("(',' || tags || ',') LIKE ?"); params.append(f"%,{tag},%")
    if s:
        where.append("(first_name LIKE ? OR last_name LIKE ? OR username LIKE ? OR phone LIKE ?)"); params += [f"%{s}%"] * 4
    w = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) n FROM contacts WHERE {w}", params)["n"]
    rows = db.q(f"SELECT * FROM contacts WHERE {w} ORDER BY id DESC LIMIT 100 OFFSET ?", params + [(p - 1) * 100])
    return page(request, "contacts.html", rows=rows, total=total, tag=tag, s=s, p=p, tags=all_tags())


def all_tags() -> list[str]:
    tags = set()
    for r in db.q("SELECT DISTINCT tags FROM contacts WHERE tags!=''"):
        tags.update(t.strip() for t in r["tags"].split(",") if t.strip())
    return sorted(tags)


@app.post("/contacts/import-tg")
async def contacts_import_tg(source: str = Form("contacts"), tag: str = Form("")):
    if not tg.me:
        return back("/contacts", err="Сначала авторизуйте аккаунт")
    try:
        n = await (tg.import_dialogs(tag) if source == "dialogs" else tg.import_contacts(tag))
    except errors.RPCError as e:
        return back("/contacts", err=str(e))
    db.log(f"Импорт из Telegram ({source}): новых {n}")
    return back("/contacts", msg=f"Импортировано новых контактов: {n}")


@app.post("/contacts/import-csv")
async def contacts_import_csv(file: UploadFile = File(...), tag: str = Form("")):
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)
    added = updated = bad = 0
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        uname = row.pop("username", "") or row.pop("telegram", "")
        uname = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)?@?", "", uname).strip("/") or None
        phone = re.sub(r"[^\d+]", "", row.pop("phone", "") or row.pop("телефон", "")) or None
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        uid = row.pop("user_id", "") or row.pop("id", "")
        uid = int(uid) if uid.isdigit() else None
        first = row.pop("first_name", "") or row.pop("name", "") or row.pop("имя", "")
        last = row.pop("last_name", "") or row.pop("фамилия", "")
        row_tags = row.pop("tags", "")
        if not (uname or phone or uid):
            bad += 1
            continue
        tags = ",".join(filter(None, [row_tags, tag.strip()]))
        extra = json.dumps({k: v for k, v in row.items() if k and v}, ensure_ascii=False)
        existing = None
        if uid:
            existing = db.one("SELECT id FROM contacts WHERE tg_user_id=?", (uid,))
        if not existing and uname:
            existing = db.one("SELECT id FROM contacts WHERE lower(username)=lower(?)", (uname,))
        if not existing and phone:
            existing = db.one("SELECT id FROM contacts WHERE phone=?", (phone,))
        if existing:
            db.ex("""UPDATE contacts SET username=COALESCE(?,username), phone=COALESCE(?,phone),
                     first_name=COALESCE(NULLIF(?,''),first_name), last_name=COALESCE(NULLIF(?,''),last_name),
                     extra=?, tags=CASE WHEN ?='' THEN tags WHEN tags='' THEN ? ELSE tags||','||? END WHERE id=?""",
                  (uname, phone, first, last, extra, tags, tags, tags, existing["id"]))
            updated += 1
        else:
            db.ex("""INSERT INTO contacts(tg_user_id, username, phone, first_name, last_name, extra, tags, created_at)
                     VALUES (?,?,?,?,?,?,?,?)""", (uid, uname, phone, first, last, extra, tags, db.now_utc()))
            added += 1
    db.log(f"Импорт CSV {file.filename}: +{added}, обновлено {updated}, пропущено {bad}")
    return back("/contacts", msg=f"Добавлено {added}, обновлено {updated}, пропущено строк {bad}")


@app.post("/contacts/{cid}/optout")
async def contact_optout(cid: int):
    db.ex("UPDATE contacts SET opted_out=1-opted_out WHERE id=?", (cid,))
    return back("/contacts")


@app.post("/contacts/delete")
async def contacts_delete(tag: str = Form(""), confirm: str = Form("")):
    if confirm != "да":
        return back("/contacts", err="Для удаления введите «да»")
    if tag:
        db.ex("DELETE FROM contacts WHERE (',' || tags || ',') LIKE ?", (f"%,{tag},%",))
    else:
        db.ex("DELETE FROM contacts")
    db.ex("DELETE FROM messages WHERE contact_id NOT IN (SELECT id FROM contacts) AND status='queued'")
    return back("/contacts", msg="Удалено")


# ---------- шаблоны ----------
@app.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request, edit: int = 0):
    tpl = db.one("SELECT * FROM templates WHERE id=?", (edit,)) if edit else None
    sample = db.one("SELECT * FROM contacts WHERE opted_out=0 ORDER BY id LIMIT 1")
    rows = db.q("SELECT * FROM templates ORDER BY id DESC")
    previews = {}
    for t in rows:
        previews[t["id"]] = render(t["body"], db.contact_vars(sample) if sample else
                                   {"first_name": "Иван", "name": "Иван Петров"})
    return page(request, "templates.html", rows=rows, tpl=tpl, previews=previews,
                vars={t["id"]: variables_in(t["body"]) for t in rows})


@app.post("/templates/save")
async def templates_save(name: str = Form(...), body: str = Form(...), tid: int = Form(0)):
    if tid:
        db.ex("UPDATE templates SET name=?, body=? WHERE id=?", (name, body, tid))
    else:
        db.ex("INSERT INTO templates(name, body, created_at) VALUES (?,?,?)", (name, body, db.now_utc()))
    return back("/templates", msg="Шаблон сохранён")


@app.post("/templates/{tid}/delete")
async def templates_delete(tid: int):
    if db.one("SELECT 1 FROM campaigns WHERE template_id=?", (tid,)):
        return back("/templates", err="Шаблон используется в кампании")
    db.ex("DELETE FROM templates WHERE id=?", (tid,))
    return back("/templates", msg="Удалён")


@app.post("/templates/{tid}/test")
async def templates_test(tid: int):
    if not tg.me:
        return back("/templates", err="Аккаунт не авторизован")
    t = db.one("SELECT * FROM templates WHERE id=?", (tid,))
    if not t:
        return back("/templates", err="Шаблон не найден")
    sample = db.one("SELECT * FROM contacts ORDER BY id LIMIT 1")
    text = render(t["body"], db.contact_vars(sample) if sample else {"first_name": "Иван", "name": "Иван Петров"})
    try:
        async with tg.lock:
            await tg.client.send_message("me", "🧪 Тест шаблона «%s»:\n\n%s" % (t["name"], text))
    except errors.FloodWaitError as e:
        return back("/templates", err=f"Telegram просит подождать {e.seconds} сек")
    except errors.RPCError as e:
        return back("/templates", err=f"Не отправлено: {e}")
    return back("/templates", msg="Отправлено в «Избранное»")


# ---------- кампании ----------
@app.get("/campaigns", response_class=HTMLResponse)
async def campaigns(request: Request):
    return page(request, "campaigns.html", tpls=db.q("SELECT id, name FROM templates ORDER BY id DESC"),
                tags=all_tags(), camps=db.q("""SELECT c.*, t.name tpl, COUNT(m.id) total FROM campaigns c
                    LEFT JOIN templates t ON t.id=c.template_id LEFT JOIN messages m ON m.campaign_id=c.id
                    GROUP BY c.id ORDER BY c.id DESC"""))


@app.post("/campaigns/create")
async def campaigns_create(name: str = Form(...), template_id: int = Form(...), tag: str = Form(""),
                           skip_contacted: str = Form(""), start: str = Form("")):
    where, params = ["opted_out=0"], []
    if tag:
        where.append("(',' || tags || ',') LIKE ?"); params.append(f"%,{tag},%")
    if skip_contacted:
        where.append("""id NOT IN (SELECT m.contact_id FROM messages m JOIN campaigns c ON c.id=m.campaign_id
                        WHERE c.template_id=? AND m.status IN ('sent','read','replied'))""")
        params.append(template_id)
    ids = [r["id"] for r in db.q(f"SELECT id FROM contacts WHERE {' AND '.join(where)} ORDER BY id", params)]
    if not ids:
        return back("/campaigns", err="Нет подходящих контактов")
    status = "running" if start else "draft"
    cid = db.ex("INSERT INTO campaigns(name, template_id, status, created_at, started_at) VALUES (?,?,?,?,?)",
                (name, template_id, status, db.now_utc(), db.now_utc() if start else None))
    for i in ids:
        db.ex("INSERT OR IGNORE INTO messages(campaign_id, contact_id) VALUES (?,?)", (cid, i))
    db.log(f"Создана кампания «{name}»: {len(ids)} получателей")
    worker.wake.set()
    return back(f"/campaigns/{cid}", msg=f"Кампания создана: {len(ids)} получателей")


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
async def campaign_view(request: Request, cid: int, status: str = ""):
    c = db.one("SELECT c.*, t.name tpl, t.body FROM campaigns c JOIN templates t ON t.id=c.template_id WHERE c.id=?", (cid,))
    if not c:
        return back("/campaigns", err="Не найдена")
    stats = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM messages WHERE campaign_id=? GROUP BY status", (cid,))}
    sql = """SELECT m.*, ct.first_name, ct.last_name, ct.username, ct.phone FROM messages m
             JOIN contacts ct ON ct.id=m.contact_id WHERE m.campaign_id=?"""
    params = [cid]
    if status:
        sql += " AND m.status=?"; params.append(status)
    rows = db.q(sql + " ORDER BY m.sent_at IS NULL, m.sent_at DESC, m.id LIMIT 500", params)
    total = sum(stats.values())
    delivered = stats.get("sent", 0) + stats.get("read", 0) + stats.get("replied", 0)
    queued = stats.get("queued", 0)
    per_day = max(worker.daily_limit(), 1)
    return page(request, "campaign.html", c=c, stats=stats, rows=rows, total=total, delivered=delivered,
                fstatus=status, eta_days=-(-queued // per_day) if queued else 0)


@app.post("/campaigns/{cid}/{action}")
async def campaign_action(cid: int, action: str):
    if action == "start":
        db.ex("UPDATE campaigns SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?", (db.now_utc(), cid))
    elif action == "pause":
        db.ex("UPDATE campaigns SET status='paused' WHERE id=?", (cid,))
    elif action == "retry":
        n = db.one("SELECT COUNT(*) n FROM messages WHERE campaign_id=? AND status='failed'", (cid,))["n"]
        db.ex("UPDATE messages SET status='queued', error=NULL WHERE campaign_id=? AND status='failed'", (cid,))
        db.ex("UPDATE campaigns SET status='running' WHERE id=?", (cid,))
        worker.wake.set()
        return back(f"/campaigns/{cid}", msg=f"Возвращено в очередь: {n}")
    elif action == "delete":
        db.ex("DELETE FROM messages WHERE campaign_id=?", (cid,))
        db.ex("DELETE FROM campaigns WHERE id=?", (cid,))
        return back("/campaigns", msg="Кампания удалена")
    worker.wake.set()
    return back(f"/campaigns/{cid}")


@app.get("/campaigns/{cid}/export.csv")
async def campaign_export(cid: int):
    rows = db.q("""SELECT ct.first_name, ct.last_name, ct.username, ct.phone, ct.tg_user_id, m.status, m.error,
                   m.sent_at, m.read_at, m.replied_at, m.text FROM messages m JOIN contacts ct ON ct.id=m.contact_id
                   WHERE m.campaign_id=? ORDER BY m.id""", (cid,))
    buf = io.StringIO()
    buf.write("﻿")  # для Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Имя", "Фамилия", "Username", "Телефон", "User ID", "Статус", "Ошибка",
                "Отправлено (UTC)", "Прочитано (UTC)", "Ответ (UTC)", "Текст"])
    for r in rows:
        w.writerow([r[0], r[1], r[2], r[3], r[4], STATUS_RU.get(r[5], r[5]), *list(r)[6:]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=campaign_{cid}.csv"})


@app.post("/reads/refresh")
async def reads_refresh():
    if not tg.me:
        return back("/", err="Аккаунт не авторизован")
    n = await tg.refresh_reads()
    return back("/", msg=f"Обновлено прочтений: {n}")


# ---------- настройки ----------
@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return page(request, "settings.html", s=db.all_settings(), limit=worker.daily_limit(),
                warmup_day=worker.warmup_day())


INT_SETTINGS = {"warmup_start": "Лимит в 1-й день", "warmup_step": "Прибавка в день", "daily_max": "Потолок в день",
                "delay_min": "Пауза от", "delay_max": "Пауза до"}


def validate_settings(form) -> tuple[dict, str]:
    """Проверяем всё до сохранения: кривое значение в БД роняет дашборд и воркер."""
    vals = {k: str(form[k]).strip() for k in [*INT_SETTINGS, "work_start", "work_end", "stop_words",
                                               "warmup_start_date"] if k in form}
    for k, title in INT_SETTINGS.items():
        if k in vals and not (vals[k].isdigit() and int(vals[k]) <= 100000):
            return {}, f"«{title}»: нужно целое число от 0"
    for k in ("work_start", "work_end"):
        if k in vals:
            try:
                vals[k] = datetime.strptime(vals[k], "%H:%M").strftime("%H:%M")
            except ValueError:
                return {}, "Рабочие часы: формат ЧЧ:ММ, например 10:00"
    if vals.get("warmup_start_date"):
        try:
            date.fromisoformat(vals["warmup_start_date"])
        except ValueError:
            return {}, "Дата начала прогрева: формат ГГГГ-ММ-ДД"
    return vals, ""


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    vals, err = validate_settings(form)
    if err:
        return back("/settings", err=f"Не сохранено. {err}")
    for key, v in vals.items():
        db.set_setting(key, v)
    db.set_setting("warmup_enabled", "1" if form.get("warmup_enabled") else "0")
    if form.get("clear_pause"):
        db.set_setting("paused_until", "")
        db.log("Пауза снята вручную", "warn")
    worker.wake.set()
    return back("/settings", msg="Сохранено")


@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request):
    return page(request, "log.html", log=db.q("SELECT * FROM event_log ORDER BY id DESC LIMIT 500"))


# ---------- списки обращений из xlsx ----------
from . import outreach  # noqa: E402

LIST_STATE_RU = {"new": "новая", "queued": "в очереди", "sending": "отправляется", "sent": "доставлено", "read": "прочитано",
                 "replied": "ответил", "failed": "ошибка", "skipped": "пропущено"}
templates.env.globals["LIST_STATE_RU"] = LIST_STATE_RU
templates.env.globals["KIND_RU"] = outreach.KIND_RU


def _read_filter(list_id: int, qp) -> dict:
    if "f" not in qp:
        return outreach.default_filter(list_id)
    return {"kind": qp.getlist("kind"), "dialog": qp.getlist("dialog"), "address": qp.getlist("address"),
            "src": qp.getlist("src"), "state": qp.getlist("state"), "real": qp.getlist("real"),
            "q": qp.get("q", "").strip(), "mismatch": bool(qp.get("mismatch")), "use_real": bool(qp.get("use_real")),
            "has_text": bool(qp.get("has_text")), "has_target": bool(qp.get("has_target"))}


@app.get("/lists", response_class=HTMLResponse)
async def lists_page(request: Request):
    rows = db.q("""SELECT l.*, COUNT(i.id) total, SUM(i.state='queued') q,
                   SUM(i.state IN ('sent','read','replied')) s, SUM(i.state='failed') f, SUM(i.state='replied') rep
                   FROM lists l LEFT JOIN list_items i ON i.list_id=l.id GROUP BY l.id ORDER BY l.id DESC""")
    return page(request, "lists.html", rows=rows)


@app.post("/lists/upload")
async def lists_upload(file: UploadFile = File(...)):
    data = await file.read()
    try:
        lid, n, warns = outreach.import_file(data, file.filename or "список.xlsx")
    except Exception as e:
        return back("/lists", err=f"Не удалось прочитать файл: {e}")
    msg = f"Загружено строк: {n}." + (" " + "; ".join(warns) if warns else "")
    return back(f"/lists/{lid}", msg=msg)


@app.get("/lists/{lid}", response_class=HTMLResponse)
async def list_view(request: Request, lid: int, show: str = ""):
    lst = db.one("SELECT * FROM lists WHERE id=?", (lid,))
    if not lst:
        return back("/lists", err="Список не найден")
    f = _read_filter(lid, request.query_params)
    w, p = outreach.filter_sql(lid, f)
    matched = db.one(f"SELECT COUNT(*) n FROM list_items WHERE {w}", p)["n"]
    enq = db.one(f"SELECT COUNT(*) n FROM list_items WHERE {w} AND state IN ('new','failed','skipped')", p)["n"]
    stats = {r["state"]: r["n"] for r in db.q("SELECT state, COUNT(*) n FROM list_items WHERE list_id=? GROUP BY state", (lid,))}
    mismatch_n = db.one(f"SELECT COUNT(*) n FROM list_items WHERE list_id=? AND {outreach.MISMATCH_SQL}", (lid,))["n"]
    checked_n = db.one("SELECT COUNT(*) n FROM list_items WHERE list_id=? AND real_dialog IS NOT NULL", (lid,))["n"]
    if show:
        rows = db.q("SELECT * FROM list_items WHERE list_id=? AND state=? ORDER BY COALESCE(order_idx, 1e9), id LIMIT 500",
                    (lid, show))
    else:
        rows = db.q(f"SELECT * FROM list_items WHERE {w} ORDER BY COALESCE(order_idx, 1e9), id LIMIT 300", p)
    queued = stats.get("queued", 0)
    return page(request, "list.html", l=lst, f=f, facets=outreach.facets(lid), matched=matched, enq=enq,
                stats=stats, rows=rows, show=show, total=sum(stats.values()),
                limit=worker.daily_limit(), sent_today=worker.sent_today(),
                eta_days=-(-queued // max(worker.daily_limit(), 1)) if queued else 0,
                prep=tg.prepare_state.get(lid), qs=str(request.query_params),
                mismatch_n=mismatch_n, checked_n=checked_n)


@app.post("/lists/{lid}/enqueue")
async def list_enqueue(request: Request, lid: int):
    form = await request.form()
    f = {"kind": form.getlist("kind"), "dialog": form.getlist("dialog"), "address": form.getlist("address"),
         "src": form.getlist("src"), "state": form.getlist("state"), "real": form.getlist("real"),
         "q": (form.get("q") or "").strip(), "mismatch": bool(form.get("mismatch")), "use_real": bool(form.get("use_real")),
         "has_text": bool(form.get("has_text")), "has_target": bool(form.get("has_target"))}
    n = outreach.enqueue(lid, f)
    if form.get("start"):
        db.ex("UPDATE lists SET status='running' WHERE id=?", (lid,))
        worker.wake.set()
    db.log(f"Список #{lid}: в очередь добавлено {n}")
    return back(f"/lists/{lid}?show=queued", msg=f"В очередь добавлено: {n}" + (" · отправка запущена" if form.get("start") else ""))


@app.get("/lists/{lid}/prepare-status")
async def list_prepare_status(lid: int):
    return tg.prepare_state.get(lid) or {"running": False, "step": "не запускался"}


@app.post("/lists/{lid}/{action}")
async def list_action(lid: int, action: str):
    if action == "start":
        db.ex("UPDATE lists SET status='running' WHERE id=?", (lid,))
        worker.wake.set()
    elif action == "pause":
        db.ex("UPDATE lists SET status='paused' WHERE id=?", (lid,))
    elif action == "unqueue":
        db.ex("UPDATE list_items SET state='new', order_idx=NULL WHERE list_id=? AND state='queued'", (lid,))
        return back(f"/lists/{lid}", msg="Очередь очищена")
    elif action == "prepare":
        if not tg.me:
            return back(f"/lists/{lid}", err="Аккаунт не авторизован")
        st = tg.prepare_state.get(lid)
        if not (st and st.get("running")):
            asyncio.create_task(tg.prepare_list(lid))
        return back(f"/lists/{lid}", msg="Ищу получателей: загружаю диалоги и участников чатов…")
    elif action == "delete":
        db.ex("DELETE FROM list_items WHERE list_id=?", (lid,))
        db.ex("DELETE FROM lists WHERE id=?", (lid,))
        return back("/lists", msg="Список удалён")
    return back(f"/lists/{lid}")


@app.post("/lists/item/{iid}/send")
async def list_item_send_now(iid: int, request: Request):
    """Тестовая отправка одной строки сразу — мимо очереди, лимита и рабочих часов."""
    it = db.one("SELECT * FROM list_items WHERE id=?", (iid,))
    ref = local_url(request.headers.get("referer"), f"/lists/{it['list_id']}" if it else "/lists")
    if not it:
        return back("/lists", err="Строка не найдена")
    if not tg.me:
        return back(ref, err="Аккаунт не авторизован")
    pu = worker.paused_until()
    if pu:
        return back(ref, err=f"Отправка на паузе до {pu:%d.%m %H:%M} из-за ограничения Telegram")
    if it["state"] not in ("new", "queued", "failed", "skipped"):
        return back(ref, err="Эта строка уже отправлена")
    try:
        res = await worker.send_list_item(it)
    except worker.TRANSIENT_ERRORS as e:
        return back(ref, err=f"Нет связи с Telegram ({type(e).__name__}), строка оставлена как была — повторите позже")
    if res == "busy":
        return back(ref, err="Эта строка уже отправляется — обновите страницу")
    it = db.one("SELECT * FROM list_items WHERE id=?", (iid,))
    who = it["title"] or it["first_name"] or f"строка {iid}"
    db.log(f"Тестовая отправка «{who}»: {res}{' — ' + it['error'] if it['error'] else ''}")
    if res == "sent":
        return back(ref, msg=f"Отправлено сейчас: {who}")
    return back(ref, err=f"Не отправлено ({who}): {it['error'] or res}")


@app.post("/lists/item/{iid}/{action}")
async def list_item_action(iid: int, action: str):
    it = db.one("SELECT list_id FROM list_items WHERE id=?", (iid,))
    if not it:
        return back("/lists")
    if action == "skip":
        db.ex("UPDATE list_items SET state='skipped', error='исключено вручную' WHERE id=? AND state IN ('new','queued','failed')", (iid,))
    elif action == "reset":
        db.ex("UPDATE list_items SET state='new', error=NULL, order_idx=NULL WHERE id=? AND state IN ('skipped','failed')", (iid,))
    return back(f"/lists/{it['list_id']}?show=queued")


@app.get("/lists/{lid}/export.csv")
async def list_export(lid: int):
    rows = db.q("""SELECT row_no, kind, title, first_name, last_name, link, topic_title, dialog, address, text_no,
                   src_status, state, error, sent_at, read_at, replied_at FROM list_items WHERE list_id=? ORDER BY id""", (lid,))
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Исх. №", "Тип", "Название", "Имя", "Фамилия", "Ссылка", "Куда", "Диалог", "Ты/вы", "Текст №",
                "Статус из файла", "Статус отправки", "Ошибка", "Отправлено (UTC)", "Прочитано (UTC)", "Ответ (UTC)"])
    for r in rows:
        r = list(r)
        r[1] = outreach.KIND_RU.get(r[1], r[1])
        r[11] = LIST_STATE_RU.get(r[11], r[11])
        w.writerow(r)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=list_{lid}.csv"})
