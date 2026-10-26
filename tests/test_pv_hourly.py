"""Почасовой прогноз PV vs факт: compute_pv_hourly и GET /api/pv/hourly."""
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.db import Database
from app.planner.timeutil import at_local
from app.planner_service import compute_pv_hourly

TZ = "Europe/Kyiv"
KYIV = ZoneInfo(TZ)


def kyiv_ts(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=KYIV).timestamp())


@pytest.fixture
def db(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO strategy_settings(reserve_soc_pct, max_grid_charge_soc_pct, normal_floor_pct, battery_capacity_kwh, "
            "battery_usable_pct, round_trip_efficiency_pct, control_mode, writes_enabled, decision_time, "
            "export_price_per_kwh, wear_cost_per_kwh, risk, default_plan_soc_pct, updated_at) "
            "VALUES (30, 80, 30, 24, 95, 90, 'advise', false, '22:45', 0, 0.4, 'balanced', 60, now())"
        )
        conn.execute("UPDATE forecast_providers SET enabled = (code = 'open_meteo')")
    database = Database(pg_url)
    yield database
    database.close()


def put_forecast(db, fetched_at, hours_w, provider="open_meteo"):
    """hours_w: {ts начала часа: Вт}."""
    db.insert_forecast(provider, "p", fetched_at, [
        SimpleNamespace(target_ts=ts, gti=None, ghi=None, power_w=w, p10=None, p90=None, temp_c=None)
        for ts, w in hours_w.items()
    ])


def put_pv(db, start, end, watt, step=30):
    for ts in range(start, end + 1, step):
        db.insert_sample(ts, {"pv1_power_w": watt / 2, "pv2_power_w": watt / 2})


def test_hours_totals_and_asof(db):
    day = date(2026, 6, 15)
    noon = kyiv_ts(2026, 6, 15, 12)
    asof = kyiv_ts(2026, 6, 14, 22, 45)
    put_forecast(db, asof - 3600, {noon: 2000.0, noon + 3600: 1000.0})
    put_forecast(db, asof + 600, {noon: 9999.0})  # получен после as-of — игнорируется
    put_pv(db, noon, noon + 2 * 3600, 3000.0)
    now = kyiv_ts(2026, 6, 15, 15)
    res = compute_pv_hourly(db, day, now)
    assert res["forecast_asof_ts"] == asof
    assert len(res["hours"]) == 24
    h12, h13, h14 = res["hours"][12], res["hours"][13], res["hours"][14]
    assert h12["hour"] == "12:00" and h12["ts"] == noon
    assert h12["p50_kwh"] == pytest.approx(2.0)
    assert h13["p50_kwh"] == pytest.approx(1.0)
    assert h12["actual_kwh"] == pytest.approx(3.0, abs=0.01)
    assert h14["actual_kwh"] is None or h14["actual_kwh"] == pytest.approx(0.0, abs=0.01)
    assert res["hours"][16]["actual_kwh"] is None  # час ещё не начался
    assert h12["providers"] == {"open_meteo": pytest.approx(2.0)}
    t = res["totals"]
    assert t["p50_kwh"] == pytest.approx(3.0)
    assert t["actual_kwh"] == pytest.approx(6.0, abs=0.02)
    assert t["providers"]["open_meteo"] == pytest.approx(3.0)
    assert t["actual_vs_p50_pct"] == pytest.approx(6.0 / 3.0 * 100, abs=1)
    assert res["warnings"] == []


def test_asof_in_future_uses_now(db):
    day = date(2026, 6, 16)
    now = kyiv_ts(2026, 6, 15, 12)
    res = compute_pv_hourly(db, day, now)
    assert res["forecast_asof_ts"] == now


def test_no_forecast_warning_keeps_actual(db):
    day = date(2026, 6, 15)
    noon = kyiv_ts(2026, 6, 15, 12)
    put_pv(db, noon, noon + 3600, 1000.0)
    res = compute_pv_hourly(db, day, kyiv_ts(2026, 6, 15, 14))
    assert res["warnings"]
    assert res["totals"]["p50_kwh"] is None and res["totals"]["p10_kwh"] is None
    assert all(h["p50_kwh"] is None and h["providers"] == {} for h in res["hours"])
    assert res["hours"][12]["actual_kwh"] == pytest.approx(1.0, abs=0.01)
    assert res["totals"]["actual_vs_p50_pct"] is None


def test_dst_day_has_25_hours(db):
    day = date(2026, 10, 25)
    start = at_local(day, 0, TZ)
    put_forecast(db, kyiv_ts(2026, 10, 24, 20), {start + i * 3600: 1000.0 for i in range(25)})
    res = compute_pv_hourly(db, day, kyiv_ts(2026, 10, 26, 12))
    assert len(res["hours"]) == 25
    assert [h["ts"] for h in res["hours"]] == [start + i * 3600 for i in range(25)]
    assert res["totals"]["p50_kwh"] == pytest.approx(25.0)
    spring = compute_pv_hourly(db, date(2026, 3, 29), kyiv_ts(2026, 3, 30, 12))
    assert len(spring["hours"]) == 23


@pytest.fixture
def client(pg_url, db, monkeypatch):
    async def noop(self):
        return None

    monkeypatch.setattr(main.Collector, "run", noop, raising=True)
    monkeypatch.setattr(main.config, "WEB_PASSWORD", "")
    with TestClient(main.app) as c:
        yield c


def test_api_range_and_shape(client):
    today = datetime.now(ZoneInfo(config.TZ)).date()
    r = client.get("/api/pv/hourly")
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == today.isoformat() and body["hours"] and "totals" in body
    for d in (today - timedelta(days=7), today + timedelta(days=2)):
        assert client.get("/api/pv/hourly", params={"date": d.isoformat()}).status_code == 200
    for d in (today - timedelta(days=8), today + timedelta(days=3)):
        assert client.get("/api/pv/hourly", params={"date": d.isoformat()}).status_code == 400
