"""Списки обращений: импорт xlsx, фильтры, постановка в очередь по правилам.

Формат файла: лист с шапкой, где есть колонки «Тип», «Ссылка» и текст обращения.
Каждая строка — отдельный адресат (человек или чат) со своим готовым текстом.
"""
import re

from . import db
from .xlsx import read_xlsx

# колонка файла → поле БД (сравнение по началу названия, без регистра)
COLUMNS = {
    "исх": "row_no",
    "тип": "kind_raw",
    "имя": "first_name",
    "фамилия": "last_name",
    "название": "title",
    "папка": "folder",
    "ссылка на тему": "topic_link",
    "ссылка": "link",
    "план": "plan",
    "куда": "topic_title",
    "примечание": "note",
    "текст №": "text_no",
    "обращение": "text",
    "текст": "text",
    "статус": "src_status",
    "на ты": "address",
    "история": "dialog",
}
KIND = {"чат": "chat", "человек": "person", "бот": "bot", "канал": "chat", "группа": "chat"}
KIND_RU = {"chat": "Чат", "person": "Человек", "bot": "Бот", "other": "Другое"}


def _map_header(header: list) -> dict[int, str]:
    res = {}
    for i, h in enumerate(header):
        h = str(h or "").strip().lower()
        if not h:
            continue
        # сначала длинные ключи («ссылка на тему» раньше «ссылка», «текст №» раньше «текст»)
        for key in sorted(COLUMNS, key=len, reverse=True):
            if h.startswith(key):
                field = COLUMNS[key]
                if field not in res.values():
                    res[i] = field
                break
    return res


def parse_link(link: str) -> dict:
    """web.telegram.org/a/#-100123_45 → peer_id, topic_id; t.me/name → username."""
    out = {"peer_id": None, "topic_id": None, "username": None}
    link = (link or "").strip()
    if not link:
        return out
    m = re.search(r"#(-?\d+)(?:_(\d+))?", link)
    if m:
        out["peer_id"] = int(m.group(1))
        if m.group(2):
            out["topic_id"] = int(m.group(2))
        return out
    m = re.search(r"#@([\w\d_]{3,})", link)
    if m:
        out["username"] = m.group(1)
        return out
    m = re.search(r"t\.me/c/(\d+)(?:/(\d+))?", link)
    if m:
        out["peer_id"] = int("-100" + m.group(1))
        return out
    m = re.search(r"(?:t\.me|telegram\.me)/(?:s/)?@?([A-Za-z][\w\d_]{3,})", link)
    if m and m.group(1).lower() not in ("joinchat", "addlist", "share"):
        out["username"] = m.group(1)
    elif link.startswith("@"):
        out["username"] = link[1:]
    return out


