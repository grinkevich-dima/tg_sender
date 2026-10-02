"""Инбокс: диалоги с лидами, ответ из панели, этап воронки, заметка."""
from fastapi import APIRouter, Form, Request

from .. import ai, auth, autopilot, db, delivery, inbox, leads
from .common import back, page, user

router = APIRouter(prefix="/inbox")
PER_PAGE = 50
SOURCE_RU = {"campaign": "кампания", "inbox": "из панели", "telegram": "в Telegram", "incoming": "", "ai": "🤖 ИИ"}


def visible_accounts(u: dict) -> list[int] | None:
    """Чьи диалоги видит пользователь: админ — все (None), менеджер — своих аккаунтов."""
    return None if auth.is_admin(u) else [a["id"] for a in auth.user_accounts(u)]


def _lead(request: Request, lead_id: int) -> dict | None:
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lead_id,))
    acc_ids = visible_accounts(user(request))
    if not lead or (acc_ids is not None and lead["owner_account_id"] not in acc_ids):
        return None
    return lead


def _dialogs(request: Request, f: dict) -> tuple[list[dict], int]:
    acc_ids = visible_accounts(user(request))
    where = ["EXISTS (SELECT 1 FROM messages m WHERE m.lead_id=l.id)"]
    params: list = []
    if acc_ids is not None:
        where.append("l.owner_account_id = ANY(%s)")
        params.append(acc_ids)
    if f["mine"] and acc_ids is None:
        where.append("a.user_id=%s")
        params.append(user(request)["id"])
    if f["stage"]:
        where.append("l.stage_id = %s" if f["stage"] > 0 else "l.stage_id IS NULL")
        if f["stage"] > 0:
            params.append(f["stage"])
    if f["segment"]:
        where.append("l.id IN (SELECT lead_id FROM segment_leads WHERE segment_id=%s)")
        params.append(f["segment"])
    if f["q"]:
        where.append("(l.first_name ILIKE %s OR l.last_name ILIKE %s OR l.username ILIKE %s OR l.title ILIKE %s)")
        params += [f"%{f['q']}%"] * 4
    if f["show"] == "waiting":       # последнее сообщение — от человека: ждёт ответа
        where.append("last.direction='in'")
    elif f["show"] == "handoff":
        where.append("l.ai_handoff IS NOT NULL")
    elif f["show"] == "unread":
        where.append("last.direction='in' AND last.created_at > COALESCE(l.inbox_read_at, '-infinity')")
    w = " AND ".join(where)
    base = f"""FROM leads l LEFT JOIN tg_accounts a ON a.id=l.owner_account_id
               LEFT JOIN funnel_stages s ON s.id=l.stage_id
               LEFT JOIN LATERAL (SELECT m.* FROM messages m WHERE m.lead_id=l.id ORDER BY m.created_at DESC, m.id DESC LIMIT 1) last ON true
               WHERE {w}"""
    total = db.val(f"SELECT COUNT(*) {base}", params)
    rows = db.q(f"""SELECT l.*, s.name AS stage_name, s.is_goal, s.is_lost,
                    COALESCE(NULLIF(a.label,''), a.first_name) AS acc_label,
                    last.text AS last_text, last.direction AS last_dir, last.created_at AS last_at,
                    (last.direction='in' AND last.created_at > COALESCE(l.inbox_read_at, '-infinity')) AS unread
                    {base} ORDER BY last.created_at DESC NULLS LAST LIMIT %s OFFSET %s""",
                params + [PER_PAGE, (max(f["p"], 1) - 1) * PER_PAGE])
    return rows, total


def _filters(qp) -> dict:
    def num(k):
        v = qp.get(k, "")
        return int(v) if str(v).lstrip("-").isdigit() else 0
    return {"show": qp.get("show", "all"), "stage": num("stage"), "segment": num("segment"), "q": qp.get("q", "").strip(),
            "mine": qp.get("mine") == "1", "p": num("p") or 1}


def _ctx(request: Request) -> dict:
    f = _filters(request.query_params)
    rows, total = _dialogs(request, f)
    qs = "&".join(f"{k}={v}" for k, v in request.query_params.items() if k != "p")
    return {"rows": rows, "total": total, "f": f, "qs": qs, "per_page": PER_PAGE, "stages": inbox.stages(),
            "segs": db.q("SELECT id, name FROM segments ORDER BY name"),
            "unread_n": inbox.unread_count(visible_accounts(user(request)))}


