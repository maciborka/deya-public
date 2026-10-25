import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.planner.load import build_load_profile, expand_profile
from app.planner.optimize import evaluate_target, perfect_forecast_cost, plan_night
from app.planner.params import PlannerParams
from app.planner.pv import ForecastErr, bias_correction, consensus, provider_weights, scenarios
from app.planner.sim import simulate
from app.planner.timeutil import slot_floor, slot_index

KYIV = ZoneInfo("Europe/Kyiv")
SLOT = 900


def ts_at(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=KYIV).timestamp())


def price(ts):
    h = datetime.fromtimestamp(ts, KYIV).hour
    return 2.16 if h >= 23 or h < 7 else 4.32


def pv_days(start_ts, n_slots, daily_kwh: dict):
    """Синусоида 07:00–19:00, суточная энергия по локальной дате."""
    peak_sum = sum(math.sin(math.pi * (k + 0.5) / 48) for k in range(48))
    out = {}
    for i in range(n_slots):
        ts = start_ts + i * SLOT
        dt = datetime.fromtimestamp(ts, KYIV)
        t = dt.hour + dt.minute / 60
        kwh = daily_kwh.get(dt.date(), 0.0)
        out[ts] = 0.0 if not 7 <= t < 19 else kwh * 1000 / 0.25 / peak_sum * math.sin(math.pi * (t - 7 + 0.125) / 12)
    return out


def pv_for(now, kwh_tomorrow, kwh_day2):
    d0 = datetime.fromtimestamp(now, KYIV).date()
    days = {d0 + timedelta(days=1): kwh_tomorrow}
    if kwh_day2 is not None:
        days[d0 + timedelta(days=2)] = kwh_day2
    pv = pv_days(slot_floor(now, KYIV), 4 * 24 * 3, days)
    last = max(days)
    return {ts: w for ts, w in pv.items() if datetime.fromtimestamp(ts, KYIV).date() <= last}


def run_plan(soc, kwh_tomorrow, kwh_day2, params=None, load_w=600.0, now=None, pv=None):
    params = params or PlannerParams()
    now = now or ts_at(2026, 10, 21, 22, 45)
    pv = pv if pv is not None else pv_for(now, kwh_tomorrow, kwh_day2)
    dec = plan_night(now, soc, scenarios(pv), [load_w] * 96, [load_w * 1.25] * 96, params, price)
    # выбранный target — argmin стоимости среди допустимых по ограничению дневного импорта
    feasible = {c: v for c, v in dec.candidate_costs.items()
                if dec.candidate_day_import_prob[c] <= dec.day_import_tol + 1e-9}
    assert dec.day_import_constraint_met == bool(feasible)
    if feasible and dec.margin_uah_per_kwh > 0:
        assert dec.expected_cost_uah <= min(feasible.values()) + 1e-6
    if not feasible:
        assert dec.day_import_prob == min(dec.candidate_day_import_prob.values())
    return dec


# --- sim ---

def test_sim_energy_conservation_and_limits():
    p = PlannerParams()
    start = ts_at(2026, 10, 21, 20)
    n = 4 * 24
    pv = list(pv_days(start, n, {datetime(2026, 10, 22).date(): 70.0}).values())
    load = [300 + 2500 * ((i * 7) % 5 == 0) for i in range(n)]
    r = simulate(25.0, start, pv, load, p, price, 80.0, tou=True)
    used_pv = r.pv_kwh - r.curtailed_kwh
    assert r.grid_import_kwh + used_pv + r.battery_discharge_kwh == pytest.approx(
        r.load_kwh + r.battery_charge_kwh + r.grid_export_kwh, abs=1e-6
    )
    stored = r.battery_charge_kwh * p.eta_charge - r.battery_discharge_kwh / p.eta_discharge
    assert (r.end_soc - 25.0) * p.kwh_per_pct == pytest.approx(stored, abs=1e-6)
    assert all(0 <= s <= 100 for s in r.soc_pct)
    max_step = max(b - a for a, b in zip(r.soc_pct, r.soc_pct[1:]))
    assert max_step * p.kwh_per_pct <= p.max_charge_w * 0.25 / 1000 * p.eta_charge + 1e-9
    assert r.cost_uah == pytest.approx(sum(imp * price(ts) for imp, ts in zip(r.import_by_slot, r.slot_ts)))


