"""ИИ: общая инструкция, профили кампаний (инструкция, база знаний, примеры), проверка ответа."""
from fastapi import APIRouter, Form, Request
from psycopg.errors import UniqueViolation

from .. import ai, auth, db, sandbox
from ..config import AI_BASE_URL, AI_MODEL
from ..templating import render
from .common import back, page, user

router = APIRouter(prefix="/ai")


def _profile(pid: int):
    return db.one("SELECT p.*, u.name AS author FROM ai_profiles p LEFT JOIN users u ON u.id=p.created_by WHERE p.id=%s", (pid,))


def _can_edit(u: dict, prof: dict | None) -> bool:
    return bool(prof) and (auth.is_admin(u) or prof["created_by"] == u["id"])


@router.get("")
async def ai_page(request: Request):
    profiles = db.q("""SELECT p.*, u.name AS author,
                       (SELECT COUNT(*) FROM ai_cards c WHERE c.profile_id=p.id) cards,
                       (SELECT COUNT(*) FROM ai_examples e WHERE e.profile_id=p.id AND e.active) examples,
                       (SELECT string_agg(c.name, ', ') FROM campaigns c WHERE c.ai_profile_id=p.id) campaigns
                       FROM ai_profiles p LEFT JOIN users u ON u.id=p.created_by ORDER BY p.name""")
    drafts = db.one("""SELECT COUNT(*) total, COUNT(sent_at) sent,
                       COUNT(*) FILTER (WHERE similarity >= %s) as_is FROM ai_drafts""", (ai.EXAMPLE_SIMILARITY,))
    return page(request, "ai.html", profiles=profiles, base=db.get_setting("ai_base_instruction"),
                cards=db.q("SELECT * FROM ai_cards WHERE profile_id IS NULL ORDER BY id"), drafts=drafts,
                status=await ai.status(), model=AI_MODEL, base_url=AI_BASE_URL)


@router.post("/base")
async def ai_base(request: Request, instruction: str = Form("")):
    if not auth.is_admin(user(request)):
        return back("/ai", err="Общую инструкцию меняет админ")
    if not instruction.strip():
        return back("/ai", err="Инструкция не может быть пустой")
    db.set_setting("ai_base_instruction", instruction.strip())
    return back("/ai", msg="Общая инструкция сохранена")


@router.post("/profiles/create")
async def profile_create(request: Request, name: str = Form("")):
    if not name.strip():
        return back("/ai", err="Укажите название профиля")
    try:
        pid = db.ex("INSERT INTO ai_profiles(name, created_by) VALUES (%s, %s) RETURNING id", (name.strip(), user(request)["id"]))
    except UniqueViolation:
        return back("/ai", err="Профиль с таким названием уже есть")
    return back(f"/ai/profiles/{pid}", msg="Профиль создан: опишите, кто пишет и о чём, добавьте карточки и примеры")


@router.get("/profiles/{pid}")
async def profile_page(request: Request, pid: int):
    prof = _profile(pid)
    if not prof:
        return back("/ai", err="Профиль не найден")
    return page(request, "ai_profile.html", p=prof, can_edit=_can_edit(user(request), prof),
                cards=db.q("SELECT * FROM ai_cards WHERE profile_id=%s ORDER BY id", (pid,)),
                examples=db.q("SELECT * FROM ai_examples WHERE profile_id=%s ORDER BY active DESC, created_at DESC", (pid,)),
                campaigns=db.q("SELECT id, name FROM campaigns WHERE ai_profile_id=%s ORDER BY id DESC", (pid,)),
                ai_on=ai.configured())


@router.post("/profiles/{pid}")
async def profile_save(request: Request, pid: int, name: str = Form(""), instruction: str = Form("")):
    prof = _profile(pid)
    if not _can_edit(user(request), prof):
        return back(f"/ai/profiles/{pid}", err="Менять профиль может автор или админ")
    if not name.strip():
        return back(f"/ai/profiles/{pid}", err="Название не может быть пустым")
    try:
        db.ex("UPDATE ai_profiles SET name=%s, instruction=%s WHERE id=%s", (name.strip(), instruction.strip(), pid))
    except UniqueViolation:
        return back(f"/ai/profiles/{pid}", err="Профиль с таким названием уже есть")
    return back(f"/ai/profiles/{pid}", msg="Сохранено")


@router.post("/profiles/{pid}/delete")
async def profile_delete(request: Request, pid: int):
    prof = _profile(pid)
    if not _can_edit(user(request), prof):
        return back(f"/ai/profiles/{pid}", err="Удалить профиль может автор или админ")
    db.ex("DELETE FROM ai_profiles WHERE id=%s", (pid,))
    return back("/ai", msg=f"Профиль «{prof['name']}» удалён; кампании с ним работают на общей инструкции")


# ---------- карточки базы знаний (общие: profile_id=0 → NULL, только админ) ----------
def _card_allowed(request: Request, pid: int) -> bool:
    u = user(request)
    return auth.is_admin(u) if pid == 0 else _can_edit(u, _profile(pid))


