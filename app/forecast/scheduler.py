import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .. import config
from ..db import Database
from . import registry
from .base import PanelGroup

log = logging.getLogger("deya.forecast")



def site_panels(provider) -> list[PanelGroup]:
    return [PanelGroup(name=n, lat=0.0, lon=0.0, tilt=0.0, azimuth=0.0, kwp=0.0) for n in provider.site_names()]


def parse_hours(raw: str) -> list[int]:
    hours = sorted({int(h) for h in raw.split(",") if h.strip() and 0 <= int(h) <= 23})
    return hours or [5, 11, 17, 21]


def next_run_ts(now: datetime, hours: list[int]) -> datetime:
    for day in (0, 1):
        for h in hours:
            cand = (now + timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            if cand > now:
                return cand
    raise RuntimeError("unreachable")


class ForecastScheduler:
    def __init__(self, db: Database, tz_name: str, hours: list[int], days: int):
        self._db = db
        self._tz = ZoneInfo(tz_name)
        self._hours = hours
        self._days = days

    async def run_cycle(self) -> None:
        panels = await asyncio.to_thread(self._db.enabled_panels)
        rows = await asyncio.to_thread(self._db.forecast_providers)
        providers = registry.get_enabled(rows)
        if not panels and not any(p.site_level for p in providers):
            log.info("прогноз не запрашивается: нет включённых групп панелей (добавьте в админке)")
            return
        for provider in providers:
            for panel in site_panels(provider) if provider.site_level else panels:
                try:
                    await self._fetch_one(provider, panel)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.error("прогноз %s/%s: %s", provider.code, panel.name, e)
                    await self._log_run(provider.code, panel.name, False, 0, str(e)[:500])

    async def _fetch_one(self, provider, panel) -> None:
        now = int(time.time())
        if provider.min_interval_sec:
            last = await asyncio.to_thread(self._db.last_ok_run_ts, provider.code, panel.name)
            if last is not None and now - last < provider.min_interval_sec:
                return
        samples = await asyncio.to_thread(provider.fetch, panel, self._days)
        await asyncio.to_thread(self._db.insert_forecast, provider.code, panel.name, now, samples)
        await self._log_run(provider.code, panel.name, True, len(samples), None)

    async def _log_run(self, provider: str, panel: str, ok: bool, n: int, error: str | None) -> None:
        try:
            await asyncio.to_thread(self._db.log_forecast_run, int(time.time()), provider, panel, ok, n, error)
        except Exception:
            log.exception("не удалось записать forecast_runs")

    async def run(self) -> None:
        while True:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка цикла прогноза")
            now = datetime.now(self._tz)
            # Через epoch: вычитание aware-datetime с одним tzinfo игнорирует смену UTC-смещения (перевод часов).
            await asyncio.sleep(max(1.0, next_run_ts(now, self._hours).timestamp() - now.timestamp()))
