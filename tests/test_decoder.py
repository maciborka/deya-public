import json
from pathlib import Path

from app.db import Database
from app.decoder import METRIC_NAMES, decode, settings_regs

PROBE = Path(__file__).resolve().parent.parent / "data" / "probe_20261021_101052.json"
FALLBACK = {587: 5117, 588: 67, 590: 62165, 591: 58956, 20: 54464, 21: 1, 516: 55140, 517: 0, 625: 601, 540: 1250}


def _regs():
    if PROBE.exists():
        return {int(k): v for k, v in json.loads(PROBE.read_text())["regs"].items()}
    return dict(FALLBACK)


def test_decode_values():
    d = decode(_regs())
    assert d["soc_pct"] == 67
    assert d["batt_voltage_v"] == 51.17
    assert d["batt_power_w"] == -3371
    assert d["batt_current_a"] == -65.8
    assert d["grid_power_w"] == 601
    assert d["dc_temp_c"] == 25.0
    assert d["batt_charge_total_kwh"] == 5514.0
    assert set(d) == set(METRIC_NAMES)


def test_decode_missing_is_none():
    d = decode({588: 67})
    assert d["soc_pct"] == 67
    assert d["batt_voltage_v"] is None
    assert d["batt_power_w"] is None
    assert d["batt_charge_total_kwh"] is None


def test_settings_regs():
    s = settings_regs({97: 1, 98: 2, 177: 3, 178: 4})
    assert s == {98: 2, 177: 3}


def test_db(pg_url):
    db = Database(pg_url)
    assert db.latest_sample() is None
    db.insert_sample(100, decode(_regs()))
    db.insert_sample(130, decode({588: 70}))
    assert db.latest_sample()["soc_pct"] == 70
    assert len(db.samples_between(0, 1000, 10)) == 2
    assert len(db.samples_between(0, 1000, 1)) == 1
    assert db.maybe_insert_settings(100, {98: 1, 99: 2}) is True
    assert db.maybe_insert_settings(130, {98: 1, 99: 2}) is False
    assert db.maybe_insert_settings(160, {98: 1, 99: 3}) is True
    assert db.latest_settings()["regs"]["99"] == 3
    db.log_error("boom")
    assert db.last_error()["error"] == "boom"
    db.close()
