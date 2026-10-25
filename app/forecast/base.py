import json
import logging
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger("deya.forecast")

HTTP_TIMEOUT = 20
HTTP_ATTEMPTS = 3
HTTP_PAUSE_SEC = 3


@dataclass
class ForecastSample:
    target_ts: int  # начало часа, unix UTC
    gti: float | None = None
    ghi: float | None = None
    power_w: float | None = None
    p10: float | None = None
    p90: float | None = None
    temp_c: float | None = None


@dataclass
class PanelGroup:
    name: str
    lat: float
    lon: float
    tilt: float
    azimuth: float  # конвенция Open-Meteo: 0=юг, восток<0, запад>0
    kwp: float


def az_solcast(az: float) -> float:
    az = float(az)
    return 180.0 - az if az >= 0 else -180.0 - az


def k_temp(temp_c: float | None) -> float:
    if temp_c is None:
        return 1.0
    return min(1.1, max(0.5, 1 - 0.004 * (temp_c - 25)))


def gti_to_power(gti: float | None, kwp: float, temp_c: float | None) -> float | None:
    if gti is None:
        return None
    return gti / 1000 * kwp * 1000 * k_temp(temp_c)


def http_get_json(url: str, headers: dict | None = None) -> dict:
    """GET с таймаутом и повторами. В логи пишется URL без query-параметров."""
    safe_url = url.split("?", 1)[0]
    last: Exception | None = None
    for attempt in range(HTTP_ATTEMPTS):
        if attempt:
            time.sleep(HTTP_PAUSE_SEC)
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            log.warning("GET %s: HTTP %s (попытка %d)", safe_url, e.code, attempt + 1)
            if e.code == 429:
                break
        except Exception as e:
            last = e
            log.warning("GET %s: %s (попытка %d)", safe_url, type(e).__name__, attempt + 1)
    raise RuntimeError(f"GET {safe_url} не удался: {type(last).__name__}: {last}") from None


class BaseProvider(ABC):
    code: str = ""
    requires_key: bool = False
    min_interval_sec: int = 0  # минимальный интервал между успешными запросами на панель
    site_level: bool = False  # True: прогноз на весь сайт, по одному вызову на сайт за запуск (panel = имя сайта)

    def __init__(self, api_key: str = "", params: dict | None = None):
        self.api_key = api_key
        self.params = params or {}

    def enabled(self) -> bool:
        return True

    def site_names(self) -> list[str]:
        """Для site_level-провайдера: имена сайтов, по одному вызову на каждый (panel = имя)."""
        return ["site"]

    @abstractmethod
    def fetch(self, panel: PanelGroup, days: int) -> list[ForecastSample]:
        """Синхронный запрос прогноза; вызывается через asyncio.to_thread."""
