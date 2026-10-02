"""Дашборд, общие настройки команды, журнал, профиль."""
from fastapi import APIRouter, Form, Request

from .. import auth, db, inbox, worker
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
    funnel = inbox.funnel(None if auth.is_admin(u) else [a["id"] for a in accs])
    return page(request, "dashboard.html", accs=accs, camps=camps, replies=replies, configured=configured(), funnel=funnel,
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
                recontact_days=recontact_days(), stages=inbox.funnel(),
                autopilot=db.get_setting("ai_autopilot") == "1", autopilot_daily=db.get_setting("ai_autopilot_daily"),
                log_keep_days=db.get_setting("log_keep_days"), notify_account=db.get_setting("notify_account_id"),
                accounts=[a for a in auth.user_accounts(user(request), only_active=True) if tgm.get(a["id"]).authorized])


@router.post("/settings")
async def settings_save(request: Request, stop_words: str = Form(""), recontact_days: str = Form("30"),
                        ai_autopilot: str = Form(""), ai_autopilot_daily: str = Form("5"), log_keep_days: str = Form("90")):
    if not auth.is_admin(user(request)):
        return back("/", err="Только админ")
    if not (recontact_days.strip().isdigit() and int(recontact_days) <= 3650):
        return back("/settings", err="«Не писать повторно» — число дней от 0 до 3650")
    words = ",".join(w.strip() for w in stop_words.split(",") if w.strip())
    if not words:
        return back("/settings", err="Нужно хотя бы одно стоп-слово — иначе отказ не сработает")
    db.set_setting("stop_words", words)
    db.set_setting("recontact_days", str(int(recontact_days)))
    if not (ai_autopilot_daily.strip().isdigit() and 1 <= int(ai_autopilot_daily) <= 50):
        return back("/settings", err="Лимит автоответов — от 1 до 50 в сутки")
    if (db.get_setting("ai_autopilot") == "1") != bool(ai_autopilot):
        db.log(f"Автоответы ИИ {'включены' if ai_autopilot else 'ВЫКЛЮЧЕНЫ'} для всей команды ({user(request)['login']})", "warn")
    db.set_setting("ai_autopilot", "1" if ai_autopilot else "0")
    db.set_setting("ai_autopilot_daily", str(int(ai_autopilot_daily)))
    if not (log_keep_days.strip().isdigit() and 7 <= int(log_keep_days) <= 3650):
        return back("/settings", err="Срок хранения журнала — от 7 до 3650 дней")
    db.set_setting("log_keep_days", str(int(log_keep_days)))
    form = await request.form()
    na = str(form.get("notify_account_id") or "")
    db.set_setting("notify_account_id", na if na.isdigit() else "")
    return back("/settings", msg="Сохранено")


@router.post("/settings/notify-test")
async def notify_test(request: Request):
    if not auth.is_admin(user(request)):
        return back("/", err="Только админ")
    from .. import notify
    notify.reset()
    if not notify.admin("Тестовое уведомление: так будут приходить важные события панели.", key="test"):
        return back("/settings", err="Не отправлено: выберите подключённый аккаунт для уведомлений и сохраните")
    return back("/settings", msg="Отправлено — проверьте «Избранное» выбранного аккаунта")


@router.get("/log")
async def log_page(request: Request):
    u = user(request)
    ids = None if auth.is_admin(u) else [a["id"] for a in auth.user_accounts(u)]
    return page(request, "log.html", log=log_rows(500, ids))


@router.get("/me")
async def me_page(request: Request):
    return page(request, "me.html")


# ---------- этапы воронки (админ) ----------
@router.post("/settings/stages/add")
async def stage_add(request: Request, name: str = Form("")):
    if not auth.is_admin(user(request)):
        return back("/", err="Только админ")
    name = name.strip()[:60]
    if not name or db.one("SELECT 1 FROM funnel_stages WHERE lower(name)=lower(%s)", (name,)):
        return back("/settings", err="Укажите новое название этапа")
    pos = (db.val("SELECT MAX(position) FROM funnel_stages WHERE NOT is_lost") or 0) + 1
    db.ex("UPDATE funnel_stages SET position=position+1 WHERE position >= %s", (pos,))   # отказ остаётся последним
    db.ex("INSERT INTO funnel_stages(name, position) VALUES (%s, %s)", (name, pos))
    return back("/settings", msg=f"Этап «{name}» добавлен")


@router.post("/settings/stages/{sid}")
async def stage_save(request: Request, sid: int, name: str = Form(""), position: str = Form(""),
                     is_goal: str = Form(""), is_lost: str = Form("")):
    if not auth.is_admin(user(request)):
        return back("/", err="Только админ")
    name = name.strip()[:60]
    if not name or not position.lstrip("-").isdigit():
        return back("/settings", err="Название и порядок этапа обязательны")
    if db.one("SELECT 1 FROM funnel_stages WHERE lower(name)=lower(%s) AND id!=%s", (name, sid)):
        return back("/settings", err="Этап с таким названием уже есть")
    db.ex("UPDATE funnel_stages SET name=%s, position=%s, is_goal=%s, is_lost=%s WHERE id=%s",
          (name, int(position), bool(is_goal), bool(is_lost), sid))
    return back("/settings", msg="Этап сохранён")


@router.post("/settings/stages/{sid}/delete")
async def stage_delete(request: Request, sid: int):
    if not auth.is_admin(user(request)):
        return back("/", err="Только админ")
    st = db.one("SELECT * FROM funnel_stages WHERE id=%s", (sid,))
    if not st or st["auto"]:
        return back("/settings", err="Этапы «написали» и «ответил» ставятся автоматически — их можно переименовать, но не удалить")
    n = db.val("SELECT COUNT(*) FROM leads WHERE stage_id=%s", (sid,))
    db.ex("DELETE FROM funnel_stages WHERE id=%s", (sid,))
    return back("/settings", msg=f"Этап «{st['name']}» удалён" + (f"; у {n} лидов этап сброшен" if n else ""))
