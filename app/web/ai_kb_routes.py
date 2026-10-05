"""Наполнение базы знаний ИИ: импорт, сбор из переписок, мастер, предложения на проверку, «нет ответа в базе»,
копирование карточек и проверка базы. Всё, что предлагает ИИ, сохраняется только после просмотра человеком."""
from fastapi import APIRouter, File, Form, Request, UploadFile
from psycopg.errors import UniqueViolation

from .. import ai, ai_kb, auth, db
from ..tasks import spawn
from .ai_routes import _can_edit, _profile
from .common import UploadTooLarge, back, page, read_upload, user

router = APIRouter(prefix="/ai")


def _editable(request: Request, pid: int):
    prof = _profile(pid)
    return prof if _can_edit(user(request), prof) else None


def _start(pid: int, coro, what: str):
    if ai_kb.busy(pid):
        coro.close()
        return back(f"/ai/profiles/{pid}", err="Уже идёт наполнение базы этого профиля — дождитесь окончания")
    spawn(coro, f"наполнение базы ИИ: {what}")
    return back(f"/ai/profiles/{pid}#kb", msg=f"Запущено: {what}. Предложения появятся ниже — проверьте их перед сохранением")


# ---------- G1: импорт ----------
@router.post("/profiles/{pid}/import")
async def kb_import(request: Request, pid: int, text: str = Form(""), url: str = Form(""),
                    file: UploadFile | None = File(None)):
    if not _editable(request, pid):
        return back(f"/ai/profiles/{pid}", err="Наполнять базу может автор профиля или админ")
    if not ai.configured():
        return back(f"/ai/profiles/{pid}", err="ИИ не настроен")
    uid = user(request)["id"]
    if file is not None and file.filename:
        try:
            data = await read_upload(file)
            body = ai_kb.file_text(file.filename, data)
        except (UploadTooLarge, ai_kb.KBError) as e:
            return back(f"/ai/profiles/{pid}", err=str(e))
        return _start(pid, ai_kb.import_text(pid, uid, body, f"файл {file.filename}"), f"импорт файла {file.filename}")
    if url.strip():
        return _start(pid, ai_kb.import_url(pid, uid, url.strip()), "импорт страницы сайта")
    if text.strip():
        return _start(pid, ai_kb.import_text(pid, uid, text, "вставленный текст"), "импорт текста")
    return back(f"/ai/profiles/{pid}", err="Вставьте текст, выберите файл или укажите ссылку")


@router.get("/profiles/{pid}/kb-status")
async def kb_status(request: Request, pid: int):
    st = ai_kb.jobs.get(pid) or {}
    return {**st, "suggestions": db.val("SELECT COUNT(*) FROM ai_suggestions WHERE profile_id=%s", (pid,)) or 0}


# ---------- G3: из переписок ----------
@router.post("/profiles/{pid}/mine")
async def kb_mine(request: Request, pid: int, days: int = Form(90), scope: str = Form("profile")):
    if not _editable(request, pid):
        return back(f"/ai/profiles/{pid}", err="Наполнять базу может автор профиля или админ")
    if not ai.configured():
        return back(f"/ai/profiles/{pid}", err="ИИ не настроен")
    days = days if days in (30, 90, 180, 365) else 90
    scope = scope if scope in ("profile", "all") else "profile"
    return _start(pid, ai_kb.mine_dialogs(pid, user(request)["id"], days, scope), "сбор из переписок")


# ---------- G4: мастер ----------
@router.post("/wizard/create")
async def wizard_create(request: Request, name: str = Form("")):
    if not name.strip():
        return back("/ai", err="Укажите название профиля")
    try:
        pid = db.ex("INSERT INTO ai_profiles(name, created_by) VALUES (%s, %s) RETURNING id", (name.strip(), user(request)["id"]))
    except UniqueViolation:
        return back("/ai", err="Профиль с таким названием уже есть")
    return back(f"/ai/profiles/{pid}/wizard")


@router.get("/profiles/{pid}/wizard")
async def wizard_page(request: Request, pid: int):
    prof = _editable(request, pid)
    if not prof:
        return back(f"/ai/profiles/{pid}", err="Мастер доступен автору профиля или админу")
    return page(request, "ai_wizard.html", p=prof, questions=ai_kb.WIZARD, ai_on=ai.configured())


