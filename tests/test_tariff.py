from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database

KYIV = ZoneInfo("Europe/Kyiv")


@pytest.fixture
def seeded(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        plan_id = conn.execute(
            "INSERT INTO tariff_plans(name, valid_from, is_active, currency, notes) "
            "VALUES ('Двухзонный', '2026-01-01', true, 'UAH', '') RETURNING id"
        ).fetchone()[0]
        for name, start, end, price in (("День", "07:00", "23:00", 4.32), ("Ночь", "23:00", "07:00", 2.16)):
            conn.execute(
                "INSERT INTO tariff_zones(plan_id, name, start_time, end_time, price_per_kwh, direction) "
                "VALUES (%s, %s, %s, %s, %s, 'import')",
                (plan_id, name, start, end, price),
            )
    return pg_url


@pytest.mark.parametrize(
    "hhmm,zone,price",
    [("06:59", "Ночь", 2.16), ("07:00", "День", 4.32), ("22:59", "День", 4.32), ("23:00", "Ночь", 2.16), ("03:00", "Ночь", 2.16)],
)
def test_price_at(seeded, hhmm, zone, price):
    db = Database(seeded)
    try:
        h, m = map(int, hhmm.split(":"))
        res = db.price_at(datetime(2026, 6, 15, h, m, tzinfo=KYIV))
        assert res["name"] == zone
        assert res["price_per_kwh"] == pytest.approx(price)
    finally:
        db.close()


def test_price_at_unix_ts(seeded):
    db = Database(seeded)
    try:
        ts = int(datetime(2026, 6, 15, 12, 0, tzinfo=KYIV).timestamp())
        assert db.price_at(ts)["name"] == "День"
    finally:
        db.close()


def test_api_tariff(seeded, monkeypatch):
    async def noop(self):
        return None

    monkeypatch.setattr(main.Collector, "run", noop, raising=True)
    with TestClient(main.app) as c:
        data = c.get("/api/tariff").json()
    assert data["plan"]["name"] == "Двухзонный"
    assert [z["name"] for z in data["zones"]] == ["День", "Ночь"]
    assert data["current"]["name"] in ("День", "Ночь")
    assert data["current"]["price_per_kwh"] in (4.32, 2.16)


def test_api_tariff_none(pg_url, monkeypatch):
    async def noop(self):
        return None

    monkeypatch.setattr(main.Collector, "run", noop, raising=True)
    with TestClient(main.app) as c:
        assert c.get("/api/tariff").status_code == 404
