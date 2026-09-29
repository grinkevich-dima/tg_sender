"""Работа с личным аккаунтом Telegram (Telethon, MTProto)."""
import asyncio
from typing import Awaitable, Callable

from telethon import TelegramClient, events, errors
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import User

from . import db
from .config import API_HASH, API_ID, SESSION_PATH

# Точка расширения: сюда позже подключится ИИ-автоответчик (Claude).
# Каждый хук получает (event, contact_row | None).
IncomingHook = Callable[[events.NewMessage.Event, object], Awaitable[None]]
incoming_hooks: list[IncomingHook] = []


class TgManager:
    def __init__(self):
        self.client: TelegramClient | None = None
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.me: User | None = None
        self.lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(API_ID and API_HASH)

    async def start(self):
        if not self.configured:
            db.log("TG_API_ID / TG_API_HASH не заданы в .env — авторизация невозможна", "error")
            return
        self.client = TelegramClient(
            str(SESSION_PATH), API_ID, API_HASH,
            device_model="TG Sender Panel", system_version="1.0", app_version="1.0",
            flood_sleep_threshold=0,  # FloodWait обрабатываем сами
        )
        self._register_handlers()
        await self.client.connect()
        if await self.client.is_user_authorized():
            self.me = await self.client.get_me()
            db.log(f"Аккаунт подключён: {self.display_me()}")

    async def stop(self):
        if self.client:
            await self.client.disconnect()

    async def authorized(self) -> bool:
        return bool(self.client and self.client.is_connected() and self.me)

    def display_me(self) -> str:
        if not self.me:
            return "—"
        name = " ".join(filter(None, [self.me.first_name, self.me.last_name]))
        un = f"@{self.me.username}" if self.me.username else ""
        return f"{name} {un} (+{self.me.phone})".strip()

    # ---------- авторизация ----------
    async def send_code(self, phone: str):
        if not self.client.is_connected():
            await self.client.connect()
        # Telethon кэширует hash прошлого запроса и тогда шлёт ResendCode, который
        # быстро упирается в SEND_CODE_UNAVAILABLE. Отменяем старый и просим новый код.
        from telethon import utils
        from telethon.tl.functions.auth import CancelCodeRequest
        key = utils.parse_phone(phone)
        old_hash = self.client._phone_code_hash.pop(key, None)
        if old_hash:
            try:
                await self.client(CancelCodeRequest(key, old_hash))
            except errors.RPCError:
                pass
        res = await self.client.send_code_request(phone)
        self.phone = phone
        self.phone_code_hash = res.phone_code_hash
        kind = type(res.type).__name__.replace("SentCodeType", "")
        where = {"App": "в приложение Telegram (чат «Telegram»)", "Sms": "по SMS", "Call": "звонком",
                 "FlashCall": "flash-звонком", "MissedCall": "пропущенным звонком (последние цифры номера)",
                 "FragmentSms": "через Fragment", "EmailCode": "на e-mail", "SetUpEmailRequired": "— Telegram требует привязать e-mail"
                 }.get(kind, kind)
        db.log(f"Код входа отправлен {where}")
        return where

    # ---------- вход по QR-коду ----------
    # Токен QR живёт ~30 сек, поэтому пересоздаём его, пока пользователь не отсканирует.
    async def qr_start(self) -> str:
        if not self.client.is_connected():
            await self.client.connect()
        if getattr(self, "_qr_task", None) and not self._qr_task.done():
            self._qr_task.cancel()
        self.qr = await self.client.qr_login()
        self.qr_status = "waiting"
        self.qr_version = 1
        self._qr_task = asyncio.create_task(self._qr_wait())
        return self.qr.url

    async def _qr_wait(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 180
        try:
            while True:
                try:
                    await self.qr.wait()          # ждём до истечения текущего токена
                    break
                except asyncio.TimeoutError:
                    if loop.time() > deadline:
                        self.qr_status = "expired"
                        return
                    await self.qr.recreate()
                    self.qr_version += 1
            self.me = await self.client.get_me()
            self.qr_status = "ok"
            db.log(f"Вход по QR выполнен: {self.display_me()}")
        except errors.SessionPasswordNeededError:
            self.qr_status = "password"
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.qr_status = f"error: {e}"
            db.log(f"Ошибка входа по QR: {type(e).__name__}: {e}", "error")

    async def sign_in_code(self, code: str) -> str:
        """Возвращает 'ok' или 'password' (нужен пароль 2FA)."""
        try:
            await self.client.sign_in(phone=self.phone, code=code.strip(), phone_code_hash=self.phone_code_hash)
        except errors.SessionPasswordNeededError:
            return "password"
        self.me = await self.client.get_me()
        db.log(f"Вход выполнен: {self.display_me()}")
        return "ok"

    async def sign_in_password(self, password: str):
        await self.client.sign_in(password=password)
        self.me = await self.client.get_me()
        db.log(f"Вход выполнен (2FA): {self.display_me()}")

    async def logout(self):
        if self.client:
            await self.client.log_out()
        self.me = None
        db.log("Выход из аккаунта", "warn")
        # после log_out клиент нужно пересоздать
        await self.start()

    # ---------- контакты ----------
    async def import_contacts(self, tag: str = "") -> int:
        res = await self.client(GetContactsRequest(hash=0))
        return self._save_users([u for u in res.users if not u.bot and not u.deleted], tag)

    async def import_dialogs(self, tag: str = "", limit: int = 500) -> int:
        users = []
        async for d in self.client.iter_dialogs(limit=limit):
            e = d.entity
            if isinstance(e, User) and not e.bot and not e.deleted and not e.is_self and e.id != 777000:
                users.append(e)
        return self._save_users(users, tag)

    def _save_users(self, users: list[User], tag: str) -> int:
        n = 0
        for u in users:
            existing = db.one("SELECT id, tags FROM contacts WHERE tg_user_id=?", (u.id,))
            if existing:
                tags = _merge_tags(existing["tags"], tag)
                db.ex("UPDATE contacts SET username=?, phone=COALESCE(?, phone), first_name=?, last_name=?, tags=? WHERE id=?",
                      (u.username, u.phone, u.first_name or "", u.last_name or "", tags, existing["id"]))
            else:
                db.ex("INSERT INTO contacts(tg_user_id, username, phone, first_name, last_name, tags, created_at) VALUES (?,?,?,?,?,?,?)",
                      (u.id, u.username, u.phone, u.first_name or "", u.last_name or "", tag.strip(), db.now_utc()))
                n += 1
        return n

    async def resolve(self, contact):
        """Находит получателя: сначала по id из кэша сессии, потом по username/телефону."""
        if contact["tg_user_id"]:
            try:
                return await self.client.get_input_entity(contact["tg_user_id"])
            except (ValueError, TypeError):
                pass
        key = contact["username"] or contact["phone"]
        if not key:
            raise ValueError("нет username/телефона/ID")
        entity = await self.client.get_entity(key)
        db.ex("UPDATE contacts SET tg_user_id=? WHERE id=?", (entity.id, contact["id"]))
        return entity

    # ---------- получатели из списков xlsx ----------
    @staticmethod
    def _alt_ids(pid: int) -> list[int]:
        """Варианты записи ID группы: web.telegram.org/a даёт -100…, /k и старые группы — просто -…"""
        out = [pid]
        s = str(pid)
        if s.startswith("-100") and len(s) > 5:
            out.append(-int(s[4:]))                 # вдруг это обычная группа, а «-100» дописали
        elif pid < 0:
            out.append(int("-100" + s[1:]))         # супергруппа, записанная без «-100»
        return out

    async def resolve_item(self, item):
        if item["peer_id"]:
            for pid in self._alt_ids(item["peer_id"]):
                try:
                    ent = await self.client.get_input_entity(pid)
                    if pid != item["peer_id"]:
                        db.ex("UPDATE list_items SET peer_id=? WHERE id=?", (pid, item["id"]))
                    return ent
                except (ValueError, TypeError):
                    pass
        if item["username"]:
            return await self.client.get_entity(item["username"])
        if item["kind"] == "chat" and item["title"]:
            want = item["title"].strip().casefold()   # SQLite lower() не умеет кириллицу — сравниваем в Python
            row = next((r for r in db.q("SELECT peer_id, title FROM tg_dialogs WHERE kind!='user'")
                        if (r["title"] or "").strip().casefold() == want), None)
            if row:
                ent = await self.client.get_input_entity(row["peer_id"])
                db.ex("UPDATE list_items SET peer_id=? WHERE id=?", (row["peer_id"], item["id"]))
                db.log(f"Чат «{item['title']}» найден по названию (ID {row['peer_id']} вместо {item['peer_id']})", "warn")
                return ent
        if item["kind"] == "chat":
            raise ValueError("чат не найден среди ваших диалогов: проверьте ссылку и что вы состоите в чате "
                             "(для обновления списка диалогов нажмите «Найти получателей»)")
        if item["peer_id"]:
            raise ValueError("ID нет в кэше сессии. Нажмите «Найти получателей» на странице списка; "
                             "если не поможет — у вас нет общего диалога/чата с этим человеком")
        raise ValueError("в строке нет ссылки на чат/человека")

    prepare_state: dict = {}

    async def prepare_list(self, list_id: int):
        """Прогревает кэш сессии: все диалоги + участники чатов из списка, чтобы найти людей по ID."""
        st = self.prepare_state[list_id] = {"running": True, "step": "диалоги", "found": 0, "total": 0}
        # Загрузка всех диалогов — много запросов подряд, Telegram отвечает FloodWait на десятки секунд.
        # На время поиска разрешаем Telethon самому выжидать такие паузы (до 5 мин), потом возвращаем 0.
        old_threshold = self.client.flood_sleep_threshold
        self.client.flood_sleep_threshold = 300
        try:
            n = 0
            private = set()   # собеседники, с которыми есть личная переписка
            async for d in self.client.iter_dialogs(limit=None):
                n += 1
                kind = "user" if d.is_user else ("channel" if d.is_channel and not d.is_group else "group")
                db.ex("INSERT OR REPLACE INTO tg_dialogs(peer_id, title, kind, updated_at) VALUES (?,?,?,?)",
                      (d.id, d.name or "", kind, db.now_utc()))
                if d.is_user and d.message is not None:
                    private.add(d.entity.id)
                if n % 100 == 0:
                    st["step"] = f"диалоги: {n} (Telegram может делать паузы, это нормально)"
            st["step"] = f"диалоги: {n}"
            checked = mismatch = 0
            for it in db.q("SELECT id, peer_id, dialog FROM list_items WHERE list_id=? AND kind!='chat' AND peer_id > 0",
                           (list_id,)):
                real = "Диалог есть" if it["peer_id"] in private else "Диалога нет"
                db.ex("UPDATE list_items SET real_dialog=? WHERE id=?", (real, it["id"]))
                checked += 1
                if (it["dialog"] or "") in ("Диалог есть", "Диалога нет") and it["dialog"] != real:
                    mismatch += 1
            st["checked"], st["mismatch"] = checked, mismatch
            db.log(f"Сверка диалогов списка #{list_id}: проверено {checked}, расхождений с файлом {mismatch}")
            people = db.q("SELECT id, peer_id FROM list_items WHERE list_id=? AND kind!='chat' AND peer_id IS NOT NULL",
                          (list_id,))
            st["total"] = len(people)

            def unresolved():
                out = []
                for p in people:
                    try:
                        self.client.session.get_input_entity(p["peer_id"])
                    except (ValueError, TypeError):
                        out.append(p)
                return out

            missing = unresolved()
            chats = db.q("SELECT DISTINCT peer_id, title FROM list_items WHERE list_id=? AND kind='chat' AND peer_id IS NOT NULL",
                         (list_id,))
            for ch in chats:
                if not missing:
                    break
                st["step"] = f"участники чата «{ch['title']}»"
                try:
                    async for _ in self.client.iter_participants(ch["peer_id"], limit=10000):
                        pass
                    await asyncio.sleep(3)
                except (errors.RPCError, ValueError, TypeError) as e:
                    db.log(f"Участники «{ch['title']}» недоступны: {type(e).__name__}", "warn")
                await asyncio.sleep(1)
                missing = unresolved()
            st["found"] = st["total"] - len(missing)
            st["step"] = "готово"
            db.log(f"Поиск получателей: найдено {st['found']} из {st['total']} людей списка #{list_id}")
        except Exception as e:
            if isinstance(e, errors.FloodWaitError):
                st["step"] = f"Telegram попросил подождать {e.seconds} сек — запустите поиск ещё раз чуть позже"
            else:
                st["step"] = f"ошибка: {e}"
            db.log(f"Поиск получателей: {type(e).__name__}: {e}", "error")
        finally:
            self.client.flood_sleep_threshold = old_threshold
            st["running"] = False

    async def refresh_reads(self) -> int:
        """Досинхронизация прочтений (если панель была выключена, когда их читали)."""
        from telethon.tl.functions.messages import GetPeerDialogsRequest
        from telethon.tl.types import InputDialogPeer

        rows = db.q("""SELECT DISTINCT peer_id FROM messages WHERE status='sent' AND peer_id IS NOT NULL
                       UNION SELECT DISTINCT peer_id FROM list_items WHERE state='sent' AND peer_id > 0""")
        updated = 0
        ids = [r["peer_id"] for r in rows]
        for i in range(0, len(ids), 50):
            peers = []
            for pid in ids[i:i + 50]:
                try:
                    peers.append(InputDialogPeer(await self.client.get_input_entity(pid)))
                except (ValueError, TypeError):
                    continue
            if not peers:
                continue
            res = await self.client(GetPeerDialogsRequest(peers=peers))
            for d in res.dialogs:
                pid = getattr(d.peer, "user_id", None)
                if pid:
                    before = db.one("SELECT total_changes() AS n")["n"]
                    db.ex("""UPDATE messages SET status='read', read_at=?
                             WHERE peer_id=? AND status='sent' AND tg_message_id<=?""",
                          (db.now_utc(), pid, d.read_outbox_max_id))
                    db.ex("""UPDATE list_items SET state='read', read_at=?
                             WHERE peer_id=? AND state='sent' AND tg_message_id<=?""",
                          (db.now_utc(), pid, d.read_outbox_max_id))
                    updated += db.one("SELECT total_changes() AS n")["n"] - before
            await asyncio.sleep(1)
        return updated

    # ---------- входящие события ----------
    def _register_handlers(self):
        c = self.client

        @c.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_incoming(event):
            uid = event.sender_id
            db.ex("""UPDATE list_items SET state='replied', replied_at=?
                     WHERE peer_id=? AND state IN ('sent','read')""", (db.now_utc(), uid))
            low = (event.raw_text or "").lower().strip()
            stops = [w.strip().lower() for w in db.get_setting("stop_words").split(",") if w.strip()]
            if low and any(low == w or low.startswith(w) for w in stops):
                db.ex("UPDATE list_items SET state='skipped', error='отписался' WHERE peer_id=? AND state='queued'", (uid,))
                if not db.one("SELECT 1 FROM contacts WHERE tg_user_id=?", (uid,)):
                    db.ex("INSERT INTO contacts(tg_user_id, opted_out, created_at) VALUES (?,1,?)", (uid, db.now_utc()))
            contact = db.one("SELECT * FROM contacts WHERE tg_user_id=?", (uid,))
            if contact:
                db.ex("""UPDATE messages SET status='replied', replied_at=?
                         WHERE contact_id=? AND status IN ('sent','read')""", (db.now_utc(), contact["id"]))
                text = (event.raw_text or "").lower().strip()
                stop_words = [w.strip().lower() for w in db.get_setting("stop_words").split(",") if w.strip()]
                if text and any(text == w or text.startswith(w) for w in stop_words):
                    db.ex("UPDATE contacts SET opted_out=1 WHERE id=?", (contact["id"],))
                    db.ex("UPDATE messages SET status='skipped', error='отписался' WHERE contact_id=? AND status='queued'",
                          (contact["id"],))
                    db.log(f"Контакт #{contact['id']} отписался: «{event.raw_text[:50]}»", "warn")
            for hook in incoming_hooks:
                try:
                    await hook(event, contact)
                except Exception as ex:  # хук не должен ронять приём
                    db.log(f"Ошибка хука входящих: {ex}", "error")

        @c.on(events.MessageRead(inbox=False))
        async def on_read(event):
            # собеседник прочитал наши сообщения до max_id включительно
            db.ex("""UPDATE messages SET status='read', read_at=?
                     WHERE peer_id=? AND status='sent' AND tg_message_id<=?""",
                  (db.now_utc(), event.chat_id, event.max_id))
            db.ex("""UPDATE list_items SET state='read', read_at=?
                     WHERE peer_id=? AND state='sent' AND tg_message_id<=?""",
                  (db.now_utc(), event.chat_id, event.max_id))


def _merge_tags(old: str, new: str) -> str:
    tags = [t.strip() for t in (old or "").split(",") if t.strip()]
    for t in (new or "").split(","):
        t = t.strip()
        if t and t not in tags:
            tags.append(t)
    return ",".join(tags)


tg = TgManager()
