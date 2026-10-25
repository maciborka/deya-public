"""Переход часов Europe/Kyiv: осень 25.10.2026 (04:00 EEST → 03:00 EET, сутки 25 ч)
и весна 28.03.2027 (03:00 EET → 04:00 EEST, сутки 23 ч). Без БД и сети."""
import asyncio
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app import main
from app import planner_service as ps
from app.db import Database
from app.flows import integrate_day_import
from app.forecast import scheduler as fsched
from app.planner.load import build_load_profile, expand_profile
from app.planner.optimize import _night_window, hourly_plan
from app.planner.params import PlannerParams
from app.planner.sim import simulate
from app.planner.timeutil import SLOT_SEC, at_local, local, slot_floor, slot_index

TZ_NAME = "Europe/Kyiv"
KYIV = ZoneInfo(TZ_NAME)
AUTUMN = date(2026, 10, 25)
SPRING = date(2027, 3, 28)
DAYS = [pytest.param(AUTUMN, 25, id="autumn"), pytest.param(SPRING, 23, id="spring")]
NIGHT, DAY = 2.16, 4.32
ZONES = [
    {"name": "День", "start_time": dtime(7), "end_time": dtime(23), "price_per_kwh": DAY, "direction": "import"},
    {"name": "Ночь", "start_time": dtime(23), "end_time": dtime(7), "price_per_kwh": NIGHT, "direction": "import"},
]
TARIFF = {"id": 1, "name": "Двухзонный", "currency": "UAH", "zones": ZONES}


def midnight(d: date) -> int:
    return int(datetime.combine(d, dtime(0), KYIV).timestamp())


def day_slots(d: date) -> list[int]:
    return list(range(midnight(d), midnight(d + timedelta(days=1)), SLOT_SEC))


def expected_price(ts: int) -> float:
    h = local(ts, KYIV).hour
    return NIGHT if h >= 23 or h < 7 else DAY


class FakeTariffDb:
    def current_tariff(self, at=None):
        return TARIFF


def fake_database() -> Database:
    db = object.__new__(Database)
    db.current_tariff = lambda at=None: TARIFF
    return db


# --- границы суток и ночное окно -------------------------------------------------------------

@pytest.mark.parametrize("d,hours", DAYS)
def test_local_day_length(d, hours):
    assert midnight(d + timedelta(days=1)) - midnight(d) == hours * 3600
    assert len(day_slots(d)) == hours * 4
    assert at_local(d + timedelta(days=1), 0, TZ_NAME) - at_local(d, 0, TZ_NAME) == hours * 3600


@pytest.mark.parametrize("d,hours", DAYS)
def test_local_slots_unique_and_aligned(d, hours):
    slots = day_slots(d)
    assert all(slot_floor(ts, KYIV) == ts for ts in slots)
    assert all(slot_floor(ts + 899, KYIV) == ts for ts in slots)
    assert all(local(ts, KYIV).date() == d for ts in slots)
    labels = [(local(ts, KYIV).hour, local(ts, KYIV).minute, local(ts, KYIV).utcoffset()) for ts in slots]
    assert len(set(labels)) == len(slots)


@pytest.mark.parametrize("night,hours", [(AUTUMN - timedelta(days=1), 9), (SPRING - timedelta(days=1), 7)])
def test_night_window_length(night, hours):
    t23 = at_local(night, 23, TZ_NAME)
    t07 = at_local(night + timedelta(days=1), 7, TZ_NAME)
    assert local(t23, KYIV).hour == 23 and local(t07, KYIV).hour == 7
    assert t07 - t23 == hours * 3600
    params = PlannerParams()
    for ts in range(t23, t07, SLOT_SEC):
        assert params.is_night(local(ts, KYIV).hour)
        assert ps.night_date_for(ts, params) == night
        assert _night_window(ts, params) == night
    assert ps.night_date_for(t07, params) == night + timedelta(days=1)


def test_repeated_hour_belongs_to_same_night():
    first = int(datetime(2026, 10, 25, 3, 30, tzinfo=KYIV).timestamp())
    second = int(datetime(2026, 10, 25, 3, 30, fold=1, tzinfo=KYIV).timestamp())
    assert second - first == 3600
    params = PlannerParams()
    assert ps.night_date_for(first, params) == ps.night_date_for(second, params) == date(2026, 10, 24)


# --- тариф -----------------------------------------------------------------------------------

@pytest.mark.parametrize("d,hours", DAYS)
def test_tariff_price_every_slot(d, hours):
    price = ps.TariffPrice(FakeTariffDb(), TZ_NAME)
    db = fake_database()
    slots = day_slots(d)
    for ts in slots:
        assert price(ts) == pytest.approx(expected_price(ts)), local(ts, KYIV)
        assert db.price_at(ts)["price_per_kwh"] == pytest.approx(expected_price(ts))
    night = sum(1 for ts in slots if price(ts) == NIGHT)
    assert len(slots) - night == 16 * 4  # день 07–23 всегда 16 ч
    assert night == (hours - 16) * 4


