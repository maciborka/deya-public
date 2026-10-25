import asyncio

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app import inverter_writer
from app.db import Database
from app.inverter_writer import execute, validate, write_order

LIVE = {146: 255, 147: 0}
LIVE.update({148 + i: v for i, v in enumerate([0, 655, 700, 1700, 1800, 2305])})
LIVE.update({154 + i: 12000 for i in range(6)})
LIVE.update({160 + i: 4900 for i in range(6)})
LIVE.update({166 + i: v for i, v in enumerate([50, 50, 80, 50, 50, 50])})
LIVE.update({172 + i: v for i, v in enumerate([1, 1, 0, 0, 0, 1])})


class FakeClient:
    def __init__(self, regs, ignore_writes=(), fail_write=False, fail_after=None):
        self.regs = dict(regs)
        self.writes = []
        self.ignore_writes = set(ignore_writes)
        self.fail_write = fail_write
        self.fail_after = fail_after

    def read_block_sync(self, start, n):
        return {r: self.regs.get(r, 0) for r in range(start, start + n)}

    def write_register_sync(self, addr, value):
        if self.fail_write or (self.fail_after is not None and len(self.writes) >= self.fail_after):
            raise TimeoutError("нет ответа")
        self.writes.append((addr, value))
        if addr not in self.ignore_writes:
            self.regs[addr] = value


class FakeDb:
    def __init__(self):
        self.backup = None
        self.finished = None

    def save_write_backup(self, rid, backup):
        self.backup = backup

    def finish_write_request(self, rid, status, readback, error):
        self.finished = {"status": status, "readback": readback, "error": error}


def request(changes, expected_old=None):
    if expected_old is None:
        expected_old = {k: LIVE[int(k)] for k in changes}
    return {"id": 1, "changes": changes, "expected_old": expected_old}


def run(client, changes, expected_old=None):
    db = FakeDb()
    status = execute(client, db, request(changes, expected_old), sleep=lambda s: None)
    return status, db


def test_done_writes_each_register_and_reads_back():
    client = FakeClient(LIVE)
    status, db = run(client, {"153": 2300, "171": 90})
    assert status == "done"
    assert client.writes == [(171, 90), (153, 2300)]
    assert db.backup[153] == 2305
    assert db.finished["readback"][153] == 2300
    assert db.finished["error"] is None


def test_stale_when_live_differs_from_expected():
    client = FakeClient(LIVE)
    status, db = run(client, {"171": 90}, {"171": 60})
    assert status == "stale"
    assert client.writes == []
    assert "171" in db.finished["error"]


def test_failed_readback_mismatch():
    client = FakeClient(LIVE, ignore_writes={171})
    status, db = run(client, {"171": 90})
    assert status == "failed"
    assert client.writes == [(171, 90)]
    assert "рег 171" in db.finished["error"]


@pytest.mark.parametrize("reg", ["146", "160", "141", "130", "147"])
def test_whitelist_refusal(reg):
    client = FakeClient(LIVE)
    status, db = run(client, {reg: 1, "171": 90}, {reg: LIVE.get(int(reg), 0), "171": 50})
    assert status == "failed"
    assert client.writes == []
    assert "запрещена" in db.finished["error"]


def test_range_and_order_refusal():
    client = FakeClient(LIVE)
    status, db = run(client, {"150": 600})
    assert status == "failed" and client.writes == []
    assert "не возрастают" in db.finished["error"]


def test_write_exception_marks_failed_with_readback():
    client = FakeClient(LIVE, fail_write=True)
    status, db = run(client, {"171": 90})
    assert status == "failed"
    assert "TimeoutError" in db.finished["error"]
    assert db.finished["readback"][171] == 50


def test_validate_ranges():
    assert validate({154: 12001}, LIVE)
    assert validate({166: 4}, LIVE)
    assert validate({172: 4}, LIVE)
    assert validate({148: 2400}, LIVE)
    assert validate({148: 60}, LIVE)
    assert validate({153: 2359, 148: 3, 166: 5, 154: 0, 172: 3, 173: 2}, LIVE) == []


