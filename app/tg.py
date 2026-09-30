"""Личные аккаунты Telegram команды (Telethon, MTProto): по клиенту на аккаунт."""
import asyncio
from typing import Awaitable, Callable

from psycopg.errors import UniqueViolation
from telethon import TelegramClient, errors, events, utils
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import User

from . import db, leads
from .config import API_HASH, API_ID, SESSIONS_DIR

# Точка расширения: сюда подключится ИИ-разбор ответов.
# Каждый хук получает (account_client, event, lead_row | None).
IncomingHook = Callable[["AccountClient", events.NewMessage.Event, object], Awaitable[None]]
incoming_hooks: list[IncomingHook] = []

CODE_WHERE = {"App": "в приложение Telegram (чат «Telegram»)", "Sms": "по SMS", "Call": "звонком",
              "FlashCall": "flash-звонком", "MissedCall": "пропущенным звонком (последние цифры номера)",
              "FragmentSms": "через Fragment", "EmailCode": "на e-mail",
              "SetUpEmailRequired": "— Telegram требует привязать e-mail"}


def configured() -> bool:
    return bool(API_ID and API_HASH)


class DuplicateAccount(Exception):
    """Этот Telegram-аккаунт уже подключён в панели другой записью."""


class AccountClient:
    def __init__(self, account_id: int):
        self.id = account_id
        self.client: TelegramClient | None = None
        self.me: User | None = None
        self.lock = asyncio.Lock()        # одна операция Telegram за раз на аккаунт
        self.phone: str | None = None
        self.phone_code_hash: str | None = None
        self.qr = None
        self.qr_status = "none"
        self.qr_version = 0
        self._qr_task: asyncio.Task | None = None

    # ---------- подключение ----------
    @property
    def session_path(self):
        return SESSIONS_DIR / f"acc_{self.id}"   # Telethon добавит .session

    async def start(self):
        if not configured():
            return
        self.client = TelegramClient(
            str(self.session_path), API_ID, API_HASH,
            device_model="TG Sender Panel", system_version="1.0", app_version="2.0",
            flood_sleep_threshold=0,  # FloodWait обрабатываем сами
        )
        self._register_handlers()
        await self.client.connect()
        if await self.client.is_user_authorized():
            try:
                await self._on_login("Аккаунт подключён")
            except DuplicateAccount:
                pass

    async def stop(self):
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
        if self.client:
            await self.client.disconnect()

    @property
    def authorized(self) -> bool:
        return bool(self.client and self.client.is_connected() and self.me)

    def display(self) -> str:
        if not self.me:
            return "—"
        name = " ".join(filter(None, [self.me.first_name, self.me.last_name]))
        un = f"@{self.me.username}" if self.me.username else ""
        return f"{name} {un}".strip()

    async def _ensure_connected(self):
        if self.client is None:
            await self.start()
        if self.client is None:
            raise RuntimeError("Не заданы TG_API_ID / TG_API_HASH в .env")
        if not self.client.is_connected():
            await self.client.connect()

    async def _on_login(self, what: str):
        me = await self.client.get_me()
        other = db.one("SELECT id FROM tg_accounts WHERE tg_user_id=%s AND id!=%s", (me.id, self.id))
        if other:
            db.log(f"Аккаунт {me.first_name} (+{me.phone}) уже подключён как #{other['id']} — вход отменён", "error", self.id)
            await self.client.log_out()
            self.me = None
            raise DuplicateAccount(f"Этот Telegram-аккаунт уже подключён в панели (#{other['id']})")
        self.me = me
        db.ex("""UPDATE tg_accounts SET tg_user_id=%s, username=%s, first_name=%s, last_name=%s, phone=%s,
                 status=CASE WHEN status IN ('new','logged_out') THEN 'active' ELSE status END WHERE id=%s""",
              (me.id, me.username, me.first_name, me.last_name, f"+{me.phone}" if me.phone else None, self.id))
        db.log(f"{what}: {self.display()}", account_id=self.id)

    # ---------- вход по коду ----------
    async def send_code(self, phone: str) -> str:
        await self._ensure_connected()
        # Telethon кэширует hash прошлого запроса и тогда шлёт ResendCode, который
        # быстро упирается в SEND_CODE_UNAVAILABLE. Отменяем старый и просим новый код.
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
        where = CODE_WHERE.get(kind, kind)
        db.log(f"Код входа отправлен {where}", account_id=self.id)
        return where

    async def sign_in_code(self, code: str) -> str:
        """Возвращает 'ok' или 'password' (нужен пароль 2FA)."""
        await self._ensure_connected()
        try:
            await self.client.sign_in(phone=self.phone, code=code.strip(), phone_code_hash=self.phone_code_hash)
        except errors.SessionPasswordNeededError:
            return "password"
        await self._on_login("Вход выполнен")
        return "ok"

    async def sign_in_password(self, password: str):
        await self._ensure_connected()
        await self.client.sign_in(password=password)
        await self._on_login("Вход выполнен (2FA)")

    # ---------- вход по QR-коду ----------
    # Токен QR живёт ~30 сек, поэтому пересоздаём его, пока пользователь не отсканирует.
    async def qr_start(self) -> str:
        await self._ensure_connected()
        if self._qr_task and not self._qr_task.done():
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
            await self._on_login("Вход по QR выполнен")
            self.qr_status = "ok"
        except errors.SessionPasswordNeededError:
            self.qr_status = "password"
        except asyncio.CancelledError:
            pass
        except DuplicateAccount as e:
            self.qr_status = f"error: {e}"
        except Exception as e:
            self.qr_status = f"error: {e}"
            db.log(f"Ошибка входа по QR: {type(e).__name__}: {e}", "error", self.id)

    async def logout(self):
        if self.client and self.me:
            await self.client.log_out()
        await self.stop()
        self.me = None
        self.client = None
        db.ex("UPDATE tg_accounts SET status='logged_out' WHERE id=%s", (self.id,))
        db.log("Выход из аккаунта", "warn", self.id)

    # ---------- импорт лидов ----------
    async def import_contacts(self, tag: str = "", segment_id: int | None = None) -> int:
        async with self.lock:
            res = await self.client(GetContactsRequest(hash=0))
        return leads.import_tg_users([u for u in res.users if not u.bot and not u.deleted], tag, self.id, segment_id)

    async def import_dialogs(self, tag: str = "", limit: int = 500, segment_id: int | None = None) -> int:
        users = []
        async with self.lock:
            async for d in self.client.iter_dialogs(limit=limit):
                e = d.entity
                if isinstance(e, User) and not e.bot and not e.deleted and not e.is_self and e.id != 777000:
                    users.append(e)
        return leads.import_tg_users(users, tag, self.id, segment_id)

    # ---------- поиск адресата ----------
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

    def _set_lead_tg_id(self, lead_id: int, tg_id: int) -> None:
        try:
            with db.tx():
                db.ex("UPDATE leads SET tg_id=%s WHERE id=%s", (tg_id, lead_id))
        except UniqueViolation:
            pass     # этот ID уже у другого лида — оставляем как есть

    async def resolve(self, lead: dict):
        """Находит адресата. Вызывать под self.lock."""
        if lead["tg_id"]:
            ids = self._alt_ids(lead["tg_id"]) if lead["kind"] == "chat" else [lead["tg_id"]]
            for pid in ids:
                try:
                    ent = await self.client.get_input_entity(pid)
                    if pid != lead["tg_id"]:
                        self._set_lead_tg_id(lead["id"], pid)
                    return ent
                except (ValueError, TypeError):
                    pass
        if lead["username"]:
            return await self.client.get_entity(lead["username"])
        if lead["kind"] != "chat" and lead["phone"]:
            return await self.client.get_entity(lead["phone"])
        if lead["kind"] == "chat" and lead["title"]:
            row = db.one("""SELECT peer_id FROM tg_dialogs WHERE account_id=%s AND kind!='user'
                            AND lower(trim(title))=lower(trim(%s)) LIMIT 1""", (self.id, lead["title"]))
            if row:
                ent = await self.client.get_input_entity(row["peer_id"])
                self._set_lead_tg_id(lead["id"], row["peer_id"])
                db.log(f"Чат «{lead['title']}» найден по названию (ID {row['peer_id']} вместо {lead['tg_id']})",
                       "warn", self.id)
                return ent
        if lead["kind"] == "chat":
            raise ValueError("чат не найден среди диалогов аккаунта: проверьте ссылку и что аккаунт состоит в чате "
                             "(для обновления списка диалогов нажмите «Найти получателей»)")
        if lead["tg_id"]:
            raise ValueError("ID нет в кэше сессии. Нажмите «Найти получателей» на странице кампании; "
                             "если не поможет — у аккаунта нет общего диалога/чата с этим человеком")
        raise ValueError("нет ссылки на чат/человека")

    # ---------- найти получателей ----------
    async def prepare(self, campaign_id: int, st: dict):
        """Прогревает кэш сессии: все диалоги + участники чатов кампании, чтобы найти людей по ID.
        Держит self.lock: повышенный flood_sleep_threshold не должен действовать на отправку."""
        async with self.lock:
            old_threshold = self.client.flood_sleep_threshold
            # Загрузка всех диалогов — много запросов подряд, Telegram отвечает FloodWait на десятки секунд.
            # На время поиска разрешаем Telethon самому выжидать такие паузы (до 5 мин).
            self.client.flood_sleep_threshold = 300
            try:
                await self._prepare(campaign_id, st)
            finally:
                self.client.flood_sleep_threshold = old_threshold

    def _save_dialog(self, d) -> bool:
        """Запоминает диалог в tg_dialogs. Возвращает True, если это личная переписка."""
        kind = "user" if d.is_user else ("channel" if d.is_channel and not d.is_group else "group")
        has_private = bool(d.is_user and d.message is not None)
        e = d.entity
        # своя группа: аккаунт её создал или в ней админ
        is_admin = kind == "group"
        db.ex("""INSERT INTO tg_dialogs(account_id, peer_id, title, kind, has_private, is_admin, members, updated_at)
                 VALUES (%s,%s,%s,%s,%s,%s,%s, now()) ON CONFLICT (account_id, peer_id) DO UPDATE
                 SET title=excluded.title, kind=excluded.kind, has_private=excluded.has_private,
                     is_admin=excluded.is_admin, members=excluded.members, updated_at=now()""",
              (self.id, d.id, d.name or "", kind, has_private, is_admin, getattr(e, "participants_count", None)))
        return has_private

    # ---------- свои группы ----------
    async def refresh_groups(self, st: dict):
        """Обновляет список диалогов, чтобы найти группы, где аккаунт создатель или админ."""
        async with self.lock:
            old_threshold = self.client.flood_sleep_threshold
            self.client.flood_sleep_threshold = 300
            try:
                n = 0
                async for d in self.client.iter_dialogs(limit=None):
                    n += 1
                    self._save_dialog(d)
                    if n % 100 == 0:
                        st["step"] = f"диалоги: {n} (Telegram может делать паузы, это нормально)"
                st["groups"] = db.val("SELECT COUNT(*) FROM tg_dialogs WHERE account_id=%s", (self.id,))
                st["step"] = "готово"
            finally:
                self.client.flood_sleep_threshold = old_threshold

    async def import_group_members(self, peer_id: int, segment_id: int, tag: str = "") -> tuple[int, int, int]:
        """Участники своей группы → лиды сегмента, закреплённые за этим аккаунтом.
        Возвращает (всего участников, добавлено в сегмент, новых лидов в базе)."""
        group = db.one("SELECT * FROM tg_dialogs WHERE account_id=%s AND peer_id=%s", (self.id, peer_id))
        if not group:
            raise ValueError("Можно брать участников только из групп")
        people = []
        async with self.lock:
            old_threshold = self.client.flood_sleep_threshold
            self.client.flood_sleep_threshold = 300
            try:
                async for u in self.client.iter_participants(peer_id):
                    if u.bot or u.deleted or u.is_self:
                        continue
                    joined = getattr(getattr(u, "participant", None), "date", None)
                    people.append((u, joined))
            finally:
                self.client.flood_sleep_threshold = old_threshold
        added = new = 0
        with db.tx():
            for u, joined in people:
                extra = {"group": group["title"]}
                if joined:
                    extra["joined"] = joined.astimezone(db.now_local().tzinfo).strftime("%d.%m.%Y")
                lid, created = leads.upsert_lead({"tg_id": u.id, "username": u.username, "phone": leads.norm_phone(u.phone),
                                                  "first_name": u.first_name or "", "last_name": u.last_name or "",
                                                  "source": "group"}, leads.split_tags(tag), extra, self.id)
                added += leads.add_to_segment(segment_id, lid, "group")
                new += created
        db.log(f"Из группы «{group['title']}»: участников {len(people)}, в сегмент #{segment_id} добавлено {added}",
               account_id=self.id)
        return len(people), added, new

    async def _prepare(self, campaign_id: int, st: dict):
        who = self.display()
        n = 0
        private = set()
        async for d in self.client.iter_dialogs(limit=None):
            n += 1
            if self._save_dialog(d):
                private.add(d.entity.id)
            if n % 100 == 0:
                st["step"] = f"{who}: диалоги {n} (Telegram может делать паузы, это нормально)"
        st["step"] = f"{who}: диалоги {n}"

        people = db.q("""SELECT cl.id, cl.account_id, l.tg_id FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id
                         WHERE cl.campaign_id=%s AND l.kind!='chat' AND l.tg_id > 0
                           AND (cl.account_id=%s OR cl.account_id IS NULL)""", (campaign_id, self.id))
        for p in people:
            if p["account_id"] == self.id or p["tg_id"] in private:
                # у закреплённых — по этому аккаунту; у свободных «Диалог есть», если он есть хоть у одного
                db.ex("UPDATE campaign_leads SET real_dialog=%s WHERE id=%s",
                      ("Диалог есть" if p["tg_id"] in private else "Диалога нет", p["id"]))
            else:
                db.ex("UPDATE campaign_leads SET real_dialog=COALESCE(real_dialog, 'Диалога нет') WHERE id=%s", (p["id"],))

        def unresolved():
            out = []
            for p in people:
                try:
                    self.client.session.get_input_entity(p["tg_id"])
                except (ValueError, TypeError):
                    out.append(p)
            return out

        missing = unresolved()
        chats = db.q("""SELECT DISTINCT l.tg_id, l.title FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id
                        WHERE cl.campaign_id=%s AND l.kind='chat' AND l.tg_id IS NOT NULL""", (campaign_id,))
        for ch in chats:
            if not missing:
                break
            st["step"] = f"{who}: участники чата «{ch['title']}»"
            try:
                async for _ in self.client.iter_participants(ch["tg_id"], limit=10000):
                    pass
                await asyncio.sleep(3)
            except (errors.RPCError, ValueError, TypeError) as e:
                db.log(f"Участники «{ch['title']}» недоступны: {type(e).__name__}", "warn", self.id)
            await asyncio.sleep(1)
            missing = unresolved()
        st["found"] += len(people) - len(missing)
        st["total"] += len(people)

    # ---------- прочтения ----------
    async def refresh_reads(self) -> int:
        """Досинхронизация прочтений (если панель была выключена, когда их читали)."""
        from telethon.tl.functions.messages import GetPeerDialogsRequest
        from telethon.tl.types import InputDialogPeer

        ids = [r["tg_id"] for r in db.q("""SELECT DISTINCT l.tg_id FROM campaign_leads cl JOIN leads l ON l.id=cl.lead_id
                                           WHERE cl.account_id=%s AND cl.state='sent' AND l.tg_id > 0""", (self.id,))]
        updated = 0
        for i in range(0, len(ids), 50):
            async with self.lock:
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
                    updated += self._mark_read(pid, d.read_outbox_max_id)
            await asyncio.sleep(1)
        return updated

    def _mark_read(self, peer_id: int, max_id: int) -> int:
        return db.changed("""UPDATE campaign_leads cl SET state='read', read_at=now() FROM leads l
                             WHERE l.id=cl.lead_id AND cl.account_id=%s AND l.tg_id=%s AND cl.state='sent'
                               AND cl.tg_message_id <= %s""", (self.id, peer_id, max_id))

    # ---------- входящие события ----------
    def _register_handlers(self):
        c = self.client

        @c.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_incoming(event):
            await self.handle_incoming(event.sender_id, event.raw_text, event.id, event)

        @c.on(events.MessageRead(inbox=False))
        async def on_read(event):
            # собеседник прочитал наши сообщения до max_id включительно
            self._mark_read(event.chat_id, event.max_id)

    async def handle_incoming(self, uid: int, text: str | None, tg_message_id: int | None, event=None):
        lead = db.one("SELECT * FROM leads WHERE tg_id=%s", (uid,))
        if lead:
            db.ex("""INSERT INTO messages(account_id, lead_id, direction, tg_message_id, text)
                     VALUES (%s, %s, 'in', %s, %s)""", (self.id, lead["id"], tg_message_id, text))
            db.ex("""UPDATE campaign_leads SET state='replied', replied_at=now()
                     WHERE lead_id=%s AND account_id=%s AND state IN ('sent', 'read')""", (lead["id"], self.id))
        if leads.is_stop_message(text, db.get_setting("stop_words")):
            if not lead:
                lid, _ = leads.upsert_lead({"tg_id": uid, "source": "stop"})
                lead = db.one("SELECT * FROM leads WHERE id=%s", (lid,))
            leads.opt_out(lead["id"], f"ответил «{(text or '')[:50]}»")
            db.log(f"Лид #{lead['id']} отписался: «{(text or '')[:50]}»", "warn", self.id)
        for hook in incoming_hooks:
            try:
                await hook(self, event, lead)
            except Exception as ex:  # хук не должен ронять приём
                db.log(f"Ошибка хука входящих: {ex}", "error", self.id)


