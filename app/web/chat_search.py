"""Поиск групп: темы поиска, ручные ссылки, список найденных групп со статусами."""
from fastapi import APIRouter, Form, Request

from .. import auth, chat_search as cs, db
from ..tg import tgm
from ..tasks import spawn
from .common import back, local_url, page, user

router = APIRouter(prefix="/chat-search")
PER_PAGE = 100
SORTS = {"score": "f.score DESC, f.members DESC NULLS LAST", "members": "f.members DESC NULLS LAST",
         "activity": "f.msgs_per_day DESC NULLS LAST", "new": "f.created_at DESC"}


def _account(request: Request, account_id: int):
    acc = db.one("SELECT * FROM tg_accounts WHERE id=%s", (account_id,))
    ok = auth.can_use_account(user(request), acc) and tgm.get(account_id).authorized
    return acc if ok else None


def _busy(account_id: int) -> bool:
    return tgm.preparing_account(account_id)


@router.get("")
async def search_page(request: Request, status: str = "active", search: int = 0, q: str = "", min_score: int = 0,
                      sort: str = "score", p: int = 1):
    u = user(request)
    searches = db.q("""SELECT s.*, u.name AS author, COALESCE(NULLIF(a.label,''), a.first_name) acc_label,
                       (SELECT COUNT(DISTINCT found_chat_id) FROM found_chat_hits h WHERE h.search_id=s.id) found
                       FROM chat_searches s LEFT JOIN users u ON u.id=s.created_by LEFT JOIN tg_accounts a ON a.id=s.account_id
                       ORDER BY s.id DESC""")
    where, params = ["true"], []
    if status == "active":
        where.append("f.status IN ('new','interesting')")
    elif status in cs.STATUS_RU:
        where.append("f.status=%s")
        params.append(status)
    if search:
        where.append("f.id IN (SELECT found_chat_id FROM found_chat_hits WHERE search_id=%s)")
        params.append(search)
    if q:
        where.append("(f.title ILIKE %s OR f.username ILIKE %s OR f.about ILIKE %s)")
        params += [f"%{q}%"] * 3
    if min_score:
        where.append("f.score >= %s")
        params.append(min_score)
    w = " AND ".join(where)
    total = db.val(f"SELECT COUNT(*) FROM found_chats f WHERE {w}", params)
    my_ids = [a["id"] for a in auth.user_accounts(u)]
    rows = db.q(f"""SELECT f.*,
                    (SELECT COUNT(*) FROM found_chat_hits h WHERE h.found_chat_id=f.id) hits,
                    (SELECT string_agg(DISTINCT h.query, ', ') FROM found_chat_hits h WHERE h.found_chat_id=f.id) queries,
                    EXISTS (SELECT 1 FROM tg_dialogs d WHERE d.peer_id=f.tg_id AND d.account_id = ANY(%s)) member
                    FROM found_chats f WHERE {w} ORDER BY {SORTS.get(sort, SORTS['score'])}
                    LIMIT %s OFFSET %s""", [my_ids] + params + [PER_PAGE, (max(p, 1) - 1) * PER_PAGE])
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM found_chats GROUP BY status")}
    accounts = [a for a in auth.user_accounts(u, only_active=True) if tgm.get(a["id"]).authorized]
    running = {sid: st for sid, st in tgm.search_state.items() if st.get("running")}
    return page(request, "chat_search.html", searches=searches, rows=rows, total=total, counts=counts,
                accounts=accounts, running=running, states=tgm.search_state, status=status, search=search, q=q,
                min_score=min_score, sort=sort, p=p, per_page=PER_PAGE, STATUS_RU=cs.STATUS_RU, VIA_RU=cs.VIA_RU,
                max_queries=cs.MAX_QUERIES)


@router.post("/create")
async def search_create(request: Request, name: str = Form(""), keywords: str = Form(""), geo: str = Form(""),
                        stop_words: str = Form(""), account_id: int = Form(0)):
    if not _account(request, account_id):
        return back("/chat-search", err="Выберите свой подключённый аккаунт")
    if _busy(account_id):
        return back("/chat-search", err="Аккаунт уже занят поиском — дождитесь окончания")
    kws = cs.split_words(keywords)
    if not kws:
        return back("/chat-search", err="Укажите хотя бы одно ключевое слово")
    sid = db.ex("""INSERT INTO chat_searches(name, keywords, geo, stop_words, account_id, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (name.strip() or ", ".join(kws[:3]), kws, cs.split_words(geo), cs.split_words(stop_words),
                 account_id, user(request)["id"]))
    spawn(tgm.run_chat_search(sid, account_id), f"поиск групп #{sid}")
    n = len(cs.build_queries(kws, cs.split_words(geo)))
    return back(f"/chat-search?search={sid}&status=all",
                msg=f"Поиск запущен: {n} запросов с паузами, это займёт несколько минут. Отправка с аккаунта на это время стоит")


@router.post("/links")
async def search_links(request: Request, links: str = Form(""), keywords: str = Form(""), stop_words: str = Form(""),
                       account_id: int = Form(0)):
    if not _account(request, account_id):
        return back("/chat-search", err="Выберите свой подключённый аккаунт")
    if _busy(account_id):
        return back("/chat-search", err="Аккаунт уже занят поиском — дождитесь окончания")
    lines = [ln for ln in links.splitlines() if ln.strip()][:100]
    if not lines:
        return back("/chat-search", err="Вставьте хотя бы одну ссылку")
    sid = db.ex("""INSERT INTO chat_searches(name, keywords, stop_words, account_id, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (f"Ссылки ({len(lines)})", cs.split_words(keywords), cs.split_words(stop_words), account_id,
                 user(request)["id"]))
    spawn(tgm.run_chat_search(sid, account_id, links=lines), f"проверка ссылок #{sid}")
    return back(f"/chat-search?search={sid}&status=all", msg=f"Проверяю ссылки: {len(lines)}")


@router.get("/state")
async def search_state():
    return {sid: {k: v for k, v in st.items() if k != "account_id"} for sid, st in tgm.search_state.items()}


@router.post("/{sid}/rerun")
async def search_rerun(request: Request, sid: int):
    s = db.one("SELECT * FROM chat_searches WHERE id=%s", (sid,))
    if not s or not s["keywords"]:
        return back("/chat-search", err="Поиск не найден")
    if not _account(request, s["account_id"]):
        return back("/chat-search", err="Повторить может владелец аккаунта этого поиска")
    if _busy(s["account_id"]):
        return back("/chat-search", err="Аккаунт уже занят поиском — дождитесь окончания")
    spawn(tgm.run_chat_search(sid, s["account_id"]), f"поиск групп #{sid}")
    return back(f"/chat-search?search={sid}&status=all", msg="Поиск запущен заново: новые группы добавятся, оценки обновятся")


@router.post("/{sid}/delete")
async def search_delete(request: Request, sid: int):
    s = db.one("SELECT * FROM chat_searches WHERE id=%s", (sid,))
    if not s or not (auth.is_admin(user(request)) or s["created_by"] == user(request)["id"]):
        return back("/chat-search", err="Удалить поиск может его автор или админ")
    db.ex("DELETE FROM chat_searches WHERE id=%s", (sid,))
    return back("/chat-search", msg="Поиск удалён. Найденные группы остались в общем списке")


@router.post("/found/{fid}/{status}")
async def found_status(request: Request, fid: int, status: str):
    if status not in cs.STATUS_RU:
        return back("/chat-search", err="Неизвестный статус")
    db.ex("UPDATE found_chats SET status=%s WHERE id=%s", (status, fid))
    return back(local_url(request.headers.get("referer"), "/chat-search"))