def status_group(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return "(пусто)"
    part = re.split(r" · |; |\. |: ", s, maxsplit=1)[0].strip().rstrip(".")
    return part[:60]


def _texts_from_sheets(sheets: dict) -> dict[str, str]:
    """Тексты по номеру с листов вида «Тексты …» (колонки «№» и «…текст»)."""
    texts = {}
    for name, rows in sheets.items():
        if "текст" not in name.lower():
            continue
        for hi, row in enumerate(rows[:10]):
            low = [str(c or "").strip().lower() for c in row]
            if "№" in low and any("текст" in c for c in low):
                ni = low.index("№")
                ti = max(i for i, c in enumerate(low) if "текст" in c)
                for r in rows[hi + 1:]:
                    if len(r) > max(ni, ti) and r[ni] is not None and r[ti]:
                        texts[str(r[ni]).strip()] = str(r[ti])
                break
    return texts


def import_file(data: bytes, filename: str) -> tuple[int, int, list[str]]:
    sheets = read_xlsx(data)
    warnings = []
    # ищем лист и строку шапки, где есть «Тип» и «Ссылка»
    target = None
    for name, rows in sheets.items():
        for hi, row in enumerate(rows[:15]):
            m = _map_header(row)
            if "link" in m.values() and ("kind_raw" in m.values() or "text" in m.values()):
                target = (name, hi, m)
                break
        if target:
            break
    if not target:
        raise ValueError("Не нашёл лист с колонками «Тип», «Ссылка» и текстом обращения")
    sheet, hi, cmap = target
    texts = _texts_from_sheets(sheets)

    list_id = db.ex("INSERT INTO lists(name, filename, created_at) VALUES (?,?,?)",
                    (re.sub(r"\.xlsx$", "", filename or "список", flags=re.I), filename, db.now_utc()))
    n = 0
    warn_topic = []
    for row in sheets[sheet][hi + 1:]:
        rec = {f: row[i] for i, f in cmap.items() if i < len(row)}
        rec = {k: (str(v).strip() if v is not None else None) for k, v in rec.items()}
        if not any(rec.get(k) for k in ("link", "title", "first_name", "text")):
            continue
        kind = KIND.get((rec.get("kind_raw") or "").lower(), "other")
        p = parse_link(rec.get("link"))
        if rec.get("topic_link"):
            t = parse_link(rec["topic_link"])
            if not p["peer_id"]:
                p["peer_id"] = t["peer_id"]
            if t["topic_id"] and t["peer_id"] == p["peer_id"]:
                p["topic_id"] = t["topic_id"]
            elif t["topic_id"]:
                warn_topic.append(rec.get("title") or rec.get("row_no") or "?")
        text = rec.get("text")
        if not text and rec.get("text_no"):
            no = rec["text_no"].split(".")[0]
            if no in texts:
                text = texts[no].replace("[Имя]", rec.get("first_name") or "").strip()
        if kind == "chat" and not rec.get("dialog"):
            rec["dialog"] = "Не применимо"
        db.ex("""INSERT INTO list_items(list_id, row_no, kind, title, first_name, last_name, link, peer_id, username,
                 topic_id, topic_title, folder, dialog, address, plan, note, text_no, text, src_status, src_group)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (list_id, rec.get("row_no"), kind, rec.get("title"), rec.get("first_name"), rec.get("last_name"),
               rec.get("link"), p["peer_id"], p["username"], p["topic_id"], rec.get("topic_title"), rec.get("folder"),
               rec.get("dialog"), rec.get("address"), rec.get("plan"), rec.get("note"), rec.get("text_no"), text,
               rec.get("src_status"), status_group(rec.get("src_status"))))
        n += 1
    if warn_topic:
        warnings.append("«Ссылка на тему» ведёт в другой чат, тема не применена: " + ", ".join(warn_topic[:5]))
    no_target = db.one("SELECT COUNT(*) n FROM list_items WHERE list_id=? AND peer_id IS NULL AND username IS NULL",
                       (list_id,))["n"]
    if no_target:
        warnings.append(f"{no_target} строк без распознанной ссылки — их отправить не получится")
    no_text = db.one("SELECT COUNT(*) n FROM list_items WHERE list_id=? AND (text IS NULL OR text='')", (list_id,))["n"]
    if no_text:
        warnings.append(f"{no_text} строк без текста")
    db.log(f"Импорт списка «{filename}»: {n} строк (лист «{sheet}»)")
    return list_id, n, warnings


# ---------- фильтры ----------
REAL_EXPR = "CASE WHEN kind='chat' THEN 'Не применимо' ELSE COALESCE(real_dialog, 'не проверено') END"
MISMATCH_SQL = ("real_dialog IS NOT NULL AND dialog IN ('Диалог есть','Диалога нет') AND dialog != real_dialog")
FILTER_FIELDS = {"kind": "kind", "dialog": "dialog", "real": REAL_EXPR, "address": "address",
                 "src": "src_group", "state": "state"}


def filter_sql(list_id: int, f: dict) -> tuple[str, list]:
    where, params = ["list_id=?"], [list_id]
    for key, col in FILTER_FIELDS.items():
        vals = f.get(key) or []
        if vals:
            where.append(f"COALESCE({col}, '(пусто)') IN ({','.join('?' * len(vals))})")
            params += vals
    if f.get("q"):
        where.append("(COALESCE(folder,'') || ' ' || COALESCE(title,'') || ' ' || COALESCE(note,'') || ' ' || COALESCE(text,'')) LIKE ?")
        params.append(f"%{f['q']}%")
    if f.get("mismatch"):
        where.append(MISMATCH_SQL)
    if f.get("has_text"):
        where.append("text IS NOT NULL AND text != ''")
    if f.get("has_target"):
        where.append("(peer_id IS NOT NULL OR username IS NOT NULL)")
    return " AND ".join(where), params


def facets(list_id: int) -> dict[str, list]:
    out = {}
    for key, col in FILTER_FIELDS.items():
        out[key] = db.q(f"SELECT COALESCE({col}, '(пусто)') v, COUNT(*) n FROM list_items WHERE list_id=? "
                        f"GROUP BY 1 ORDER BY n DESC", (list_id,))
    return out


def default_filter(list_id: int) -> dict:
    """По умолчанию: новые строки с текстом и ссылкой, кроме «Не писать» и уже отправленных по файлу."""
    fc = facets(list_id)
    src = [r["v"] for r in fc["src"]
           if not re.match(r"(?i)(отправлено|не писать|только личные)", r["v"])]
    addr = [r["v"] for r in fc["address"] if r["v"].lower() != "не писать"]
    return {"state": ["new"], "src": src, "address": addr, "kind": [], "dialog": [], "real": [],
            "mismatch": False, "use_real": False, "has_text": True, "has_target": True, "q": ""}


def _group(item, use_real: bool = False) -> int:
    if item["kind"] == "chat":
        return 0
    d = item["real_dialog"] if use_real and item["real_dialog"] else item["dialog"]
    return 1 if (d or "").lower().startswith("диалог есть") else 2


def enqueue(list_id: int, f: dict) -> int:
    """Ставит отфильтрованные строки в очередь в порядке: чат → с диалогом → без диалога → …
    use_real: делить на «с диалогом / без» по данным Telegram (если сверка уже была), иначе по файлу."""
    w, p = filter_sql(list_id, f)
    rows = db.q(f"SELECT id, kind, dialog, real_dialog FROM list_items WHERE {w} AND state IN ('new','failed','skipped') ORDER BY id", p)
    groups = [[], [], []]
    for r in rows:
        groups[_group(r, f.get("use_real"))].append(r["id"])
    ordered = []
    while any(groups):
        for g in groups:
            if g:
                ordered.append(g.pop(0))
    start = (db.one("SELECT MAX(order_idx) m FROM list_items WHERE list_id=?", (list_id,))["m"] or 0) + 1
    for i, item_id in enumerate(ordered):
        db.ex("UPDATE list_items SET state='queued', error=NULL, order_idx=? WHERE id=?", (start + i, item_id))
    return len(ordered)
