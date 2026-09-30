"""Поиск групп Telegram по теме: запросы, группы обсуждения каналов, проверка ссылок, оценка 0–100.

Ищем и оцениваем только публичную информацию (как поиск в приложении Telegram); никуда не вступаем.
"""
import asyncio
import math
import random
import re
from datetime import timedelta

from telethon import errors
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon import utils
from telethon.tl.types import Channel, ChatInvite, ChatInviteAlready, ChatInvitePeek

from . import db

MAX_QUERIES = 40             # запросов на один поиск
CHANNELS_PER_QUERY = 5       # у скольких найденных каналов смотреть группу обсуждения
RECHECK_AFTER = timedelta(days=7)
PAUSE = (3, 6)               # сек между запросами к поиску Telegram
STATUS_RU = {"new": "новая", "interesting": "интересна", "joined": "вступил", "rejected": "отклонена"}
VIA_RU = {"search": "поиск", "discussion": "обсуждение канала", "link": "ссылка"}


# ---------- чистые функции ----------
def split_words(s: str | None) -> list[str]:
    return [w.strip() for w in re.split(r"[,\n;]", s or "") if w.strip()]


def build_queries(keywords: list[str], geo: list[str]) -> list[str]:
    """Каждое ключевое слово само по себе и в паре с каждым местом; без повторов, не больше MAX_QUERIES."""
    out = []
    for kw in keywords:
        for g in [""] + list(geo):
            q = f"{kw} {g}".strip()
            if q.lower() not in (x.lower() for x in out):
                out.append(q)
    return out[:MAX_QUERIES]


def _stem(word: str) -> str:
    """Грубая основа слова: без 1–2 гласных/ь/й на конце, чтобы «предприниматель» находил «предпринимателей»."""
    w = word.lower()
    for _ in range(2):
        if len(w) > 5 and w[-1] in "аеёиоуыьэюяй":
            w = w[:-1]
    return w


def count_matches(text: str, keywords: list[str]) -> int:
    """Сколько ключевых слов нашлось; во фразе из нескольких слов должны найтись все (по основам)."""
    low = (text or "").lower()
    return sum(1 for kw in keywords if kw.strip() and all(_stem(w) in low for w in kw.split()))


def find_stop(text: str, stop_words: list[str]) -> str | None:
    low = (text or "").lower()
    return next((w for w in stop_words if w.lower() in low), None)


def detect_lang(texts: list[str]) -> str | None:
    letters = "".join(re.findall(r"[^\W\d_]", " ".join(texts)))
    if len(letters) < 30:
        return None
    cyr = sum(1 for ch in letters if "а" <= ch.lower() <= "я" or ch.lower() in "ёіўєї")
    return "кириллица" if cyr / len(letters) > 0.5 else "латиница"


def score(match: int, n_keywords: int, members: int | None, msgs_per_day: float | None, days_silent: float | None,
          stop_hit: str | None) -> int:
    """40 — совпадение с темой, 35 — живость, 25 — размер. Стоп-слово → 0, молчит больше 30 дней → не выше 10."""
    if stop_hit:
        return 0
    m = min(match / max(min(n_keywords, 3), 1), 1)
    a = min((msgs_per_day or 0) / 20, 1)
    s = min(math.log10(max(members or 1, 1)) / 4, 1)          # 10 000 участников — максимум
    total = 40 * m + 35 * a + 25 * s
    if days_silent is None or days_silent > 30:
        total = min(total, 10)
    return round(total)


def parse_link(line: str) -> tuple[str, str] | None:
    """('invite', hash) для t.me/+… и joinchat, ('username', name) для t.me/name и @name."""
    line = line.strip()
    m = re.search(r"t(?:elegram)?\.me/(?:\+|joinchat/)([\w-]+)", line)
    if m:
        return "invite", m.group(1)
    m = re.search(r"(?:t(?:elegram)?\.me/|^@)([A-Za-z][\w]{3,})", line)
    return ("username", m.group(1)) if m else None


