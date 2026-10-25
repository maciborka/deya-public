from datetime import datetime
from zoneinfo import ZoneInfo

from .. import config
from . import base
from .base import BaseProvider, ForecastSample, PanelGroup

URL = "https://api.forecast.solar/estimate/{lat}/{lon}/{tilt}/{az}/{kwp}"


class ForecastSolarProvider(BaseProvider):
    code = "forecast_solar"
    # бесплатный API ограничен по частоте — не чаще 1 раза в час на панель
    min_interval_sec = 3600

    def fetch(self, panel: PanelGroup, days: int) -> list[ForecastSample]:
        url = URL.format(lat=panel.lat, lon=panel.lon, tilt=panel.tilt, az=panel.azimuth, kwp=panel.kwp)
        return parse(base.http_get_json(url))


def parse(data: dict) -> list[ForecastSample]:
    tz_name = (((data.get("message") or {}).get("info") or {}).get("timezone")) or config.TZ
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(config.TZ)
    watts = (data.get("result") or {}).get("watts") or {}
    buckets: dict[int, list[float]] = {}
    for raw, w in watts.items():
        if w is None:
            continue
        ts = int(datetime.fromisoformat(raw).replace(tzinfo=tz).timestamp())
        buckets.setdefault(ts // 3600 * 3600, []).append(float(w))
    return [ForecastSample(target_ts=ts, power_w=sum(v) / len(v)) for ts, v in sorted(buckets.items())]
