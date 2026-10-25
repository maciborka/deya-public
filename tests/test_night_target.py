"""Ручная ночная цель SOC (strategy_settings.night_target_*): выбор target, отпечаток настроек, окно до 06:30."""
import asyncio
from datetime import date
from types import SimpleNamespace

import psycopg
import pytest

from app.planner.optimize import plan_night
from app.planner.params import PlannerParams
from app.planner.pv import scenarios
from app import planner_service
from app.planner_service import (
    PlannerScheduler, _same_fp, apply_status, build_params, compute, compute_and_save, manual_night_target, serialize,
    settings_fingerprint, to_row,
)
from test_autowrite import REGS
from test_planner import EMERGENCY, NORMAL, price, pv_for, ts_at
from test_planner_service import FIXED_NOW, STRATEGY, TZ, db, kyiv_ts, seeded  # noqa: F401 — фикстуры

NIGHT = date(2026, 10, 21)
NOW = ts_at(2026, 10, 21, 22, 45)
ONCE = {"night_target_mode": "once", "night_target_manual_pct": 70, "night_target_manual_night": NIGHT}


def plan(manual=None, params=None, soc=40.0, kwh=10.0, load_w=600.0):
    pv = pv_for(NOW, kwh, kwh)
    return plan_night(NOW, soc, scenarios(pv), [load_w] * 96, [load_w * 1.25] * 96, params or PlannerParams(**NORMAL),
                      price, manual_target=manual)


def test_manual_night_target_modes():
    assert manual_night_target({}, NIGHT) is None
    assert manual_night_target({"night_target_mode": "auto", "night_target_manual_pct": 70}, NIGHT) is None
    assert manual_night_target(dict(ONCE), NIGHT) == 70.0
    assert manual_night_target(dict(ONCE, night_target_mode="always", night_target_manual_night=None), NIGHT) == 70.0
    # once на прошедшую ночь — истекло, авто
    assert manual_night_target(ONCE, date(2026, 10, 22)) is None
    assert manual_night_target(dict(ONCE, night_target_manual_pct=None), NIGHT) is None


def test_plan_manual_target_and_auto_for_comparison():
    auto = plan()
    dec = plan(manual=70)
    assert not auto.manual and auto.auto_target_pct is None
    assert dec.manual and dec.target_pct == 70 and dec.auto_target_pct == auto.target_pct
    assert dec.auto_day_import_prob == pytest.approx(auto.day_import_prob)
    assert dec.expected_cost_uah == pytest.approx(dec.candidate_costs[70])
    assert dec.day_import_prob == pytest.approx(dec.candidate_day_import_prob[70])
    assert all(t.soc_pct == 70 for t in dec.tou_plan if t.grid_charge)
    assert "Ручная ночная цель 70 %" in dec.explanation and f"{auto.target_pct:g} %" in dec.explanation


def test_plan_manual_target_clamped_to_floor_and_ceiling():
    assert plan(manual=5).target_pct == 15  # обычный пол
    assert plan(manual=20, params=PlannerParams(**EMERGENCY)).target_pct == 50  # аварийный пол
    assert plan(manual=95, params=PlannerParams(**dict(NORMAL, night_ceiling_pct=80.0))).target_pct == 80


def test_plan_manual_target_warning_when_day_import_likely():
    dec = plan(manual=50, params=PlannerParams(**EMERGENCY), kwh=0.0, load_w=1000.0)
    assert dec.day_import_prob > dec.day_import_tol and not dec.day_import_constraint_met
    assert len(dec.warnings) == 1 and dec.warnings[0].startswith("ручная цель: вероятность дневной докупки ")
    assert f"(авто дало бы {dec.auto_day_import_prob * 100:.0f} %)" in dec.warnings[0]
    ok = plan(manual=60, kwh=60.0)
    assert ok.day_import_constraint_met and ok.warnings == []


