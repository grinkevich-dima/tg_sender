"""Дашборд, общие настройки команды, журнал, профиль."""
from fastapi import APIRouter, Form, Request

from .. import auth, db, worker
from ..campaigns import recontact_days
from ..tg import configured, tgm
from .common import back, log_rows, page, user

router = APIRouter()


@router.get("/")
async def dashboard(request: Request):
    u = user(request)
    accs = auth.user_accounts(u)
    for a in accs:
        a["authorized"] = tgm.get(a["id"]).authorized
        a["sent_today"] = worker.sent_today(a["id"])
        a["limit"] = worker.daily_limit(a)
        a["queued"] = db.val("SELECT COUNT(*) FROM campaign_leads WHERE account_id=%s AND state='queued'", (a["id"],))
        a["wstate"] = worker.state.get(a["id"], {})
        a["paused"] = worker.paused_until(a)
    vis, p = ("true", []) if auth.is_admin(u) else ("c.created_by=%s", [u["id"]])
    camps = db.q(f"""SELECT c.id, c.name, c.status, COUNT(cl.id) total,
                     COUNT(*) FILTER (WHERE cl.state='queued') q,
                     COUNT(*) FILTER (WHERE cl.state IN ('sent','read','replied')) s,
                     COUNT(*) FILTER (WHERE cl.state IN ('read','replied')) r,
                     COUNT(*) FILTER (WHERE cl.state='replied') rep,
                     COUNT(*) FILTER (WHERE cl.state='failed') f
                     FROM campaigns c LEFT JOIN campaign_leads cl ON cl.campaign_id=c.id
                     WHERE {vis} GROUP BY c.id ORDER BY c.id DESC LIMIT 10""", p)
    replies = db.q("""SELECT m.*, l.first_name, l.last_name, l.username, l.title,
                      COALESCE(NULLIF(a.label,''), a.first_name) acc_label
                      FROM messages m JOIN leads l ON l.id=m.lead_id JOIN tg_accounts a ON a.id=m.account_id
                      WHERE m.direction='in' AND (%s OR a.user_id=%s) ORDER BY m.id DESC LIMIT 10""",
                   (auth.is_admin(u), u["id"]))
    return page(request, "dashboard.html", accs=accs, camps=camps, replies=replies, configured=configured(),
                leads_n=db.val("SELECT COUNT(*) FROM leads"),
                log=log_rows(15, None if auth.is_admin(u) else [a["id"] for a in accs]))


@router.post("/reads/refresh")
async def reads_refresh(request: Request):
    n = 0
    for a in auth.user_accounts(user(request), only_active=True):
        acc = tgm.get(a["id"])
        if acc.authorized:
            n += await acc.refresh_reads()
    return back("/", msg=f"Обновлено прочтений: {n}")


@router.get("/settings")
async def settings_page(request: Request):
    if not auth.is_admin(user(request)):
        return back("/", err="Общие настройки меняет админ. Лимиты своего аккаунта — в разделе «Аккаунты»")
    return page(request, "settings.html", stop_words=db.get_setting("stop_words"),
                recontact_days=recontact_days())


@router.post("/settings")
async def settings_save(request: Request, stop_words: str = Form(""), recontact_days: str = Form("30")):
    if not auth.is_admin(user(request)):
        return back("/", err="Только админ")
    if not (recontact_days.strip().isdigit() and int(recontact_days) <= 3650):
        return back("/settings", err="«Не писать повторно» — число дней от 0 до 3650")
    words = ",".join(w.strip() for w in stop_words.split(",") if w.strip())
    if not words:
        return back("/settings", err="Нужно хотя бы одно стоп-слово — иначе отказ не сработает")
    db.set_setting("stop_words", words)
    db.set_setting("recontact_days", str(int(recontact_days)))
    return back("/settings", msg="Сохранено")


@router.get("/log")
async def log_page(request: Request):
    u = user(request)
    ids = None if auth.is_admin(u) else [a["id"] for a in auth.user_accounts(u)]
    return page(request, "log.html", log=log_rows(500, ids))


@router.get("/me")
async def me_page(request: Request):
    return page(request, "me.html")
