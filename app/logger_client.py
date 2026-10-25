import asyncio
import logging
import time

from pysolarmanv5 import PySolarmanV5

log = logging.getLogger("deya.logger")

BLOCKS = [(0, 22), (60, 3), (98, 40), (138, 40), (178, 60), (500, 60), (560, 60), (620, 64)]
LIVE_BLOCKS = [b for b in BLOCKS if b[0] >= 500]  # телеметрия; настройки (<500) читаются реже
RETRIES = 3
RETRY_PAUSE = 1.5
BLOCK_PAUSE = 0.5
BACKOFF_MAX = 300.0


class LoggerClient:
    """Единственный владелец соединения с логгером.

    Чтение: read_all (опрос) и read_block_sync. Запись: write_register_sync — один регистр через FC16
    (FC06 инвертор отвергает), без повторов; вызывается только воркером очереди записи (app/inverter_writer.py)
    из того же цикла, что и опрос, поэтому параллельных обращений к логгеру нет.
    """

    def __init__(self, ip: str, serial: int, port: int):
        self._ip = ip
        self._serial = serial
        self._port = port
        self._conn: PySolarmanV5 | None = None
        self._backoff = 0.0
        self._next_connect = 0.0

    def _connect(self) -> PySolarmanV5:
        return PySolarmanV5(self._ip, self._serial, port=self._port,
                            mb_slave_id=1, socket_timeout=10, verbose=False)

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.disconnect()
            except Exception:
                pass
            self._conn = None

    def _read_all_sync(self, blocks=BLOCKS) -> tuple[dict[int, int], list[str]]:
        wait = self._next_connect - time.monotonic()
        if self._conn is None and wait > 0:
            time.sleep(wait)
        if self._conn is None:
            try:
                self._conn = self._connect()
            except Exception as e:
                self._fail_connect()
                raise ConnectionError(f"connect: {e!r}") from e

        regs: dict[int, int] = {}
        errors: list[str] = []
        for start, n in blocks:
            for attempt in range(RETRIES):
                try:
                    vals = self._conn.read_holding_registers(register_addr=start, quantity=n)
                    regs.update({start + i: v for i, v in enumerate(vals)})
                    break
                except Exception as e:
                    if attempt == RETRIES - 1:
                        errors.append(f"{start}+{n}: {e!r}")
                    else:
                        time.sleep(RETRY_PAUSE)
            time.sleep(BLOCK_PAUSE)

        if not regs:
            self._close()
            self._fail_connect()
        else:
            self._backoff = 0.0
            self._next_connect = 0.0
        return regs, errors

    def _ensure_conn(self) -> PySolarmanV5:
        if self._conn is None:
            try:
                self._conn = self._connect()
            except Exception as e:
                self._fail_connect()
                raise ConnectionError(f"connect: {e!r}") from e
        return self._conn

    def read_block_sync(self, start: int, n: int) -> dict[int, int]:
        """Один блок holding-регистров (с повторами чтения); при неудаче — исключение."""
        conn = self._ensure_conn()
        for attempt in range(RETRIES):
            try:
                vals = conn.read_holding_registers(register_addr=start, quantity=n)
                return {start + i: v for i, v in enumerate(vals)}
            except Exception:
                if attempt == RETRIES - 1:
                    self._close()
                    raise
                time.sleep(RETRY_PAUSE)

    def write_register_sync(self, addr: int, value: int) -> None:
        """Запись одного регистра через FC16 без повторов (повтор может задвоить запись)."""
        conn = self._ensure_conn()
        try:
            conn.write_multiple_holding_registers(register_addr=addr, values=[value])
        except Exception:
            self._close()
            raise

    def _fail_connect(self) -> None:
        self._backoff = min(BACKOFF_MAX, self._backoff * 2 if self._backoff else 5.0)
        self._next_connect = time.monotonic() + self._backoff
        log.warning("переподключение через %.0f с", self._backoff)

    async def read_all(self, blocks=BLOCKS) -> tuple[dict[int, int], list[str]]:
        return await asyncio.to_thread(self._read_all_sync, blocks)

    async def close(self) -> None:
        await asyncio.to_thread(self._close)
