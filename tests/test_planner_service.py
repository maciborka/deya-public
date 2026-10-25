import asyncio
import math
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import Database
from app.planner.timeutil import at_local
from app.planner_service import (
    PlannerScheduler,
    TariffPrice,
    _inputs_summary,
    actual_import_cost,
    build_params,
    compute,
    compute_and_save,
    compute_forecast24,
    evaluate_decision,
    floor_mismatch,
    redecide_if_settings_changed,
    tou_floor_slots,
)

KYIV = ZoneInfo("Europe/Kyiv")
TZ = "Europe/Kyiv"
STEP = 600
PANELS = ("east", "west")


def kyiv_ts(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=KYIV).timestamp())


def bell(h: float, peak: float) -> float:
    return peak * math.sin(math.pi * (h - 7) / 12) if 7 <= h < 19 else 0.0


@pytest.fixture
def db(pg_url):
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
        conn.execute(
            "INSERT INTO strategy_settings(reserve_soc_pct, max_grid_charge_soc_pct, normal_floor_pct, battery_capacity_kwh, "
            "battery_usable_pct, round_trip_efficiency_pct, control_mode, writes_enabled, decision_time, "
            "export_price_per_kwh, wear_cost_per_kwh, risk, default_plan_soc_pct, updated_at) "
            "VALUES (30, 80, 30, 24, 95, 90, 'advise', false, '22:45', 0, 0.4, 'balanced', 60, now())"
        )
    database = Database(pg_url)
    yield database
    database.close()


def seed_samples(db, start_ts, end_ts, soc=50.0):
    for ts in range(start_ts - start_ts % STEP, end_ts, STEP):
        h = datetime.fromtimestamp(ts, KYIV).hour + datetime.fromtimestamp(ts, KYIV).minute / 60
        load = 800.0 if 18 <= h < 23 else 500.0
        pv = bell(h, 4000.0)
        db.insert_sample(ts, {
            "soc_pct": soc, "load_power_w": load, "pv1_power_w": pv / 2, "pv2_power_w": pv / 2,
            "grid_power_w": 400.0 if (h >= 23 or h < 7) else 0.0,
        })


def seed_forecast(db, now_ts, fetched_at=None, scale=1.0):
    today = datetime.fromtimestamp(now_ts, KYIV).date() - timedelta(days=1)
    a = at_local(today, 0, TZ)
    fetched_at = fetched_at or now_ts - 3600
    for provider, k in (("open_meteo", 1.0), ("forecast_solar", 0.9)):
        for panel in PANELS:
            samples = []
            for i in range(5 * 24):
                ts = a + i * 3600
                h = datetime.fromtimestamp(ts, KYIV).hour
                samples.append(SimpleNamespace(
                    target_ts=ts, gti=None, ghi=None, power_w=bell(h + 0.5, 3000.0) * k * scale,
                    p10=None, p90=None, temp_c=None,
                ))
            db.insert_forecast(provider, panel, fetched_at, samples)


FIXED_NOW = kyiv_ts(2026, 6, 15, 22, 50)


@pytest.fixture
def seeded(db):
    seed_samples(db, FIXED_NOW - 15 * 86400, FIXED_NOW)
    seed_forecast(db, FIXED_NOW)
    return db


def count_decisions(url):
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT count(*) FROM charge_decisions").fetchone()[0]


