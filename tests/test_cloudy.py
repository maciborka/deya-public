"""Пасмурный день → ночной заряд до потолка (cloudy_full_charge_kwh)."""
from dataclasses import asdict

import pytest

from app.planner.params import PlannerParams
from app.planner_service import _same_fp, build_params, settings_fingerprint
from test_autowrite import REGS
from test_night_target import plan
from test_planner import NORMAL
from test_planner_service import STRATEGY


def cplan(thr, **kw):
    return plan(params=PlannerParams(**dict(NORMAL, cloudy_full_charge_kwh=thr)), **kw)


def test_cloudy_raises_target_to_ceiling():
    base = cplan(None, kwh=10.0)
    dec = cplan(12.0, kwh=10.0)
    assert dec.pv_p50_day_kwh == pytest.approx(10.0, abs=0.01)
    assert dec.cloudy_full and dec.target_pct == 100.0 and dec.cloudy_pre_rule_target_pct == base.target_pct
    assert dec.day_import_constraint_met and dec.day_import_prob == pytest.approx(dec.candidate_day_import_prob[100.0])
    assert not dec.epsilon_applied and not dec.manual
    assert "Пасмурно: солнца завтра по прогнозу 10.0 кВт·ч < порога 12 — заряжаем до потолка 100 %" in dec.explanation
    assert f"расчёт дал {base.target_pct:g} %" in dec.explanation and "Минимальный заряд" not in dec.explanation
    assert all(t.soc_pct == 100 for t in dec.tou_plan if t.grid_charge)


def test_sunny_unchanged():
    base = cplan(None, kwh=20.0)
    dec = cplan(12.0, kwh=20.0)
    assert not dec.cloudy_full and dec.cloudy_pre_rule_target_pct is None
    assert dec.target_pct == base.target_pct and dec.explanation == base.explanation
    assert dec.pv_p50_day_kwh == pytest.approx(20.0, abs=0.01)


@pytest.mark.parametrize("thr", [None, 0, 0.0])
def test_off(thr):
    dec = cplan(thr, kwh=6.0)
    assert not dec.cloudy_full and dec.pv_p50_day_kwh is not None


def test_manual_wins_but_flag_set():
    dec = plan(manual=60, params=PlannerParams(**dict(NORMAL, cloudy_full_charge_kwh=12.0)), kwh=10.0)
    assert dec.manual and dec.target_pct == 60 and dec.cloudy_full
    assert dec.cloudy_pre_rule_target_pct == dec.auto_target_pct and "Пасмурно" not in dec.explanation


def test_not_applied_when_soc_at_ceiling():
    params = PlannerParams(**dict(NORMAL, night_ceiling_pct=80.0, cloudy_full_charge_kwh=12.0))
    dec = plan(params=params, soc=85.0, kwh=10.0)
    assert not dec.cloudy_full


def test_not_applied_when_target_already_ceiling():
    dec = cplan(12.0, kwh=0.0, load_w=1500.0)
    assert dec.target_pct == 100.0 and not dec.cloudy_full


def test_not_applied_when_margin_nonpositive():
    dec = plan(params=PlannerParams(**dict(NORMAL, cloudy_full_charge_kwh=12.0, wear_cost_per_kwh=5.0)), kwh=10.0)
    assert dec.margin_uah_per_kwh <= 0 and not dec.cloudy_full


def test_fingerprint_and_old_decisions():
    assert settings_fingerprint(STRATEGY)["cloudy_full_charge_kwh"] is None
    assert settings_fingerprint(dict(STRATEGY, cloudy_full_charge_kwh=0))["cloudy_full_charge_kwh"] is None
    fp = settings_fingerprint(dict(STRATEGY, cloudy_full_charge_kwh=12))
    assert fp["cloudy_full_charge_kwh"] == 12.0
    assert fp != settings_fingerprint(dict(STRATEGY, cloudy_full_charge_kwh=10))
    legacy = {k: v for k, v in fp.items() if k != "cloudy_full_charge_kwh"}
    assert _same_fp(legacy, fp)
    assert not _same_fp(dict(fp, cloudy_full_charge_kwh=10.0), fp)
    assert not _same_fp(dict(fp, cloudy_full_charge_kwh=None), fp)


def test_build_params_passes_threshold():
    assert build_params(dict(STRATEGY, cloudy_full_charge_kwh=12.0), REGS).cloudy_full_charge_kwh == 12.0
    assert build_params(dict(STRATEGY, cloudy_full_charge_kwh=0), REGS).cloudy_full_charge_kwh is None
    assert build_params(dict(STRATEGY, cloudy_full_charge_kwh=None), REGS).cloudy_full_charge_kwh is None
    assert build_params(STRATEGY, REGS).cloudy_full_charge_kwh is None


def test_old_stored_params_load():
    stored = asdict(PlannerParams())
    del stored["cloudy_full_charge_kwh"]
    assert PlannerParams(**stored).cloudy_full_charge_kwh is None