class TgPool:
    """Все аккаунты команды. Клиенты создаются при старте панели и при подключении нового аккаунта."""

    def __init__(self):
        self.accounts: dict[int, AccountClient] = {}
        self.prepare_state: dict[int, dict] = {}   # campaign_id → ход «Найти получателей»
        self.groups_state: dict[int, dict] = {}    # account_id → ход «Обновить список своих групп»

    def get(self, account_id: int) -> AccountClient:
        if account_id not in self.accounts:
            self.accounts[account_id] = AccountClient(account_id)
        return self.accounts[account_id]

    async def start_all(self):
        if not configured():
            db.log("TG_API_ID / TG_API_HASH не заданы в .env — подключение аккаунтов невозможно", "error")
            return
        for a in db.q("SELECT id FROM tg_accounts WHERE status IN ('active', 'paused') ORDER BY id"):
            acc = self.get(a["id"])
            try:
                await acc.start()
            except Exception as e:
                db.log(f"Не удалось подключить аккаунт: {type(e).__name__}: {e}", "error", a["id"])

    async def stop_all(self):
        for acc in self.accounts.values():
            try:
                await acc.stop()
            except Exception:
                pass

    async def refresh_groups(self, account_id: int):
        st = self.groups_state[account_id] = {"running": True, "step": "диалоги", "groups": 0}
        try:
            await self.get(account_id).refresh_groups(st)
        except Exception as e:
            st["step"] = (f"Telegram попросил подождать {e.seconds} сек — повторите позже"
                          if isinstance(e, errors.FloodWaitError) else f"ошибка: {e}")
            db.log(f"Список своих групп: {type(e).__name__}: {e}", "error", account_id)
        finally:
            st["running"] = False

    @property
    def preparing(self) -> bool:
        return any(st.get("running") for st in self.prepare_state.values())

    def preparing_account(self, account_id: int) -> bool:
        return (any(st.get("running") and account_id in st.get("accounts", ()) for st in self.prepare_state.values())
                or bool(self.groups_state.get(account_id, {}).get("running")))

    async def prepare_campaign(self, campaign_id: int):
        from .campaigns import campaign_accounts
        accs = [self.get(a["id"]) for a in campaign_accounts(campaign_id)]
        accs = [a for a in accs if a.authorized]
        st = self.prepare_state[campaign_id] = {"running": True, "step": "запуск", "found": 0, "total": 0,
                                                "accounts": [a.id for a in accs]}
        try:
            if not accs:
                st["step"] = "нет подключённых аккаунтов"
                return
            # сверка «есть ли диалог» у ещё не закреплённых лидов считается заново по всем аккаунтам
            db.ex("UPDATE campaign_leads SET real_dialog=NULL WHERE campaign_id=%s AND account_id IS NULL", (campaign_id,))
            for acc in accs:
                await acc.prepare(campaign_id, st)
            st["step"] = "готово"
            db.log(f"Поиск получателей кампании #{campaign_id}: найдено {st['found']} из {st['total']}")
        except Exception as e:
            if isinstance(e, errors.FloodWaitError):
                st["step"] = f"Telegram попросил подождать {e.seconds} сек — запустите поиск ещё раз чуть позже"
            else:
                st["step"] = f"ошибка: {e}"
            db.log(f"Поиск получателей: {type(e).__name__}: {e}", "error")
        finally:
            st["running"] = False


tgm = TgPool()