def test_input_series(db):
    now = FIXED_NOW
    seed_samples(db, now - 2 * 86400, now)
    load = db.load_samples_15m(now - 86400, now)
    assert len(load) >= 95
    assert all(t % 900 == 0 for t, _ in load)
    noon = kyiv_ts(2026, 6, 15, 12, 0)
    assert dict(load)[noon] == pytest.approx(500.0)
    pv = db.pv_samples_15m(noon, noon + 900)
    assert pv[noon] == pytest.approx(bell(12.0, 4000.0), rel=0.02)

    day_start = at_local(datetime(2026, 6, 16, tzinfo=KYIV).date(), 0, TZ)
    early, late = now - 7200, now + 7200
    for fetched, power in ((early, 1000.0), (late, 9999.0), (now - 3600, 2000.0)):
        for panel in PANELS:
            db.insert_forecast("open_meteo", panel, fetched, [SimpleNamespace(
                target_ts=day_start + 12 * 3600, gti=None, ghi=None, power_w=power, p10=None, p90=None, temp_c=None)])
    fc = db.forecast_asof(day_start, day_start + 86400, now)
    assert fc["open_meteo"][day_start + 12 * 3600]["p"] == pytest.approx(4000.0)  # последний fetched <= now, сумма по 2 панелям


def test_history_errors_cutoff(db):
    target = kyiv_ts(2026, 6, 14, 12, 0)
    cutoff = kyiv_ts(2026, 6, 13, 23, 0)
    for fetched, power in ((cutoff - 3600, 1000.0), (cutoff + 600, 7777.0)):
        db.insert_forecast("open_meteo", "p", fetched, [SimpleNamespace(
            target_ts=target, gti=None, ghi=None, power_w=power, p10=None, p90=None, temp_c=None)])
    db.insert_sample(target + 60, {"pv1_power_w": 600.0, "pv2_power_w": 400.0})
    rows = db.forecast_history_errors(kyiv_ts(2026, 6, 14, 0), kyiv_ts(2026, 6, 15, 0), TZ)
    assert len(rows) == 1
    assert rows[0]["forecast_w"] == pytest.approx(1000.0)
    assert rows[0]["actual_w"] == pytest.approx(1000.0)
    assert rows[0]["fetched_at"] == cutoff - 3600


def test_compute_plausible(seeded):
    res = compute(seeded, FIXED_NOW)
    dec = res["decision"]
    assert 30 <= dec.target_pct <= 100
    assert dec.explanation
    assert res["night_date"].isoformat() == "2026-06-15"
    assert set(res["providers"]) == {"open_meteo", "forecast_solar"}
    assert sum(res["weights"].values()) == pytest.approx(1.0)


def test_final_saved_once(seeded):
    assert compute_and_save(seeded, FIXED_NOW) is True
    assert compute_and_save(seeded, FIXED_NOW + 60) is False
    assert count_decisions(seeded._dsn) == 1
    row = seeded.latest_final_decision()
    assert row["status"] == "advice" and row["is_final"]
    assert row["inputs"]["params"]["tz"] == TZ
    assert row["tou_plan"][0]["time"] == "00:00"


def test_scheduler_tick_respects_mode_and_time(seeded):
    sched = PlannerScheduler(seeded, TZ)
    asyncio.run(sched.tick(kyiv_ts(2026, 6, 15, 22, 30)))
    assert count_decisions(seeded._dsn) == 0
    asyncio.run(sched.tick(FIXED_NOW))
    asyncio.run(PlannerScheduler(seeded, TZ).tick(FIXED_NOW + 120))
    assert count_decisions(seeded._dsn) == 1
    with psycopg.connect(seeded._dsn, autocommit=True) as conn:
        conn.execute("TRUNCATE charge_decisions RESTART IDENTITY")
        conn.execute("UPDATE strategy_settings SET control_mode = 'off'")
    asyncio.run(PlannerScheduler(seeded, TZ).tick(FIXED_NOW))
    assert count_decisions(seeded._dsn) == 0


def test_evaluation_after_horizon(seeded):
    assert compute_and_save(seeded, FIXED_NOW)
    end = kyiv_ts(2026, 6, 16, 23, 30)
    seed_samples(seeded, FIXED_NOW, end)
    asyncio.run(PlannerScheduler(seeded, TZ).tick(end))
    row = seeded.latest_final_decision()
    assert row["actual_cost_uah"] is not None and row["actual_cost_uah"] > 0
    assert row["perfect_cost_uah"] is not None
    assert row["model_cost_decided_uah"] is not None
    assert row["regret_uah"] == pytest.approx(row["model_cost_decided_uah"] - row["perfect_cost_uah"])
    assert row["regret_uah"] >= -1e-6
    assert row["evaluated_at"] is not None