@router.get("")
async def inbox_page(request: Request):
    return page(request, "inbox.html", lead=None, **_ctx(request))


@router.get("/unread")
async def inbox_unread(request: Request):
    return {"unread": inbox.unread_count(visible_accounts(user(request)))}


@router.get("/{lead_id}")
async def dialog_page(request: Request, lead_id: int):
    lead = _lead(request, lead_id)
    if not lead:
        return back("/inbox", err="Диалог не найден")
    inbox.mark_read(lead_id)
    msgs = db.q("""SELECT m.*, u.name AS sender_name, c.name AS campaign_name, c.id AS campaign_id
                   FROM messages m LEFT JOIN users u ON u.id=m.sender_user_id
                   LEFT JOIN campaign_leads cl ON cl.id=m.campaign_lead_id LEFT JOIN campaigns c ON c.id=cl.campaign_id
                   WHERE m.lead_id=%s ORDER BY m.created_at, m.id""", (lead_id,))
    card = {
        "account": db.one("SELECT * FROM tg_accounts WHERE id=%s", (lead["owner_account_id"],)),
        "segments": db.q("""SELECT s.id, s.name FROM segment_leads sl JOIN segments s ON s.id=sl.segment_id
                            WHERE sl.lead_id=%s ORDER BY s.name""", (lead_id,)),
        "campaigns": db.q("""SELECT c.id, c.name, cl.state FROM campaign_leads cl JOIN campaigns c ON c.id=cl.campaign_id
                             WHERE cl.lead_id=%s ORDER BY c.id DESC""", (lead_id,)),
    }
    profiles = db.q("SELECT id, name FROM ai_profiles ORDER BY name")
    camp = autopilot.campaign_for_lead(lead_id)
    card["autopilot"] = {"campaign": camp, "on": bool(camp and camp["ai_autoreply"]) and autopilot.enabled(),
                         "job": db.one("SELECT * FROM ai_reply_jobs WHERE lead_id=%s AND status IN ('pending','sending')",
                                       (lead_id,))}
    return page(request, "inbox.html", lead=db.one("SELECT * FROM leads WHERE id=%s", (lead_id,)), msgs=msgs, card=card,
                ai_on=ai.configured(), ai_profiles=profiles, ai_profile=ai.lead_profile(lead_id), LABELS=ai.LABELS,
                LABEL_STAGE=ai.LABEL_STAGE,
                SOURCE_RU=SOURCE_RU, last_id=max((m["id"] for m in msgs), default=0), **_ctx(request))


@router.get("/{lead_id}/messages")
async def dialog_new_messages(request: Request, lead_id: int, after: int = 0):
    """Новые сообщения диалога для подгрузки без перезагрузки."""
    if not _lead(request, lead_id):
        return {"messages": []}
    rows = db.q("""SELECT id, direction, text, source, created_at FROM messages WHERE lead_id=%s AND id > %s
                   ORDER BY created_at, id""", (lead_id, after))
    if any(r["direction"] == "in" for r in rows):
        inbox.mark_read(lead_id)
    deleted = [r["id"] for r in db.q("SELECT id FROM messages WHERE lead_id=%s AND deleted_at IS NOT NULL", (lead_id,))]
    return {"messages": [{**r, "created_at": r["created_at"].astimezone(db.now_local().tzinfo).strftime("%d.%m %H:%M")}
                         for r in rows], "deleted": deleted}


def _back_to(lead_id: int, request: Request) -> str:
    """Обратно в диалог с теми же фильтрами списка (формы шлют их в адресе)."""
    return f"/inbox/{lead_id}" + (f"?{request.url.query}" if request.url.query else "")


@router.post("/{lead_id}/draft")
async def dialog_draft(request: Request, lead_id: int, profile_id: int = Form(0)):
    """Черновик ответа от ИИ (JSON для кнопки «Предложить ответ»)."""
    if not _lead(request, lead_id):
        return {"error": "Диалог не найден"}
    try:
        d = await ai.draft(lead_id, user(request)["id"], profile_id or None)
    except ai.AIError as e:
        return {"error": str(e)}
    return d