def test_sim_night_grid_charge_limit_and_midnight():
    p = PlannerParams()
    start = ts_at(2026, 10, 21, 23)
    n = 8 * 4
    r = simulate(30.0, start, [0.0] * n, [0.0] * n, p, price, 80.0)
    step = p.night_charge_limit_w * 0.25 / 1000 * p.eta_charge / p.kwh_per_pct
    assert r.soc_pct[1] - r.soc_pct[0] == pytest.approx(step)
    assert r.end_soc == pytest.approx(80.0)
    after_midnight = r.slot_ts.index(ts_at(2026, 10, 22, 0))
    assert r.soc_pct[after_midnight + 1] > r.soc_pct[after_midnight]
    assert r.grid_charge_kwh == pytest.approx(50 * p.kwh_per_pct / p.eta_charge)


def test_sim_clamp_and_floor():
    p = PlannerParams()
    start = ts_at(2026, 10, 22, 8)
    r = simulate(150.0, start, [0.0] * 40, [11000.0] * 40, p, price, 30.0)
    assert r.soc_pct[0] == 100.0
    assert r.min_soc == pytest.approx(p.floor_pct)
    assert r.reserve_hits_slots > 0
    small = PlannerParams(inverter_max_w=2000.0)
    r2 = simulate(-5.0, start, [20000.0] * 4, [0.0] * 4, small, price, 30.0)
    assert r2.soc_pct[0] == 0.0 and r2.curtailed_kwh > 0
    assert r2.pv_kwh == pytest.approx(4 * small.pv_max_w * 0.25 / 1000)
    assert r2.grid_export_kwh == pytest.approx(4 * 2000.0 * 0.25 / 1000)


def test_sim_night_holds_target_when_above():
    p = PlannerParams()
    start = ts_at(2026, 10, 21, 23)
    r = simulate(70.0, start, [0.0] * 32, [2000.0] * 32, p, price, 60.0)
    assert r.min_soc == pytest.approx(60.0)
    assert r.grid_charge_kwh == 0.0


# --- optimize ---

def test_sunny_tomorrow_low_target():
    dec = run_plan(50.0, 60.0, 60.0)
    assert dec.target_pct <= 40
    assert dec.tomorrow_full_p50 and not dec.epsilon_applied
    assert len(dec.tou_plan) == 6
    times = [s.time for s in dec.tou_plan]
    assert times == sorted(times) and len(set(times)) == 6


def test_review_case_discharge_beats_hold():
    dec = run_plan(65.0, 18.0, 30.0, now=ts_at(2026, 10, 21, 23))
    assert dec.soc_at_23_pct == pytest.approx(65.0)
    assert dec.target_pct == 30.0
    assert dec.expected_cost_uah < dec.counterfactuals["hold"]
    assert dec.candidate_costs[62.0] > dec.expected_cost_uah
    assert not dec.epsilon_applied


def test_hold_candidate_considered():
    dec = run_plan(75.0, 8.0, 8.0, load_w=800.0)
    hold = round(dec.soc_at_23_pct - 3.0, 6)
    assert hold in dec.candidate_costs
    assert dec.expected_cost_uah <= dec.candidate_costs[hold] + 1e-6


def test_hourly_pv_same_as_15min():
    now = ts_at(2026, 10, 21, 22, 45)
    pv15 = pv_for(now, 12.0, 20.0)
    hourly: dict[int, list[float]] = {}
    for ts, w in pv15.items():
        hourly.setdefault(ts - ts % 3600, []).append(w)
    pv_h = {ts: sum(v) / len(v) for ts, v in hourly.items()}
    a = run_plan(50.0, 0, 0, now=now, pv=pv15)
    b = run_plan(50.0, 0, 0, now=now, pv=pv_h)
    assert a.target_pct == b.target_pct
    assert not b.pv_coverage_incomplete


