import os
import re
from datetime import datetime
from urllib.parse import urlencode

from . import base
from .base import BaseProvider, ForecastSample, PanelGroup

RADIATION_URL = "https://api.solcast.com.au/data/forecast/radiation_and_weather"
ROOFTOP_URL = "https://api.solcast.com.au/rooftop_sites/{resource_id}/forecasts"


class SolcastProvider(BaseProvider):
    code = "solcast"
    requires_key = True
    min_interval_sec = 3 * 3600

    def __init__(self, api_key: str = "", params: dict | None = None):
        super().__init__(api_key or os.environ.get("SOLCAST_API_KEY", ""), params)
        sites = self.params.get("sites")
        self.sites: dict[str, str] = (
            {str(k): str(v) for k, v in sites.items() if v} if isinstance(sites, dict) else {}
        )
        resource_id = str(self.params.get("resource_id") or "")
        if not self.sites and resource_id:
            self.sites = {"site": resource_id}
        self.site_level = bool(self.sites)

    def site_names(self) -> list[str]:
        return list(self.sites)

    def enabled(self) -> bool:
        return bool(self.api_key)

    def fetch(self, panel: PanelGroup, days: int) -> list[ForecastSample]:
        if self.site_level:
            url = ROOFTOP_URL.format(resource_id=self.sites[panel.name])
            data = base.http_get_json(
                f"{url}?{urlencode({'format': 'json', 'hours': 72})}",
                {"Authorization": f"Bearer {self.api_key}"},
            )
            return parse_rooftop(data)
        params = {
            "format": "json",
            "output_parameters": "gti,gti10,gti90,air_temp",
            "period": "PT60M",
            "hours": days * 24,
        }
        url = RADIATION_URL
        params.update(
            latitude=panel.lat,
            longitude=panel.lon,
            tilt=panel.tilt,
            azimuth=base.az_solcast(panel.azimuth),
            array_type="fixed",
        )
        data = base.http_get_json(
            f"{url}?{urlencode(params)}", {"Authorization": f"Bearer {self.api_key}"}
        )
        return parse(data, panel.kwp)


def parse(data: dict, kwp: float) -> list[ForecastSample]:
    def f(v):
        return None if v is None else float(v)

    out = []
    for row in data.get("forecasts") or []:
        end = row.get("period_end")
        if not end:
            continue
        # period_end — конец интервала → храним начало
        end_ts = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
        temp = f(row.get("air_temp"))
        gti = f(row.get("gti"))
        out.append(
            ForecastSample(
                target_ts=end_ts - 3600,
                gti=gti,
                power_w=base.gti_to_power(gti, kwp, temp),
                p10=base.gti_to_power(f(row.get("gti10")), kwp, temp),
                p90=base.gti_to_power(f(row.get("gti90")), kwp, temp),
                temp_c=temp,
            )
        )
    out.sort(key=lambda s: s.target_ts)
    return out


def _period_sec(period: str | None) -> int:
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", period or "")
    sec = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60) if m else 0
    return sec or 1800


def parse_rooftop(data: dict) -> list[ForecastSample]:
    """rooftop_sites: pv_estimate* в кВт на весь сайт, период (обычно 30 мин) -> часовые средние в Вт."""
    buckets: dict[int, list[tuple[float | None, float | None, float | None]]] = {}
    for row in data.get("forecasts") or []:
        end = row.get("period_end")
        if not end:
            continue
        end_ts = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
        start = end_ts - _period_sec(row.get("period"))

        def w(key):
            v = row.get(key)
            return None if v is None else float(v) * 1000

        buckets.setdefault(start // 3600 * 3600, []).append((w("pv_estimate"), w("pv_estimate10"), w("pv_estimate90")))

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    return [
        ForecastSample(
            target_ts=h,
            gti=None,
            power_w=avg([r[0] for r in rows]),
            p10=avg([r[1] for r in rows]),
            p90=avg([r[2] for r in rows]),
        )
        for h, rows in sorted(buckets.items())
    ]
