from app.outages import find_outages, grid_state


def s(ts, v=(230, 230, 230), batt=0.0, load=0.0, soc=50):
    return {"ts": ts, "grid_l1_voltage_v": v[0], "grid_l2_voltage_v": v[1], "grid_l3_voltage_v": v[2],
            "batt_power_w": batt, "load_power_w": load, "soc_pct": soc}


DOWN = (0, 0, 0)


def test_grid_state():
    assert grid_state(s(0)) == "ok"
    assert grid_state(s(0, (180, 230, 230))) == "ok"
    assert grid_state(s(0, (179.9, 230, 230))) == "partial"
    assert grid_state(s(0, (0, 0, 230))) == "partial"
    assert grid_state(s(0, DOWN)) == "down"
    assert grid_state(s(0, (None, None, None))) is None
    assert grid_state({"ts": 0}) is None


def test_basic_outage():
    rows = [s(0), s(10), s(20, DOWN, 1000, 1200, 50), s(30, DOWN, 1000, 1200, 49), s(60, DOWN, 1000, 1200, 48), s(70)]
    out = find_outages(rows)
    assert len(out) == 1
    o = out[0]
    assert (o["start_ts"], o["end_ts"], o["duration_sec"]) == (20, 60, 40)
    assert o["kind"] == "down"
    assert o["phases_down"] == [1, 2, 3]
    assert (o["soc_start"], o["soc_end"]) == (50, 48)
    assert o["batt_kwh"] == round(1000 * 40 / 3600 / 1000, 3)
    assert o["load_kwh"] == round(1200 * 40 / 3600 / 1000, 3)


def test_partial_and_kind_upgrade():
    rows = [s(0), s(10, (230, 100, 230)), s(50, (230, 100, 230)), s(60, (230, 100, 0)), s(100, (230, 100, 0)), s(110)]
    o = find_outages(rows)[0]
    assert o["kind"] == "partial"
    assert o["phases_down"] == [2, 3]
    rows[3] = rows[4] = s(60, DOWN)
    assert find_outages(rows)[0]["kind"] == "down"


def test_short_outage_discarded():
    assert find_outages([s(0), s(10, DOWN), s(30, DOWN), s(39, DOWN), s(40)]) == []


def test_gap_is_not_outage_and_closes_segment():
    rows = [s(0), s(10, DOWN), s(50, DOWN), s(1000, DOWN), s(1040, DOWN), s(1050)]
    out = find_outages(rows)
    assert [(o["start_ts"], o["end_ts"]) for o in out] == [(10, 50), (1000, 1040)]
    assert find_outages([s(0), s(1000)]) == []


def test_ongoing():
    out = find_outages([s(0), s(10, DOWN), s(60, DOWN)])
    assert out[0]["end_ts"] is None
    assert out[0]["duration_sec"] == 50
