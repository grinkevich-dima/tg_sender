"""Наполнение базы знаний ИИ: всё, что ИИ предлагает, — на проверку человеку (ai_suggestions), сразу ничего не сохраняется.

- импорт: текст, файл (PDF, DOCX, TXT, HTML) или страница сайта → карточки;
- «нет ответа в базе» (ai_gaps): вопросы, на которых ИИ не хватило фактов — ответ одной формой становится карточкой;
- сбор из переписок: пары «клиент → ответ человека» → примеры и карточки;
- мастер: ответы на вопросы о бизнесе → инструкция профиля и стартовые карточки;
- проверка базы: дубли, длинные карточки, факты в инструкции, противоречия;
- копирование карточек между профилями.
"""
import asyncio
import io
import ipaddress
import itertools
import json
import re
import socket
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree

from . import ai, db

MAX_TEXT = 60_000          # символов текста на один импорт (≈ 25–30 страниц)
CHUNK = 6_000              # символов на один запрос к ИИ
MAX_FETCH = 3 * 1024 * 1024
MAX_PAIRS = 150            # пар «клиент → мы» на один сбор из переписок
PAIRS_PER_CALL = 25
CARD_MAX = 700             # карточка длиннее — «длинная» при проверке

jobs: dict[int, dict] = {}  # profile_id → ход фоновой задачи наполнения {"running", "what", "step", "found", "error"}


class KBError(Exception):
    """Понятная ошибка наполнения: не тот файл, недоступная ссылка и т.п."""


# ---------- текст из файлов и страниц ----------
class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}
    BLOCK = {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "td", "th"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    return _tidy("".join(p.parts))


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def docx_text(data: bytes) -> str:
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 50 * 1024 * 1024:
                raise KBError("Слишком большой документ")
            root = ElementTree.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError):
        raise KBError("Не удалось прочитать DOCX — сохраните документ заново или вставьте текст")
    paras = ["".join(t.text or "" for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p")]
    return _tidy("\n".join(paras))


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:60])
    except (PdfReadError, ValueError, KeyError) as e:
        raise KBError(f"Не удалось прочитать PDF: {e}")
    if not text.strip():
        raise KBError("В PDF нет текста (похоже, это скан) — вставьте текст вручную")
    return _tidy(text)


def file_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return pdf_text(data)
    if name.endswith(".docx"):
        return docx_text(data)
    if name.endswith((".html", ".htm")):
        return html_text(_decode(data))
    if name.endswith((".txt", ".md", ".csv")) or not name:
        return _tidy(_decode(data))
    raise KBError("Поддерживаются PDF, DOCX, TXT, MD, HTML. Из Word (.doc) сохраните как .docx")


