import time
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import config, main


@pytest.fixture
def client(pg_url, monkeypatch):
    monkeypatch.setattr(main.Collector, "run", _noop, raising=True)
    with TestClient(main.app) as c:
        yield c


async def _noop(self):
    return None


def _fill(db, rows):
    for ts, m in rows:
        db.insert_sample(ts, m)


def test_samples_step_and_auto(client):
    db = main.app.state.db
    base = (int(time.time()) // 3600) * 3600 - 3 * 86400
    _fill(db, [(base + i * 30, {"soc_pct": 10 + (i % 2) * 10}) for i in range(240)])
    raw = client.get("/api/samples", params={"from": base, "to": base + 7200}).json()
    assert len(raw) == 240
    stepped = client.get("/api/samples", params={"from": base, "to": base + 7200, "step": 600}).json()
    assert len(stepped) == 12
    assert stepped[0]["soc_pct"] == pytest.approx(15)
    assert stepped[0]["ts"] % 600 == 0
    auto = client.get("/api/samples", params={"from": base - 4 * 86400, "to": base + 86400}).json()
    assert 0 < len(auto) <= 2000
    assert len(auto) < 240 or auto[0]["ts"] % 250 == 0


def test_daily(client):
    db = main.app.state.db
    tz = ZoneInfo(config.TZ)
    today = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    t_today = int(today.timestamp())
    t_yest = t_today - 86400
    _fill(db, [
        (t_yest, {"pv_today_kwh": 5.0, "load_today_kwh": 8.0}),
        (t_yest + 30, {"pv_today_kwh": 7.5, "load_today_kwh": 9.0, "grid_import_today_kwh": 1.5}),
        (t_today, {"pv_today_kwh": 2.0, "batt_charge_today_kwh": 3.0}),
    ])
    rows = client.get("/api/daily", params={"days": 5}).json()
    assert [r["date"] for r in rows] == [
        datetime.fromtimestamp(t_yest, tz).date().isoformat(),
        today.date().isoformat(),
    ]
    assert rows[0]["pv_today_kwh"] == 7.5
    assert rows[0]["load_today_kwh"] == 9.0
    assert rows[0]["grid_import_today_kwh"] == 1.5
    assert rows[1]["batt_charge_today_kwh"] == 3.0
    assert rows[1]["grid_export_today_kwh"] is None


def test_index_and_static(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text
    assert client.get("/static/vendor/uPlot.iife.min.js").status_code == 200


def test_strategy(client):
    assert client.get("/api/strategy").status_code == 404
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO strategy_settings(reserve_soc_pct, max_grid_charge_soc_pct, battery_capacity_kwh, "
            "battery_usable_pct, round_trip_efficiency_pct, control_mode, writes_enabled, decision_time, updated_at, "
            "export_price_per_kwh, wear_cost_per_kwh, risk, default_plan_soc_pct) "
            "VALUES (30, 80, 24.00, 95, 90, 'off', false, '21:00', now(), 0, 0.4, 'balanced', 60)"
        )
    body = client.get("/api/strategy").json()
    assert body["reserve_soc_pct"] == 30
    assert (body["normal_floor_pct"], body["emergency_mode"], body["emergency_floor_pct"]) == (15, "off", 50)  # db_default
    assert body["control_mode"] == "off"
    assert body["writes_enabled"] is False
    assert body["decision_time"] == "21:00"
    assert body["battery_capacity_kwh"] == 24.0


def test_mode(client):
    assert client.get("/api/mode").status_code == 404
    regs = {141: 0, 142: 1, 130: 1, 128: 100, 146: 255, 148: 0, 149: 655, 150: 700, 151: 1700, 152: 1800,
            153: 2305, 166: 50, 167: 50, 168: 80, 172: 1, 173: 1, 174: 0}
    main.app.state.db.maybe_insert_settings(1792000000, regs)
    body = client.get("/api/mode").json()
    assert body["energy_pattern"] == "Battery First"
    assert body["work_mode"] == "Zero Export To Load"
    assert body["tou"]["days_label"] == "все дни"
    assert len(body["tou"]["slots"]) == 6
    assert body["tou"]["slots"][5]["start"] == "23:05"
    assert body["tou"]["slots"][5]["end"] == "00:00"