def test_tariff_repeated_hour_both_night():
    price = ps.TariffPrice(FakeTariffDb(), TZ_NAME)
    for fold in (0, 1):
        ts = int(datetime(2026, 10, 25, 3, 15, fold=fold, tzinfo=KYIV).timestamp())
        assert price(ts) == NIGHT


# --- импорт днём / потоки (API, границы суток) -----------------------------------------------

class FakeApiDb:
    def __init__(self, start, end):
        self.rows = [{"ts": t, "grid_power_w": 1000.0, "pv1_power_w": 0.0, "pv2_power_w": 0.0,
                      "load_power_w": 1000.0, "batt_power_w": 0.0} for t in range(start, end + 1, 60)]

    def current_tariff(self, at=None):
        return TARIFF

    def samples_between(self, a, b, limit, cols=None):
        return [r for r in self.rows if a <= r["ts"] <= b]


@pytest.mark.parametrize("d,hours", DAYS)
def test_api_day_bounds(d, hours, monkeypatch):
    start, end = midnight(d), midnight(d + timedelta(days=1))
    monkeypatch.setattr(main.app.state, "db", FakeApiDb(start, end), raising=False)
    monkeypatch.setattr(main.time, "time", lambda: end + 3600)
    client = TestClient(main.app)
    fl = client.get("/api/flows", params={"date": d.isoformat()}).json()
    assert fl["to_ts"] - fl["from_ts"] == hours * 3600
    assert fl["totals_kwh"]["grid_import"] == pytest.approx(hours, abs=0.01)
    di = client.get("/api/day-import", params={"date": d.isoformat()}).json()
    assert di["import_kwh"] == pytest.approx(16.0, abs=0.01)
    assert di["coverage"] == pytest.approx(1.0, abs=0.001)
    assert len(di["by_hour"]) == 24


@pytest.mark.parametrize("d,hours", DAYS)
def test_integrate_day_import_by_hour(d, hours):
    start, end = midnight(d), midnight(d + timedelta(days=1))
    rows = [{"ts": t, "grid_power_w": 1000.0} for t in range(start, end + 1, 60)]
    price = ps.TariffPrice(FakeTariffDb(), TZ_NAME)
    res = integrate_day_import(rows, start, end, price, DAY, lambda ts: local(ts, KYIV).hour)
    by = {h["hour"]: h["import_kwh"] for h in res["by_hour"]}
    assert all(by[h] == pytest.approx(1.0) for h in range(7, 23))
    assert all(by[h] == 0 for h in (*range(0, 7), 23))
    assert res["cost_uah"] == pytest.approx(16 * DAY, abs=0.05)


@pytest.mark.parametrize("today", [AUTUMN + timedelta(days=1), SPRING + timedelta(days=1)])
def test_daily_max_window_covers_requested_days(today, monkeypatch):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(today, dtime(12), tz)

    captured = {}
    db = object.__new__(Database)
    db._all = lambda sql, params: captured.setdefault("p", params) and [{"date": str(i)} for i in range(10)]
    monkeypatch.setattr(db_mod, "datetime", FakeDT)
    out = db.daily_max(3, TZ_NAME)
    assert captured["p"][1] <= midnight(today - timedelta(days=2))
    assert len(out) == 3


# --- почасовые бакеты плана ------------------------------------------------------------------

def _sim_day(d: date):
    params = PlannerParams()
    slots = day_slots(d)
    price = ps.TariffPrice(FakeTariffDb(), TZ_NAME)
    n = len(slots)
    r = simulate(50.0, slots[0], [0.0] * n, [1000.0] * n, params, price, 50.0, tou=True)
    return params, slots, price, r


@pytest.mark.parametrize("d,hours", DAYS)
def test_hourly_plan_buckets(d, hours):
    params, slots, price, r = _sim_day(d)
    hp = hourly_plan(r, params, price)
    assert len(hp) == hours
    assert len({h["ts"] for h in hp}) == hours
    assert all(h["load_kwh"] == pytest.approx(1.0) for h in hp)
    labels = [h["hour"] for h in hp]
    if hours == 25:
        assert labels.count("03:00") == 2
        a, b = [h["ts"] for h in hp if h["hour"] == "03:00"]
        assert b - a == 3600
    else:
        assert "03:00" not in labels
    assert sum(h["cost_uah"] for h in hp) == pytest.approx(r.cost_uah)