def test_write_order_keeps_times_increasing():
    # 149 и 150 сдвигаются вверх, 152 и 153 — вниз; 171 не время
    changes = {149: 900, 150: 1000, 152: 1730, 153: 2200, 171: 90}
    order = write_order(changes, LIVE)
    assert order == [171, 152, 153, 150, 149]
    times = {r: LIVE[r] for r in range(148, 154)}
    for reg in order:
        if reg in times:
            times[reg] = changes[reg]
            seq = [times[r] for r in range(148, 154)]
            assert all(b > a for a, b in zip(seq, seq[1:])), (reg, seq)


def test_execute_uses_write_order():
    client = FakeClient(LIVE)
    status, db = run(client, {"152": 1730, "150": 1000, "171": 90})
    assert status == "done"
    assert [r for r, _ in client.writes] == [171, 152, 150]


def test_partial_failure_reports_written_and_left():
    client = FakeClient(LIVE, fail_after=1)
    status, db = run(client, {"171": 90, "170": 60})
    assert status == "failed"
    err = db.finished["error"]
    assert "записаны: 170" in err and "не записаны: 171" in err
    assert db.finished["readback"][170] == 60
    assert "несовпадения" not in err


def test_readback_ignores_non_whitelist():
    client = FakeClient(LIVE)
    orig = client.read_block_sync
    calls = []

    def read(start, n):
        regs = orig(start, n)
        calls.append(1)
        if len(calls) > 1:
            regs[146], regs[160] = 0, 1  # вне WHITELIST — не сверяем
        return regs

    client.read_block_sync = read
    status, db = run(client, {"171": 90})
    assert status == "done"


@pytest.fixture
def db(pg_url):
    database = Database(pg_url)
    yield database
    database.close()


def insert_request(url, status="pending", changes=None, expected_old=None):
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(
            "INSERT INTO inverter_write_requests(created_at, created_by, changes, expected_old, status) "
            "VALUES (now(), 'test', %s, %s, %s) RETURNING id",
            (Jsonb(changes or {"171": 90}), Jsonb(expected_old or {"171": 50}), status),
        ).fetchone()[0]


def test_queue_claim_and_finish(pg_url, db):
    insert_request(pg_url, status="cancelled")
    rid = insert_request(pg_url)
    client = FakeClient(LIVE)
    assert asyncio.run(inverter_writer.process_queue(client, db)) is True
    assert asyncio.run(inverter_writer.process_queue(client, db)) is False
    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "SELECT status, backup, readback, started_at, finished_at FROM inverter_write_requests WHERE id = %s",
            (rid,),
        ).fetchone()
    assert row[0] == "done"
    assert row[1]["171"] == 50 and row[2]["171"] == 90
    assert row[3] is not None and row[4] is not None


def test_expiry_stuck_and_old_pending(pg_url, db):
    stuck = insert_request(pg_url, status="running")
    fresh_running = insert_request(pg_url, status="running")
    old_pending = insert_request(pg_url)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("UPDATE inverter_write_requests SET started_at = now() - interval '11 minutes' WHERE id = %s", (stuck,))
        conn.execute("UPDATE inverter_write_requests SET started_at = now() WHERE id = %s", (fresh_running,))
        conn.execute("UPDATE inverter_write_requests SET created_at = now() - interval '16 minutes' WHERE id = %s",
                     (old_pending,))
    new_pending = insert_request(pg_url)
    assert db.fail_stuck_writes(10) == 1
    assert db.expire_pending_writes(15) == 1
    with psycopg.connect(pg_url) as conn:
        rows = dict(conn.execute("SELECT id, (status, error) FROM inverter_write_requests").fetchall())
    assert rows[stuck] == ("failed", "зависший запрос")
    assert rows[fresh_running][0] == "running"
    assert rows[old_pending] == ("stale", "запрос устарел, не выполнен")
    assert rows[new_pending][0] == "pending"


def test_running_marked_failed_on_start(pg_url, db):
    rid = insert_request(pg_url, status="running")
    assert db.fail_interrupted_writes() == 1
    with psycopg.connect(pg_url) as conn:
        status, error = conn.execute(
            "SELECT status, error FROM inverter_write_requests WHERE id = %s", (rid,)
        ).fetchone()
    assert (status, error) == ("failed", "прервано перезапуском")