def test_partial_evaluation_morning(seeded):
    assert compute_and_save(seeded, FIXED_NOW)
    seed_samples(seeded, FIXED_NOW, kyiv_ts(2026, 6, 16, 8, 0))
    row = seeded.latest_final_decision()
    assert evaluate_decision(seeded, row, kyiv_ts(2026, 6, 16, 7, 10)) is None
    res = evaluate_decision(seeded, row, kyiv_ts(2026, 6, 16, 7, 40))
    assert res["actual"] > 0 and res["perfect"] is None and res["regret"] is None and res["decided"] is None


@pytest.fixture
def client(pg_url, db, monkeypatch):
    monkeypatch.setattr(main.Collector, "run", _noop, raising=True)
    monkeypatch.setattr(main.PlannerScheduler, "run", _noop, raising=True)
    with TestClient(main.app) as c:
        yield c


async def _noop(self):
    return None


def test_preview_does_not_write(client, db, pg_url):
    now = int(time.time())
    seed_samples(db, now - 15 * 86400, now)
    seed_forecast(db, now)
    resp = client.get("/api/plan/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["control_mode"] == "advise"
    d = body["decision"]
    assert d["preview"] is True and d["id"] is None
    assert 30 <= d["target_pct"] <= 100
    assert d["explanation"]
    assert count_decisions(pg_url) == 0
    again = client.get("/api/plan/preview").json()
    assert again["decision"]["created_at"] == d["created_at"]  # кэш 60 с
    assert client.get("/api/plan").json()["decision"] is None
    assert client.get("/api/plan/history").json() == {"decisions": []}


def test_preview_without_data_is_422(client):
    assert client.get("/api/plan/preview").status_code == 422


def test_plan_and_history_after_save(client, db, pg_url):
    now = int(time.time())
    seed_samples(db, now - 15 * 86400, now)
    seed_forecast(db, now)
    assert compute_and_save(db, now)
    resp = client.get("/api/plan").json()
    plan = resp["decision"]
    assert plan["is_final"] and plan["status"] == "advice"
    assert plan["applied_at"] is None and plan["apply_note"] == "" and plan["write_request_id"] is None
    assert resp["apply"]["status"] == "advice" and resp["apply"]["write_request_status"] is None
    assert "apply" in client.get("/api/plan/preview").json()
    hist = client.get("/api/plan/history", params={"days": 30}).json()["decisions"]
    assert [h["id"] for h in hist] == [plan["id"]]


def test_actual_cost_scales_for_gaps(db):
    t23 = kyiv_ts(2026, 6, 15, 23, 0)
    slots = list(range(t23, t23 + 8 * 3600, 900))
    price = TariffPrice(db, TZ)
    full = {s: 0.1 for s in slots}
    cost, cov = actual_import_cost(full, slots, price)
    assert cov == 1.0 and cost == pytest.approx(32 * 0.1 * 2.16)
    holey = {s: 0.1 for i, s in enumerate(slots) if i % 4 != 0}  # 75 % покрытия
    cost, cov = actual_import_cost(holey, slots, price)
    assert cov == pytest.approx(0.75)
    assert cost == pytest.approx(32 * 0.1 * 2.16)
    sparse = {s: 0.1 for i, s in enumerate(slots) if i % 2 == 0}  # 50 %
    cost, cov = actual_import_cost(sparse, slots, price)
    assert cost is None and cov == pytest.approx(0.5)


def test_tick_one_bad_row_does_not_stop_others(seeded, monkeypatch):
    from app import planner_service

    assert compute_and_save(seeded, FIXED_NOW)
    with psycopg.connect(seeded._dsn, autocommit=True) as conn:
        conn.execute("UPDATE charge_decisions SET night_date = '2026-06-14'")
    assert compute_and_save(seeded, FIXED_NOW)
    calls = []

    def flaky(db, row, now_ts):
        calls.append(row["night_date"])
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"actual": 1.0, "coverage_pct": 100.0, "perfect": None, "regret": None, "decided": None}

    monkeypatch.setattr(planner_service, "evaluate_decision", flaky)
    asyncio.run(PlannerScheduler(seeded, TZ).tick(kyiv_ts(2026, 6, 17, 12, 0)))
    assert len(calls) == 2
    with psycopg.connect(seeded._dsn) as conn:
        assert conn.execute("SELECT count(*) FROM charge_decisions WHERE evaluated_at IS NOT NULL").fetchone()[0] == 0


