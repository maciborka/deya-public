import time
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from app import config, main
from app.flows import integrate_day_import, integrate_flows, split_power
from app.planner_service import apply_status
from tests.test_api import _fill, client  # noqa: F401


def row(ts, pv=0.0, load=0.0, grid=0.0, batt=0.0):
    return {"ts": ts, "pv1_power_w": pv, "pv2_power_w": 0.0, "load_power_w": load, "grid_power_w": grid,
            "batt_power_w": batt}


def hour(rows_args):
    """Постоянные мощности ровно час с шагом 60 с."""
    return [row(i * 60, **rows_args) for i in range(61)]


def test_day_pv_covers_load_and_charges_battery():
    r = integrate_flows(hour(dict(pv=5000, load=2000, grid=0, batt=-3000)), 0, 3600)
    f = r["flows_kwh"]
    assert f["pv_to_load"] == pytest.approx(2.0) and f["pv_to_batt"] == pytest.approx(3.0)
    assert f["pv_to_grid"] == 0 and f["grid_to_load"] == 0 and f["grid_to_batt"] == 0
    assert r["totals_kwh"]["pv"] == pytest.approx(5.0) and r["coverage"] == 1.0


def test_night_grid_charge():
    r = integrate_flows(hour(dict(pv=0, load=1000, grid=4000, batt=-3000)), 0, 3600)
    f = r["flows_kwh"]
    assert f["grid_to_load"] == pytest.approx(1.0) and f["grid_to_batt"] == pytest.approx(3.0)
    assert f["pv_to_load"] == 0 and r["totals_kwh"]["grid_import"] == pytest.approx(4.0)


def test_export_and_discharge():
    r = integrate_flows(hour(dict(pv=6000, load=1000, grid=-5000, batt=0)), 0, 3600)
    assert r["flows_kwh"]["pv_to_grid"] == pytest.approx(5.0)
    assert r["totals_kwh"]["grid_export"] == pytest.approx(5.0)
    f = split_power(pv=500, load=3000, grid=500, batt=2000)
    assert f["pv_to_load"] == 500 and f["batt_to_load"] == 2000 and f["grid_to_load"] == 500


def test_gap_not_integrated():
    rows = [row(0, load=1000), row(60, load=1000), row(1000, load=1000), row(1060, load=1000)]
    r = integrate_flows(rows, 0, 2000)
    assert r["flows_kwh"]["grid_to_load"] == pytest.approx(0.033, abs=1e-3) and r["totals_kwh"]["load"] == pytest.approx(2 * 60 * 1000 / 3.6e6, abs=1e-3)
    assert r["coverage"] == pytest.approx(0.06)


def test_missing_values_skipped():
    rows = [row(0, load=1000), {**row(60, load=1000), "load_power_w": None}, row(120, load=1000)]
    assert integrate_flows(rows, 0, 120)["coverage"] == 0


def test_api_flows(client):
    tz = ZoneInfo(config.TZ)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    base = int(midnight.timestamp())
    day = midnight.date().isoformat()
    _fill(main.app.state.db, [(base + i * 60, {"pv1_power_w": 2000, "load_power_w": 1000, "grid_power_w": -1000,
                                               "batt_power_w": 0}) for i in range(61)])
    with mock.patch("app.main.time.time", return_value=base + 7200):
        body = client.get("/api/flows", params={"date": day}).json()
    assert body["date"] == day and body["from_ts"] == base
    assert body["flows_kwh"]["pv_to_load"] == pytest.approx(1.0)
    assert body["flows_kwh"]["pv_to_grid"] == pytest.approx(1.0)
    assert body["coverage"] == pytest.approx(0.5)
    assert set(body["totals_kwh"]) == {"pv", "load", "grid_import", "grid_export", "batt_charge", "batt_discharge"}
    assert client.get("/api/flows").status_code == 200