def test_cloudy_day2_raises_target_vs_sunny_day2():
    cloudy = run_plan(40.0, 2.0, 2.0)
    sunny2 = run_plan(40.0, 2.0, 60.0)
    assert cloudy.target_pct > sunny2.target_pct
    assert cloudy.expected_cost_uah > sunny2.expected_cost_uah


def test_pv_coverage_and_day2_missing():
    now = ts_at(2026, 10, 21, 22, 45)
    dec = run_plan(50.0, 20.0, None, now=now)
    assert dec.day2_forecast_missing
    pv = pv_for(now, 20.0, 20.0)
    with pytest.raises(ValueError, match="Прогноз PV на завтра"):
        run_plan(50.0, 0, 0, now=now, pv={ts: w for ts, w in pv.items() if datetime.fromtimestamp(ts, KYIV).hour < 11})
    gap = ts_at(2026, 10, 22, 12)
    partial = {ts: w for ts, w in pv.items() if not gap <= ts < gap + 3600}
    assert run_plan(50.0, 0, 0, now=now, pv=partial).pv_coverage_incomplete


def test_cloudy_two_days_high_target():
    dec = run_plan(40.0, 2.0, 2.0)
    assert dec.target_pct >= 60
    assert dec.savings_vs_no_charge_uah > 0
    assert dec.tou_plan[0].soc_pct == dec.target_pct and dec.tou_plan[0].grid_charge


def test_negative_margin_no_charge_when_day_covered():
    """Маржа ≤ 0, днём солнца хватает — заряда от сети нет."""
    params = PlannerParams(wear_cost_per_kwh=3.0, **NORMAL)
    dec = run_plan(30.0, 60.0, 60.0, params=params)
    assert dec.margin_uah_per_kwh <= 0
    assert dec.day_import_constraint_met
    assert dec.target_pct <= max(params.reserve_soc_pct, dec.soc_at_23_pct)
    assert "Маржа" in dec.explanation


def test_negative_margin_still_charges_if_day_import():
    """Маржа ≤ 0 не отменяет ночной заряд, если без него днём покупаем из сети."""
    params = PlannerParams(wear_cost_per_kwh=3.0, **NORMAL)
    dec = run_plan(30.0, 2.0, 2.0, params=params)
    assert dec.margin_uah_per_kwh <= 0
    assert dec.target_pct > dec.soc_at_23_pct + 1
    assert dec.candidate_day_import_prob[round(dec.soc_at_23_pct, 6)] > dec.day_import_tol
    assert dec.day_import_constraint_met and dec.day_import_prob <= dec.day_import_tol
    assert "Маржа" in dec.explanation and "заряжаем" in dec.explanation


# --- ограничение дневного импорта ---

NORMAL = {"reserve_soc_pct": 15.0, "night_ceiling_pct": 100.0, "floor_profile": [(0, 1440, 15.0)]}
EMERGENCY = {"reserve_soc_pct": 50.0, "night_ceiling_pct": 100.0, "floor_profile": [(0, 1440, 50.0)]}


def test_day_import_sunny_low_target():
    dec = run_plan(50.0, 60.0, 60.0, params=PlannerParams(**NORMAL))
    assert dec.target_pct <= 30
    assert dec.day_import_constraint_met and dec.day_import_prob == 0
    assert dec.expected_day_import_kwh == pytest.approx(0.0, abs=1e-6)
    assert not dec.warnings


def test_day_import_cloudy_emergency_full_charge_and_warning():
    dec = run_plan(40.0, 0.0, 0.0, params=PlannerParams(**EMERGENCY), load_w=1000.0)
    assert dec.target_pct == 100.0
    assert not dec.day_import_constraint_met
    assert dec.expected_day_import_kwh > 0.5
    assert dec.warnings and "Даже при заряде до 100 %" in dec.warnings[0]
    assert dec.warnings[0] in dec.explanation