TOU_REGS = {
    "148": 0, "149": 655, "150": 700, "151": 1700, "152": 1800, "153": 2305,
    "166": 50, "167": 50, "168": 50, "169": 50, "170": 50, "171": 50,
    "172": 1, "173": 1, "174": 0, "175": 0, "176": 0, "177": 1,
}
STRATEGY = {
    "battery_capacity_kwh": 24, "battery_usable_pct": 95, "round_trip_efficiency_pct": 90, "export_price_per_kwh": 0,
    "reserve_soc_pct": 30, "emergency_mode": "off", "normal_floor_pct": 30, "emergency_floor_pct": 50,
    "max_grid_charge_soc_pct": 80, "wear_cost_per_kwh": 0.4, "risk": "balanced",
    "default_plan_soc_pct": 60,
}


def test_floor_slots_from_regs_and_mismatch():
    slots = tou_floor_slots(TOU_REGS)
    assert [s["start"] for s in slots] == ["00:00", "06:55", "07:00", "17:00", "18:00", "23:05"]
    assert [s["grid_charge"] for s in slots] == [True, True, False, False, False, True]
    assert floor_mismatch(slots, 30) is True
    assert floor_mismatch(slots, 50) is False
    regs = dict(TOU_REGS, **{"168": 30, "169": 30, "170": 30})
    assert floor_mismatch(tou_floor_slots(regs), 30) is False  # ночные (сеть) пороги не влияют на флаг
    assert floor_mismatch(None, 30) is False
    assert tou_floor_slots({k: v for k, v in TOU_REGS.items() if k != "170"}) is None


def test_build_params_floor_by_emergency_mode():
    off = dict(STRATEGY, emergency_mode="off", normal_floor_pct=15, emergency_floor_pct=50, max_grid_charge_soc_pct=100)
    p = build_params(off, TOU_REGS)
    assert (p.reserve_soc_pct, p.floor_pct, p.night_ceiling_pct) == (15.0, 15.0, 100.0)
    assert p.floor_profile == [(0, 1440, 15.0)] and p.floor_slots is None
    assert all(p.floor_pct_at(m) == 15.0 for m in (0, 7 * 60, 12 * 60, 1439))  # слоты снимка (50) пол не задают
    p = build_params(dict(off, emergency_mode="always"), TOU_REGS)
    assert (p.reserve_soc_pct, p.floor_pct, p.night_ceiling_pct) == (50.0, 50.0, 100.0)
    assert all(p.floor_pct_at(m) == 50.0 for m in (0, 7 * 60, 12 * 60, 1439))
    assert build_params(off, {}).floor_profile == [(0, 1440, 15.0)]
    # normal_floor меняется только в обычном режиме
    assert build_params(dict(off, normal_floor_pct=20, emergency_mode="always"), {}).floor_pct == 50.0
    assert build_params(dict(off, normal_floor_pct=20), {}).floor_pct == 20.0