# ---------- работа с Telegram ----------
async def _evaluate(client, entity) -> dict:
    """Описание, участники, активность и язык по последним сообщениям публичной группы."""
    info = {"about": "", "members": getattr(entity, "participants_count", None), "texts": [],
            "last_message_at": None, "msgs_per_day": None, "last_message_id": None}
    try:
        full = await client(GetFullChannelRequest(entity))
        info["about"] = full.full_chat.about or ""
        info["members"] = full.full_chat.participants_count or info["members"]
    except (errors.RPCError, ValueError, TypeError):
        pass
    try:
        msgs = await client.get_messages(entity, limit=30)
    except (errors.RPCError, ValueError, TypeError):
        msgs = []
    dated = [m for m in msgs if getattr(m, "date", None)]
    if dated:
        info["last_message_at"] = max(m.date for m in dated)
        info["last_message_id"] = max((getattr(m, "id", 0) or 0) for m in dated) or None
        week_ago = db.now_utc() - timedelta(days=7)
        recent = [m for m in dated if m.date >= week_ago]
        if len(recent) == len(dated) and len(dated) >= 2:
            # все 30 сообщений уложились меньше чем в неделю — считаем по их реальному промежутку
            span = max((max(m.date for m in dated) - min(m.date for m in dated)).total_seconds() / 86400, 1 / 24)
            info["msgs_per_day"] = round(len(dated) / span, 1)
        else:
            info["msgs_per_day"] = round(len(recent) / 7, 1)
        info["texts"] = [m.message or "" for m in dated]
    return info


def _save(search: dict, query: str, via: str, *, tg_id=None, username=None, invite_link=None, title="",
          info: dict | None = None, parent=None) -> int:
    """Добавляет или обновляет найденную группу и отмечает, каким запросом она найдена."""
    kws, stops = search["keywords"], search["stop_words"]
    row = (db.one("SELECT * FROM found_chats WHERE tg_id=%s", (tg_id,)) if tg_id else None) or \
          (db.one("SELECT * FROM found_chats WHERE invite_link=%s", (invite_link,)) if invite_link else None)
    fields = {"title": title}
    if username:
        fields["username"] = username
    if tg_id:
        fields["tg_id"] = tg_id
    if invite_link:
        fields["invite_link"] = invite_link
    if parent is not None:          # канал, через который открывается группа обсуждения
        fields["parent_username"] = getattr(parent, "username", None)
        fields["parent_title"] = getattr(parent, "title", None)
    if info is not None:
        text = " ".join([title, info["about"], *info["texts"]])
        silent = (db.now_utc() - info["last_message_at"]).total_seconds() / 86400 if info["last_message_at"] else None
        match, stop = count_matches(text, kws), find_stop(" ".join([title, info["about"]]), stops)
        fields.update(about=info["about"], members=info["members"], last_message_at=info["last_message_at"],
                      last_message_id=info.get("last_message_id"),
                      msgs_per_day=info["msgs_per_day"], lang=detect_lang(info["texts"]), match=match, stop_hit=stop,
                      score=score(match, len(kws), info["members"], info["msgs_per_day"], silent, stop),
                      checked_at=db.now_utc())
    if row:
        sets = ", ".join(f"{k}=%s" for k in fields)
        db.ex(f"UPDATE found_chats SET {sets} WHERE id=%s", (*fields.values(), row["id"]))
        fid = row["id"]
    else:
        fields.update(via=via, first_search_id=search["id"])
        fid = db.ex(f"INSERT INTO found_chats({', '.join(fields)}) VALUES ({', '.join(['%s'] * len(fields))}) RETURNING id",
                    tuple(fields.values()))
    db.ex("INSERT INTO found_chat_hits(found_chat_id, search_id, query) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
          (fid, search["id"], query))
    return fid


def _needs_check(tg_id: int) -> bool:
    row = db.one("SELECT status, checked_at FROM found_chats WHERE tg_id=%s", (tg_id,))
    if not row:
        return True
    if row["status"] == "rejected":
        return False
    return not row["checked_at"] or db.now_utc() - row["checked_at"] > RECHECK_AFTER


async def _add_group(client, search, query, via, ch, st, parent=None):
    tg_id = utils.get_peer_id(ch)
    info = await _evaluate(client, ch) if _needs_check(tg_id) else None
    _save(search, query, via, tg_id=tg_id, username=getattr(ch, "username", None), title=ch.title or "", info=info,
          parent=parent)
    st["found"] = db.val("SELECT COUNT(DISTINCT found_chat_id) FROM found_chat_hits WHERE search_id=%s", (search["id"],))


