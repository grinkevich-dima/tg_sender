"""Лиды: поиск дубликатов, импорт (CSV, Telegram), теги, отписка."""
import csv
import io
import re

from . import db

LEAD_FIELDS = ("kind", "tg_id", "username", "phone", "first_name", "last_name", "title", "source")


def norm_username(v: str | None) -> str | None:
    v = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)?@?", "", (v or "").strip()).strip("/")
    return v or None


def norm_phone(v: str | None) -> str | None:
    p = re.sub(r"[^\d+]", "", v or "")
    if not p:
        return None
    return p if p.startswith("+") else "+" + p


def find_lead(tg_id=None, username=None, phone=None) -> dict | None:
    """Ищем того же человека/чат по ID, потом по username, потом по телефону."""
    if tg_id:
        row = db.one("SELECT * FROM leads WHERE tg_id=%s", (tg_id,))
        if row:
            return row
    if username:
        row = db.one("SELECT * FROM leads WHERE lower(username)=lower(%s)", (username,))
        if row:
            return row
    if phone:
        return db.one("SELECT * FROM leads WHERE phone=%s", (phone,))
    return None


def upsert_lead(data: dict, tags: list[str] | None = None, extra: dict | None = None,
                owner_account_id: int | None = None) -> tuple[int, bool]:
    """Создаёт лида или дополняет найденного: заполняет только пустые поля — импорт одного менеджера
    не переименовывает лидов общей базы. Возвращает (id, создан_ли)."""
    data = {k: v for k, v in data.items() if k in LEAD_FIELDS}
    tags = [t for t in (tags or []) if t]
    existing = find_lead(data.get("tg_id"), data.get("username"), data.get("phone"))
    if existing:
        sets, params = [], []
        for k, v in data.items():
            if v in (None, "") or k in ("kind", "source") or existing[k] not in (None, ""):
                continue
            if k in ("tg_id", "username", "phone") and find_lead(**{k: v}):
                continue  # значение уже у другого лида — не ломаем уникальность
            sets.append(f"{k}=%s")
            params.append(v)
        if tags:
            sets.append("tags=(SELECT array_agg(DISTINCT t) FROM unnest(tags || %s::text[]) t)")
            params.append(tags)
        if extra:
            sets.append("extra=extra || %s")
            params.append(db.jsonb(extra))
        if owner_account_id and not existing["owner_account_id"]:
            sets.append("owner_account_id=%s")
            params.append(owner_account_id)
        if sets:
            db.ex(f"UPDATE leads SET {', '.join(sets)} WHERE id=%s", (*params, existing["id"]))
        return existing["id"], False
    cols = dict(data)
    cols["tags"] = sorted(set(tags))
    cols["extra"] = db.jsonb(extra or {})
    if owner_account_id:
        cols["owner_account_id"] = owner_account_id
    for k in ("first_name", "last_name"):
        cols[k] = cols.get(k) or ""
    lid = db.ex(f"INSERT INTO leads({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                tuple(cols.values()))
    return lid, True


def split_tags(s: str | None) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def all_tags() -> list[str]:
    return [r["t"] for r in db.q("SELECT DISTINCT unnest(tags) t FROM leads ORDER BY 1")]


def add_to_segment(segment_id: int | None, lead_id: int, source: str) -> bool:
    """Добавляет лида в сегмент; False — уже был там."""
    if not segment_id:
        return False
    return bool(db.changed("""INSERT INTO segment_leads(segment_id, lead_id, source) VALUES (%s, %s, %s)
                              ON CONFLICT DO NOTHING""", (segment_id, lead_id, source)))


def import_csv(raw: bytes, tag: str = "", owner_account_id: int | None = None,
               segment_id: int | None = None) -> tuple[int, int, int]:
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    added = updated = bad = 0
    with db.tx():
        for row in csv.DictReader(io.StringIO(text), dialect=dialect):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            uname = norm_username(row.pop("username", "") or row.pop("telegram", ""))
            phone = norm_phone(row.pop("phone", "") or row.pop("телефон", ""))
            uid = row.pop("user_id", "") or row.pop("id", "")
            uid = int(uid) if uid.isdigit() else None
            first = row.pop("first_name", "") or row.pop("name", "") or row.pop("имя", "")
            last = row.pop("last_name", "") or row.pop("фамилия", "")
            tags = split_tags(row.pop("tags", "")) + split_tags(tag)
            if not (uname or phone or uid):
                bad += 1
                continue
            extra = {k: v for k, v in row.items() if k and v}
            lid, created = upsert_lead({"tg_id": uid, "username": uname, "phone": phone, "first_name": first,
                                        "last_name": last, "source": "csv"}, tags, extra, owner_account_id)
            add_to_segment(segment_id, lid, "csv")
            added += created
            updated += not created
    return added, updated, bad


def import_tg_users(users, tag: str, account_id: int, segment_id: int | None = None) -> int:
    """Контакты/собеседники аккаунта → лиды, закреплённые за этим аккаунтом (он их уже знает)."""
    n = 0
    with db.tx():
        for u in users:
            lid, created = upsert_lead({"tg_id": u.id, "username": u.username, "phone": norm_phone(u.phone),
                                        "first_name": u.first_name or "", "last_name": u.last_name or "",
                                        "source": "telegram"}, split_tags(tag), None, account_id)
            add_to_segment(segment_id, lid, "telegram")
            n += created
    return n


# ---- отписка ----
def is_stop_message(text: str | None, stop_words: str) -> bool:
    """Сообщение начинается со стоп-слова целиком: «Стоп!», «stop please» — да; «стопудово», «stopping» — нет."""
    low = (text or "").lower().strip()
    words = [w.strip().lower() for w in (stop_words or "").split(",") if w.strip()]
    return bool(low) and any(re.match(rf"{re.escape(w)}(?!\w)", low) for w in words)


def opt_out(lead_id: int, reason: str) -> None:
    """Отписка на всю команду: лид исключается из всех очередей и будущих кампаний."""
    db.ex("UPDATE leads SET opted_out_at=COALESCE(opted_out_at, now()), opt_out_reason=%s WHERE id=%s",
          (reason, lead_id))
    db.ex("""UPDATE campaign_leads SET state='skipped', error='отписался'
             WHERE lead_id=%s AND state IN ('new', 'queued')""", (lead_id,))


def opt_in(lead_id: int) -> None:
    db.ex("UPDATE leads SET opted_out_at=NULL, opt_out_reason=NULL WHERE id=%s", (lead_id,))