@router.post("/profiles/{pid}/wizard")
async def wizard_submit(request: Request, pid: int):
    if not _editable(request, pid):
        return back(f"/ai/profiles/{pid}", err="Мастер доступен автору профиля или админу")
    if not ai.configured():
        return back(f"/ai/profiles/{pid}", err="ИИ не настроен")
    form = await request.form()
    answers = {k: str(form.get(k) or "") for k, _, _ in ai_kb.WIZARD}
    if not any(v.strip() for v in answers.values()):
        return back(f"/ai/profiles/{pid}/wizard", err="Ответьте хотя бы на несколько вопросов")
    return _start(pid, ai_kb.wizard(pid, user(request)["id"], answers), "мастер профиля")


# ---------- предложения на проверку ----------
@router.post("/profiles/{pid}/suggestions")
async def suggestions_apply(request: Request, pid: int):
    if not _editable(request, pid):
        return back(f"/ai/profiles/{pid}", err="Нет прав менять профиль")
    form = await request.form()
    action = form.get("action")
    if action == "reject_all":
        n = ai_kb.reject(pid)
        return back(f"/ai/profiles/{pid}", msg=f"Отклонено предложений: {n}")
    picked = {}
    for v in form.getlist("pick"):
        if str(v).isdigit():
            sid = int(v)
            picked[sid] = {"title": str(form.get(f"title_{sid}") or ""), "body": str(form.get(f"body_{sid}") or "")}
    if action == "reject":
        n = ai_kb.reject(pid, list(picked))
        return back(f"/ai/profiles/{pid}#kb", msg=f"Отклонено: {n}")
    if not picked:
        return back(f"/ai/profiles/{pid}#kb", err="Отметьте, что сохранить")
    cards, examples, instr = ai_kb.accept(pid, picked)
    parts = [f"карточек {cards}"] + ([f"примеров {examples}"] if examples else []) + (["инструкция заменена"] if instr else [])
    return back(f"/ai/profiles/{pid}#kb", msg="Сохранено: " + ", ".join(parts))


# ---------- G2: нет ответа в базе (pid 0 — общая база, только админ) ----------
def _gap_allowed(request: Request, pid: int) -> bool:
    return auth.is_admin(user(request)) if pid == 0 else bool(_editable(request, pid))


def _gap_back(pid: int) -> str:
    return "/ai#gaps" if pid == 0 else f"/ai/profiles/{pid}#gaps"


@router.post("/gaps/{pid}/{gid}/answer")
async def gap_answer(request: Request, pid: int, gid: int, title: str = Form(""), body: str = Form("")):
    if not _gap_allowed(request, pid):
        return back(_gap_back(pid), err="Нет прав менять эту базу знаний")
    if not (title.strip() and body.strip()):
        return back(_gap_back(pid), err="Нужны заголовок и ответ")
    if not ai_kb.answer_gap(gid, pid or None, title, body):
        return back(_gap_back(pid), err="Вопрос не найден — возможно, на него уже ответили")
    return back(_gap_back(pid), msg="Карточка добавлена — теперь ИИ знает ответ")


@router.post("/gaps/{pid}/{gid}/dismiss")
async def gap_dismiss(request: Request, pid: int, gid: int):
    if not _gap_allowed(request, pid):
        return back(_gap_back(pid), err="Нет прав менять эту базу знаний")
    db.ex("DELETE FROM ai_gaps WHERE id=%s AND profile_id IS NOT DISTINCT FROM %s", (gid, pid or None))
    return back(_gap_back(pid))


# ---------- G5: копирование и проверка ----------
@router.post("/profiles/{pid}/cards-copy")
async def cards_copy(request: Request, pid: int):
    form = await request.form()
    target = int(form.get("target") or 0) if str(form.get("target") or "").isdigit() else 0
    if not _profile(pid) or not _editable(request, target):
        return back(f"/ai/profiles/{pid}", err="Копировать можно в профиль, который вы можете менять")
    if target == pid:
        return back(f"/ai/profiles/{pid}", err="Выберите другой профиль")
    ids = [int(v) for v in form.getlist("card") if str(v).isdigit()]
    if not ids:
        return back(f"/ai/profiles/{pid}", err="Отметьте карточки для копирования")
    n = ai_kb.copy_cards(pid, target, ids)
    skipped = len(ids) - n
    return back(f"/ai/profiles/{pid}", msg=f"Скопировано карточек: {n}" + (f" (уже были там: {skipped})" if skipped else ""))


@router.post("/profiles/{pid}/check")
async def kb_check(request: Request, pid: int):
    if not _profile(pid):
        return {"error": "Профиль не найден"}
    try:
        return {"issues": await ai_kb.check(pid)}
    except ai.AIError as e:
        return {"error": str(e)}