def test_samples_fields_and_downsample(client):
    base = (int(time.time()) // 3600) * 3600 - 40 * 86400
    _fill(main.app.state.db, [(base + i * 600, {"soc_pct": 50, "pv1_power_w": 100}) for i in range(1000)])
    body = client.get("/api/samples", params={"from": base, "to": base + 600000, "fields": "soc_pct"}).json()
    assert 0 < len(body) <= 1000 and set(body[0]) == {"ts", "soc_pct"}
    body = client.get("/api/samples", params={"from": base, "to": base + 600000, "step": 60}).json()
    assert len(body) <= 1000
    assert client.get("/api/samples", params={"from": base, "to": base + 600000, "fields": "x;drop"}).status_code == 422
    now = int(time.time())
    _fill(main.app.state.db, [(now - 100, {"soc_pct": 77})])
    body = client.get("/api/samples", params={"hours": 1, "fields": "soc_pct"}).json()
    assert [r["soc_pct"] for r in body] == [77]


def test_latest_ts_is_sample_ts(client):
    _fill(main.app.state.db, [(1_700_000_000, {"soc_pct": 5})])
    assert client.get("/api/latest").json()["ts"] == 1_700_000_000


def test_next_run_ts(client):
    ts = client.get("/api/forecast/status").json()["next_run_ts"]
    now = time.time()
    assert now < ts <= now + 86400 + 3600
    assert datetime.fromtimestamp(ts, ZoneInfo(config.TZ)).hour in (5, 11, 17, 21)


def test_apply_status_pending_none(client):
    db = main.app.state.db
    tz = ZoneInfo(config.TZ)
    day = datetime(2026, 6, 15, tzinfo=tz)
    evening = int(day.replace(hour=20).timestamp())
    after = int(day.replace(hour=23, minute=30).timestamp())
    assert apply_status(db, evening)["status"] == "pending"
    assert apply_status(db, after)["status"] == "none"


def test_integrate_day_import_only_expensive():
    price = lambda ts: 4.0 if ts < 3600 else 2.0  # noqa: E731
    rows = [row(i * 60, grid=1000) for i in range(121)]  # 2 часа по 1 кВт
    res = integrate_day_import(rows, 0, 7200, price, 4.0, lambda ts: ts // 3600)
    assert res["import_kwh"] == pytest.approx(1.0, abs=0.02)
    assert res["cost_uah"] == pytest.approx(4.0, abs=0.1)
    assert res["by_hour"][0]["import_kwh"] == pytest.approx(1.0, abs=0.02) and res["by_hour"][1]["import_kwh"] == 0
    assert res["coverage"] == pytest.approx(1.0)
    # экспорт не считается, разрыв > 5 мин не интегрируется
    rows = [row(0, grid=-500), row(60, grid=-500), row(1000, grid=3000)]
    assert integrate_day_import(rows, 0, 1000, price, 4.0, lambda ts: 0)["import_kwh"] == 0


def test_api_day_import(client):
    import psycopg
    with psycopg.connect(config.database_url(), autocommit=True) as conn:
        pid = conn.execute(
            "INSERT INTO tariff_plans(name, valid_from, is_active, currency, notes) "
            "VALUES ('t', '2020-01-01', true, 'UAH', '') RETURNING id").fetchone()[0]
        for name, a, b, p in (("День", "07:00", "23:00", 4.32), ("Ночь", "23:00", "07:00", 2.16)):
            conn.execute("INSERT INTO tariff_zones(plan_id, name, start_time, end_time, price_per_kwh, direction) "
                         "VALUES (%s, %s, %s, %s, %s, 'import')", (pid, name, a, b, p))
    tz = ZoneInfo(config.TZ)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    base = int(midnight.timestamp())
    day = midnight.date().isoformat()
    db = main.app.state.db
    # ночью (03:00–04:00) 2 кВт — не считается; днём (12:00–13:00) 1 кВт — считается
    _fill(db, [(base + 3 * 3600 + i * 60, {"grid_power_w": 2000}) for i in range(61)])
    _fill(db, [(base + 12 * 3600 + i * 60, {"grid_power_w": 1000}) for i in range(61)])
    with mock.patch("app.main.time.time", return_value=base + 20 * 3600):
        body = client.get("/api/day-import", params={"date": day}).json()
    assert body["date"] == day
    assert body["import_kwh"] == pytest.approx(1.0, abs=0.02)
    assert body["cost_uah"] == pytest.approx(4.32, abs=0.1)
    assert len(body["by_hour"]) == 24 and body["by_hour"][12]["import_kwh"] == pytest.approx(1.0, abs=0.02)
    assert body["by_hour"][3]["import_kwh"] == 0
    assert 0 < body["coverage"] < 1
