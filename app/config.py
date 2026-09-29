import os
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tg:tg@127.0.0.1:5432/tg")

# Имена хостов, по которым открывают панель (через запятую, «*» — любые). Защита от DNS-rebinding.
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
# Cookie входа только по HTTPS — включите, когда панель работает за nginx/Caddy с TLS
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "0") == "1"

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_DIR = DATA_DIR / "sessions"          # по файлу сессии Telethon на аккаунт
SESSIONS_DIR.mkdir(exist_ok=True)
LEGACY_SESSION = DATA_DIR / "account.session"  # сессия однопользовательской версии — подключается при первом запуске


def _secret_key() -> str:
    """Ключ подписи cookie входа: из .env или сгенерированный один раз и сохранённый в data/."""
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = DATA_DIR / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip()


SECRET_KEY = _secret_key()

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Minsk"))

# Общие настройки команды (меняет админ в панели, хранятся в БД)
DEFAULT_SETTINGS = {
    "stop_words": "стоп,stop,отписаться,unsubscribe,не пишите",
    "recontact_days": "30",       # не писать одному человеку из разных кампаний чаще раза в N дней
}