def test_compute_floor_from_settings_not_inverter(seeded):
    seeded.maybe_insert_settings(FIXED_NOW - 600, {int(k): v for k, v in TOU_REGS.items()})
    res = compute(seeded, FIXED_NOW)
    assert res["params"].floor_slots is None and res["params"].floor_profile == [(0, 1440, 30.0)]
    inp = _inputs_summary(res)
    assert inp["reserve_soc_pct"] == 30
    assert inp["floor_mismatch"] is True
    assert [s["soc_pct"] for s in inp["floor_by_slot"]] == [50.0] * 6
    assert any("система выставит 30 %" in w for w in inp["warnings"])
    hp = inp["hourly_plan"]
    assert len(hp["hours"]) == 24 and hp["hours"][0]["hour"] == "23:00"
    assert sum(h["grid_import_kwh"] for h in hp["hours"]) == pytest.approx(hp["total_import_kwh"], abs=0.01)
    assert sum(h["cost_uah"] for h in hp["hours"]) == pytest.approx(hp["total_cost_uah"], abs=0.05)
    # пол 30 % во все часы (слоты инвертора с 50 % на пол не влияют)
    assert min(h["soc_end_pct"] for h in hp["hours"]) >= 30.0 - 1e-6


def test_inputs_summary_day_import_fields(seeded):
    res = compute(seeded, FIXED_NOW)
    dec = res["decision"]
    dec.warnings.append("днём, вероятно, купим 2 кВт·ч")
    dec.warnings.append("днём, вероятно, купим 2 кВт·ч")
    inp = _inputs_summary(res)
    for k in ("day_import_prob", "expected_day_import_kwh", "day_import_tol"):
        assert isinstance(inp[k], float)
    assert isinstance(inp["day_import_constraint_met"], bool)
    assert inp["candidate_day_import_prob"]
    assert all(isinstance(k, str) for k in inp["candidate_day_import_prob"])
    assert inp["warnings"].count("днём, вероятно, купим 2 кВт·ч") == 1
    import json
    json.dumps(inp)


def test_compute_emergency_floor_matches_inverter(seeded):
    st = dict(seeded.strategy_settings())
    assert st["emergency_mode"] == "off"
    with psycopg.connect(seeded._dsn, autocommit=True) as conn:
        conn.execute("UPDATE strategy_settings SET emergency_mode='always', emergency_floor_pct=50")
    seeded.maybe_insert_settings(FIXED_NOW - 600, {int(k): v for k, v in TOU_REGS.items()})
    res = compute(seeded, FIXED_NOW)
    assert res["params"].floor_pct == 50.0 and res["params"].night_ceiling_pct == 80.0
    inp = _inputs_summary(res)
    assert inp["floor_mismatch"] is False and inp["reserve_soc_pct"] == 50.0
    hp = inp["hourly_plan"]
    assert min(h["soc_end_pct"] for h in hp["hours"]) >= 50.0 - 1e-6 or hp["hours"][0]["soc_end_pct"] < 50.0


def test_compute_without_snapshot_uses_settings_floor(seeded):
    res = compute(seeded, FIXED_NOW)
    assert res["params"].floor_slots is None
    inp = _inputs_summary(res)
    assert inp["floor_by_slot"] is None and inp["floor_mismatch"] is False


def test_preview_has_hourly_plan(client, db):
    now = int(time.time())
    seed_samples(db, now - 15 * 86400, now)
    seed_forecast(db, now)
    d = client.get("/api/plan/preview").json()["decision"]
    hp = d["hourly_plan"]
    assert hp == d["inputs"]["hourly_plan"]
    assert hp["hours"] and {"ts", "hour", "pv_kwh", "load_kwh", "soc_end_pct", "grid_import_kwh", "cost_uah"} <= set(hp["hours"][0])
    assert sum(h["grid_import_kwh"] for h in hp["hours"]) == pytest.approx(hp["total_import_kwh"], abs=0.01)