async def run_search(acc, search_id: int, st: dict) -> None:
    """Поиск по всем запросам темы. Держит acc.lock: на время поиска отправка с аккаунта стоит."""
    search = db.one("SELECT * FROM chat_searches WHERE id=%s", (search_id,))
    queries = build_queries(search["keywords"], search["geo"])
    st.update(total=len(queries), done=0, found=0)
    db.ex("UPDATE chat_searches SET status='running', step=NULL WHERE id=%s", (search_id,))
    client = acc.client
    async with acc.lock:
        old = client.flood_sleep_threshold
        client.flood_sleep_threshold = 300          # короткие FloodWait поиска Telethon выждет сам
        try:
            for i, q in enumerate(queries):
                st["step"] = f"запрос {i + 1} из {len(queries)}: «{q}»"
                res = await client(SearchRequest(q=q, limit=50))
                channels = []
                for ch in res.chats:
                    if not isinstance(ch, Channel):
                        continue
                    if ch.megagroup and ch.username:
                        await _add_group(client, search, q, "search", ch, st)
                    elif ch.broadcast and len(channels) < CHANNELS_PER_QUERY:
                        channels.append(ch)
                for chan in channels:                  # канал → его группа обсуждения
                    try:
                        full = await client(GetFullChannelRequest(chan))
                    except errors.RPCError:
                        continue
                    linked = full.full_chat.linked_chat_id
                    group = next((c for c in full.chats if c.id == linked and getattr(c, "megagroup", False)), None)
                    if group:
                        await _add_group(client, search, q, "discussion", group, st, parent=chan)
                st["done"] = i + 1
                if i < len(queries) - 1:
                    await asyncio.sleep(random.uniform(*PAUSE))
        finally:
            client.flood_sleep_threshold = old
    mark_joined()
    db.ex("UPDATE chat_searches SET status='done', step=%s, finished_at=now() WHERE id=%s",
          (f"найдено групп: {st['found']}", search_id))


async def check_links(acc, search_id: int, lines: list[str], st: dict) -> tuple[int, list[str]]:
    """Ручной список ссылок: проверяем, что это группы, и добавляем их в найденные."""
    search = db.one("SELECT * FROM chat_searches WHERE id=%s", (search_id,))
    client, ok, skipped = acc.client, 0, []
    async with acc.lock:
        for i, line in enumerate(lines):
            st["step"] = f"ссылка {i + 1} из {len(lines)}"
            parsed = parse_link(line)
            if not parsed:
                skipped.append(f"{line}: не похоже на ссылку Telegram")
                continue
            kind, value = parsed
            try:
                if kind == "username":
                    ent = await client.get_entity(value)
                    if not (isinstance(ent, Channel) and ent.megagroup):
                        skipped.append(f"{line}: это не группа")
                        continue
                    await _add_group(client, search, line, "link", ent, st)
                else:
                    inv = await client(CheckChatInviteRequest(value))
                    chat = getattr(inv, "chat", None) if isinstance(inv, (ChatInviteAlready, ChatInvitePeek)) else None
                    if chat is not None:
                        await _add_group(client, search, line, "link", chat, st)
                    elif isinstance(inv, ChatInvite) and not inv.broadcast:
                        _save(search, line, "link", invite_link=f"https://t.me/+{value}", title=inv.title,
                              info={"about": getattr(inv, "about", "") or "", "members": inv.participants_count,
                                    "texts": [], "last_message_at": None, "msgs_per_day": None})
                    else:
                        skipped.append(f"{line}: это канал, а не группа")
                        continue
                ok += 1
            except errors.FloodWaitError:
                raise
            except (errors.RPCError, ValueError) as e:
                skipped.append(f"{line}: {type(e).__name__.replace('Error', '')}")
            await asyncio.sleep(2)
    mark_joined()
    return ok, skipped


def mark_joined() -> int:
    """Группы, которые уже есть в диалогах аккаунтов команды, отмечаем «вступил»."""
    return db.changed("""UPDATE found_chats f SET status='joined' WHERE status IN ('new','interesting')
                         AND EXISTS (SELECT 1 FROM tg_dialogs d WHERE d.peer_id=f.tg_id)""")