def test_day_import_emergency_floor_raises_target():
    normal = run_plan(30.0, 10.0, 10.0, params=PlannerParams(**NORMAL))
    emergency = run_plan(30.0, 10.0, 10.0, params=PlannerParams(**EMERGENCY))
    assert emergency.target_pct > normal.target_pct
    assert normal.day_import_constraint_met


def test_day_import_safe_stricter_than_balanced():
    for kwh in (6.0, 10.0, 14.0):
        bal = run_plan(30.0, kwh, kwh, params=PlannerParams(risk="balanced", **NORMAL))
        safe = run_plan(30.0, kwh, kwh, params=PlannerParams(risk="safe", **NORMAL))
        assert safe.day_import_tol < bal.day_import_tol
        assert safe.target_pct >= bal.target_pct
        assert safe.day_import_prob <= safe.day_import_tol or not safe.day_import_constraint_met


def test_day_import_uses_safety_margin(monkeypatch):
    """Запас пола +3 % (погрешность BMS) не снижает target и учитывается в вероятности дневного импорта."""
    import app.planner.optimize as opt
    with_margin = run_plan(30.0, 10.0, 10.0, params=PlannerParams(risk="safe", **NORMAL))
    monkeypatch.setattr(opt, "SAFETY_MARGIN_PCT", 0.0)
    no_margin = run_plan(30.0, 10.0, 10.0, params=PlannerParams(risk="safe", **NORMAL))
    assert with_margin.target_pct >= no_margin.target_pct
    assert all(with_margin.candidate_day_import_prob[c] >= no_margin.candidate_day_import_prob[c] - 1e-9
               for c in with_margin.candidate_day_import_prob)


def test_dst_horizon_grid():
    now = ts_at(2026, 10, 24, 22, 45)
    dec = run_plan(50.0, 10.0, 10.0, now=now)
    assert dec.horizon_start_ts == ts_at(2026, 10, 24, 23)
    assert dec.horizon_end_ts == ts_at(2026, 10, 25, 23)
    assert dec.n_slots == 25 * 4
    assert dec.n_slots * SLOT == dec.horizon_end_ts - dec.horizon_start_ts


# --- load ---

def test_load_fallback_on_little_data():
    now = ts_at(2026, 10, 21, 22)
    samples = [(now - 3 * 3600 + k * 60, 900.0) for k in range(180)]
    prof = build_load_profile(samples, now, KYIV)
    assert prof.fallback
    assert prof.p50 == [600.0] * 96 and prof.p75 == [750.0] * 96


def test_load_profile_interpolates_gaps_and_spread():
    now = ts_at(2026, 10, 21, 0)
    samples = []
    for day in range(1, 6):
        for i in range(96):
            if 40 <= i < 44:
                continue
            ts = now - day * 86400 + i * SLOT
            samples.append((ts, 500.0 + 100 * day + (1000.0 if i >= 44 else 0.0)))
    prof = build_load_profile(samples, now, KYIV)
    assert not prof.fallback and prof.days == 5
    assert prof.p50[39] < prof.p50[41] < prof.p50[44]
    assert all(b >= a for a, b in zip(prof.p50, prof.p75))
    assert prof.p75[0] > prof.p50[0]
    assert prof.p50[0] == 800.0  # взвешенная медиана по дням
    assert prof.p75[0] == 900.0


def test_load_profile_two_days_p75_factor():
    now = ts_at(2026, 10, 21, 0)
    samples = [(now - day * 86400 + i * SLOT, 400.0) for day in (1, 2) for i in range(96)]
    prof = build_load_profile(samples, now, KYIV)
    assert prof.p75[10] == pytest.approx(500.0)


