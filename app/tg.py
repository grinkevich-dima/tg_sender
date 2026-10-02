"""Личные аккаунты Telegram команды (Telethon, MTProto): по клиенту на аккаунт."""
import asyncio
from typing import Awaitable, Callable

from psycopg.errors import UniqueViolation
from telethon import TelegramClient, errors, events
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import User

from . import ai, autopilot, db, inbox, leads
from .config import API_HASH, API_ID, SESSIONS_DIR
from .tasks import spawn
from .tg_groups import GroupsMixin
from .tg_login import DuplicateAccount, LoginMixin  # noqa: F401  (DuplicateAccount — часть API модуля)

# Точка расширения: сюда подключится ИИ-разбор ответов.
# Каждый хук получает (account_client, event, lead_row | None).
IncomingHook = Callable[["AccountClient", events.NewMessage.Event, object], Awaitable[None]]
incoming_hooks: list[IncomingHook] = []

def configured() -> bool:
    return bool(API_ID and API_HASH)


class AccountClient(LoginMixin, GroupsMixin):
    """Один аккаунт Telegram: подключение, поиск адресата, события, прочтения, сверка переписки.
    Вход — tg_login.LoginMixin; диалоги и группы — tg_groups.GroupsMixin."""

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
            catch_up=True,            # после переподключения догрузить события, пропущенные пока панель не работала
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





    # ---------- вход по QR-коду ----------
    # Токен QR живёт ~30 сек, поэтому пересоздаём его, пока пользователь не отсканирует.



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






    # ---------- догрузка пропущенной переписки ----------
    async def sync_recent(self, limit: int = 100) -> int:
        """Сверка последних диалогов аккаунта с перепиской в панели: всё, что пришло или ушло, пока панель не работала
        (или событие потерялось), дописывается в историю и проходит обычную обработку (ответ, отписка, автопилот).
        Возвращает, сколько сообщений дописано."""
        found: list[tuple[int, object]] = []
        async with self.lock:
            async for d in self.client.iter_dialogs(limit=limit):
                if not d.is_user or d.message is None:
                    continue
                lead = db.one("SELECT id FROM leads WHERE tg_id=%s AND (owner_account_id=%s OR owner_account_id IS NULL)",
                              (d.id, self.id))
                if not lead:
                    continue
                known = db.val("""SELECT COALESCE(MAX(tg_message_id), 0) FROM messages
                                  WHERE account_id=%s AND lead_id=%s""", (self.id, lead["id"])) or 0
                if not known or d.message.id <= known:
                    continue        # переписки в панели ещё нет (чужой личный диалог) или всё уже есть
                msgs = await self.client.get_messages(d.entity, min_id=known, limit=30)
                found += [(d.id, m) for m in reversed(msgs)]
        added = 0
        for peer_id, m in found:
            text = getattr(m, "message", None) or ""
            if getattr(m, "out", False):
                added += bool(self.handle_outgoing(peer_id, text, m.id))
            else:
                before = db.val("SELECT COUNT(*) FROM messages WHERE account_id=%s AND direction='in'", (self.id,))
                await self.handle_incoming(peer_id, text, m.id)
                added += (db.val("SELECT COUNT(*) FROM messages WHERE account_id=%s AND direction='in'", (self.id,)) or 0) - before
        if added:
            db.log(f"Догружено пропущенных сообщений: {added}", account_id=self.id)
        return added

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

        @c.on(events.NewMessage(outgoing=True, func=lambda e: e.is_private))
        async def on_outgoing(event):
            # менеджер написал лиду прямо в Telegram — сохраняем в историю инбокса
            self.handle_outgoing(event.chat_id, event.raw_text, event.id)

        @c.on(events.MessageRead(inbox=False))
        async def on_read(event):
            # собеседник прочитал наши сообщения до max_id включительно
            self._mark_read(event.chat_id, event.max_id)

    def handle_outgoing(self, peer_id: int, text: str | None, tg_message_id: int | None) -> bool:
        """Своё сообщение лиду, отправленное не из панели. Пишем только лидам из базы, закреплённым за этим аккаунтом
        (или ещё ни за кем) — чужие личные переписки менеджера панель не собирает."""
        lead = db.one("SELECT * FROM leads WHERE tg_id=%s AND (owner_account_id=%s OR owner_account_id IS NULL)",
                      (peer_id, self.id))
        if not lead:
            return False
        new = inbox.record(self.id, lead["id"], "out", text, tg_message_id, "telegram")
        if new and autopilot.enabled():
            try:
                loop = asyncio.get_running_loop()     # вызов из обработчика события Telegram
            except RuntimeError:
                loop = None                           # вне цикла (скрипты, тесты) — проверку запускает вызывающий
            if loop:
                spawn(autopilot.check_manual(self.id, lead["id"], tg_message_id), "проверка: менеджер написал сам")
        return new

    async def handle_incoming(self, uid: int, text: str | None, tg_message_id: int | None, event=None):
        lead = db.one("SELECT * FROM leads WHERE tg_id=%s", (uid,))
        if lead:
            if inbox.record(self.id, lead["id"], "in", text, tg_message_id, "incoming") and ai.configured() and text:
                mid = db.val("""SELECT id FROM messages WHERE account_id=%s AND lead_id=%s AND direction='in'
                                AND tg_message_id=%s""", (self.id, lead["id"], tg_message_id))
                # разбор ответа ИИ и автоответ (если включён в кампании) — в фоне, приём не ждёт
                spawn(autopilot.after_incoming(mid, lead["id"], self.id), f"разбор и автоответ лиду #{lead['id']}")
            inbox.auto_stage(lead["id"], "replied")
            db.ex("""UPDATE campaign_leads SET state='replied', replied_at=now(), next_step_at=NULL,
                     chain_note=CASE WHEN next_step_at IS NOT NULL THEN 'дожимы остановлены: ответил' ELSE chain_note END
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
        self.search_state: dict[int, dict] = {}    # chat_search_id → ход «Поиск групп»

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
                or bool(self.groups_state.get(account_id, {}).get("running"))
                or any(st.get("running") and st.get("account_id") == account_id for st in self.search_state.values()))

    async def run_chat_search(self, search_id: int, account_id: int, links: list[str] | None = None):
        """Поиск групп по теме (или проверка списка ссылок) в фоне, с ходом в search_state."""
        from . import chat_search
        st = self.search_state[search_id] = {"running": True, "account_id": account_id, "step": "запуск",
                                             "done": 0, "total": 0, "found": 0}
        try:
            if links is not None:
                ok, skipped = await chat_search.check_links(self.get(account_id), search_id, links, st)
                st["step"] = f"проверено ссылок: {ok}" + (f"; пропущено: {'; '.join(skipped[:10])}" if skipped else "")
                db.ex("UPDATE chat_searches SET status='done', step=%s, finished_at=now() WHERE id=%s", (st["step"], search_id))
            else:
                await chat_search.run_search(self.get(account_id), search_id, st)
                st["step"] = "готово"
        except Exception as e:
            st["step"] = (f"Telegram попросил подождать {e.seconds} сек — продолжите поиск позже"
                          if isinstance(e, errors.FloodWaitError) else f"ошибка: {e}")
            db.ex("UPDATE chat_searches SET status='error', step=%s WHERE id=%s", (st["step"], search_id))
            db.log(f"Поиск групп #{search_id}: {type(e).__name__}: {e}", "error", account_id)
        finally:
            st["running"] = False

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