def test_forecast24_shape_and_band(seeded):
    out = compute_forecast24(seeded, FIXED_NOW)
    assert out["slot_sec"] == 900 and out["target_source"] == "planner"
    assert len(out["points"]) == 96
    assert out["points"][1]["ts"] - out["points"][0]["ts"] == 900
    assert out["target_plan_pct"] == pytest.approx(compute(seeded, FIXED_NOW)["decision"].target_pct, abs=0.01)
    assert set(out["totals"]) == {"as_is", "plan"} and set(out["providers_kwh"]) == {"open_meteo", "forecast_solar"}
    for p in out["points"]:
        assert p["soc_lo"] <= p["soc_plan"] <= p["soc_hi"]
        assert p["pv_min_w"] <= p["pv_w"] <= p["pv_max_w"]
    # нет TOU-снимка: as_is без ночного заряда
    assert out["target_as_is_pct"] == 0 and any("as_is" in w for w in out["warnings"])


def test_forecast24_hourly_energy(seeded):
    out = compute_forecast24(seeded, FIXED_NOW)
    hh, e = out["hourly"], out["energy_24h"]
    assert 24 <= len(hh) <= 25 and e
    for k, v in e.items():
        assert sum(h[k] for h in hh) == pytest.approx(v, abs=0.01), k
    for h in hh:
        assert h["load_from_pv_kwh"] + h["load_from_batt_kwh"] + h["load_from_grid_kwh"] == pytest.approx(h["load_kwh"], abs=0.01)
        assert h["batt_charge_from_pv_kwh"] + h["batt_charge_from_grid_kwh"] == pytest.approx(h["batt_charge_kwh"], abs=0.01)
        assert h["batt_charge_from_grid_kwh"] >= 0 and h["load_from_grid_kwh"] >= 0
    assert e["grid_import_kwh"] == pytest.approx(out["totals"]["plan"]["import_kwh"], abs=0.05)


def test_forecast24_what_if_changes_plan(seeded):
    base = compute_forecast24(seeded, FIXED_NOW)
    low = compute_forecast24(seeded, FIXED_NOW, target_pct=30)
    high = compute_forecast24(seeded, FIXED_NOW, target_pct=100)
    assert high["target_source"] == "what_if" and high["target_plan_pct"] == 100
    at7 = lambda o: next(p["soc_plan"] for p in o["points"] if p["ts"] == kyiv_ts(2026, 6, 16, 7, 0))
    assert at7(high) > at7(low)
    assert high["totals"]["plan"]["import_kwh"] > low["totals"]["plan"]["import_kwh"]
    assert [p["soc_as_is"] for p in high["points"]] == [p["soc_as_is"] for p in base["points"]]


def test_forecast24_without_data_is_value_error(db):
    with pytest.raises(ValueError):
        compute_forecast24(db, FIXED_NOW)


def test_forecast_api(client, db):
    assert client.get("/api/plan/forecast").status_code == 422
    now = int(time.time())
    seed_samples(db, now - 15 * 86400, now)
    seed_forecast(db, now)
    resp = client.get("/api/plan/forecast")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["points"]) == 96
    r2 = client.get("/api/plan/forecast?target=70&hours=6")
    assert r2.status_code == 200 and len(r2.json()["points"]) == 24 and r2.json()["target_plan_pct"] == 70
    assert client.get("/api/plan/forecast?target=101").status_code == 422
    assert client.get("/api/plan/forecast?hours=5").status_code == 422


def test_forecast24_as_is_uses_inverter_night_slot(seeded):
    seeded.maybe_insert_settings(FIXED_NOW - 600, {int(k): v for k, v in TOU_REGS.items()})
    out = compute_forecast24(seeded, FIXED_NOW)
    assert out["target_as_is_pct"] == 50
    assert not any("as_is" in w for w in out["warnings"])


def test_forecast24_as_is_picks_slot_at_2330_and_warns(seeded):
    regs = dict(TOU_REGS, **{"166": 60})  # 00:00 -> 60 %, 23:05 -> 50 %
    seeded.maybe_insert_settings(FIXED_NOW - 600, {int(k): v for k, v in regs.items()})
    out = compute_forecast24(seeded, FIXED_NOW)
    assert out["target_as_is_pct"] == 50
    assert any("Ночные слоты TOU различаются" in w and "23:05" in w for w in out["warnings"])