def test_expand_profile_across_dst():
    profile = [float(i) for i in range(96)]
    start = ts_at(2026, 10, 25, 0)
    series = expand_profile(profile, start, 104, KYIV)
    assert len(series) == 104
    after = ts_at(2026, 10, 25, 3)  # EET, после перевода часов
    assert series[(after - start) // SLOT] == 12.0
    assert series[(ts_at(2026, 10, 26, 0) - start) // SLOT] == 0.0
    assert slot_index(ts_at(2026, 10, 25, 23, 45), KYIV) == 95


# --- pv ---

def test_consensus_weights_and_gaps():
    h0 = ts_at(2026, 10, 22, 12)
    fc = {"a": {h0: 1000.0, h0 + 3600: 2000.0}, "b": {h0: 2000.0}}
    cons = consensus(fc, {"a": 0.75, "b": 0.25})
    assert len(cons) == 8
    assert cons[h0] == pytest.approx(1250.0) and cons[h0 + 2700] == pytest.approx(1250.0)
    assert cons[h0 + 3600] == pytest.approx(2000.0)
    assert consensus(fc)[h0] == pytest.approx(1500.0)


def test_provider_weights():
    base = ts_at(2026, 10, 1, 12)
    hist = []
    for day in range(8):
        t = base + day * 86400
        hist.append(ForecastErr("good", t, t - 16 * 3600, 1100.0, 1000.0))
        hist.append(ForecastErr("bad", t, t - 16 * 3600, 1300.0, 1000.0))
        hist.append(ForecastErr("bad", t, t - 2 * 3600, 1000.0, 1000.0))  # тот же день — не учитывается
    w = provider_weights(hist)
    assert w["good"] == pytest.approx(0.9) and w["bad"] == pytest.approx(0.1)
    short = provider_weights(hist[:6], providers=["good", "bad", "solcast"])
    assert all(v == pytest.approx(1 / 3) for v in short.values()) and len(short) == 3
    assert provider_weights([], providers=["a", "b"]) == {"a": 0.5, "b": 0.5}


def test_scenarios_and_bias():
    h0 = ts_at(2026, 10, 22, 12)
    cons = {h0 + k * SLOT: 1000.0 for k in range(8)}
    sc = scenarios(cons)
    assert sc["p10"][h0] == pytest.approx(550.0) and sc["p90"][h0] == pytest.approx(1200.0)
    sc2 = scenarios(cons, {h0: 100.0, h0 + 3600: 900.0}, {h0: 1000.0, h0 + 3600: 1000.0}, {h0: 3000.0, h0 + 3600: 1100.0})
    assert sc2["p10"][h0] == pytest.approx(200.0) and sc2["p90"][h0] == pytest.approx(2000.0)
    assert sc2["p10"][h0 + 3600] == pytest.approx(900.0)
    f = bias_correction({10: 3.0, 11: 0.1})
    assert f[10] == 1.5 and f[11] == 0.5 and f[0] == 1.0


# --- evaluate_target / regret ---

def test_evaluate_target_matches_perfect_and_regret_nonnegative():
    now = ts_at(2026, 10, 21, 22, 45)
    params = PlannerParams()
    pv = pv_for(now, 18.0, 25.0)
    load = [600.0] * 96
    perfect = perfect_forecast_cost(now, 40.0, pv, load, params, price)
    assert evaluate_target(now, 40.0, pv, load, params, price, perfect.target_pct) == pytest.approx(
        perfect.expected_cost_uah, abs=1e-6
    )
    for target in (30.0, 45.0, 60.0, 80.0, 100.0):
        assert evaluate_target(now, 40.0, pv, load, params, price, target) - perfect.expected_cost_uah >= -1e-6


def test_evaluate_target_regret_zero_when_forecast_equals_fact():
    now = ts_at(2026, 10, 21, 22, 45)
    params = PlannerParams()
    pv = pv_for(now, 8.0, 20.0)
    dec = plan_night(now, 40.0, {"p10": pv, "p50": pv, "p90": pv}, [600.0] * 96, [600.0] * 96, params, price)
    perfect = perfect_forecast_cost(now, 40.0, pv, [600.0] * 96, params, price)
    decided = evaluate_target(now, 40.0, pv, [600.0] * 96, params, price, dec.target_pct)
    assert decided - perfect.expected_cost_uah == pytest.approx(0.0, abs=1e-6)


# --- профиль пола ---

# дневные окна с разными полами + 50 % ночью (окно [start, end) в минутах суток)
PROFILE = [(0, 420, 50), (420, 1020, 20), (1020, 1080, 60), (1080, 1385, 40), (1385, 1440, 50)]


def test_params_floor_by_time_of_day():
    p = PlannerParams(floor_profile=PROFILE)
    assert p.floor_pct_at(0) == 50
    assert p.floor_pct_at(419) == 50 and p.floor_pct_at(7 * 60) == 20
    assert p.floor_pct_at(17 * 60 + 30) == 60
    assert p.floor_pct_at(23 * 60 + 10) == 50
    assert p.floor_pct_at(1439) == 50
    assert PlannerParams(floor_profile=[(0, 1440, 1)]).floor_pct_at(0) == 5  # не ниже shutdown/usable
    # вне окон профиля — резерв; без профиля — резерв
    assert PlannerParams(floor_profile=[(60, 120, 70)]).floor_pct_at(600) == PlannerParams().floor_pct
    assert PlannerParams().floor_pct_at(600) == PlannerParams().floor_pct
    # пересечение окон — наибольший пол
    assert PlannerParams(floor_profile=[(0, 1440, 15), (600, 700, 50)]).floor_pct_at(650) == 50
    # floor_slots (устарело) на пол не влияют
    assert PlannerParams(floor_slots=[[0, 90]]).floor_pct_at(0) == PlannerParams().floor_pct
    # профиль «всегда» — одно окно на сутки
    p = PlannerParams(reserve_soc_pct=50, floor_profile=[(0, 1440, 50)])
    assert {p.floor_pct_at(m) for m in range(0, 1440, 15)} == {50}


def test_sim_respects_floor_profile():
    p = PlannerParams(floor_profile=PROFILE)
    start = ts_at(2026, 10, 21, 23)
    n = 96
    r = simulate(90.0, start, [0.0] * n, [3000.0] * n, p, price, 30.0, tou=False)
    for i, ts in enumerate(r.slot_ts):
        dt = datetime.fromtimestamp(ts, KYIV)
        assert r.soc_pct[i + 1] >= min(p.floor_pct_at(dt.hour * 60 + dt.minute), r.soc_pct[i]) - 1e-6
    assert r.min_soc == pytest.approx(20.0)
    assert r.grid_import_kwh > 0
    assert sum(r.load_by_slot) == pytest.approx(r.load_kwh)


def test_sim_night_charge_slot_is_max_of_floor_and_target():
    """Ночной слот с зарядом: SOC держится не ниже max(пол, target)."""
    p = PlannerParams(reserve_soc_pct=50, floor_profile=[(0, 1440, 50)])
    start = ts_at(2026, 10, 21, 23)
    n = 32  # 23:00–07:00
    r = simulate(40.0, start, [0.0] * n, [500.0] * n, p, price, 60.0, tou=True)
    assert r.soc_pct[-1] >= 60.0 - 1e-6  # дозаряд до target
    r2 = simulate(90.0, start, [0.0] * n, [500.0] * n, p, price, 30.0, tou=True)
    assert min(r2.soc_pct) >= 50.0 - 1e-6  # target 30 < пол 50: ночью разряд только до пола


def test_plan_with_slot_floors_and_hourly():
    p = PlannerParams(floor_profile=PROFILE)
    dec = run_plan(60.0, 10.0, 10.0, params=p)
    h = dec.hourly
    assert len(h) == 24 and h[0]["hour"] == "23:00" and h[-1]["hour"] == "22:00"
    for x in h:
        assert x["soc_end_pct"] >= 20.0 - 1e-6
        assert x["cost_uah"] == pytest.approx(x["grid_import_kwh"] * price(x["ts"]))
