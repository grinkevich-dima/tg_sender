import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

API_ID = int(os.getenv("TG_API_ID", "0") or 0)
API_HASH = os.getenv("TG_API_HASH", "")

PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "sender.db"
SESSION_PATH = DATA_DIR / "account"  # Telethon добавит .session

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Minsk"))

# Значения по умолчанию для настроек (меняются в панели, хранятся в БД)
DEFAULT_SETTINGS = {
    "warmup_enabled": "1",
    "warmup_start_date": "",      # заполняется при первой отправке
    "warmup_start": "10",         # лимит в 1-й день
    "warmup_step": "5",           # +N в день
    "daily_max": "50",            # потолок в день
    "delay_min": "45",            # сек между сообщениями
    "delay_max": "150",
    "work_start": "10:00",
    "work_end": "20:00",
    "stop_words": "стоп,stop,отписаться,unsubscribe,не пишите",
    "paused_until": "",           # ISO, выставляется при FloodWait/PeerFlood
}