def test_forecast24_band_without_providers_equals_p50(seeded, monkeypatch):
    from app import planner_service
    orig = planner_service._gather

    def gather(*a, **kw):
        g = orig(*a, **kw)
        g["fc"] = {p: {} for p in g["fc"]}
        return g

    monkeypatch.setattr(planner_service, "_gather", gather)
    out = compute_forecast24(seeded, FIXED_NOW)
    assert max(p["pv_w"] for p in out["points"]) > 0
    for p in out["points"]:
        assert p["pv_min_w"] == p["pv_w"] == p["pv_max_w"]
    assert all(v == 0 for v in out["providers_kwh"].values())


def test_forecast24_what_if_does_not_call_plan_night(seeded, monkeypatch):
    from app import planner_service

    def boom(*a, **kw):
        raise AssertionError("plan_night не должен вызываться")

    monkeypatch.setattr(planner_service, "plan_night", boom)
    out = compute_forecast24(seeded, FIXED_NOW, target_pct=40)
    assert out["target_source"] == "what_if" and out["target_plan_pct"] == 40


def _final_rows(url):
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT id, is_final, status, apply_note, inputs->'settings_fp' FROM charge_decisions ORDER BY id"
        ).fetchall()


def _set_strategy(url, sql_set):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"UPDATE strategy_settings SET {sql_set}")


def test_decision_stores_settings_fingerprint(seeded):
    assert compute_and_save(seeded, FIXED_NOW)
    fp = seeded.final_decision_meta(FIXED_NOW_NIGHT)["settings_fp"]
    assert fp["emergency_mode"] == "off" and fp["floor"] == 30 and fp["max_grid_charge_soc_pct"] == 80
    assert fp["risk"] == "balanced"


FIXED_NOW_NIGHT = datetime(2026, 6, 15).date()


def test_redecide_on_settings_change_in_window(seeded):
    assert compute_and_save(seeded, FIXED_NOW)
    asyncio.run(PlannerScheduler(seeded, TZ).tick(FIXED_NOW + 60))
    assert len(_final_rows(seeded._dsn)) == 1  # отпечаток тот же — не пересчитываем
    _set_strategy(seeded._dsn, "emergency_mode = 'always'")
    asyncio.run(PlannerScheduler(seeded, TZ).tick(FIXED_NOW + 120))
    (old_id, old_final, old_status, old_note, _), (new_id, new_final, new_status, _, new_fp) = _final_rows(seeded._dsn)
    assert (old_final, old_status) == (False, "skipped") and "изменились настройки" in old_note
    assert new_final and new_status == "advice" and new_fp["emergency_mode"] == "always" and new_fp["floor"] == 50
    assert seeded.final_decision_meta(FIXED_NOW_NIGHT)["id"] == new_id
    asyncio.run(PlannerScheduler(seeded, TZ).tick(FIXED_NOW + 180))
    assert len(_final_rows(seeded._dsn)) == 2  # повторно не пересчитывается


def test_redecide_not_outside_window_or_when_queued(seeded):
    assert compute_and_save(seeded, FIXED_NOW)
    _set_strategy(seeded._dsn, "emergency_mode = 'always'")
    asyncio.run(PlannerScheduler(seeded, TZ).tick(kyiv_ts(2026, 6, 15, 22, 30)))  # до decision_time
    asyncio.run(PlannerScheduler(seeded, TZ).tick(kyiv_ts(2026, 6, 15, 23, 40)))  # после 23:30
    assert len(_final_rows(seeded._dsn)) == 1
    with psycopg.connect(seeded._dsn, autocommit=True) as conn:
        conn.execute("UPDATE charge_decisions SET status = 'queued'")
    assert redecide_if_settings_changed(seeded, seeded.strategy_settings(), FIXED_NOW_NIGHT, FIXED_NOW + 120) is False
    rows = _final_rows(seeded._dsn)
    assert len(rows) == 1 and rows[0][1] and rows[0][2] == "queued"