def test_fingerprint_includes_night_target():
    base = dict(STRATEGY)
    fp = settings_fingerprint(base)
    assert (fp["night_target_mode"], fp["night_target_manual_pct"], fp["night_target_manual_night"]) == ("auto", None, None)
    assert settings_fingerprint(dict(base, night_target_manual_pct=70)) == fp  # в авто значение не влияет
    once = settings_fingerprint(dict(base, **ONCE))
    assert once["night_target_manual_pct"] == 70 and once["night_target_manual_night"] == "2026-10-21"
    assert once != settings_fingerprint(dict(base, **dict(ONCE, night_target_manual_pct=60)))
    assert once != settings_fingerprint(dict(base, **dict(ONCE, night_target_manual_night=date(2026, 10, 22))))
    always = settings_fingerprint(dict(base, **dict(ONCE, night_target_mode="always")))
    assert always["night_target_manual_night"] is None and always != once and always != fp
    # решения до ручной цели (ключей нет) — равны авто, без лишнего пересчёта
    legacy = {k: v for k, v in fp.items() if not k.startswith("night_target")}
    assert _same_fp(legacy, fp) and not _same_fp(legacy, once) and not _same_fp(None, fp)


def _inputs(st: dict) -> dict:
    params = build_params(st, REGS)
    pv = pv_for(NOW, 10.0, 10.0)
    return {
        "st": st, "regs": REGS, "params": params, "soc": 40.0, "price": price, "night_date": NIGHT,
        "profile": SimpleNamespace(p50=[600.0] * 96, p75=[750.0] * 96, fallback=False, days=14),
        "fc": {"open_meteo": {}}, "weights": {"open_meteo": 1.0}, "cons": pv, "scen": scenarios(pv),
    }


@pytest.mark.parametrize("extra, manual", [
    ({}, False),
    (ONCE, True),
    (dict(ONCE, night_target_mode="always", night_target_manual_night=None), True),
    (dict(ONCE, night_target_manual_night=date(2026, 10, 20)), False),  # once истёк
])
def test_compute_selects_manual_target(extra, manual):
    st = dict(STRATEGY, **extra)
    row = to_row(compute(None, NOW, _inputs=_inputs(st)))
    inp = row["inputs"]
    assert inp["manual"] is manual and inp["late_apply"] is manual
    out = serialize(row)
    assert out["manual"] is manual
    if manual:
        assert row["target_pct"] == 70 and inp["auto_target_pct"] is not None and out["auto_target_pct"] == inp["auto_target_pct"]
    else:
        assert inp["auto_target_pct"] is None and out["auto_target_pct"] is None


# --- БД: пересчёт и автозапись ручной цели до 06:30 ---

D = date(2026, 6, 15)
AT_01 = kyiv_ts(2026, 6, 16, 1, 0)


def sql(url, query, params=None):
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture
def auto_db(seeded):  # noqa: F811
    sql(seeded._dsn, "UPDATE strategy_settings SET control_mode = 'auto', writes_enabled = true")
    seeded.maybe_insert_settings(FIXED_NOW - 3600, {int(k): v for k, v in REGS.items()})
    return seeded


def rows(url):
    return sql(url, "SELECT id, is_final, status, target_pct, apply_note, inputs->'manual', inputs->'late_apply' "
                    "FROM charge_decisions ORDER BY id")


def requests(url):
    return sql(url, "SELECT changes, created_by FROM inverter_write_requests WHERE created_by LIKE 'planner:2%' ORDER BY id")


def set_manual(url, mode, pct, night=D):
    sql(url, "UPDATE strategy_settings SET night_target_mode = %s, night_target_manual_pct = %s, "
             "night_target_manual_night = %s", (mode, pct, night))


def tick(db_, ts):
    db_.insert_sample(ts - 60, {"soc_pct": 50.0})  # свежая телеметрия для SOC и guard
    asyncio.run(PlannerScheduler(db_, TZ).tick(ts))


def test_manual_change_after_2330_replaces_applied_and_writes(auto_db):
    url = auto_db._dsn
    assert compute_and_save(auto_db, FIXED_NOW)
    sql(url, "UPDATE charge_decisions SET status = 'applied'")
    sql(url, "UPDATE strategy_settings SET risk = 'safe'")
    tick(auto_db, AT_01)  # авто-настройка после 23:30 — не пересчитываем
    assert len(rows(url)) == 1 and requests(url) == []
    set_manual(url, "once", 70)
    tick(auto_db, AT_01 + 60)
    (old_id, old_final, old_status, *_), (new_id, new_final, new_status, target, note, manual, late) = rows(url)
    assert (old_final, old_status) == (False, "applied")
    assert new_final and target == 70 and manual is True and late is True
    assert new_status == "queued" and "попытка 1" in note
    assert requests(url) == [({"166": 70, "171": 70}, f"planner:2026-06-15#{new_id}")]