def _card_back(pid: int) -> str:
    return "/ai" if pid == 0 else f"/ai/profiles/{pid}"


@router.post("/cards/{pid}/add")
async def card_add(request: Request, pid: int, title: str = Form(""), body: str = Form("")):
    if not _card_allowed(request, pid):
        return back(_card_back(pid), err="Нет прав менять эту базу знаний")
    if not (title.strip() and body.strip()):
        return back(_card_back(pid), err="Нужны заголовок и текст карточки")
    db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, %s, %s)", (pid or None, title.strip(), body.strip()))
    return back(_card_back(pid), msg="Карточка добавлена")


@router.post("/cards/{pid}/{cid}")
async def card_save(request: Request, pid: int, cid: int, title: str = Form(""), body: str = Form("")):
    if not _card_allowed(request, pid):
        return back(_card_back(pid), err="Нет прав менять эту базу знаний")
    if not (title.strip() and body.strip()):
        return back(_card_back(pid), err="Нужны заголовок и текст карточки")
    db.ex("UPDATE ai_cards SET title=%s, body=%s, updated_at=now() WHERE id=%s AND profile_id IS NOT DISTINCT FROM %s",
          (title.strip(), body.strip(), cid, pid or None))
    return back(_card_back(pid), msg="Карточка сохранена")


@router.post("/cards/{pid}/{cid}/delete")
async def card_delete(request: Request, pid: int, cid: int):
    if not _card_allowed(request, pid):
        return back(_card_back(pid), err="Нет прав менять эту базу знаний")
    db.ex("DELETE FROM ai_cards WHERE id=%s AND profile_id IS NOT DISTINCT FROM %s", (cid, pid or None))
    return back(_card_back(pid), msg="Карточка удалена")


# ---------- примеры ----------
@router.post("/profiles/{pid}/examples/add")
async def example_add(request: Request, pid: int, question: str = Form(""), answer: str = Form("")):
    if not _can_edit(user(request), _profile(pid)):
        return back(f"/ai/profiles/{pid}", err="Нет прав менять профиль")
    if not (question.strip() and answer.strip()):
        return back(f"/ai/profiles/{pid}", err="Нужны вопрос клиента и наш ответ")
    db.ex("INSERT INTO ai_examples(profile_id, question, answer) VALUES (%s, %s, %s)", (pid, question.strip(), answer.strip()))
    return back(f"/ai/profiles/{pid}", msg="Пример добавлен")


@router.post("/profiles/{pid}/examples/{eid}/{action}")
async def example_action(request: Request, pid: int, eid: int, action: str):
    if not _can_edit(user(request), _profile(pid)):
        return back(f"/ai/profiles/{pid}", err="Нет прав менять профиль")
    if action == "toggle":
        db.ex("UPDATE ai_examples SET active = NOT active WHERE id=%s AND profile_id=%s", (eid, pid))
    elif action == "delete":
        db.ex("DELETE FROM ai_examples WHERE id=%s AND profile_id=%s", (eid, pid))
    return back(f"/ai/profiles/{pid}")


# ---------- проверка ----------
@router.post("/profiles/{pid}/try")
async def profile_try(request: Request, pid: int, question: str = Form("")):
    """Как профиль ответит на вопрос — без реального лида (JSON)."""
    if not _profile(pid) or not question.strip():
        return {"error": "Напишите вопрос клиента"}
    fake = {"id": 0, "first_name": "Ирина", "last_name": "", "title": None, "extra": {}, "stage_id": None, "note": ""}
    msgs, _ = ai.build_messages(fake, pid, history=[{"direction": "in", "text": question.strip()}])
    try:
        return {"text": await ai.chat(msgs)}
    except ai.AIError as e:
        return {"error": str(e)}


# ---------- тренажёр ----------


def _sandbox(request: Request, sid: int):
    s = sandbox.get(sid)
    u = user(request)
    return s if s and (s["user_id"] == u["id"] or auth.is_admin(u)) else None


@router.get("/sandbox")
async def sandbox_list(request: Request):
    u = user(request)
    rows = db.q("""SELECT s.*, p.name AS profile_name, u.name AS user_name,
                   (SELECT COUNT(*) FROM ai_sandbox_messages m WHERE m.sandbox_id=s.id) n,
                   (SELECT COUNT(*) FROM ai_sandbox_messages m WHERE m.sandbox_id=s.id AND m.saved) saved
                   FROM ai_sandboxes s LEFT JOIN ai_profiles p ON p.id=s.profile_id JOIN users u ON u.id=s.user_id
                   WHERE %s OR s.user_id=%s ORDER BY s.id DESC LIMIT 50""", (auth.is_admin(u), u["id"]))
    return page(request, "ai_sandbox_list.html", rows=rows, PERSONAS=sandbox.PERSONAS, ai_on=ai.configured(),
                profiles=db.q("SELECT id, name FROM ai_profiles ORDER BY name"),
                stages=db.q("SELECT name FROM funnel_stages ORDER BY position"),
                pre=int(request.query_params.get("profile") or 0))


