"""Сегменты: создание, наполнение из CSV / Telegram / по тегу, состав."""
import asyncio

from fastapi import APIRouter, File, Form, Request, UploadFile
from psycopg.errors import UniqueViolation
from telethon import errors

from .. import auth, db, inbox, leads, segments
from ..tasks import spawn
from ..tg import tgm
from .common import UploadTooLarge, back, page, read_upload, user

router = APIRouter(prefix="/segments")
PER_PAGE = 100


def _can_edit(u: dict, seg: dict | None) -> bool:
    return bool(seg) and (auth.is_admin(u) or seg["created_by"] == u["id"])


@router.get("")
async def segments_page(request: Request):
    return page(request, "segments.html", rows=segments.listing())


@router.post("/create")
async def segments_create(request: Request, name: str = Form(...), description: str = Form("")):
    if not name.strip():
        return back("/segments", err="Укажите название")
    try:
        sid = segments.create(name, description, user(request)["id"])
    except UniqueViolation:
        return back("/segments", err="Сегмент с таким названием уже есть")
    return back(f"/segments/{sid}", msg="Сегмент создан — наполните его")


@router.get("/{sid}")
async def segment_view(request: Request, sid: int, s: str = "", p: int = 1):
    seg = segments.get(sid)
    if not seg:
        return back("/segments", err="Сегмент не найден")
    u = user(request)
    where, params = ["sl.segment_id=%s"], [sid]
    if s:
        where.append("(l.first_name ILIKE %s OR l.last_name ILIKE %s OR l.username ILIKE %s OR l.phone ILIKE %s)")
        params += [f"%{s}%"] * 4
    w = " AND ".join(where)
    total = db.val(f"SELECT COUNT(*) FROM segment_leads sl JOIN leads l ON l.id=sl.lead_id WHERE {w}", params)
    rows = db.q(f"""SELECT l.*, sl.source AS seg_source, sl.added_at, a.label AS owner_label, a.first_name AS owner_first,
                    (SELECT MAX(m.created_at) FROM messages m WHERE m.lead_id=l.id AND m.direction='out') last_out
                    FROM segment_leads sl JOIN leads l ON l.id=sl.lead_id LEFT JOIN tg_accounts a ON a.id=l.owner_account_id
                    WHERE {w} ORDER BY sl.added_at DESC, l.id DESC LIMIT %s OFFSET %s""",
                params + [PER_PAGE, (max(p, 1) - 1) * PER_PAGE])
    stats = next((r for r in segments.listing() if r["id"] == sid), {})
    accounts = [a for a in auth.user_accounts(u, only_active=True) if tgm.get(a["id"]).authorized]
    acc_ids = [a["id"] for a in accounts]
    groups = db.q("""SELECT d.*, COALESCE(NULLIF(a.label,''), a.first_name) acc_label FROM tg_dialogs d
                     JOIN tg_accounts a ON a.id=d.account_id
                     WHERE d.is_admin AND d.account_id = ANY(%s) ORDER BY d.title""", (acc_ids,))
    return page(request, "segment.html", seg=seg, rows=rows, total=total, s=s, p=p, per_page=PER_PAGE, stats=stats,
                can_edit=_can_edit(u, seg), tags=leads.all_tags(), accounts=accounts, groups=groups,
                funnel=inbox.funnel(segment_id=sid),
                groups_state={a: tgm.groups_state.get(a) for a in acc_ids if tgm.groups_state.get(a)})


@router.post("/{sid}/import-csv")
async def segment_import_csv(request: Request, sid: int, file: UploadFile = File(...), tag: str = Form("")):
    if not segments.get(sid):
        return back("/segments", err="Сегмент не найден")
    before = segments.size(sid)
    try:
        raw = await read_upload(file)
    except UploadTooLarge as e:
        return back(f"/segments/{sid}", err=str(e))
    added, updated, bad = leads.import_csv(raw, tag, segment_id=sid)
    joined = segments.size(sid) - before
    db.log(f"Сегмент #{sid}: CSV {file.filename} — в сегмент добавлено {joined} (новых лидов {added}, пропущено {bad})")
    return back(f"/segments/{sid}", msg=f"В сегмент добавлено {joined}. Новых лидов в базе: {added}, "
                                        f"уже были: {updated}, пропущено строк без username/телефона/ID: {bad}")


@router.post("/{sid}/import-tg")
async def segment_import_tg(request: Request, sid: int, account_id: int = Form(...), source: str = Form("contacts"),
                            tag: str = Form("")):
    if not segments.get(sid):
        return back("/segments", err="Сегмент не найден")
    acc = db.one("SELECT * FROM tg_accounts WHERE id=%s", (account_id,))
    client = tgm.get(account_id)
    if not auth.can_use_account(user(request), acc) or not client.authorized:
        return back(f"/segments/{sid}", err="Выберите свой подключённый аккаунт")
    before = segments.size(sid)
    try:
        if source == "dialogs":
            await client.import_dialogs(tag, segment_id=sid)
        else:
            await client.import_contacts(tag, segment_id=sid)
    except errors.RPCError as e:
        return back(f"/segments/{sid}", err=str(e))
    joined = segments.size(sid) - before
    db.log(f"Сегмент #{sid}: из Telegram ({source}) добавлено {joined}", account_id=account_id)
    return back(f"/segments/{sid}", msg=f"В сегмент добавлено {joined}. Эти лиды закреплены за аккаунтом, из которого импортированы")


