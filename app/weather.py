import time
from urllib.parse import urlencode

from .forecast import base

URL = "https://api.open-meteo.com/v1/forecast"
CACHE_SEC = 3600
DEFAULT_LAT = 48.44
DEFAULT_LON = 22.72
DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,"
    "cloud_cover_mean,wind_speed_10m_max,sunrise,sunset"
)
CURRENT = "temperature_2m,weather_code,is_day,cloud_cover,wind_speed_10m"

_cache: dict[tuple[float, float], dict] = {}


class WeatherUnavailable(Exception):
    pass


def fetch(lat: float, lon: float) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "Europe/Kyiv",
        "forecast_days": 7,
        "wind_speed_unit": "ms",
        "daily": DAILY,
        "current": CURRENT,
    }
    return base.http_get_json(f"{URL}?{urlencode(params)}")


def parse(data: dict, lat: float, lon: float, fetched_at: int) -> dict:
    daily = data.get("daily") or {}
    cur = data.get("current") or {}

    def col(name: str, i: int):
        vals = daily.get(name) or []
        return vals[i] if i < len(vals) else None

    days = [
        {
            "date": d,
            "code": col("weather_code", i),
            "t_max": col("temperature_2m_max", i),
            "t_min": col("temperature_2m_min", i),
            "precip_mm": col("precipitation_sum", i),
            "precip_prob": col("precipitation_probability_max", i),
            "cloud": col("cloud_cover_mean", i),
            "wind_max": col("wind_speed_10m_max", i),
            "sunrise": col("sunrise", i),
            "sunset": col("sunset", i),
        }
        for i, d in enumerate(daily.get("time") or [])
    ]
    return {
        "fetched_at": fetched_at,
        "stale": False,
        "location": {"lat": lat, "lon": lon},
        "current": {
            "time": cur.get("time"),
            "temp": cur.get("temperature_2m"),
            "code": cur.get("weather_code"),
            "is_day": bool(cur["is_day"]) if cur.get("is_day") is not None else None,
            "cloud": cur.get("cloud_cover"),
            "wind": cur.get("wind_speed_10m"),
        },
        "days": days,
    }


def get_weather(lat: float, lon: float, now: float | None = None) -> dict:
    """Кэш на 60 мин; при ошибке сети — последний кэш с stale=True, без кэша — WeatherUnavailable."""
    now = time.time() if now is None else now
    key = (round(lat, 3), round(lon, 3))
    cached = _cache.get(key)
    if cached and now - cached["fetched_at"] < CACHE_SEC:
        return cached
    try:
        result = parse(fetch(lat, lon), lat, lon, int(now))
    except Exception as e:
        if cached:
            return {**cached, "stale": True}
        raise WeatherUnavailable(str(e)) from None
    _cache[key] = result
    return result
