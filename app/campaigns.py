"""Кампании: из шаблона по лидам или из готового списка xlsx; фильтры, очередь, распределение по аккаунтам.

Формат xlsx: лист с шапкой, где есть колонки «Тип», «Ссылка» и текст обращения.
Каждая строка — отдельный адресат (человек или чат) со своим готовым текстом.
"""
import random
import re

from psycopg.errors import UniqueViolation

from . import db
from .leads import norm_username, upsert_lead
from .templating import render
from .xlsx import read_xlsx

# колонка файла → поле (сравнение по началу названия, без регистра)
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


def recontact_days() -> int:
    v = db.get_setting("recontact_days")
    return int(v) if v.isdigit() else 30


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


def set_accounts(campaign_id: int, account_ids: list[int]) -> None:
    db.ex("DELETE FROM campaign_accounts WHERE campaign_id=%s", (campaign_id,))
    for a in account_ids:
        db.ex("INSERT INTO campaign_accounts(campaign_id, account_id) VALUES (%s, %s)", (campaign_id, a))


def campaign_accounts(campaign_id: int) -> list[dict]:
    return db.q("""SELECT a.* FROM campaign_accounts ca JOIN tg_accounts a ON a.id=ca.account_id
                   WHERE ca.campaign_id=%s ORDER BY a.id""", (campaign_id,))


# ---------- импорт xlsx ----------
def import_xlsx(data: bytes, filename: str, user_id: int, account_ids: list[int]) -> tuple[int, int, list[str]]:
    sheets = read_xlsx(data)
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
    warnings = []
    with db.tx():
        cid = db.ex("""INSERT INTO campaigns(name, source, filename, created_by) VALUES (%s, 'xlsx', %s, %s)
                       RETURNING id""", (re.sub(r"\.xlsx$", "", filename or "список", flags=re.I), filename, user_id))
        set_accounts(cid, account_ids)
        n, dups, warn_topic = _insert_rows(cid, sheets[sheet][hi + 1:], cmap, texts)
    if dups:
        warnings.append(f"{dups} повторяющихся строк (тот же адресат и тема) пропущено — одному человеку одно сообщение")
    if warn_topic:
        warnings.append("«Ссылка на тему» ведёт в другой чат, тема не применена: " + ", ".join(warn_topic[:5]))
    no_target = db.val("""SELECT COUNT(*) FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id
                          WHERE cl.campaign_id=%s AND l.tg_id IS NULL AND l.username IS NULL""", (cid,))
    if no_target:
        warnings.append(f"{no_target} строк без распознанной ссылки — их отправить не получится")
    no_text = db.val("SELECT COUNT(*) FROM campaign_leads WHERE campaign_id=%s AND COALESCE(custom_text, '')=''", (cid,))
    if no_text:
        warnings.append(f"{no_text} строк без текста")
    db.log(f"Импорт списка «{filename}»: {n} строк (лист «{sheet}») → кампания #{cid}")
    return cid, n, warnings


