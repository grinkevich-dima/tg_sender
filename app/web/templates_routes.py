"""Шаблоны сообщений (библиотека команды) и тест в «Избранное»."""
from fastapi import APIRouter, Form, Request
from telethon import errors

from .. import auth, db
from ..templating import render, variables_in
from ..tg import tgm
from .common import back, page, user

router = APIRouter(prefix="/templates")
SAMPLE = {"first_name": "Иван", "name": "Иван Петров"}


def _sample_vars():
    lead = db.one("SELECT * FROM leads WHERE opted_out_at IS NULL AND kind='person' AND first_name!='' ORDER BY id LIMIT 1")
    return db.lead_vars(lead) if lead else SAMPLE


@router.get("")
async def templates_page(request: Request, edit: int = 0):
    tpl = db.one("SELECT * FROM templates WHERE id=%s", (edit,)) if edit else None
    rows = db.q("SELECT t.*, u.name AS author FROM templates t LEFT JOIN users u ON u.id=t.created_by ORDER BY t.id DESC")
    sample = _sample_vars()
    return page(request, "templates.html", rows=rows, tpl=tpl,
                previews={t["id"]: render(t["body"], sample) for t in rows},
                vars={t["id"]: variables_in(t["body"]) for t in rows},
                accounts=[a for a in auth.user_accounts(user(request), only_active=True) if tgm.get(a["id"]).authorized])


@router.post("/save")
async def templates_save(request: Request, name: str = Form(...), body: str = Form(...), tid: int = Form(0)):
    if tid:
        db.ex("UPDATE templates SET name=%s, body=%s WHERE id=%s", (name, body, tid))
    else:
        db.ex("INSERT INTO templates(name, body, created_by) VALUES (%s, %s, %s)", (name, body, user(request)["id"]))
    return back("/templates", msg="Шаблон сохранён")


@router.post("/{tid}/delete")
async def templates_delete(tid: int):
    db.ex("DELETE FROM templates WHERE id=%s", (tid,))
    return back("/templates", msg="Удалён. Кампании хранят свою копию текста и не пострадают")


@router.post("/{tid}/test")
async def templates_test(request: Request, tid: int, account_id: int = Form(0)):
    t = db.one("SELECT * FROM templates WHERE id=%s", (tid,))
    if not t:
        return back("/templates", err="Шаблон не найден")
    acc = db.one("SELECT * FROM tg_accounts WHERE id=%s", (account_id,))
    if not auth.can_use_account(user(request), acc) or not tgm.get(account_id).authorized:
        return back("/templates", err="Выберите свой подключённый аккаунт")
    if tgm.preparing_account(account_id):
        return back("/templates", err="Идёт поиск получателей — попробуйте позже")
    client = tgm.get(account_id)
    try:
        async with client.lock:
            await client.client.send_message("me", "🧪 Тест шаблона «%s»:\n\n%s" % (t["name"], render(t["body"], _sample_vars())))
    except errors.FloodWaitError as e:
        return back("/templates", err=f"Telegram просит подождать {e.seconds} сек")
    except errors.RPCError as e:
        return back("/templates", err=f"Не отправлено: {e}")
    return back("/templates", msg="Отправлено в «Избранное»")