@pytest.mark.parametrize("d,hours", DAYS)
def test_forecast24_hourly_energy_buckets(d, hours):
    params, slots, price, r = _sim_day(d)
    hourly = ps._hourly_energy(r, slots, params, price, SLOT_SEC / 3600)
    assert len(hourly) == hours
    assert len({h["ts"] for h in hourly}) == hours
    assert [h["ts"] for h in hourly] == sorted(h["ts"] for h in hourly)
    assert all(h["load_kwh"] == pytest.approx(1.0) for h in hourly)
    assert sum(h["grid_import_kwh"] for h in hourly) == pytest.approx(r.grid_import_kwh, abs=0.01)


# --- профиль нагрузки ------------------------------------------------------------------------

@pytest.mark.parametrize("d,hours", DAYS)
def test_load_profile_across_dst(d, hours):
    now_ts = midnight(d + timedelta(days=3))
    samples = [(ts, 100.0 + slot_index(ts, KYIV)) for ts in range(now_ts - 14 * 86400, now_ts, SLOT_SEC)]
    prof = build_load_profile(samples, now_ts, TZ_NAME, days=14)
    assert not prof.fallback
    assert len(prof.p50) == 96
    assert prof.p50 == pytest.approx([100.0 + i for i in range(96)])
    series = expand_profile(prof.p50, midnight(d), hours * 4, TZ_NAME)
    assert len(series) == hours * 4
    assert series == pytest.approx([100.0 + slot_index(ts, KYIV) for ts in day_slots(d)])
    # после суток с переходом ряд снова начинается с полуночи следующей даты
    nxt = expand_profile(prof.p50, midnight(d + timedelta(days=1)), 1, TZ_NAME)
    assert nxt == [100.0]


# --- решение 22:45 ---------------------------------------------------------------------------

class FakePlannerDb:
    def __init__(self):
        self.saved: set = set()

    def strategy_settings(self):
        return {"control_mode": "advice", "decision_time": dtime(22, 45)}

    def final_decision_exists(self, night):
        return night in self.saved

    def decisions_to_evaluate(self, since):
        return []


@pytest.mark.parametrize("d", [AUTUMN, SPRING], ids=["autumn", "spring"])
def test_decision_fires_once_per_night(d, monkeypatch):
    db = FakePlannerDb()
    calls: list[int] = []

    def compute_and_save(_db, now_ts):
        night = ps.night_date_for(now_ts, PlannerParams())
        calls.append(now_ts)
        _db.saved.add(night)
        return True

    monkeypatch.setattr(ps, "compute_and_save", compute_and_save)
    for name in ("redecide_if_settings_changed", "auto_apply", "floor_sync"):
        monkeypatch.setattr(ps, name, lambda *a, **k: False)
    sched = ps.PlannerScheduler(db, TZ_NAME)
    start = int(datetime.combine(d - timedelta(days=1), dtime(12), KYIV).timestamp())
    end = int(datetime.combine(d + timedelta(days=1), dtime(12), KYIV).timestamp())

    async def run():
        for ts in range(start, end, ps.TICK_SEC):
            await sched.tick(ts)

    asyncio.run(run())
    got = [local(ts, KYIV) for ts in calls]
    assert [(t.date(), t.hour, t.minute) for t in got] == [(d - timedelta(days=1), 22, 45), (d, 22, 45)]


# --- планировщик прогнозов 05/11/17/21 -------------------------------------------------------

class _Stop(Exception):
    pass


@pytest.mark.parametrize("d", [AUTUMN, SPRING], ids=["autumn", "spring"])
def test_forecast_scheduler_runs_once_at_local_hours(d, monkeypatch):
    clock = {"t": float(datetime.combine(d - timedelta(days=1), dtime(12, 0, 30), KYIV).timestamp())}
    end = datetime.combine(d + timedelta(days=1), dtime(12), KYIV).timestamp()
    runs: list[float] = []

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock["t"], tz)

    async def fake_sleep(sec):
        clock["t"] += sec
        if clock["t"] > end:
            raise _Stop

    async def run_cycle(self):
        runs.append(clock["t"])

    monkeypatch.setattr(fsched, "datetime", FakeDT)
    monkeypatch.setattr(fsched, "asyncio", SimpleNamespace(sleep=fake_sleep, CancelledError=asyncio.CancelledError))
    monkeypatch.setattr(fsched.ForecastScheduler, "run_cycle", run_cycle)
    s = fsched.ForecastScheduler(None, TZ_NAME, [5, 11, 17, 21], 3)
    with pytest.raises(_Stop):
        asyncio.run(s.run())
    got = [local(int(t), KYIV) for t in runs[1:]]  # первый запуск — при старте
    prev, cur = d - timedelta(days=1), d
    expected = [(prev, 17), (prev, 21), (cur, 5), (cur, 11), (cur, 17), (cur, 21), (d + timedelta(days=1), 5),
                (d + timedelta(days=1), 11)]
    assert [(t.date(), t.hour) for t in got] == expected
    assert all(t.minute == 0 and t.second == 0 for t in got)
