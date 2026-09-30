"""Лиды: общая база команды, импорт, отписка."""
from fastapi import APIRouter, File, Form, Request, UploadFile

from .. import auth, db, leads
from .common import back, local_url, page, user

router = APIRouter(prefix="/leads")
PER_PAGE = 100


@router.get("")
async def leads_page(request: Request, tag: str = "", s: str = "", owner: str = "", segment: int = 0, p: int = 1):
    u = user(request)
    where, params = ["true"], []
    if tag:
        where.append("%s = ANY(l.tags)")
        params.append(tag)
    if s:
        where.append("(l.first_name ILIKE %s OR l.last_name ILIKE %s OR l.username ILIKE %s OR l.phone ILIKE %s "
                     "OR l.title ILIKE %s)")
        params += [f"%{s}%"] * 5
    if segment:
        where.append("l.id IN (SELECT lead_id FROM segment_leads WHERE segment_id=%s)")
        params.append(segment)
    if owner == "mine":
        where.append("a.user_id=%s")
        params.append(u["id"])
    elif owner == "none":
        where.append("l.owner_account_id IS NULL")
    elif owner.isdigit():
        where.append("l.owner_account_id=%s")
        params.append(int(owner))
    w = " AND ".join(where)
    base = f"FROM leads l LEFT JOIN tg_accounts a ON a.id=l.owner_account_id WHERE {w}"
    total = db.val(f"SELECT COUNT(*) {base}", params)
    rows = db.q(f"""SELECT l.*, a.label AS owner_label, a.first_name AS owner_first,
                    (SELECT MAX(m.created_at) FROM messages m WHERE m.lead_id=l.id AND m.direction='out') last_out,
                    (SELECT MAX(m.created_at) FROM messages m WHERE m.lead_id=l.id AND m.direction='in') last_in
                    {base} ORDER BY l.id DESC LIMIT %s OFFSET %s""", params + [PER_PAGE, (max(p, 1) - 1) * PER_PAGE])
    return page(request, "leads.html", rows=rows, total=total, tag=tag, s=s, owner=owner, segment=segment, p=p,
                per_page=PER_PAGE, segs=db.q("SELECT id, name FROM segments ORDER BY name"),
                tags=leads.all_tags(), accounts=auth.user_accounts(u, only_active=True),
                all_accounts=db.q("SELECT id, label, first_name FROM tg_accounts WHERE tg_user_id IS NOT NULL ORDER BY id"))


@router.post("/import-csv")
async def import_csv(request: Request, file: UploadFile = File(...), tag: str = Form("")):
    added, updated, bad = leads.import_csv(await file.read(), tag)
    db.log(f"Импорт CSV {file.filename}: +{added}, обновлено {updated}, пропущено {bad} ({user(request)['login']})")
    return back("/leads", msg=f"Добавлено {added}, обновлено {updated}, пропущено строк {bad}")


@router.post("/{lid}/optout")
async def toggle_optout(request: Request, lid: int):
    lead = db.one("SELECT * FROM leads WHERE id=%s", (lid,))
    if not lead:
        return back("/leads", err="Лид не найден")
    if lead["opted_out_at"]:
        if not auth.is_admin(user(request)):
            return back("/leads", err="Вернуть отписавшегося может только админ")
        leads.opt_in(lid)
    else:
        leads.opt_out(lid, f"исключён вручную ({user(request)['login']})")
    return back(local_url(request.headers.get("referer"), "/leads"))


@router.post("/delete")
async def delete_leads(request: Request, tag: str = Form(""), confirm: str = Form("")):
    if not auth.is_admin(user(request)):
        return back("/leads", err="Удалять лидов может только админ")
    if confirm != "да":
        return back("/leads", err="Для удаления введите «да»")
    # лиды с перепиской не удаляем — иначе потеряется история и защита от повторных сообщений
    cond = "NOT EXISTS (SELECT 1 FROM messages m WHERE m.lead_id=leads.id)"
    if tag:
        n = db.changed(f"DELETE FROM leads WHERE %s = ANY(tags) AND {cond}", (tag,))
    else:
        n = db.changed(f"DELETE FROM leads WHERE {cond}")
    db.log(f"Удалено лидов: {n} ({user(request)['login']})", "warn")
    return back("/leads", msg=f"Удалено {n}. Лиды с перепиской сохранены")
