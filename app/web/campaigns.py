"""Кампании: создание из шаблона или из xlsx, фильтры, очередь, поиск получателей, отчёт."""
import csv
import io

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import auth, campaigns as cmp, db, delivery, leads, worker
from ..tasks import spawn
from ..tg import tgm
from .common import STATE_RU, back, eta_days, local_url, page, read_upload, user

router = APIRouter(prefix="/campaigns")


def _visible_sql(u: dict) -> tuple[str, list]:
    return ("true", []) if auth.is_admin(u) else ("c.created_by=%s", [u["id"]])


def _get(request: Request, cid: int):
    c = db.one("SELECT c.*, u.name AS author FROM campaigns c LEFT JOIN users u ON u.id=c.created_by WHERE c.id=%s", (cid,))
    return c if auth.can_edit_campaign(user(request), c) else None


def _chosen_accounts(u: dict, ids: list) -> list[int]:
    """Только свои (для админа — любые) подключённые аккаунты."""
    allowed = {a["id"] for a in auth.user_accounts(u, only_active=True)}
    return [int(i) for i in ids if str(i).isdigit() and int(i) in allowed]


@router.get("")
async def campaigns_page(request: Request):
    u = user(request)
    w, p = _visible_sql(u)
    rows = db.q(f"""SELECT c.*, u.name AS author, COUNT(cl.id) total,
                    COUNT(*) FILTER (WHERE cl.state='queued') q,
                    COUNT(*) FILTER (WHERE cl.state IN ('sent','read','replied')) s,
                    COUNT(*) FILTER (WHERE cl.state IN ('read','replied')) r,
                    COUNT(*) FILTER (WHERE cl.state='replied') rep,
                    COUNT(*) FILTER (WHERE cl.state='failed') f,
                    (SELECT string_agg(COALESCE(NULLIF(a.label,''), a.first_name, '#' || a.id), ', ')
                     FROM campaign_accounts ca JOIN tg_accounts a ON a.id=ca.account_id WHERE ca.campaign_id=c.id) accs
                    FROM campaigns c LEFT JOIN users u ON u.id=c.created_by LEFT JOIN campaign_leads cl ON cl.campaign_id=c.id
                    WHERE {w} GROUP BY c.id, u.name ORDER BY c.id DESC""", p)
    return page(request, "campaigns.html", rows=rows, tpls=db.q("SELECT id, name FROM templates ORDER BY id DESC"),
                segs=db.q("SELECT id, name FROM segments ORDER BY name"),
                tags=leads.all_tags(), accounts=auth.user_accounts(u, only_active=True),
                recontact_days=cmp.recontact_days())


