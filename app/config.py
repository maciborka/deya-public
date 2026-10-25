import os
from urllib.parse import quote

LOGGER_IP = os.environ.get("DEYA_LOGGER_IP", "")
LOGGER_SERIAL = int(os.environ.get("DEYA_LOGGER_SERIAL") or "0")  # серийник WiFi-логгера (не инвертора)
LOGGER_PORT = int(os.environ.get("DEYA_LOGGER_PORT", "8899"))
POLL_SEC = float(os.environ.get("DEYA_POLL_SEC", "10"))  # телеметрия (блоки 500+)
FULL_POLL_SEC = float(os.environ.get("DEYA_FULL_POLL_SEC", "300"))  # полное чтение с настройками
TZ = os.environ.get("DEYA_TZ", "Europe/Kyiv")

FORECAST_HOURS = os.environ.get("DEYA_FORECAST_HOURS", "5,11,17,21")
# Вход на дашборд/API (HTTP Basic); пустой пароль — без защиты (только для локальной разработки)
WEB_USER = os.environ.get("DEYA_WEB_USER", "deya")
WEB_PASSWORD = os.environ.get("DEYA_WEB_PASSWORD", "")
FORECAST_DAYS = 3


def database_url() -> str:
    url = os.environ.get("DEYA_DATABASE_URL")
    if url:
        return url
    user = os.environ.get("POSTGRES_USER", "deya")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    name = os.environ.get("POSTGRES_DB", "deya")
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{name}"
