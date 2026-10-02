"""Диалоги и группы аккаунта: снимок диалогов, свои группы и их участники, «Найти получателей».
Часть AccountClient (см. tg.py)."""
import asyncio

from telethon import errors

from . import db, leads


class GroupsMixin:
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
        is_admin = kind == "group"
        db.ex("""INSERT INTO tg_dialogs(account_id, peer_id, title, kind, has_private, is_admin, members, username,
                                        last_message_id, updated_at)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) ON CONFLICT (account_id, peer_id) DO UPDATE
                 SET title=excluded.title, kind=excluded.kind, has_private=excluded.has_private,
                     is_admin=excluded.is_admin, members=excluded.members, username=excluded.username,
                     last_message_id=excluded.last_message_id, updated_at=now()""",
              (self.id, d.id, d.name or "", kind, has_private, is_admin, getattr(e, "participants_count", None),
               getattr(e, "username", None), getattr(d.message, "id", None)))
        return has_private

    # ---------- свои группы ----------
    async def refresh_groups(self, st: dict):
        """Обновляет список диалогов, чтобы найти группы аккаунта."""
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