@router.post("/{lead_id}/apply-label/{message_id}")
async def dialog_apply_label(request: Request, lead_id: int, message_id: int):
    """Принять подсказку ИИ по входящему: поставить этап или отписать."""
    if not _lead(request, lead_id):
        return back("/inbox", err="Диалог не найден")
    label = db.val("SELECT ai_label FROM messages WHERE id=%s AND lead_id=%s", (message_id, lead_id))
    if label == "stop":
        leads.opt_out(lead_id, f"по разбору ИИ, подтвердил {user(request)['login']}")
        return back(_back_to(lead_id, request), msg="Лид отписан")
    stage = ai.LABEL_STAGE.get(label)
    if stage:
        inbox.set_stage(lead_id, db.val("SELECT id FROM funnel_stages WHERE name=%s", (stage,)))
        return back(_back_to(lead_id, request), msg=f"Этап «{stage}»")
    return back(_back_to(lead_id, request))


@router.post("/{lead_id}/send")
async def dialog_send(request: Request, lead_id: int, text: str = Form(""), draft_id: int = Form(0)):
    if not _lead(request, lead_id):
        return back("/inbox", err="Диалог не найден")
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lead_id,))
    acc = db.one("SELECT * FROM tg_accounts WHERE id=%s", (lead["owner_account_id"],)) if lead["owner_account_id"] else None
    if not auth.can_use_account(user(request), acc):
        return back(_back_to(lead_id, request), err="Отвечать может менеджер аккаунта, за которым закреплён лид")
    err = await delivery.send_reply(lead_id, text, user(request)["id"])
    if err:
        return back(_back_to(lead_id, request), err=err)
    autopilot.manager_intervened(lead_id, "из инбокса")
    db.ex("UPDATE leads SET ai_handoff=NULL WHERE id=%s", (lead_id,))       # человек ответил — вопрос закрыт
    if draft_id and ai.learn_from_send(draft_id, lead_id, text.strip()):
        return back(_back_to(lead_id, request), msg="Отправлено. Черновик ушёл почти без правок — сохранён как пример для ИИ")
    return back(_back_to(lead_id, request))


@router.post("/{lead_id}/autopilot/{action}")
async def dialog_autopilot(request: Request, lead_id: int, action: str):
    """Включить/выключить автоответы ИИ для этого человека."""
    if not _lead(request, lead_id):
        return back("/inbox", err="Диалог не найден")
    if action == "on":
        db.ex("UPDATE leads SET ai_paused=false, ai_handoff=NULL WHERE id=%s", (lead_id,))
        return back(_back_to(lead_id, request), msg="Автопилот включён: ИИ ответит на следующее сообщение")
    db.ex("UPDATE leads SET ai_paused=true WHERE id=%s", (lead_id,))
    db.ex("""UPDATE ai_reply_jobs SET status='cancelled', reason='выключен вручную', done_at=now()
             WHERE lead_id=%s AND status='pending'""", (lead_id,))
    return back(_back_to(lead_id, request), msg="Автопилот для этого человека выключен")


@router.post("/{lead_id}/stage")
async def dialog_stage(request: Request, lead_id: int, stage_id: int = Form(0)):
    if not _lead(request, lead_id):
        return back("/inbox", err="Диалог не найден")
    if stage_id and not db.one("SELECT 1 FROM funnel_stages WHERE id=%s", (stage_id,)):
        return back(_back_to(lead_id, request), err="Нет такого этапа")
    inbox.set_stage(lead_id, stage_id or None)
    return back(_back_to(lead_id, request), msg="Этап изменён")


@router.post("/{lead_id}/note")
async def dialog_note(request: Request, lead_id: int, note: str = Form("")):
    if not _lead(request, lead_id):
        return back("/inbox", err="Диалог не найден")
    db.ex("UPDATE leads SET note=%s WHERE id=%s", (note.strip()[:2000], lead_id))
    return back(_back_to(lead_id, request), msg="Заметка сохранена")


@router.post("/{lead_id}/optout")
async def dialog_optout(request: Request, lead_id: int):
    if not _lead(request, lead_id):
        return back("/inbox", err="Диалог не найден")
    leads.opt_out(lead_id, f"исключён из инбокса ({user(request)['login']})")
    return back(_back_to(lead_id, request), msg="Лид отписан: больше никаких сообщений от команды")
