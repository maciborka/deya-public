"""Автозапись ночного target планировщика в TOU (очередь inverter_write_requests, source='planner')."""
import asyncio
from datetime import date

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app import inverter_writer
from app.db import Database
from app.planner.params import PlannerParams
from app.planner_service import (
    PlannerScheduler, apply_status, build_floor_changes, build_night_changes, build_params, floor_sync_status,
)
from test_inverter_writer import LIVE, FakeClient
from test_planner_service import STRATEGY, TOU_REGS, TZ, kyiv_ts

NIGHT = date(2026, 6, 15)
T = kyiv_ts(2026, 6, 15, 22, 50)
PARAMS = PlannerParams(tz=TZ)
ST = {"emergency_mode": "off", "normal_floor_pct": 30, "emergency_floor_pct": 50, "max_grid_charge_soc_pct": 80}
ST_EMERGENCY = dict(ST, emergency_mode="always")
# Ночные слоты с зарядом от сети: 1 (00:00) и 6 (23:05); слот 2 (06:55) без сети.
# Дневные пороги (168–170) уже равны полу 30 % из ST — floor-sync в обычных тестах ничего не пишет.
REGS = dict(TOU_REGS, **{"173": 0, "167": 30, "168": 30, "169": 30, "170": 30})


def test_build_night_changes_both_regs_and_clamp():
    assert build_night_changes(REGS, 63.4, ST, PARAMS) == ({"166": 63, "171": 63}, {"166": 50, "171": 50})
    assert build_night_changes(REGS, 95, ST, PARAMS)[0] == {"166": 80, "171": 80}  # потолок
    assert build_night_changes(REGS, 10, ST, PARAMS)[0] == {"166": 30, "171": 30}  # обычный пол
    # аварийный режим «всегда»: пол 50 %, потолок заряда из настроек
    assert build_night_changes(dict(REGS, **{"166": 60, "171": 60}), 10, ST_EMERGENCY, PARAMS)[0] == {"166": 50, "171": 50}
    assert build_night_changes(REGS, 10, ST_EMERGENCY, PARAMS) == ({}, {})  # в инверторе уже 50
    assert build_night_changes(REGS, 95, dict(ST_EMERGENCY, max_grid_charge_soc_pct=100), PARAMS)[0] == {"166": 95, "171": 95}
    assert build_night_changes(REGS, 60, ST_EMERGENCY, PARAMS) == ({"166": 60, "171": 60}, {"166": 50, "171": 50})
    # слот 2 (06:55, ночь) с сетью тоже ночной слот заряда
    assert build_night_changes(TOU_REGS, 60, ST, PARAMS)[0] == {"166": 60, "167": 60, "171": 60}
    # времена/источники/мощность не трогаются
    changes, _ = build_night_changes(REGS, 70, ST, PARAMS)
    assert all(166 <= int(r) <= 171 for r in changes)


def test_build_night_changes_noop_and_skip():
    assert build_night_changes(dict(REGS, **{"166": 60, "171": 60}), 60.2, ST, PARAMS) == ({}, {})
    no_grid = dict(REGS, **{"172": 0, "177": 0})
    assert build_night_changes(no_grid, 60, ST, PARAMS)[0] == "skip"
    day_only = dict(no_grid, **{"174": 1})  # заряд от сети только в дневном слоте 07:00
    status, reason = build_night_changes(day_only, 60, ST, PARAMS)
    assert status == "skip" and "ночного слота" in reason
    assert build_night_changes({k: v for k, v in REGS.items() if k != "170"}, 60, ST, PARAMS)[0] == "skip"


def test_night_target_in_snapshot_does_not_distort_floors():
    """После автозаписи ночные SOC = target; пол во все часы — эффективный пол из настроек, не слоты снимка."""
    regs = dict(TOU_REGS, **{"166": 75, "167": 75, "171": 75})
    p = build_params(dict(STRATEGY, **ST), regs)
    assert p.floor_pct == 30
    assert all(p.floor_pct_at(m) == 30 for m in (7 * 60, 12 * 60, 22 * 60 + 59, 0))
    assert p.floor_slots is None
    pe = build_params(dict(STRATEGY, **ST_EMERGENCY), regs)
    assert all(pe.floor_pct_at(m) == 50 for m in (7 * 60, 12 * 60, 22 * 60 + 59, 0))


