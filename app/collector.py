import asyncio
import logging
import time

from . import inverter_writer
from .db import Database
from .decoder import decode, settings_regs
from .logger_client import BLOCKS, LIVE_BLOCKS, LoggerClient

log = logging.getLogger("deya.collector")


class Collector:
    def __init__(self, client: LoggerClient, db: Database, poll_sec: float, full_poll_sec: float = 300):
        self._client = client
        self._db = db
        self._poll_sec = poll_sec
        self._full_poll_sec = full_poll_sec
        self._last_full: float | None = None  # monotonic время последнего полного чтения
        self._settings_cache: dict[int, int] = {}  # регистры < 500 из последнего полного чтения
        self.last_success_ts: int | None = None

    async def poll_once(self) -> None:
        ts = int(time.time())
        full = self._last_full is None or time.monotonic() - self._last_full >= self._full_poll_sec
        regs, errors = await self._client.read_all(BLOCKS if full else LIVE_BLOCKS)
        for e in errors:
            log.warning("блок не прочитан: %s", e)
            await asyncio.to_thread(self._db.log_error, e, ts)
        if not any(r >= 500 for r in regs):
            raise RuntimeError("не прочитан ни один блок 500+")
        if full:
            self._settings_cache = {r: v for r, v in regs.items() if r < 500}
            self._last_full = time.monotonic()
        metrics = decode({**self._settings_cache, **regs})
        await asyncio.to_thread(self._db.insert_sample, ts, metrics)
        if full:
            await asyncio.to_thread(self._db.maybe_insert_settings, ts, settings_regs(regs))
        self.last_success_ts = ts

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                if await inverter_writer.process_queue(self._client, self._db):
                    self._last_full = None  # следующий опрос — полный, чтобы снимок настроек обновился
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("ошибка очереди записи: %r", e)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("ошибка опроса: %r", e)
                try:
                    await asyncio.to_thread(self._db.log_error, repr(e))
                except Exception:
                    log.exception("не удалось записать ошибку в БД")
            await asyncio.sleep(max(1.0, self._poll_sec - (time.monotonic() - started)))
