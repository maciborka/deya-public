"""Сетка слотов в локальном времени: время везде — unix-секунды UTC."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

SLOT_SEC = 900
SLOTS_PER_DAY = 96


def zone(tz: str | ZoneInfo) -> ZoneInfo:
    """Строка или ZoneInfo → ZoneInfo."""
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)


def local(ts: int, tz: str | ZoneInfo) -> datetime:
    """unix-секунды → локальное время."""
    return datetime.fromtimestamp(ts, zone(tz))


def slot_floor(ts: int, tz: str | ZoneInfo, slot_sec: int = SLOT_SEC) -> int:
    """Начало слота, содержащего ts, по локальной сетке."""
    off = int(local(ts, tz).utcoffset().total_seconds())
    return ts - (ts + off) % slot_sec


def slot_index(ts: int, tz: str | ZoneInfo) -> int:
    """Номер 15-минутного слота локальных суток (0..95)."""
    dt = local(ts, tz)
    return dt.hour * 4 + dt.minute // 15


def at_local(d: date, hour: int, tz: str | ZoneInfo) -> int:
    """unix-секунды для локальных даты и часа (при неоднозначности DST — первое вхождение)."""
    return int(datetime.combine(d, time(hour), tzinfo=zone(tz)).timestamp())
