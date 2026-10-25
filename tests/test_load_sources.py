"""Чем питался дом: бакетирование load_sources. Без БД и сети."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.flows import bucket_bounds, integrate_flows, load_sources

TZ = "Europe/Kyiv"
KYIV = ZoneInfo(TZ)


def mk(ts, pv=0.0, load=1000.0, grid=0.0, batt=0.0):
    return {"ts": ts, "pv1_power_w": pv, "pv2_power_w": 0.0, "load_power_w": load, "grid_power_w": grid, "batt_power_w": batt}


def series(start, end, step=30, **kw):
    return [mk(t, **kw) for t in range(start, end + 1, step)]


def test_bucket_5m():
    base = 1_800_000_000 // 300 * 300
    rows = series(base, base + 900, pv=600.0, load=1200.0, batt=600.0)  # 0.6 PV + 0.6 АКБ
    pts = load_sources(rows, bucket_bounds("5m", base, base + 900, TZ))
    assert [p["ts"] for p in pts] == [base, base + 300, base + 600]
    for p in pts:
        assert p["pv_kwh"] == pytest.approx(0.6 * 300 / 3600, abs=1e-3)
        assert p["batt_kwh"] == pytest.approx(0.6 * 300 / 3600, abs=1e-3)
        assert p["grid_kwh"] == 0
        assert p["coverage"] == 1.0


def test_bucket_1h_sums_match_integrate():
    base = 1_800_000_000 // 3600 * 3600
    rows = series(base, base + 3 * 3600, pv=2000.0, load=1500.0, grid=500.0)
    rows += series(base + 3 * 3600 + 30, base + 3 * 3600 + 7200, pv=0.0, load=800.0, grid=800.0)
    rows = [{**r, "load_power_w": r["load_power_w"] + (r["ts"] % 90)} for r in rows]
    end = base + 5 * 3600
    pts = load_sources(rows, bucket_bounds("1h", base, end, TZ))
    assert len(pts) == 5
    total = integrate_flows(rows, base, end)["flows_kwh"]
    assert sum(p["pv_kwh"] for p in pts) == pytest.approx(total["pv_to_load"], abs=0.005)
    assert sum(p["batt_kwh"] for p in pts) == pytest.approx(total["batt_to_load"], abs=0.005)
    assert sum(p["grid_kwh"] for p in pts) == pytest.approx(total["grid_to_load"], abs=0.005)


def test_daily_buckets_dst_autumn():
    start = int(datetime(2026, 10, 24, 0, 0, tzinfo=KYIV).timestamp())
    end = int(datetime(2026, 10, 27, 0, 0, tzinfo=KYIV).timestamp())
    bounds = bucket_bounds("1d", start, end, TZ)
    assert len(bounds) == 3
    dates = [datetime.fromtimestamp(s, KYIV).date().isoformat() for s, _ in bounds]
    assert dates == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert bounds[1][1] - bounds[1][0] == 25 * 3600
    assert all(bounds[i][1] == bounds[i + 1][0] for i in range(2))
    rows = series(start, end, step=60, load=1000.0, grid=1000.0)
    pts = load_sources(rows, bounds)
    assert pts[1]["grid_kwh"] == pytest.approx(25.0, abs=0.01)
    assert pts[0]["grid_kwh"] == pytest.approx(24.0, abs=0.01)


def test_empty_bucket_is_null():
    base = 1_800_000_000 // 300 * 300
    rows = series(base, base + 300, load=1000.0, grid=1000.0) + series(base + 1500, base + 1800, load=1000.0, grid=1000.0)
    pts = load_sources(rows, bucket_bounds("5m", base, base + 1800, TZ))
    assert pts[1]["pv_kwh"] is None and pts[1]["batt_kwh"] is None and pts[1]["grid_kwh"] is None
    assert pts[1]["coverage"] == 0
    assert pts[0]["grid_kwh"] is not None and pts[5]["grid_kwh"] is not None