@router.post("/sandbox/create")
async def sandbox_create(request: Request):
    form = await request.form()
    pid = int(form.get("profile_id") or 0) or None
    client = {k: (form.get(k) or "").strip() for k in ("name", "group", "joined", "stage", "note")}
    client["name"] = client["name"] or "Ирина"
    opening = (form.get("opening") or "").strip()
    if not opening and form.get("campaign_opening") and pid:
        body = db.val("""SELECT s.body FROM campaigns c JOIN campaign_steps s ON s.campaign_id=c.id AND s.position=1
                         WHERE c.ai_profile_id=%s ORDER BY c.id DESC LIMIT 1""", (pid,))
        if body:
            opening = render(body, db.lead_vars(sandbox.fake_lead(client) | {"username": "", "phone": ""}))
    sid = sandbox.create(user(request)["id"], pid, client, form.get("persona") or "interested",
                         form.get("persona_text") or "", opening)
    if not opening:              # ни своего текста, ни текста кампании — первое сообщение пишет бот
        try:
            await sandbox.opening_by_bot(sid)
        except ai.AIError as e:
            return back(f"/ai/sandbox/{sid}", err=f"Бот не смог написать первое сообщение: {e}. Начните как клиент или попробуйте снова")
    return back(f"/ai/sandbox/{sid}")


@router.get("/sandbox/{sid}")
async def sandbox_page(request: Request, sid: int):
    s = _sandbox(request, sid)
    if not s:
        return back("/ai/sandbox", err="Диалог не найден")
    prof = _profile(s["profile_id"]) if s["profile_id"] else None
    return page(request, "ai_sandbox.html", s=s, msgs=sandbox.messages(sid), PERSONAS=sandbox.PERSONAS, LABELS=ai.LABELS,
                can_save=bool(prof) and _can_edit(user(request), prof), ai_on=ai.configured(),
                max_turns=sandbox.MAX_ROBOT_TURNS)


@router.post("/sandbox/{sid}/say")
async def sandbox_say(request: Request, sid: int, text: str = Form("")):
    if not _sandbox(request, sid):
        return {"error": "Диалог не найден"}
    if not text.strip():
        return {"error": "Напишите сообщение клиента"}
    try:
        await sandbox.client_says(sid, text)
    except ai.AIError as e:
        return {"error": str(e)}
    return {"ok": True}


@router.post("/sandbox/{sid}/robot")
async def sandbox_robot(request: Request, sid: int, turns: int = Form(1)):
    if not _sandbox(request, sid):
        return {"error": "Диалог не найден"}
    done = 0
    try:
        for _ in range(max(1, min(turns, sandbox.MAX_ROBOT_TURNS))):
            if await sandbox.robot_turn(sid) is None:
                break
            done += 1
    except ai.AIError as e:
        return {"error": str(e), "turns": done}
    return {"ok": True, "turns": done}


@router.post("/sandbox/{sid}/retry/{mid}")
async def sandbox_retry(request: Request, sid: int, mid: int):
    """Переписать последний ответ бота заново."""
    if not _sandbox(request, sid):
        return {"error": "Диалог не найден"}
    last = db.one("SELECT * FROM ai_sandbox_messages WHERE sandbox_id=%s ORDER BY id DESC LIMIT 1", (sid,))
    if not last or last["id"] != mid or last["role"] != "bot":
        return {"error": "Переписать можно только последний ответ бота"}
    db.ex("DELETE FROM ai_sandbox_messages WHERE id=%s", (mid,))
    try:
        await sandbox.bot_reply(sid)
    except ai.AIError as e:
        return {"error": str(e)}
    return {"ok": True}


@router.post("/sandbox/{sid}/save/{mid}")
async def sandbox_save(request: Request, sid: int, mid: int, corrected: str = Form("")):
    s = _sandbox(request, sid)
    if not s:
        return back("/ai/sandbox", err="Диалог не найден")
    prof = _profile(s["profile_id"]) if s["profile_id"] else None
    if prof and not _can_edit(user(request), prof):
        return back(f"/ai/sandbox/{sid}", err="Сохранять примеры в профиль может его автор или админ")
    err = sandbox.save_example(sid, mid, corrected)
    return back(f"/ai/sandbox/{sid}", err=err) if err else back(f"/ai/sandbox/{sid}", msg="Сохранено как пример профиля")


@router.post("/sandbox/{sid}/delete")
async def sandbox_delete(request: Request, sid: int):
    if not _sandbox(request, sid):
        return back("/ai/sandbox", err="Диалог не найден")
    db.ex("DELETE FROM ai_sandboxes WHERE id=%s", (sid,))
    return back("/ai/sandbox", msg="Диалог удалён. Сохранённые примеры остались в профиле")
