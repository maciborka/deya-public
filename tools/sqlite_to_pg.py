"""Одноразовый идемпотентный перенос SQLite -> Postgres (samples, settings_snapshots, poll_errors).

Запуск (SQLite читается только на чтение, forecast_* не переносятся):
  docker compose run --rm -v ./data:/data:ro -v ./tools:/srv/tools deya python tools/sqlite_to_pg.py /data/deya.db
DSN берётся из DEYA_DATABASE_URL или POSTGRES_* (см. app/config.py).
"""

import os
import sqlite3
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app import config  # noqa: E402
from app.decoder import METRIC_NAMES  # noqa: E402

BATCH = 1000


def batches(cur, size):
    while True:
        rows = cur.fetchmany(size)
        if not rows:
            return
        yield rows


def count(pg, table):
    return pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def main(path: str) -> None:
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cols = ", ".join(METRIC_NAMES)
    marks = ", ".join(["%s"] * (len(METRIC_NAMES) + 1))
    jobs = [
        (
            "samples",
            f"SELECT ts, {cols} FROM samples ORDER BY ts",
            f"INSERT INTO samples(ts, {cols}) VALUES ({marks}) ON CONFLICT DO NOTHING",
        ),
        (
            "settings_snapshots",
            "SELECT ts, regs_json, hash FROM settings_snapshots ORDER BY id",
            "INSERT INTO settings_snapshots(ts, regs, hash) SELECT %s, %s::jsonb, %s "
            "WHERE NOT EXISTS (SELECT 1 FROM settings_snapshots WHERE ts = %s AND hash = %s)",
        ),
        (
            "poll_errors",
            "SELECT ts, error FROM poll_errors ORDER BY rowid",
            "INSERT INTO poll_errors(ts, error) SELECT %s, %s "
            "WHERE NOT EXISTS (SELECT 1 FROM poll_errors WHERE ts = %s AND error = %s)",
        ),
    ]
    with psycopg.connect(config.database_url(), autocommit=False) as pg:
        for table, select, insert in jobs:
            src_n = src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            before = count(pg, table)
            cur = src.execute(select)
            with pg.cursor() as w:
                for rows in batches(cur, BATCH):
                    if table == "settings_snapshots":
                        rows = [(ts, regs, h, ts, h) for ts, regs, h in rows]
                    elif table == "poll_errors":
                        rows = [(ts, e, ts, e) for ts, e in rows]
                    w.executemany(insert, rows)
            pg.commit()
            print(f"{table}: sqlite={src_n} pg_before={before} pg_after={count(pg, table)}")
    src.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