def test_manual_equal_to_inverter_no_request(auto_db):
    url = auto_db._dsn
    assert compute_and_save(auto_db, FIXED_NOW)
    sql(url, "UPDATE charge_decisions SET status = 'applied'")
    set_manual(url, "always", 50, None)  # в инверторе слоты 1 и 6 уже 50
    tick(auto_db, AT_01)
    new = rows(url)[-1]
    assert new[1] and new[3] == 50 and new[2] == "applied" and new[4] == "уже в инверторе"
    assert requests(url) == []


def test_manual_not_after_0630_and_not_before_decision(auto_db):
    url = auto_db._dsn
    set_manual(url, "once", 70)
    tick(auto_db, kyiv_ts(2026, 6, 15, 20, 0))  # решения ещё нет — досрочно ничего
    assert rows(url) == []
    assert compute_and_save(auto_db, FIXED_NOW)
    sql(url, "UPDATE charge_decisions SET status = 'applied'")
    first = rows(url)
    assert first[0][3] == 70 and first[0][5] is True  # 22:45 — уже ручная цель
    set_manual(url, "once", 60)
    tick(auto_db, kyiv_ts(2026, 6, 16, 6, 35))  # после 06:30 — не пересчитываем
    assert rows(url) == first and requests(url) == []


def test_manual_late_decision_written_after_2330(auto_db):
    url = auto_db._dsn
    set_manual(url, "once", 70)
    assert compute_and_save(auto_db, FIXED_NOW)  # advice с late_apply, автозапись не успела до 23:30
    tick(auto_db, AT_01)
    (did, _f, status, _t, _n, _m, late), = rows(url)
    assert late is True and status == "queued"
    assert requests(url) == [({"166": 70, "171": 70}, f"planner:2026-06-15#{did}")]


def test_waits_for_fresh_snapshot_after_done_write(auto_db):
    url = auto_db._dsn
    assert compute_and_save(auto_db, FIXED_NOW)
    sql(url, "INSERT INTO inverter_write_requests(created_at, created_by, source, changes, expected_old, status, finished_at) "
             "VALUES (now(), 'planner:old', 'planner', '{}', '{}', 'done', to_timestamp(%s))", (AT_01 - 120,))
    sql(url, "UPDATE charge_decisions SET status = 'applied'")
    set_manual(url, "once", 70)
    tick(auto_db, AT_01)  # снимок (22:50) старше выполненной заявки — ждём, попытка не тратится
    new = rows(url)[-1]
    assert new[1] and new[2] == "advice" and new[4].startswith("ожидание: снимок настроек старше")
    assert requests(url) == []
    auto_db.maybe_insert_settings(AT_01 - 30, {int(k): v for k, v in dict(REGS, **{"166": 55, "171": 55}).items()})
    tick(auto_db, AT_01 + 60)
    assert rows(url)[-1][2] == "queued" and "попытка 1" in rows(url)[-1][4]
    assert requests(url) == [({"166": 70, "171": 70}, f"planner:2026-06-15#{new[0]}")]


def test_manual_without_decision_warns_once_and_flags(auto_db, monkeypatch, caplog):
    monkeypatch.setattr(planner_service, "_NO_DECISION_WARNED", set())
    url = auto_db._dsn
    set_manual(url, "once", 70)
    with caplog.at_level("WARNING", logger="deya.planner"):
        tick(auto_db, AT_01)
        tick(auto_db, AT_01 + 600)
    assert rows(url) == []
    assert sum("финального решения нет" in r.getMessage() for r in caplog.records) == 1
    assert apply_status(auto_db, AT_01)["manual_no_decision"] is True
    assert apply_status(auto_db, kyiv_ts(2026, 6, 15, 20, 0))["manual_no_decision"] is False  # окно решения ещё не прошло
