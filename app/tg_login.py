"""Вход в аккаунт Telegram: код, облачный пароль (2FA), QR-код, выход. Часть AccountClient (см. tg.py)."""
import asyncio

from telethon import errors, utils

from . import db

CODE_WHERE = {"App": "в приложение Telegram (чат «Telegram»)", "Sms": "по SMS", "Call": "звонком",
              "FlashCall": "flash-звонком", "MissedCall": "пропущенным звонком (последние цифры номера)",
              "FragmentSms": "через Fragment", "EmailCode": "на e-mail",
              "SetUpEmailRequired": "— Telegram требует привязать e-mail"}


class DuplicateAccount(Exception):
    """Этот Telegram-аккаунт уже подключён в панели другой записью."""


class LoginMixin:
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
