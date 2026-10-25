from datetime import datetime, timezone
from urllib.parse import urlencode

from . import base
from .base import BaseProvider, ForecastSample, PanelGroup

URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoProvider(BaseProvider):
    code = "open_meteo"

    def fetch(self, panel: PanelGroup, days: int) -> list[ForecastSample]:
        params = {
            "latitude": panel.lat,
            "longitude": panel.lon,
            "tilt": panel.tilt,
            "azimuth": panel.azimuth,
            "hourly": "global_tilted_irradiance,shortwave_radiation,temperature_2m",
            "timezone": "UTC",
            "forecast_days": days,
        }
        data = base.http_get_json(f"{URL}?{urlencode(params)}")
        return parse(data, panel.kwp)


def parse(data: dict, kwp: float) -> list[ForecastSample]:
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []

    def col(name: str, i: int):
        vals = hourly.get(name) or []
        return float(vals[i]) if i < len(vals) and vals[i] is not None else None

    out = []
    for i, raw in enumerate(times):
        # метка Open-Meteo — конец интервала (среднее за предыдущий час) → храним начало
        end_ts = int(datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).timestamp())
        gti, temp = col("global_tilted_irradiance", i), col("temperature_2m", i)
        out.append(
            ForecastSample(
                target_ts=end_ts - 3600,
                gti=gti,
                ghi=col("shortwave_radiation", i),
                power_w=base.gti_to_power(gti, kwp, temp),
                temp_c=temp,
            )
        )
    return out