def _insert_rows(cid: int, rows: list, cmap: dict, texts: dict) -> tuple[int, int, list]:
    n = dups = 0
    warn_topic = []
    for row in rows:
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
        dialog = rec.get("dialog")
        if kind == "chat" and not dialog:
            dialog = "Не применимо"
        lead_id, _ = upsert_lead({"kind": kind, "tg_id": p["peer_id"], "username": norm_username(p["username"]),
                                  "first_name": rec.get("first_name") or "", "last_name": rec.get("last_name") or "",
                                  "title": rec.get("title"), "source": "xlsx"})
        meta = {k: rec[k] for k in ("folder", "plan", "note", "text_no", "link") if rec.get(k)}
        try:
            with db.tx():   # точка сохранения: дубликат не откатывает весь импорт
                db.ex("""INSERT INTO campaign_leads(campaign_id, lead_id, custom_text, topic_id, topic_title, row_no,
                         dialog, address, src_status, src_group, meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                      (cid, lead_id, text, p["topic_id"], rec.get("topic_title"), rec.get("row_no"), dialog,
                       rec.get("address"), rec.get("src_status"), status_group(rec.get("src_status")), db.jsonb(meta)))
        except UniqueViolation:
            dups += 1
            continue
        n += 1
    return n, dups, warn_topic


# ---------- кампания по шаблону ----------
def create_from_template(name: str, body: str, user_id: int, account_ids: list[int], tag: str = "",
                         start: bool = False, segment_id: int | None = None) -> tuple[int, int, int]:
    """Лиды (все, по тегу или из сегмента, кроме отписавшихся) → кампания. Возвращает (id, в очереди, пропущено)."""
    where, params = ["opted_out_at IS NULL", "kind='person'"], []
    if tag:
        where.append("%s = ANY(tags)")
        params.append(tag)
    if segment_id:
        where.append("id IN (SELECT lead_id FROM segment_leads WHERE segment_id=%s)")
        params.append(segment_id)
    ids = [r["id"] for r in db.q(f"SELECT id FROM leads WHERE {' AND '.join(where)} ORDER BY id", params)]
    with db.tx():
        cid = db.ex("""INSERT INTO campaigns(name, source, created_by, status, started_at)
                       VALUES (%s, 'template', %s, %s, %s) RETURNING id""",
                    (name, user_id, "running" if start else "draft", db.now_utc() if start else None))
        db.ex("INSERT INTO campaign_steps(campaign_id, position, body) VALUES (%s, 1, %s)", (cid, body))
        set_accounts(cid, account_ids)
        for lid in ids:
            db.ex("INSERT INTO campaign_leads(campaign_id, lead_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (cid, lid))
    queued, skipped = enqueue(cid, {"state": ["new"]})
    db.log(f"Создана кампания «{name}»: лидов {len(ids)}, в очереди {queued}, пропущено {skipped}")
    return cid, queued, skipped


def step_body(campaign_id: int, position: int = 1) -> str | None:
    return db.val("SELECT body FROM campaign_steps WHERE campaign_id=%s AND position=%s", (campaign_id, position))


def text_for(cl: dict, lead: dict) -> str:
    """Текст сообщения: готовый из строки xlsx или шаблон шага 1 с переменными лида."""
    if cl.get("custom_text"):
        return cl["custom_text"].strip()
    body = step_body(cl["campaign_id"])
    return render(body, db.lead_vars(lead)) if body else ""


# ---------- фильтры ----------
REAL_EXPR = "CASE WHEN l.kind='chat' THEN 'Не применимо' ELSE COALESCE(cl.real_dialog, 'не проверено') END"
MISMATCH_SQL = ("cl.real_dialog IS NOT NULL AND cl.dialog IN ('Диалог есть','Диалога нет') "
                "AND cl.dialog != cl.real_dialog")
FILTER_FIELDS = {"kind": "l.kind", "dialog": "cl.dialog", "real": REAL_EXPR, "address": "cl.address",
                 "src": "cl.src_group", "state": "cl.state"}
FROM_SQL = "campaign_leads cl JOIN leads l ON l.id=cl.lead_id"


def filter_sql(campaign_id: int, f: dict) -> tuple[str, list]:
    where, params = ["cl.campaign_id=%s"], [campaign_id]
    for key, col in FILTER_FIELDS.items():
        vals = f.get(key) or []
        if vals:
            where.append(f"COALESCE({col}, '(пусто)') = ANY(%s)")
            params.append(list(vals))
    if f.get("q"):
        where.append("""(COALESCE(cl.meta->>'folder','') || ' ' || COALESCE(l.title,'') || ' ' || l.first_name || ' '
                        || l.last_name || ' ' || COALESCE(cl.meta->>'note','') || ' ' || COALESCE(cl.custom_text,''))
                        ILIKE %s""")
        params.append(f"%{f['q']}%")
    if f.get("mismatch"):
        where.append(MISMATCH_SQL)
    if f.get("has_text"):
        where.append("(COALESCE(cl.custom_text, '') != '' OR EXISTS "
                     "(SELECT 1 FROM campaign_steps s WHERE s.campaign_id=cl.campaign_id))")
    if f.get("has_target"):
        where.append("(l.tg_id IS NOT NULL OR l.username IS NOT NULL OR l.phone IS NOT NULL)")
    if f.get("ids"):
        where.append("cl.id = ANY(%s)")
        params.append([int(i) for i in f["ids"]])
    if f.get("account"):
        where.append("cl.account_id = ANY(%s)")
        params.append([int(a) for a in f["account"]])
    return " AND ".join(where), params


def facets(campaign_id: int) -> dict[str, list]:
    return {key: db.q(f"""SELECT COALESCE({col}, '(пусто)') v, COUNT(*) n FROM {FROM_SQL}
                          WHERE cl.campaign_id=%s GROUP BY 1 ORDER BY n DESC""", (campaign_id,))
            for key, col in FILTER_FIELDS.items()}


def default_filter(campaign_id: int) -> dict:
    """По умолчанию: новые строки с текстом и ссылкой, кроме «Не писать» и уже отправленных по файлу."""
    fc = facets(campaign_id)
    src = [r["v"] for r in fc["src"] if not re.match(r"(?i)(отправлено|не писать|только личные)", r["v"])]
    addr = [r["v"] for r in fc["address"] if r["v"].lower() != "не писать"]
    return {"state": ["new"], "src": src, "address": addr, "kind": [], "dialog": [], "real": [], "account": [],
            "mismatch": False, "use_real": False, "has_text": True, "has_target": True, "q": ""}


def parse_filter(campaign_id: int, src) -> dict:
    """Фильтр из адресной строки (GET) или формы (POST); без «f» — фильтр по умолчанию."""
    if "f" not in src:
        return default_filter(campaign_id)
    return {"kind": src.getlist("kind"), "dialog": src.getlist("dialog"), "address": src.getlist("address"),
            "src": src.getlist("src"), "state": src.getlist("state"), "real": src.getlist("real"),
            "account": src.getlist("account"), "q": (src.get("q") or "").strip(),
            "mismatch": bool(src.get("mismatch")), "use_real": bool(src.get("use_real")),
            "has_text": bool(src.get("has_text")), "has_target": bool(src.get("has_target"))}


# ---------- очередь ----------
def _group(r, use_real: bool) -> int:
    if r["kind"] == "chat":
        return 0
    d = r["real_dialog"] if use_real and r["real_dialog"] else r["dialog"]
    if d is None and r["real_dialog"]:          # кампания по шаблону: истории из файла нет
        d = r["real_dialog"]
    return 1 if (d or "").lower().startswith("диалог есть") else 2


def _require_dialogs_snapshot(campaign_id: int, accounts: list[dict], f: dict) -> None:
    """Несколько аккаунтов и люди без закрепления: без снимка диалогов нельзя понять, кто кого знает,
    и знакомый человек может получить «Привет, …!» от незнакомого аккаунта. Требуем «Найти получателей»."""
    w, p = filter_sql(campaign_id, f)
    unpinned = db.val(f"""SELECT COUNT(*) FROM {FROM_SQL} WHERE {w} AND l.kind!='chat' AND l.owner_account_id IS NULL
                          AND cl.state IN ('new','failed','skipped')""", p)
    if not unpinned:
        return
    missing = [a for a in accounts
               if not db.val("SELECT EXISTS(SELECT 1 FROM tg_dialogs WHERE account_id=%s)", (a["id"],))]
    if missing:
        names = ", ".join(a["label"] or a["first_name"] or f"#{a['id']}" for a in missing)
        raise ValueError(f"Кампания идёт с нескольких аккаунтов — сначала нажмите «🔎 Найти получателей»: "
                         f"так знакомые люди достанутся тем, кто с ними переписывался. Нет данных по: {names}")


def enqueue(campaign_id: int, f: dict) -> tuple[int, int]:
    """Ставит отфильтрованные строки в очередь: чат → с диалогом → без диалога → …
    Каждый лид закрепляется за одним аккаунтом кампании: за тем, у кого уже есть с ним переписка,
    иначе за наименее загруженным. Лид чужого аккаунта (не из этой кампании) не берём —
    ему пишет только его менеджер. Возвращает (поставлено, пропущено)."""
    accounts = [a for a in campaign_accounts(campaign_id) if a["status"] in ("active", "paused")]
    if not accounts:
        raise ValueError("У кампании нет подключённых аккаунтов — выберите аккаунты, с которых писать")
    acc_ids = [a["id"] for a in accounts]
    days = recontact_days()
    if len(acc_ids) > 1:
        _require_dialogs_snapshot(campaign_id, accounts, f)
    w, p = filter_sql(campaign_id, f)
    rows = db.q(f"""SELECT cl.id, cl.lead_id, cl.dialog, cl.real_dialog, l.kind, l.tg_id, l.owner_account_id,
                           l.opted_out_at, a.label AS owner_label,
                           (SELECT MAX(m.created_at) FROM messages m WHERE m.lead_id=l.id AND m.direction='out'
                              AND (cl.id IS DISTINCT FROM m.campaign_lead_id)) AS last_out
                    FROM {FROM_SQL} LEFT JOIN tg_accounts a ON a.id=l.owner_account_id
                    WHERE {w} AND cl.state IN ('new','failed','skipped') ORDER BY cl.id""", p)
    load = {a: db.val("SELECT COUNT(*) FROM campaign_leads WHERE account_id=%s AND state='queued'", (a,)) or 0
            for a in acc_ids}
    warm = {}   # tg_id → аккаунты кампании, у которых есть личная переписка с этим человеком
    for r in db.q("""SELECT peer_id, account_id FROM tg_dialogs WHERE has_private AND account_id = ANY(%s)""", (acc_ids,)):
        warm.setdefault(r["peer_id"], []).append(r["account_id"])

    groups, skip = [[], [], []], []
    now = db.now_utc()
    for r in rows:
        if r["opted_out_at"]:
            skip.append((r["id"], "отписался"))
        elif r["owner_account_id"] and r["owner_account_id"] not in acc_ids:
            skip.append((r["id"], f"закреплён за другим аккаунтом ({r['owner_label'] or r['owner_account_id']})"))
        elif r["kind"] != "chat" and r["last_out"] and (now - r["last_out"]).days < days:
            skip.append((r["id"], f"уже писали {r['last_out']:%d.%m} (правило команды: не чаще раза в {days} дн.)"))
        else:
            groups[_group(r, f.get("use_real"))].append(r)
    ordered = []
    while any(groups):
        for g in groups:
            if g:
                ordered.append(g.pop(0))

    with db.tx():
        start = (db.val("SELECT MAX(order_idx) FROM campaign_leads WHERE campaign_id=%s", (campaign_id,)) or 0) + 1
        for i, r in enumerate(ordered):
            acc = r["owner_account_id"]
            if not acc:
                candidates = [a for a in warm.get(r["tg_id"], []) if a in acc_ids] or acc_ids
                acc = min(candidates, key=lambda a: (load[a], random.random()))
                db.ex("UPDATE leads SET owner_account_id=%s WHERE id=%s AND owner_account_id IS NULL", (acc, r["lead_id"]))
                acc = db.val("SELECT owner_account_id FROM leads WHERE id=%s", (r["lead_id"],))
            load[acc] = load.get(acc, 0) + 1
            db.ex("""UPDATE campaign_leads SET state='queued', error=NULL, order_idx=%s, account_id=%s WHERE id=%s""",
                  (start + i, acc, r["id"]))
        for cl_id, reason in skip:
            db.ex("UPDATE campaign_leads SET state='skipped', error=%s WHERE id=%s", (reason, cl_id))
    return len(ordered), len(skip)