@router.post("/{sid}/refresh-groups")
async def segment_refresh_groups(request: Request, sid: int, account_id: int = Form(...)):
    acc = db.one("SELECT * FROM tg_accounts WHERE id=%s", (account_id,))
    if not auth.can_use_account(user(request), acc) or not tgm.get(account_id).authorized:
        return back(f"/segments/{sid}", err="Выберите свой подключённый аккаунт")
    st = tgm.groups_state.get(account_id)
    if not (st and st.get("running")):
        spawn(tgm.refresh_groups(account_id), f"список групп аккаунта #{account_id}")
    return back(f"/segments/{sid}", msg="Ищу группы аккаунта… Обновите страницу через минуту")


@router.post("/{sid}/import-group")
async def segment_import_group(request: Request, sid: int):
    """Участники одной или нескольких отмеченных групп → сегмент. Группы обрабатываются по очереди;
    если Telegram попросит подождать, останавливаемся и сообщаем, что успели."""
    if not segments.get(sid):
        return back("/segments", err="Сегмент не найден")
    form = await request.form()
    tag = (form.get("tag") or "").strip()
    picked = []
    for v in form.getlist("group"):
        try:
            picked.append(tuple(int(x) for x in v.split(":")))
        except ValueError:
            continue
    if not picked:
        return back(f"/segments/{sid}", err="Отметьте хотя бы одну группу")
    u = user(request)
    total = added = new = done = 0
    problems = []
    for i, (account_id, peer_id) in enumerate(picked):
        acc = db.one("SELECT * FROM tg_accounts WHERE id=%s", (account_id,))
        client = tgm.get(account_id)
        title = db.val("SELECT title FROM tg_dialogs WHERE account_id=%s AND peer_id=%s", (account_id, peer_id)) or peer_id
        if not auth.can_use_account(u, acc) or not client.authorized:
            problems.append(f"«{title}»: группа не вашего подключённого аккаунта")
            continue
        if tgm.preparing_account(account_id):
            problems.append(f"«{title}»: аккаунт занят поиском, повторите позже")
            continue
        try:
            t, a, n = await client.import_group_members(peer_id, sid, tag)
        except ValueError as e:
            problems.append(f"«{title}»: {e}")
            continue
        except errors.FloodWaitError as e:
            problems.append(f"Telegram попросил подождать {e.seconds} сек — остановился на «{title}», "
                            f"не обработано групп: {len(picked) - i}")
            break
        except errors.RPCError as e:
            problems.append(f"«{title}»: не удалось получить участников ({e})")
            continue
        total, added, new, done = total + t, added + a, new + n, done + 1
        if i < len(picked) - 1:
            await asyncio.sleep(1)       # не дёргаем Telegram подряд без паузы
    msg = (f"Групп обработано: {done} из {len(picked)}. Участников: {total}, добавлено в сегмент: {added} "
           f"(новых в базе {new}). В шаблоне доступны {{group}} и {{joined}}")
    if problems:
        return back(f"/segments/{sid}", msg=msg if done else "", err="; ".join(problems))
    return back(f"/segments/{sid}", msg=msg)


@router.post("/{sid}/add-tag")
async def segment_add_tag(request: Request, sid: int, tag: str = Form(...)):
    if not segments.get(sid):
        return back("/segments", err="Сегмент не найден")
    n = segments.add_by_tag(sid, tag)
    return back(f"/segments/{sid}", msg=f"Добавлено по тегу «{tag}»: {n}")


@router.post("/{sid}/remove/{lead_id}")
async def segment_remove(request: Request, sid: int, lead_id: int):
    if not _can_edit(user(request), segments.get(sid)):
        return back(f"/segments/{sid}", err="Убирать лидов может автор сегмента или админ")
    segments.remove(sid, lead_id)
    return back(f"/segments/{sid}")


@router.post("/{sid}/edit")
async def segment_edit(request: Request, sid: int, name: str = Form(...), description: str = Form("")):
    if not _can_edit(user(request), segments.get(sid)):
        return back(f"/segments/{sid}", err="Менять может автор сегмента или админ")
    try:
        db.ex("UPDATE segments SET name=%s, description=%s WHERE id=%s", (name.strip(), description.strip(), sid))
    except UniqueViolation:
        return back(f"/segments/{sid}", err="Сегмент с таким названием уже есть")
    return back(f"/segments/{sid}", msg="Сохранено")


@router.post("/{sid}/delete")
async def segment_delete(request: Request, sid: int):
    seg = segments.get(sid)
    if not _can_edit(user(request), seg):
        return back(f"/segments/{sid}", err="Удалить может автор сегмента или админ")
    db.ex("DELETE FROM segments WHERE id=%s", (sid,))
    return back("/segments", msg=f"Сегмент «{seg['name']}» удалён. Лиды остались в общей базе")