def _public_host(host: str) -> bool:
    """Только адреса в интернете: ссылку не получится направить на саму панель или локальную сеть."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


def fetch_url(url: str) -> str:
    url = url.strip()
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise KBError("Нужна ссылка вида https://сайт/страница")
    if not _public_host(u.hostname):
        raise KBError("Эта ссылка ведёт не в интернет (локальный или внутренний адрес)")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (TG Sender knowledge import)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if not _public_host(urllib.parse.urlparse(r.geturl()).hostname or ""):
                raise KBError("Ссылка перенаправила на внутренний адрес")
            data = r.read(MAX_FETCH + 1)
            ctype = r.headers.get("Content-Type", "")
    except KBError:
        raise
    except Exception as e:
        raise KBError(f"Страница недоступна: {e}")
    if len(data) > MAX_FETCH:
        raise KBError("Страница слишком большая (больше 3 МБ)")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return pdf_text(data)
    return html_text(_decode(data)) if "html" in ctype or b"<html" in data[:2000].lower() else _tidy(_decode(data))


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """Куски по абзацам, не больше size символов (длинный абзац режется)."""
    out, cur = [], ""
    for para in text.split("\n"):
        while len(para) > size:
            out.append(para[:size])
            para = para[size:]
        if len(cur) + len(para) + 1 > size and cur:
            out.append(cur)
            cur = ""
        cur += para + "\n"
    if cur.strip():
        out.append(cur)
    return out


# ---------- ИИ: ответы в JSON ----------
def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def _clean_cards(items) -> list[dict]:
    out = []
    for c in items or []:
        if isinstance(c, dict):
            title, body = str(c.get("title") or "").strip(), str(c.get("body") or "").strip()
            if title and body:
                out.append({"title": title[:200], "body": body[:3000]})
    return out


def _clean_examples(items) -> list[dict]:
    out = []
    for e in items or []:
        if isinstance(e, dict):
            q, a = str(e.get("question") or "").strip(), str(e.get("answer") or "").strip()
            if q and a:
                out.append({"question": q[:1000], "answer": a[:1500]})
    return out


CARDS_PROMPT = (
    "Ты помогаешь наполнить базу знаний для ИИ-менеджера, который переписывается с клиентами в Telegram. "
    "Из текста ниже выдели карточки: одна карточка — одна тема. Заголовок — так, как спросил бы клиент "
    "(«Сколько стоит сайт», «Можно в рассрочку?»). Текст карточки — только факты из текста: цифры, сроки, условия, "
    "этапы, ссылки, ответы на возражения; коротко, без рекламных слов; ничего не выдумывай. Пропускай то, что клиенту "
    "не нужно (меню сайта, юридические оговорки, куки). Не дублируй темы из списка «уже есть». "
    'Ответь строго JSON: {"cards": [{"title": "...", "body": "..."}]}. Если полезного нет — {"cards": []}.')


def _existing_titles(profile_id: int) -> list[str]:
    return [r["title"] for r in db.q("""SELECT title FROM ai_cards WHERE profile_id=%s OR profile_id IS NULL
                                        UNION SELECT title FROM ai_suggestions WHERE profile_id=%s AND kind='card'""",
                                     (profile_id, profile_id))]


def _norm(s: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", (s or "").lower()).strip()


async def cards_from_text(profile_id: int, text: str, st: dict | None = None) -> list[dict]:
    text = text[:MAX_TEXT]
    parts = chunks(text)
    seen = {_norm(t) for t in _existing_titles(profile_id)}
    found: list[dict] = []
    for i, part in enumerate(parts, 1):
        if st is not None:
            st["step"] = f"разбираю часть {i} из {len(parts)}"
        have = ", ".join(sorted(seen))[:2000] or "—"
        answer = await ai.chat([{"role": "system", "content": CARDS_PROMPT},
                                {"role": "user", "content": f"Уже есть: {have}\n\nТекст:\n{part}"}],
                               temperature=0.2, max_tokens=2500)
        for c in _clean_cards(parse_json(answer).get("cards")):
            if _norm(c["title"]) not in seen:
                seen.add(_norm(c["title"]))
                found.append(c)
                if st is not None:
                    st["found"] = len(found)
    return found


def add_suggestions(profile_id: int, user_id: int | None, source: str, cards=(), examples=(), instruction: str = "") -> int:
    n = 0
    with db.tx():
        for c in cards:
            db.ex("""INSERT INTO ai_suggestions(profile_id, kind, title, body, source, created_by)
                     VALUES (%s, 'card', %s, %s, %s, %s)""", (profile_id, c["title"], c["body"], source[:200], user_id))
            n += 1
        for e in examples:
            db.ex("""INSERT INTO ai_suggestions(profile_id, kind, title, body, source, created_by)
                     VALUES (%s, 'example', %s, %s, %s, %s)""", (profile_id, e["question"], e["answer"], source[:200], user_id))
            n += 1
        if instruction.strip():
            db.ex("DELETE FROM ai_suggestions WHERE profile_id=%s AND kind='instruction'", (profile_id,))
            db.ex("""INSERT INTO ai_suggestions(profile_id, kind, body, source, created_by)
                     VALUES (%s, 'instruction', %s, %s, %s)""", (profile_id, instruction.strip(), source[:200], user_id))
            n += 1
    return n


async def _run(profile_id: int, what: str, coro_fn) -> None:
    """Фоновая задача наполнения с ходом в jobs[profile_id]; ошибки — в ход и журнал."""
    st = jobs[profile_id] = {"running": True, "what": what, "step": "начинаю", "found": 0, "error": ""}
    try:
        n = await coro_fn(st)
        st["step"] = f"готово: предложений на проверку — {n}" if n else "готово: ничего нового не нашлось"
    except (ai.AIError, KBError) as e:
        st["error"] = str(e)
    except Exception as e:
        st["error"] = f"{type(e).__name__}: {e}"
        db.log(f"Наполнение базы ИИ ({what}): {type(e).__name__}: {e}", "error")
    finally:
        st["running"] = False


def busy(profile_id: int) -> bool:
    return bool(jobs.get(profile_id, {}).get("running"))


# ---------- G1: импорт ----------
async def import_text(profile_id: int, user_id: int, text: str, source: str) -> None:
    async def go(st):
        if len(text.strip()) < 30:
            raise KBError("Текста слишком мало — нечего разбирать")
        cards = await cards_from_text(profile_id, text, st)
        return add_suggestions(profile_id, user_id, source, cards=cards)
    await _run(profile_id, f"импорт: {source}", go)


async def import_url(profile_id: int, user_id: int, url: str) -> None:
    async def go(st):
        st["step"] = "загружаю страницу"
        text = await asyncio.to_thread(fetch_url, url)
        cards = await cards_from_text(profile_id, text, st)
        return add_suggestions(profile_id, user_id, f"сайт {url[:150]}", cards=cards)
    await _run(profile_id, f"импорт: {url[:80]}", go)


# ---------- G2: нет ответа в базе ----------
def note_gap(profile_id: int | None, question: str, reason: str, source: str, lead_id: int | None = None) -> None:
    """ИИ не хватило фактов. Одинаковый вопрос в том же профиле не дублируется — растёт счётчик."""
    question = (question or "").strip()[:1000]
    if not question:
        return
    db.ex("""INSERT INTO ai_gaps(profile_id, question, reason, source, lead_id) VALUES (%s, %s, %s, %s, %s)
             ON CONFLICT (COALESCE(profile_id, 0), md5(lower(question)))
             DO UPDATE SET hits=ai_gaps.hits + 1, last_at=now(), reason=EXCLUDED.reason""",
          (profile_id, question, (reason or "")[:300], source, lead_id))


def gaps(profile_id: int | None) -> list[dict]:
    return db.q("""SELECT * FROM ai_gaps WHERE profile_id IS NOT DISTINCT FROM %s ORDER BY hits DESC, last_at DESC""",
                (profile_id,))


def answer_gap(gap_id: int, profile_id: int | None, title: str, body: str) -> bool:
    with db.tx():
        if not db.changed("DELETE FROM ai_gaps WHERE id=%s AND profile_id IS NOT DISTINCT FROM %s", (gap_id, profile_id)):
            return False
        db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, %s, %s)", (profile_id, title.strip(), body.strip()))
    return True


# ---------- G3: из переписок ----------
DIALOGS_PROMPT = (
    "Ты помогаешь обучить ИИ-менеджера на реальных переписках компании. Тема бизнеса: {topic}\n"
    "Ниже пары: что написал клиент → что ответил наш менеджер-человек. Выбери пары, которые годятся примерами "
    "для этой темы: типичный вопрос клиента и хороший ответ по делу. Пропускай болтовню, личное, не по теме, "
    "устаревшее и неудачные ответы. В выбранных парах убери персональные данные (имена, телефоны, адреса, суммы "
    "конкретных сделок) — замени общими словами, стиль ответа сохрани. Отдельно выпиши факты о бизнесе, которые "
    "менеджер называл (цены, сроки, условия, ссылки), карточками: заголовок — вопрос словами клиента, текст — факты. "
    'Ответь строго JSON: {{"examples": [{{"question": "...", "answer": "..."}}], "cards": [{{"title": "...", "body": "..."}}]}}.')


def dialog_pairs(profile_id: int, days: int, scope: str) -> list[dict]:
    """Пары «сообщения клиента подряд → ответ человека подряд» за days дней. scope: profile — только кампании
    с этим профилем; all — все диалоги."""
    where, params = "", [days]
    if scope == "profile":
        where = """AND m.lead_id IN (SELECT cl.lead_id FROM campaign_leads cl JOIN campaigns c ON c.id=cl.campaign_id
                                     WHERE c.ai_profile_id=%s)"""
        params.append(profile_id)
    rows = db.q(f"""SELECT m.lead_id, m.direction, m.text, m.source FROM messages m
                    WHERE m.created_at > now() - make_interval(days => %s) AND COALESCE(m.text, '') != ''
                      AND m.deleted_at IS NULL {where}
                    ORDER BY m.lead_id, m.created_at, m.id""", params)
    pairs = []
    for _, group in itertools.groupby(rows, key=lambda r: r["lead_id"]):
        q: list[str] = []
        a: list[str] = []
        for r in group:
            if r["direction"] == "in":
                if a:                      # клиент написал после нашего ответа — пара закрыта
                    pairs.append({"question": "\n".join(q), "answer": "\n".join(a)})
                    q, a = [], []
                q.append(r["text"])
            elif r["source"] in ("inbox", "telegram"):
                if q:                      # ответ человека на сообщения клиента
                    a.append(r["text"])
            else:                          # рассылка или ИИ — не пример человеческого ответа
                if q and a:
                    pairs.append({"question": "\n".join(q), "answer": "\n".join(a)})
                q, a = [], []
        if q and a:
            pairs.append({"question": "\n".join(q), "answer": "\n".join(a)})
    return pairs[-MAX_PAIRS:]


async def mine_dialogs(profile_id: int, user_id: int, days: int, scope: str) -> None:
    async def go(st):
        prof = db.one("SELECT name, instruction FROM ai_profiles WHERE id=%s", (profile_id,))
        topic = f"{prof['name']}. {prof['instruction'][:800]}"
        pairs = dialog_pairs(profile_id, days, scope)
        if not pairs:
            raise KBError("За этот период нет диалогов, где отвечал человек")
        seen = {_norm(t) for t in _existing_titles(profile_id)}
        examples, cards = [], []
        batches = [pairs[i:i + PAIRS_PER_CALL] for i in range(0, len(pairs), PAIRS_PER_CALL)]
        for i, batch in enumerate(batches, 1):
            st["step"] = f"читаю переписки: часть {i} из {len(batches)}"
            text = "\n\n".join(f"#{n}\nКЛИЕНТ: {p['question'][:600]}\nМЫ: {p['answer'][:800]}" for n, p in enumerate(batch, 1))
            d = parse_json(await ai.chat([{"role": "system", "content": DIALOGS_PROMPT.format(topic=topic)},
                                          {"role": "user", "content": text}], temperature=0.2, max_tokens=3000))
            examples += _clean_examples(d.get("examples"))
            for c in _clean_cards(d.get("cards")):
                if _norm(c["title"]) not in seen:
                    seen.add(_norm(c["title"]))
                    cards.append(c)
            st["found"] = len(examples) + len(cards)
        return add_suggestions(profile_id, user_id, f"переписки за {days} дн.", cards=cards, examples=examples)
    await _run(profile_id, "сбор из переписок", go)


# ---------- G4: мастер ----------
WIZARD = [
    ("who", "Кто пишет клиентам?", "Имя, роль, в мужском или женском роде. Напр.: Дмитрий, руководитель веб-агентства, мужской род"),
    ("what", "Чем вы занимаетесь?", "Продукт или услуга в двух-трёх предложениях"),
    ("whom", "Кому пишем и откуда эти люди?", "Напр.: собственники малого бизнеса из нашей группы в Telegram"),
    ("goal", "Чего хотим добиться в разговоре?", "Напр.: договориться о бесплатном созвоне на 20 минут"),
    ("flow", "Как вести разговор?", "Какие вопросы задать, когда предлагать цель"),
    ("price", "Цены", "Что сколько стоит, от чего зависит, есть ли рассрочка или скидки"),
    ("process", "Сроки и как проходит работа", "Этапы, что нужно от клиента"),
    ("faq", "Частые вопросы клиентов и ваши ответы", "Можно списком: вопрос — ответ"),
    ("objections", "Частые возражения и как отвечаете", "«Дорого», «подумаю», «уже есть» …"),
    ("links", "Ссылки", "Запись на созвон, сайт, кейсы, оплата"),
    ("tone", "Тон общения", "На «ты» или «вы», длина сообщений, смайлы, юмор"),
    ("never", "Чего нельзя", "Что не обещать, о чём не говорить"),
]
WIZARD_PROMPT = (
    "Составь профиль ИИ-менеджера, который переписывается с клиентами в Telegram, по ответам владельца бизнеса.\n"
    "1) instruction — инструкция по шаблону, коротко, по пунктам, 10–25 строк: «Кто пишет» (имя, роль, род), "
    "«Кому пишем», «Цель», «Как вести разговор» (нумерованные шаги), «Тон», «Если просят цену», «Если „не сейчас“», "
    "«Если отказ», «Если раздражён или просит не писать», «Нельзя». В инструкции — только поведение, без цен, дат и ссылок.\n"
    "2) cards — факты карточками: одна тема — одна карточка, заголовок — вопрос словами клиента, текст — факты "
    "(цены, сроки, этапы, ссылки, ответы на частые вопросы и возражения). Ничего не выдумывай: только из ответов. "
    'Ответь строго JSON: {"instruction": "...", "cards": [{"title": "...", "body": "..."}]}.')


async def wizard(profile_id: int, user_id: int, answers: dict[str, str]) -> None:
    async def go(st):
        filled = [(q, answers.get(k, "").strip()) for k, q, _ in WIZARD if answers.get(k, "").strip()]
        if not filled:
            raise KBError("Ответьте хотя бы на несколько вопросов")
        st["step"] = "составляю инструкцию и карточки"
        text = "\n\n".join(f"{q}\n{a[:3000]}" for q, a in filled)
        d = parse_json(await ai.chat([{"role": "system", "content": WIZARD_PROMPT}, {"role": "user", "content": text}],
                                     temperature=0.3, max_tokens=3500))
        instruction = str(d.get("instruction") or "").strip()
        cards = _clean_cards(d.get("cards"))
        if not instruction and not cards:
            raise ai.AIError("ИИ не смог составить профиль — попробуйте ещё раз")
        return add_suggestions(profile_id, user_id, "мастер", cards=cards, instruction=instruction)
    await _run(profile_id, "мастер профиля", go)


# ---------- предложения: принять / отклонить ----------
def suggestions(profile_id: int) -> list[dict]:
    return db.q("""SELECT s.*, u.name AS author FROM ai_suggestions s LEFT JOIN users u ON u.id=s.created_by
                   WHERE s.profile_id=%s ORDER BY s.kind='instruction' DESC, s.kind, s.id""", (profile_id,))


def accept(profile_id: int, picked: dict[int, dict]) -> tuple[int, int, bool]:
    """picked: id → {"title", "body"} (как поправил человек). Остальные предложения не трогаем.
    Возвращает (карточек, примеров, заменена ли инструкция)."""
    cards = examples = 0
    instr = False
    with db.tx():
        for s in db.q("SELECT * FROM ai_suggestions WHERE profile_id=%s AND id = ANY(%s)", (profile_id, list(picked))):
            title = (picked[s["id"]].get("title") or s["title"]).strip()
            body = (picked[s["id"]].get("body") or s["body"]).strip()
            if s["kind"] == "card" and title and body:
                db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, %s, %s)", (profile_id, title, body))
                cards += 1
            elif s["kind"] == "example" and title and body:
                db.ex("INSERT INTO ai_examples(profile_id, question, answer, source) VALUES (%s, %s, %s, 'dialogs')",
                      (profile_id, title, body))
                examples += 1
            elif s["kind"] == "instruction" and body:
                db.ex("UPDATE ai_profiles SET instruction=%s WHERE id=%s", (body, profile_id))
                instr = True
            db.ex("DELETE FROM ai_suggestions WHERE id=%s", (s["id"],))
    return cards, examples, instr


def reject(profile_id: int, ids: list[int] | None = None) -> int:
    if ids is None:
        return db.changed("DELETE FROM ai_suggestions WHERE profile_id=%s", (profile_id,))
    return db.changed("DELETE FROM ai_suggestions WHERE profile_id=%s AND id = ANY(%s)", (profile_id, ids))


# ---------- G5: копирование и проверка ----------
def copy_cards(src_profile: int, dst_profile: int, card_ids: list[int]) -> int:
    """Копии выбранных карточек в другой профиль; карточки с тем же заголовком там не дублируются."""
    have = {_norm(r["title"]) for r in db.q("SELECT title FROM ai_cards WHERE profile_id=%s", (dst_profile,))}
    n = 0
    with db.tx():
        for c in db.q("SELECT * FROM ai_cards WHERE profile_id=%s AND id = ANY(%s) ORDER BY id", (src_profile, card_ids)):
            if _norm(c["title"]) in have:
                continue
            db.ex("INSERT INTO ai_cards(profile_id, title, body) VALUES (%s, %s, %s)", (dst_profile, c["title"], c["body"]))
            have.add(_norm(c["title"]))
            n += 1
    return n


CHECK_PROMPT = (
    "Проверь базу знаний ИИ-менеджера. Найди проблемы: «дубль» — карточки об одном и том же; «противоречие» — "
    "разные факты (цены, сроки, условия) в разных карточках или в инструкции и карточке; «факт в инструкции» — "
    "цены, даты, ссылки, описания услуг в инструкции (их место в карточках); «размытый заголовок» — заголовок не "
    "похож на вопрос клиента; «нет важного» — чего явно не хватает (как записаться, цены, сроки), если об этом "
    "говорит инструкция. Длинные карточки не ищи — их проверяем сами. Пиши коротко и конкретно, со ссылкой на "
    'заголовки карточек. Ответь строго JSON: {"issues": [{"type": "дубль", "text": "..."}]}. Если всё хорошо — {"issues": []}.')


async def check(profile_id: int) -> list[dict]:
    prof = db.one("SELECT * FROM ai_profiles WHERE id=%s", (profile_id,))
    cards = db.q("SELECT id, title, body, profile_id FROM ai_cards WHERE profile_id=%s OR profile_id IS NULL "
                 "ORDER BY profile_id NULLS FIRST, id", (profile_id,))
    issues = [{"type": "длинная карточка", "text": f"«{c['title']}» — {len(c['body'])} символов: разбейте на несколько тем"}
              for c in cards if c["profile_id"] and len(c["body"]) > CARD_MAX]
    text = (f"Инструкция профиля:\n{prof['instruction'] or '(пусто)'}\n\nКарточки (общие помечены [общая]):\n" +
            "\n\n".join(f"### {'[общая] ' if c['profile_id'] is None else ''}{c['title']}\n{c['body'][:1500]}" for c in cards))
    d = parse_json(await ai.chat([{"role": "system", "content": CHECK_PROMPT}, {"role": "user", "content": text[:MAX_TEXT]}],
                                 temperature=0.1, max_tokens=2000))
    for i in d.get("issues") or []:
        if isinstance(i, dict) and str(i.get("text") or "").strip():
            issues.append({"type": str(i.get("type") or "замечание")[:40], "text": str(i["text"]).strip()[:600]})
    return issues