@router.post("/create")
async def campaigns_create(request: Request):
    u = user(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    tpl = db.one("SELECT * FROM templates WHERE id=%s", (int(form.get("template_id") or 0),))
    accs = _chosen_accounts(u, form.getlist("accounts"))
    if not (name and tpl):
        return back("/campaigns", err="Укажите название и шаблон")
    if not accs:
        return back("/campaigns", err="Выберите хотя бы один свой подключённый аккаунт")
    try:
        cid, queued, skipped = cmp.create_from_template(name, tpl["body"], u["id"], accs, (form.get("tag") or "").strip(),
                                                        bool(form.get("start")), int(form.get("segment_id") or 0) or None)
    except ValueError as e:
        return back("/campaigns", err=str(e))
    worker.wake()
    return back(f"/campaigns/{cid}", msg=f"Кампания создана: в очереди {queued}, пропущено {skipped}")


@router.post("/upload")
async def campaigns_upload(request: Request, file: UploadFile = File(...)):
    u = user(request)
    form = await request.form()
    accs = _chosen_accounts(u, form.getlist("accounts"))
    if not accs:
        return back("/campaigns", err="Выберите хотя бы один свой подключённый аккаунт")
    try:
        cid, n, warns = cmp.import_xlsx(await read_upload(file), file.filename or "список.xlsx", u["id"], accs)
    except Exception as e:
        return back("/campaigns", err=f"Не удалось прочитать файл: {e}")
    return back(f"/campaigns/{cid}", msg=f"Загружено строк: {n}." + (" " + "; ".join(warns) if warns else ""))


@router.get("/{cid}")
async def campaign_view(request: Request, cid: int, show: str = ""):
    c = _get(request, cid)
    if not c:
        return back("/campaigns", err="Кампания не найдена")
    u = user(request)
    f = cmp.parse_filter(cid, request.query_params)
    w, p = cmp.filter_sql(cid, f)
    matched = db.val(f"SELECT COUNT(*) FROM {cmp.FROM_SQL} WHERE {w}", p)
    enq = db.val(f"SELECT COUNT(*) FROM {cmp.FROM_SQL} WHERE {w} AND cl.state IN ('new','failed','skipped')", p)
    stats = {r["state"]: r["n"] for r in db.q("SELECT state, COUNT(*) n FROM campaign_leads WHERE campaign_id=%s GROUP BY state", (cid,))}
    mismatch_n = db.val(f"SELECT COUNT(*) FROM {cmp.FROM_SQL} WHERE cl.campaign_id=%s AND {cmp.MISMATCH_SQL}", (cid,))
    checked_n = db.val("SELECT COUNT(*) FROM campaign_leads WHERE campaign_id=%s AND real_dialog IS NOT NULL", (cid,))
    cols = """cl.*, l.kind, l.title, l.first_name, l.last_name, l.username, l.tg_id, l.phone, l.opted_out_at,
               COALESCE(NULLIF(a.label,''), a.first_name) AS acc_label"""
    joins = f"{cmp.FROM_SQL} LEFT JOIN tg_accounts a ON a.id=cl.account_id"
    if show:
        rows = db.q(f"""SELECT {cols} FROM {joins} WHERE cl.campaign_id=%s AND cl.state=%s
                        ORDER BY cl.sent_at DESC NULLS LAST, cl.order_idx NULLS LAST, cl.id LIMIT 500""", (cid, show))
    else:
        rows = db.q(f"SELECT {cols} FROM {joins} WHERE {w} ORDER BY cl.order_idx NULLS LAST, cl.id LIMIT 300", p)
    accs = cmp.campaign_accounts(cid)
    per_acc = []
    for a in accs:
        q = db.val("SELECT COUNT(*) FROM campaign_leads WHERE campaign_id=%s AND account_id=%s AND state='queued'", (cid, a["id"]))
        per_acc.append({**a, "queued": q, "limit": worker.daily_limit(a), "sent_today": worker.sent_today(a["id"]),
                        "authorized": tgm.get(a["id"]).authorized, "paused": worker.paused_until(a),
                        "wstate": worker.state.get(a["id"], {})})
    per_day = sum(a["limit"] for a in per_acc if a["status"] == "active")
    chain = [{"position": 1, "body": cmp.step_body(cid), "delay_days": 0, "condition": None, "id": None}] + cmp.followups(cid)
    step_stats = {r["step"]: r for r in db.q("""SELECT m.step, COUNT(*) sent FROM messages m JOIN campaign_leads cl ON cl.id=m.campaign_lead_id
                                                WHERE cl.campaign_id=%s AND m.direction='out' GROUP BY m.step""", (cid,))}
    for r in db.q("""SELECT step, COUNT(*) FILTER (WHERE state='replied') replied,
                     COUNT(*) FILTER (WHERE next_step_at IS NOT NULL) waiting
                     FROM campaign_leads WHERE campaign_id=%s GROUP BY step""", (cid,)):
        step_stats.setdefault(r["step"], {"sent": 0}).update(replied=r["replied"], waiting=r["waiting"])
    return page(request, "campaign.html", c=c, f=f, facets=cmp.facets(cid), matched=matched, enq=enq, stats=stats,
                chain=chain, step_stats=step_stats, CONDITIONS=cmp.CONDITIONS, max_followups=cmp.MAX_FOLLOWUPS,
                ai_profiles=db.q("SELECT id, name FROM ai_profiles ORDER BY name"),
                rows=rows, show=show, total=sum(stats.values()), step=cmp.step_body(cid),
                eta_days=eta_days(stats.get("queued", 0), per_day), accs=per_acc,
                my_accounts=auth.user_accounts(u, only_active=True),
                prep=tgm.prepare_state.get(cid), mismatch_n=mismatch_n, checked_n=checked_n,
                acc_names={a["id"]: a["label"] or a["first_name"] or f"#{a['id']}" for a in accs})


@router.post("/{cid}/enqueue")
async def campaign_enqueue(request: Request, cid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    form = await request.form()
    try:
        n, skipped = cmp.enqueue(cid, cmp.parse_filter(cid, form))
    except ValueError as e:
        return back(f"/campaigns/{cid}", err=str(e))
    msg = f"В очередь добавлено: {n}" + (f", пропущено {skipped} (причина — в строках)" if skipped else "")
    if form.get("start") and n:
        db.ex("UPDATE campaigns SET status='running', started_at=COALESCE(started_at, now()) WHERE id=%s", (cid,))
        worker.wake()
        msg += " · отправка запущена"
    db.log(f"Кампания #{cid}: в очередь {n}, пропущено {skipped}")
    return back(f"/campaigns/{cid}?show=queued", msg=msg)


@router.get("/{cid}/prepare-status")
async def prepare_status(request: Request, cid: int):
    if not _get(request, cid):
        return {"running": False, "step": "нет доступа"}
    st = tgm.prepare_state.get(cid) or {"running": False, "step": "не запускался"}
    return {k: v for k, v in st.items() if k != "accounts"}


@router.post("/{cid}/accounts")
async def campaign_set_accounts(request: Request, cid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    form = await request.form()
    accs = _chosen_accounts(user(request), form.getlist("accounts"))
    # чужие аккаунты, уже добавленные админом, менеджер не убирает
    keep = [a["id"] for a in cmp.campaign_accounts(cid)
            if not auth.can_use_account(user(request), a)]
    if not accs + keep:
        return back(f"/campaigns/{cid}", err="Нужен хотя бы один аккаунт")
    cmp.set_accounts(cid, accs + keep)
    return back(f"/campaigns/{cid}", msg="Аккаунты кампании обновлены. Уже закреплённые лиды остаются за своими аккаунтами")


@router.post("/{cid}/autoreply")
async def campaign_autoreply(request: Request, cid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    on = bool((await request.form()).get("on"))
    db.ex("UPDATE campaigns SET ai_autoreply=%s WHERE id=%s", (on, cid))
    if not on:
        db.ex("""UPDATE ai_reply_jobs SET status='cancelled', reason='автоответ выключен в кампании', done_at=now()
                 WHERE campaign_id=%s AND status='pending'""", (cid,))
    db.log(f"Кампания #{cid}: автоответы ИИ {'включены' if on else 'выключены'} ({user(request)['login']})", "warn")
    return back(f"/campaigns/{cid}", msg="ИИ отвечает сам в этой кампании" if on else "Автоответы выключены")


@router.post("/{cid}/ai-profile")
async def campaign_ai_profile(request: Request, cid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    v = (await request.form()).get("ai_profile_id") or ""
    pid = int(v) if v.isdigit() and db.one("SELECT 1 FROM ai_profiles WHERE id=%s", (int(v),)) else None
    db.ex("UPDATE campaigns SET ai_profile_id=%s WHERE id=%s", (pid, cid))
    return back(f"/campaigns/{cid}", msg="ИИ-профиль кампании сохранён" if pid else "ИИ-профиль снят: будет общая инструкция")


# ---------- цепочка: дожимы ----------
def _step_form(form) -> tuple[dict, str]:
    body = (form.get("body") or "").strip()
    days = str(form.get("delay_days") or "").strip()
    cond = form.get("condition") or "no_reply"
    if not body:
        return {}, "Напишите текст дожима"
    if not (days.isdigit() and 1 <= int(days) <= 60):
        return {}, "Задержка — от 1 до 60 дней"
    if cond not in cmp.CONDITIONS:
        return {}, "Неизвестное условие"
    return {"body": body, "delay_days": int(days), "condition": cond}, ""


@router.post("/{cid}/steps/add")
async def step_add(request: Request, cid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    if len(cmp.followups(cid)) >= cmp.MAX_FOLLOWUPS:
        return back(f"/campaigns/{cid}", err=f"Не больше {cmp.MAX_FOLLOWUPS} дожимов: дальше люди чаще жалуются, чем отвечают")
    vals, err = _step_form(await request.form())
    if err:
        return back(f"/campaigns/{cid}", err=err)
    pos = max(2, (db.val("SELECT MAX(position) FROM campaign_steps WHERE campaign_id=%s", (cid,)) or 1) + 1)
    db.ex("""INSERT INTO campaign_steps(campaign_id, position, body, delay_days, condition)
             VALUES (%s, %s, %s, %s, %s)""", (cid, pos, vals["body"], vals["delay_days"], vals["condition"]))
    n = cmp.reschedule(cid)
    return back(f"/campaigns/{cid}", msg="Дожим добавлен" + (f"; запланирован для {n} человек, которые не ответили" if n else ""))


@router.post("/{cid}/steps/{sid}")
async def step_save(request: Request, cid: int, sid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    vals, err = _step_form(await request.form())
    if err:
        return back(f"/campaigns/{cid}", err=err)
    db.ex("""UPDATE campaign_steps SET body=%s, delay_days=%s, condition=%s
             WHERE id=%s AND campaign_id=%s AND position > 1""",
          (vals["body"], vals["delay_days"], vals["condition"], sid, cid))
    cmp.reschedule(cid)
    return back(f"/campaigns/{cid}", msg="Дожим сохранён")


@router.post("/{cid}/steps/{sid}/delete")
async def step_delete(request: Request, cid: int, sid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    db.ex("DELETE FROM campaign_steps WHERE id=%s AND campaign_id=%s AND position > 1", (sid, cid))
    cmp.reschedule(cid)
    return back(f"/campaigns/{cid}", msg="Дожим удалён")


@router.post("/{cid}/{action}")
async def campaign_action(request: Request, cid: int, action: str):
    c = _get(request, cid)
    if not c:
        return back("/campaigns", err="Кампания не найдена")
    if action == "start":
        if not db.one("SELECT 1 FROM campaign_leads WHERE campaign_id=%s AND state='queued'", (cid,)):
            return back(f"/campaigns/{cid}", err="Очередь пуста — сначала добавьте строки кнопкой «В очередь»")
        db.ex("UPDATE campaigns SET status='running', started_at=COALESCE(started_at, now()) WHERE id=%s", (cid,))
        worker.wake()
    elif action == "pause":
        db.ex("UPDATE campaigns SET status='paused' WHERE id=%s", (cid,))
    elif action == "unqueue":
        db.ex("UPDATE campaign_leads SET state='new', order_idx=NULL WHERE campaign_id=%s AND state='queued'", (cid,))
        return back(f"/campaigns/{cid}", msg="Очередь очищена. Закрепление лидов за аккаунтами сохранено")
    elif action == "retry":
        n = db.changed("UPDATE campaign_leads SET state='new', error=NULL WHERE campaign_id=%s AND state='failed'", (cid,))
        return back(f"/campaigns/{cid}", msg=f"Ошибочные строки возвращены в «новые»: {n}. Поставьте их в очередь")
    elif action == "prepare":
        st = tgm.prepare_state.get(cid)
        if not (st and st.get("running")):
            spawn(tgm.prepare_campaign(cid), f"поиск получателей кампании #{cid}")
        return back(f"/campaigns/{cid}", msg="Ищу получателей: загружаю диалоги и участников чатов…")
    elif action == "delete":
        db.ex("DELETE FROM campaigns WHERE id=%s", (cid,))
        db.log(f"Кампания «{c['name']}» удалена ({user(request)['login']})", "warn")
        return back("/campaigns", msg="Кампания удалена. Переписка с лидами сохранена")
    return back(f"/campaigns/{cid}")


# ---------- строки кампании ----------
def _row(request: Request, rid: int):
    cl = db.one("SELECT * FROM campaign_leads WHERE id=%s", (rid,))
    if not cl or not _get(request, cl["campaign_id"]):
        return None
    return cl


@router.post("/row/{rid}/send")
async def row_send_now(request: Request, rid: int):
    """Тестовая отправка одной строки сразу — мимо очереди, лимита и рабочих часов."""
    cl = _row(request, rid)
    if not cl:
        return back("/campaigns", err="Строка не найдена")
    ref = local_url(request.headers.get("referer"), f"/campaigns/{cl['campaign_id']}")
    try:
        res, why = await delivery.send_now(rid)
    except delivery.TRANSIENT_ERRORS as e:
        return back(ref, err=f"Нет связи с Telegram ({type(e).__name__}), строка оставлена как была — повторите позже")
    except ValueError as e:
        return back(ref, err=str(e))
    if res == "busy":
        return back(ref, err="Эта строка уже отправляется — обновите страницу")
    lead = db.one("SELECT l.* FROM leads l JOIN campaign_leads cl ON cl.lead_id=l.id WHERE cl.id=%s", (rid,))
    who = lead["title"] or lead["first_name"] or f"строка {rid}"
    db.log(f"Тестовая отправка «{who}»: {res}{' — ' + why if why else ''}")
    if res == "sent":
        return back(ref, msg=f"Отправлено сейчас: {who}")
    return back(ref, err=f"Не отправлено ({who}): {why or res}")


@router.post("/row/{rid}/{action}")
async def row_action(request: Request, rid: int, action: str):
    cl = _row(request, rid)
    if not cl:
        return back("/campaigns", err="Строка не найдена")
    ref = local_url(request.headers.get("referer"), f"/campaigns/{cl['campaign_id']}")
    if action == "skip":
        db.ex("""UPDATE campaign_leads SET state='skipped', error='исключено вручную'
                 WHERE id=%s AND state IN ('new','queued','failed')""", (rid,))
    elif action == "reset":
        db.ex("""UPDATE campaign_leads SET state='new', error=NULL, order_idx=NULL
                 WHERE id=%s AND state IN ('skipped','failed')""", (rid,))
    return back(ref)


@router.get("/{cid}/export.csv")
async def campaign_export(request: Request, cid: int):
    if not _get(request, cid):
        return back("/campaigns", err="Кампания не найдена")
    rows = db.q("""SELECT cl.row_no, l.kind, COALESCE(l.title, ''), l.first_name, l.last_name, l.username, l.tg_id,
                   COALESCE(NULLIF(a.label,''), a.first_name, ''), cl.topic_title, cl.dialog, cl.address, cl.src_status,
                   cl.state, cl.error, cl.sent_at, cl.read_at, cl.replied_at
                   FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id LEFT JOIN tg_accounts a ON a.id=cl.account_id
                   WHERE cl.campaign_id=%s ORDER BY cl.id""", (cid,))
    buf = io.StringIO()
    buf.write("﻿")  # для Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Исх. №", "Тип", "Название", "Имя", "Фамилия", "Username", "Telegram ID", "Аккаунт", "Куда",
                "Диалог", "Ты/вы", "Статус из файла", "Статус отправки", "Ошибка", "Отправлено", "Прочитано", "Ответ"])
    for r in rows:
        r = list(r.values())
        r[1] = cmp.KIND_RU.get(r[1], r[1])
        r[12] = STATE_RU.get(r[12], r[12])
        r[14:17] = [v.isoformat(timespec="minutes") if v else "" for v in r[14:17]]
        w.writerow(r)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=campaign_{cid}.csv"})