@pytest.fixture
def adb(pg_url):
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO strategy_settings(reserve_soc_pct, max_grid_charge_soc_pct, normal_floor_pct, battery_capacity_kwh, "
            "battery_usable_pct, round_trip_efficiency_pct, control_mode, writes_enabled, decision_time, "
            "export_price_per_kwh, wear_cost_per_kwh, risk, default_plan_soc_pct, updated_at) "
            "VALUES (30, 80, 30, 24, 95, 90, 'auto', true, '22:45', 0, 0.4, 'balanced', 60, now())"
        )
    database = Database(pg_url)
    database.maybe_insert_settings(T - 3600, {int(k): v for k, v in REGS.items()})
    database.insert_sample(T - 60, {"soc_pct": 50.0})
    yield database
    database.close()


def sql(db, query, params=None):
    with psycopg.connect(db._dsn, autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def insert_decision(db, target=60.0, night=NIGHT, status="advice"):
    return sql(
        db,
        "INSERT INTO charge_decisions(created_at, night_date, is_final, target_pct, expected_cost_uah, "
        "cost_no_charge_uah, cost_to_ceiling_uah, cost_to_100_uah, expected_saving_uah, expected_saving_no_wear_uah, "
        "expected_grid_import_kwh, soc_at_07, min_soc_tomorrow_worst, explanation, inputs, tou_plan, status) "
        "VALUES (to_timestamp(%s), %s, true, %s, 0, 0, 0, 0, 0, 0, 0, 0, 0, '', '{}', '[]', %s) RETURNING id",
        (T - 300, night, target, status),
    )[0][0]


def decision(db, did):
    return sql(db, "SELECT status, write_request_id, applied_at, apply_note FROM charge_decisions WHERE id = %s", (did,))[0]


def requests(db):
    return sql(db, "SELECT id, source, status, changes, expected_old, created_by FROM inverter_write_requests ORDER BY id")


def tick(db, ts):
    asyncio.run(PlannerScheduler(db, TZ).tick(ts))


def finish(db, rid, status, at_ts, error=None):
    sql(db, "UPDATE inverter_write_requests SET status = %s, error = %s, finished_at = to_timestamp(%s) WHERE id = %s",
        (status, error, at_ts, rid))


def test_tick_queues_once_and_no_duplicate_while_pending(adb):
    did = insert_decision(adb, 63.4)
    tick(adb, T - 600)  # до decision_time — не пишем
    assert requests(adb) == []
    tick(adb, T)
    tick(adb, T + 60)
    reqs = requests(adb)
    assert len(reqs) == 1
    rid, source, status, changes, expected_old, author = reqs[0]
    assert (source, status, author) == ("planner", "pending", f"planner:2026-06-15#{did}")
    assert changes == {"166": 63, "171": 63} and expected_old == {"166": 50, "171": 50}
    st, wr, applied_at, note = decision(adb, did)
    assert (st, wr, applied_at) == ("queued", rid, None) and "попытка 1" in note


def test_done_becomes_applied(adb):
    did = insert_decision(adb)
    tick(adb, T)
    rid = requests(adb)[0][0]
    finish(adb, rid, "done", T + 30)
    tick(adb, T + 60)
    st, wr, applied_at, note = decision(adb, did)
    assert st == "applied" and wr == rid and int(applied_at.timestamp()) == T + 30
    assert note.startswith("записано") and "166: 50 → 60" in note


def test_cancelled_request_is_skipped_without_retry(adb):
    did = insert_decision(adb)
    tick(adb, T)
    rid = requests(adb)[0][0]
    finish(adb, rid, "cancelled", T + 30, "отменено вручную")
    tick(adb, T + 120)  # > 60 с после отмены, окно открыто
    st, wr, _, note = decision(adb, did)
    assert st == "skipped" and wr == rid
    assert f"заявка {rid} отменена" in note
    tick(adb, T + 300)
    assert len(requests(adb)) == 1 and decision(adb, did)[0] == "skipped"


def test_stale_retry_once_then_failed(adb):
    did = insert_decision(adb)
    tick(adb, T)
    first = requests(adb)[0][0]
    finish(adb, first, "stale", T + 30, "рег 166: ожидалось 50, сейчас 55")
    adb.maybe_insert_settings(T + 40, {int(k): v for k, v in dict(REGS, **{"166": 55}).items()})
    tick(adb, T + 60)  # пауза после неудачи ещё не прошла
    assert len(requests(adb)) == 1
    tick(adb, T + 120)
    reqs = requests(adb)
    assert len(reqs) == 2 and reqs[1][2] == "pending"
    assert reqs[1][4] == {"166": 55, "171": 50}  # expected_old из свежего снимка
    assert decision(adb, did)[:2] == ("queued", reqs[1][0])
    finish(adb, reqs[1][0], "stale", T + 150, "ещё раз")
    tick(adb, T + 300)
    st, _, _, note = decision(adb, did)
    assert st == "failed" and "ошибка записи" in note and "stale" in note
    tick(adb, T + 400)
    assert len(requests(adb)) == 2


def test_failure_after_window_is_failed_without_retry(adb):
    did = insert_decision(adb)
    tick(adb, T)
    finish(adb, requests(adb)[0][0], "failed", T + 30, "нет ответа")
    tick(adb, kyiv_ts(2026, 6, 15, 23, 31))
    assert len(requests(adb)) == 1 and decision(adb, did)[0] == "failed"


def test_guard_blocks_waits_then_skipped_after_window(adb):
    did = insert_decision(adb)
    sql(adb, "INSERT INTO inverter_write_requests(created_at, created_by, changes, expected_old, status) "
             "VALUES (now(), 'admin', %s, %s, 'pending')", (Jsonb({"171": 90}), Jsonb({"171": 50})))
    tick(adb, T)
    assert len(requests(adb)) == 1
    st, wr, _, note = decision(adb, did)
    assert st == "advice" and wr is None and note.startswith("ожидание")
    tick(adb, kyiv_ts(2026, 6, 15, 23, 31))
    st, _, _, note = decision(adb, did)
    assert st == "skipped" and "истекло" in note


def test_stale_telemetry_blocks(adb):
    did = insert_decision(adb)
    tick(adb, T + 20 * 60)  # последняя телеметрия старше 15 мин
    assert requests(adb) == [] and decision(adb, did)[0] == "advice"


def test_noop_when_already_set(adb):
    adb.maybe_insert_settings(T - 100, {int(k): v for k, v in dict(REGS, **{"166": 60, "171": 60}).items()})
    did = insert_decision(adb, 60.0)
    tick(adb, T)
    st, wr, applied_at, note = decision(adb, did)
    assert requests(adb) == [] and st == "applied" and wr is None and applied_at is not None and note == "уже в инверторе"


def test_skip_without_night_grid_slot(adb):
    adb.maybe_insert_settings(T - 100, {int(k): v for k, v in dict(REGS, **{"172": 0, "177": 0, "166": 30, "171": 30}).items()})
    did = insert_decision(adb)
    tick(adb, T)
    assert requests(adb) == [] and decision(adb, did)[0] == "skipped"


@pytest.mark.parametrize("mode, writes", [("advise", False), ("advise", True), ("off", True)])
def test_no_autowrite_outside_auto(adb, mode, writes):
    sql(adb, "UPDATE strategy_settings SET control_mode = %s, writes_enabled = %s", (mode, writes))
    did = insert_decision(adb)
    tick(adb, T)
    assert requests(adb) == [] and decision(adb, did)[0] == "advice"


def test_insert_and_link_is_one_transaction(adb):
    did = insert_decision(adb, status="applied")
    with pytest.raises(RuntimeError):
        adb.insert_write_request({"166": 60}, {"166": 50}, "planner", "planner:x", decision_id=did, expect_status="advice")
    with pytest.raises(RuntimeError):
        adb.insert_write_request({"166": 60}, {"166": 50}, "planner", "planner:x", decision_id=999999, expect_status="advice")
    assert requests(adb) == [] and decision(adb, did)[:2] == ("applied", None)
    did2 = insert_decision(adb, night=date(2026, 6, 16))
    rid = adb.insert_write_request({"166": 60}, {"166": 50}, "planner", "planner:y", decision_id=did2, expect_status="advice")
    assert decision(adb, did2)[:2] == ("queued", rid)


def test_apply_status(adb):
    out = apply_status(adb, T)
    assert out["has_decision"] is False and out["inverter_night_targets_pct"] == [50.0]
    did = insert_decision(adb)
    tick(adb, T)
    out = apply_status(adb, T)
    assert out["decision_id"] == did and out["status"] == "queued" and out["write_request_status"] == "pending"


def insert_planner_request(db, changes, expected_old):
    return sql(
        db,
        "INSERT INTO inverter_write_requests(created_at, created_by, source, changes, expected_old, status) "
        "VALUES (now(), 'planner:t', 'planner', %s, %s, 'pending') RETURNING id",
        (Jsonb(changes), Jsonb(expected_old)),
    )[0][0]


def request_row(db, rid):
    return sql(db, "SELECT status, error, backup FROM inverter_write_requests WHERE id = %s", (rid,))[0]


@pytest.mark.parametrize("mode, writes", [("advise", True), ("auto", False), ("off", False)])
def test_writer_cancels_planner_request_when_auto_off(adb, mode, writes):
    sql(adb, "UPDATE strategy_settings SET control_mode = %s, writes_enabled = %s", (mode, writes))
    rid = insert_planner_request(adb, {"171": 60}, {"171": 50})
    client = FakeClient(LIVE)
    assert asyncio.run(inverter_writer.process_queue(client, adb)) is True
    status, error, backup = request_row(adb, rid)
    assert (status, error, backup) == ("cancelled", "автозапись выключена", None)
    assert client.writes == []


def test_writer_executes_planner_request_in_auto(adb):
    rid = insert_planner_request(adb, {"171": 60}, {"171": 50})
    client = FakeClient(LIVE)
    asyncio.run(inverter_writer.process_queue(client, adb))
    assert request_row(adb, rid)[0] == "done" and client.writes == [(171, 60)]


@pytest.mark.parametrize("changes", [{"153": 2300}, {"172": 0}, {"171": 101}, {"171": -1}])
def test_writer_rejects_planner_non_soc(adb, changes):
    rid = insert_planner_request(adb, changes, {k: LIVE[int(k)] for k in changes})
    client = FakeClient(LIVE)
    asyncio.run(inverter_writer.process_queue(client, adb))
    status, _, backup = request_row(adb, rid)
    assert status == "failed" and backup is None and client.writes == []


def test_writer_admin_request_unaffected_by_mode(adb):
    sql(adb, "UPDATE strategy_settings SET control_mode = 'off', writes_enabled = false")
    rid = sql(adb, "INSERT INTO inverter_write_requests(created_at, created_by, changes, expected_old, status) "
                   "VALUES (now(), 'admin', %s, %s, 'pending') RETURNING id", (Jsonb({"153": 2300}), Jsonb({"153": 2305})))[0][0]
    client = FakeClient(LIVE)
    asyncio.run(inverter_writer.process_queue(client, adb))
    assert request_row(adb, rid)[0] == "done" and client.writes == [(153, 2300)]


# --- floor-sync: дневные пороги разряда ---

# Живой TOU: слоты 00:00/06:55/23:05 с сетью (SOC 80 после автозаписи), 07:00/17:00/18:00 без сети (SOC 50).
LIVE_TOU = dict(TOU_REGS, **{"166": 80, "167": 80, "171": 80})
ST15 = dict(ST, normal_floor_pct=15)


def test_build_floor_changes_normal_floor():
    changes, old = build_floor_changes(LIVE_TOU, ST15, PARAMS)
    assert changes == {"168": 15, "169": 15, "170": 15} and old == {"168": 50, "169": 50, "170": 50}


def test_build_floor_changes_emergency_already_set():
    assert build_floor_changes(LIVE_TOU, ST_EMERGENCY, PARAMS) == ({}, {})


def test_build_floor_changes_raises_low_night_slot():
    regs = dict(LIVE_TOU, **{"171": 30})
    assert build_floor_changes(regs, ST_EMERGENCY, PARAMS) == ({"171": 50}, {"171": 30})
    # высокий ночной порог и обычный режим — ночные слоты не трогаются
    assert build_floor_changes(dict(LIVE_TOU, **{"171": 30}), ST15, PARAMS)[0] == {"168": 15, "169": 15, "170": 15}
    assert build_floor_changes({k: v for k, v in LIVE_TOU.items() if k != "170"}, ST15, PARAMS) == ({}, {})


def set_live_tou(db, regs=LIVE_TOU, ts=T - 100):
    db.maybe_insert_settings(ts, {int(k): v for k, v in regs.items()})


def floor_requests(db):
    return [r for r in requests(db) if r[5].startswith("floor:")]


def test_tick_creates_floor_request_once(adb):
    set_live_tou(adb)
    tick(adb, T)  # решения нет — только floor-sync (пол 30)
    reqs = requests(adb)
    assert len(reqs) == 1
    rid, source, status, changes, expected_old, author = reqs[0]
    assert (source, status, author) == ("planner", "pending", "floor:2026-06-15")
    assert changes == {"168": 30, "169": 30, "170": 30} and expected_old == {"168": 50, "169": 50, "170": 50}
    tick(adb, T + 60)
    tick(adb, T + 120)
    assert len(requests(adb)) == 1  # заявка pending — не дублируем
    assert floor_sync_status(adb, T + 120)["status"] == "queued"


def test_floor_request_not_linked_to_decision_and_status_ok_after_done(adb):
    set_live_tou(adb)
    did = insert_decision(adb, 80.0)  # ночной target уже установлен
    tick(adb, T)
    rid = floor_requests(adb)[0][0]
    assert decision(adb, did)[:2] != ("queued", rid) and decision(adb, did)[1] is None
    finish(adb, rid, "done", T + 30)
    set_live_tou(adb, dict(LIVE_TOU, **{"168": 30, "169": 30, "170": 30}), ts=T + 40)
    tick(adb, T + 120)
    assert len(floor_requests(adb)) == 1
    out = floor_sync_status(adb, T + 120)
    assert out["status"] == "ok" and out["target_floor_pct"] == 30 and out["inverter_floor_pct"] == 30
    assert out["last_request"]["id"] == rid


def test_floor_sync_waits_for_snapshot_after_done(adb):
    set_live_tou(adb)
    tick(adb, T)
    finish(adb, floor_requests(adb)[0][0], "done", T + 30)
    tick(adb, T + 120)  # снимок старше выполненной записи — ждём
    assert len(floor_requests(adb)) == 1


def test_floor_requests_daily_limit(adb):
    set_live_tou(adb)
    now = T
    for n in range(1, 5):
        tick(adb, now)
        reqs = floor_requests(adb)
        assert len(reqs) == min(n, 3)
        finish(adb, reqs[-1][0], "failed", now + 10, "нет ответа")
        now += 120
    assert len(floor_requests(adb)) == 3
    assert floor_sync_status(adb, now)["status"] == "failed"
    nxt = kyiv_ts(2026, 6, 16, 0, 5)  # новый календарный день — новый лимит
    adb.insert_sample(nxt - 60, {"soc_pct": 50.0})
    tick(adb, nxt)
    assert len(floor_requests(adb)) == 4 and floor_requests(adb)[-1][5] == "floor:2026-06-16"


def test_floor_retry_pause_after_failure(adb):
    set_live_tou(adb)
    tick(adb, T)
    finish(adb, floor_requests(adb)[0][0], "stale", T + 10, "рег 168: ожидалось 50")
    tick(adb, T + 30)
    assert len(floor_requests(adb)) == 1
    tick(adb, T + 90)
    assert len(floor_requests(adb)) == 2


@pytest.mark.parametrize("mode, writes", [("advise", False), ("advise", True), ("off", True), ("auto", False)])
def test_no_floor_request_outside_auto(adb, mode, writes):
    sql(adb, "UPDATE strategy_settings SET control_mode = %s, writes_enabled = %s", (mode, writes))
    set_live_tou(adb)
    tick(adb, T)
    assert requests(adb) == []
    assert floor_sync_status(adb, T)["status"] == "pending"


def test_floor_sync_waits_for_guard(adb):
    set_live_tou(adb)
    sql(adb, "INSERT INTO inverter_write_requests(created_at, created_by, changes, expected_old, status) "
             "VALUES (now(), 'admin', %s, %s, 'pending')", (Jsonb({"171": 90}), Jsonb({"171": 80})))
    tick(adb, T)
    assert floor_requests(adb) == []
    sql(adb, "UPDATE inverter_write_requests SET status = 'done', finished_at = now()")
    tick(adb, T + 60)
    assert len(floor_requests(adb)) == 1
