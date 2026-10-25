import hashlib
import json
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import config
from .decoder import METRIC_NAMES
from .forecast.base import PanelGroup

DAILY_COLUMNS = [
    "pv_today_kwh",
    "load_today_kwh",
    "grid_import_today_kwh",
    "grid_export_today_kwh",
    "batt_charge_today_kwh",
    "batt_discharge_today_kwh",
]


def find_zone(zones: list[dict], t: dtime) -> dict | None:
    """Зона, в которую попадает локальное время t; end <= start означает переход через полночь."""
    for z in zones:
        start, end = z["start_time"], z["end_time"]
        if start < end:
            if start <= t < end:
                return z
        elif t >= start or t < end:
            return z
    return None


class Database:
    """Доступ к таблицам Postgres. Схемой владеет Django (admin/), здесь таблиц не создаём."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._lock = threading.Lock()
        self._conn: psycopg.Connection | None = None
        with self._lock:
            self._connect()

    def _connect(self) -> None:
        self._conn = psycopg.connect(
            self._dsn,
            connect_timeout=5,
            options="-c statement_timeout=15000",
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
            autocommit=True,
            row_factory=dict_row,
        )

    def _run(self, fn, retry: bool = True):
        """fn(cursor) под локом; при OperationalError переподключаемся и (если retry) повторяем один раз."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self._conn is None or self._conn.closed:
                        self._connect()
                    with self._conn.cursor() as cur:
                        return fn(cur)
                except psycopg.errors.QueryCanceled:
                    raise
                except psycopg.OperationalError:
                    try:
                        if self._conn is not None:
                            self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                    if attempt or not retry:
                        raise

    def _all(self, sql: str, params=None) -> list[dict]:
        return self._run(lambda cur: cur.execute(sql, params).fetchall())

    def _one(self, sql: str, params=None) -> dict | None:
        return self._run(lambda cur: cur.execute(sql, params).fetchone())

    def _exec(self, sql: str, params=None, retry: bool = True) -> None:
        self._run(lambda cur: cur.execute(sql, params), retry=retry)

    def insert_sample(self, ts: int, metrics: dict) -> None:
        names = ", ".join(METRIC_NAMES)
        marks = ", ".join("%s" for _ in METRIC_NAMES)
        updates = ", ".join(f"{n} = EXCLUDED.{n}" for n in METRIC_NAMES)
        self._exec(
            f"INSERT INTO samples(ts, {names}) VALUES (%s, {marks}) ON CONFLICT (ts) DO UPDATE SET {updates}",
            (ts, *[metrics.get(n) for n in METRIC_NAMES]),
        )

    def maybe_insert_settings(self, ts: int, regs: dict[int, int]) -> bool:
        if not regs:
            return False
        payload = json.dumps({str(k): regs[k] for k in sorted(regs)}, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()

        def work(cur) -> bool:
            row = cur.execute("SELECT hash FROM settings_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if row is not None and row["hash"] == digest:
                return False
            cur.execute(
                "INSERT INTO settings_snapshots(ts, regs, hash) VALUES (%s, %s::jsonb, %s)",
                (ts, payload, digest),
            )
            return True

        return self._run(work)

    def latest_sample(self) -> dict | None:
        return self._one("SELECT * FROM samples ORDER BY ts DESC LIMIT 1")

    def samples_between(self, from_ts: int, to_ts: int, limit: int, fields: list[str] | None = None) -> list[dict]:
        cols = "*" if not fields else ", ".join(["ts", *[f for f in fields if f in METRIC_NAMES]])
        return self._all(
            f"SELECT {cols} FROM samples WHERE ts >= %s AND ts <= %s ORDER BY ts LIMIT %s",
            (from_ts, to_ts, limit),
        )

    def samples_bucketed(self, from_ts: int, to_ts: int, step: int, fields: list[str] | None = None) -> list[dict]:
        names = METRIC_NAMES if not fields else [f for f in fields if f in METRIC_NAMES]
        cols = "".join(f", AVG({n})::float8 AS {n}" for n in names)
        return self._all(
            f"SELECT (ts / %s::bigint) * %s::bigint AS ts{cols} FROM samples "
            "WHERE ts >= %s AND ts <= %s GROUP BY 1 ORDER BY 1",
            (step, step, from_ts, to_ts),
        )

    def daily_max(self, days: int, tz_name: str) -> list[dict]:
        tz = ZoneInfo(tz_name)
        today = datetime.now(tz).date()
        start = datetime.combine(today, datetime.min.time(), tz).timestamp() - (days - 1) * 86400 - 7200
        cols = ", ".join(f"MAX({c}) AS {c}" for c in DAILY_COLUMNS)
        rows = self._all(
            f"SELECT to_char(to_timestamp(ts) AT TIME ZONE %s, 'YYYY-MM-DD') AS date, {cols} "
            "FROM samples WHERE ts >= %s GROUP BY 1 ORDER BY 1",
            (tz_name, int(start)),
        )
        return rows[-days:]

    def latest_settings(self) -> dict | None:
        row = self._one("SELECT id, ts, regs, hash FROM settings_snapshots ORDER BY id DESC LIMIT 1")
        if not row:
            return None
        return {"id": row["id"], "ts": row["ts"], "hash": row["hash"], "regs": row["regs"]}

    def log_error(self, error: str, ts: int | None = None) -> None:
        self._exec("INSERT INTO poll_errors(ts, error) VALUES (%s, %s)", (ts or int(time.time()), error))

    def last_error(self) -> dict | None:
        return self._one("SELECT ts, error FROM poll_errors ORDER BY id DESC LIMIT 1")

    def insert_forecast(self, provider: str, panel: str, fetched_at: int, samples: list) -> None:
        rows = [
            (provider, panel, fetched_at, s.target_ts, s.gti, s.ghi, s.power_w, s.p10, s.p90, s.temp_c)
            for s in samples
        ]

        def work(cur) -> None:
            with self._conn.transaction():
                cur.executemany(
                    "INSERT INTO forecast_raw(provider, panel, fetched_at, target_ts, gti, ghi, power_w, p10, p90, temp_c) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (provider, panel, fetched_at, target_ts) DO NOTHING",
                    rows,
                )

        self._run(work)

    def log_forecast_run(self, ts: int, provider: str, panel: str, ok: bool, n: int, error: str | None) -> None:
        self._exec(
            "INSERT INTO forecast_runs(ts, provider, panel, ok, n, error) VALUES (%s, %s, %s, %s, %s, %s)",
            (ts, provider, panel, ok, n, error),
            retry=False,
        )

    def last_ok_run_ts(self, provider: str, panel: str) -> int | None:
        row = self._one(
            "SELECT MAX(ts) AS ts FROM forecast_runs WHERE provider = %s AND panel = %s AND ok",
            (provider, panel),
        )
        return row["ts"]

    def recent_forecast_runs(self, limit: int) -> list[dict]:
        return self._all(
            "SELECT ts, provider, panel, ok::int AS ok, n, error FROM forecast_runs ORDER BY id DESC LIMIT %s",
            (limit,),
        )

    def forecast_providers(self) -> list[dict]:
        return self._all("SELECT code, name, enabled, api_key, params FROM forecast_providers ORDER BY id")

    def enabled_panels(self) -> list[PanelGroup]:
        rows = self._all(
            "SELECT name, lat, lon, tilt, azimuth, kwp FROM panel_groups WHERE enabled AND kwp > 0 ORDER BY id"
        )
        return [PanelGroup(**r) for r in rows]

    def forecast_hourly(self, from_ts: int, to_ts: int) -> dict[str, dict[int, float]]:
        """Мощность по провайдерам и часам: сумма по панелям, берётся последний fetched_at."""
        rows = self._all(
            "SELECT provider, target_ts, SUM(power_w) AS p FROM forecast_raw r "
            "WHERE target_ts >= %s AND target_ts < %s AND power_w IS NOT NULL AND fetched_at = ("
            "SELECT MAX(fetched_at) FROM forecast_raw WHERE provider = r.provider "
            "AND panel = r.panel AND target_ts = r.target_ts) "
            "GROUP BY provider, target_ts ORDER BY target_ts",
            (from_ts, to_ts),
        )
        result: dict[str, dict[int, float]] = {}
        for r in rows:
            result.setdefault(r["provider"], {})[r["target_ts"]] = r["p"]
        return result

    def fact_hourly(self, from_ts: int, to_ts: int) -> dict[int, float]:
        rows = self._all(
            "SELECT (ts / 3600) * 3600 AS h, AVG(pv1_power_w + pv2_power_w)::float8 AS p FROM samples "
            "WHERE ts >= %s AND ts < %s AND pv1_power_w IS NOT NULL AND pv2_power_w IS NOT NULL "
            "GROUP BY 1 ORDER BY 1",
            (from_ts, to_ts),
        )
        return {r["h"]: r["p"] for r in rows}

    def count_samples(self) -> int | None:
        """Оценка числа строк из статистики планировщика; None, если статистики ещё нет."""
        row = self._one("SELECT reltuples::bigint AS n FROM pg_class WHERE relname = 'samples'")
        if row is None or row["n"] < 0:
            return None
        return row["n"]

    def current_tariff(self, at: datetime | None = None) -> dict | None:
        """Активный тарифный план на дату `at` (локальное время TZ) вместе с зонами."""
        tz = ZoneInfo(config.TZ)
        day = (at.astimezone(tz) if at is not None and at.tzinfo else at or datetime.now(tz)).date()
        plan = self._one(
            "SELECT id, name, valid_from, valid_to, currency, notes FROM tariff_plans "
            "WHERE is_active AND valid_from <= %s AND (valid_to IS NULL OR valid_to >= %s) "
            "ORDER BY valid_from DESC LIMIT 1",
            (day, day),
        )
        if plan is None:
            return None
        zones = self._all(
            "SELECT name, start_time, end_time, price_per_kwh::float8 AS price_per_kwh, direction "
            "FROM tariff_zones WHERE plan_id = %s ORDER BY start_time, id",
            (plan["id"],),
        )
        return {**plan, "zones": zones}

    def price_at(self, ts: int | float | datetime | None = None, direction: str = "import") -> dict | None:
        """Зона и цена на момент ts (unix-секунды или datetime); локальное время TZ."""
        tz = ZoneInfo(config.TZ)
        if ts is None:
            at = datetime.now(tz)
        elif isinstance(ts, datetime):
            at = ts.astimezone(tz) if ts.tzinfo else ts.replace(tzinfo=tz)
        else:
            at = datetime.fromtimestamp(ts, tz)
        tariff = self.current_tariff(at)
        if tariff is None:
            return None
        zone = find_zone([z for z in tariff["zones"] if z["direction"] == direction], at.time())
        if zone is None:
            return None
        return {"plan": tariff["name"], "currency": tariff["currency"], **zone}

    def strategy_settings(self) -> dict | None:
        return self._one(
            "SELECT reserve_soc_pct, max_grid_charge_soc_pct, normal_floor_pct, emergency_mode, emergency_floor_pct, "
            "battery_capacity_kwh, battery_usable_pct, "
            "round_trip_efficiency_pct, control_mode, writes_enabled, decision_time, updated_at, "
            "export_price_per_kwh::float8 AS export_price_per_kwh, wear_cost_per_kwh::float8 AS wear_cost_per_kwh, "
            "risk, default_plan_soc_pct, night_target_mode, night_target_manual_pct, night_target_manual_night "
            "FROM strategy_settings ORDER BY id LIMIT 1"
        )

    def load_samples_15m(self, from_ts: int, to_ts: int) -> list[tuple[int, float]]:
        """Средняя нагрузка (Вт) по 15-минутным слотам: (начало слота, AVG load_power_w)."""
        rows = self._all(
            "SELECT (ts / 900) * 900 AS t, AVG(load_power_w)::float8 AS w FROM samples "
            "WHERE ts >= %s AND ts < %s AND load_power_w IS NOT NULL GROUP BY 1 ORDER BY 1",
            (from_ts, to_ts),
        )
        return [(r["t"], r["w"]) for r in rows]

    def pv_samples_15m(self, from_ts: int, to_ts: int) -> dict[int, float]:
        """Средняя мощность PV (pv1+pv2, Вт) по 15-минутным слотам."""
        rows = self._all(
            "SELECT (ts / 900) * 900 AS t, AVG(pv1_power_w + pv2_power_w)::float8 AS w FROM samples "
            "WHERE ts >= %s AND ts < %s AND pv1_power_w IS NOT NULL AND pv2_power_w IS NOT NULL GROUP BY 1 ORDER BY 1",
            (from_ts, to_ts),
        )
        return {r["t"]: r["w"] for r in rows}

    def grid_import_15m(self, from_ts: int, to_ts: int) -> dict[int, float]:
        """Импорт из сети (кВт·ч) по 15-минутным слотам: среднее max(grid_power_w, 0) за слот."""
        rows = self._all(
            "SELECT (ts / 900) * 900 AS t, AVG(GREATEST(grid_power_w, 0))::float8 AS w FROM samples "
            "WHERE ts >= %s AND ts < %s AND grid_power_w IS NOT NULL GROUP BY 1 ORDER BY 1",
            (from_ts, to_ts),
        )
        return {r["t"]: r["w"] * 0.25 / 1000 for r in rows}

    def forecast_asof(self, from_ts: int, to_ts: int, asof_ts: int) -> dict[str, dict[int, dict]]:
        """По провайдерам и часам: сумма по панелям/сайтам (power_w, p10, p90) из последнего fetched_at <= asof_ts."""
        rows = self._all(
            "SELECT provider, target_ts, SUM(power_w)::float8 AS p, "
            "CASE WHEN COUNT(p10) = COUNT(*) THEN SUM(p10)::float8 END AS p10, "
            "CASE WHEN COUNT(p90) = COUNT(*) THEN SUM(p90)::float8 END AS p90 "
            "FROM forecast_raw r WHERE target_ts >= %s AND target_ts < %s AND power_w IS NOT NULL "
            "AND fetched_at <= %s AND fetched_at = ("
            "SELECT MAX(fetched_at) FROM forecast_raw WHERE provider = r.provider AND panel = r.panel "
            "AND target_ts = r.target_ts AND power_w IS NOT NULL AND fetched_at <= %s) "
            "GROUP BY provider, target_ts ORDER BY target_ts",
            (from_ts, to_ts, asof_ts, asof_ts),
        )
        result: dict[str, dict[int, dict]] = {}
        for r in rows:
            result.setdefault(r["provider"], {})[r["target_ts"]] = {"p": r["p"], "p10": r["p10"], "p90": r["p90"]}
        return result

    def forecast_history_errors(self, from_ts: int, to_ts: int, tz_name: str) -> list[dict]:
        """Прогнозы провайдеров на часы [from_ts, to_ts), сделанные до 23:00 накануне (локальное время),
        вместе с фактом PV (почасовой AVG(pv1+pv2)). Строки: provider, target_ts, fetched_at, forecast_w, actual_w."""
        rows = self._all(
            "WITH cut AS ("
            " SELECT provider, panel, target_ts, MAX(fetched_at) AS f FROM forecast_raw"
            " WHERE target_ts >= %(a)s AND target_ts < %(b)s AND power_w IS NOT NULL AND fetched_at <= "
            " EXTRACT(EPOCH FROM ((date_trunc('day', to_timestamp(target_ts) AT TIME ZONE %(tz)s)"
            " - interval '1 hour') AT TIME ZONE %(tz)s))::bigint"
            " GROUP BY provider, panel, target_ts), "
            "fc AS ("
            " SELECT c.provider, c.target_ts, MAX(c.f) AS fetched_at, SUM(r.power_w)::float8 AS forecast_w"
            " FROM cut c JOIN forecast_raw r ON r.provider = c.provider AND r.panel = c.panel"
            " AND r.target_ts = c.target_ts AND r.fetched_at = c.f GROUP BY c.provider, c.target_ts), "
            "fact AS ("
            " SELECT (ts / 3600) * 3600 AS h, AVG(pv1_power_w + pv2_power_w)::float8 AS actual_w FROM samples"
            " WHERE ts >= %(a)s AND ts < %(b)s AND pv1_power_w IS NOT NULL AND pv2_power_w IS NOT NULL GROUP BY 1) "
            "SELECT fc.provider, fc.target_ts, fc.fetched_at, fc.forecast_w, fact.actual_w "
            "FROM fc JOIN fact ON fact.h = fc.target_ts ORDER BY fc.target_ts",
            {"a": from_ts, "b": to_ts, "tz": tz_name},
        )
        return rows

    def final_decision_exists(self, night_date) -> bool:
        return self._one("SELECT 1 AS x FROM charge_decisions WHERE night_date = %s AND is_final", (night_date,)) is not None

    def insert_final_decision(self, row: dict) -> bool:
        """Сохранить финальное решение; False, если на эту ночь оно уже есть."""
        cols = [
            "created_at", "night_date", "soc_at_decision", "target_pct", "expected_cost_uah", "cost_no_charge_uah",
            "cost_to_ceiling_uah", "cost_to_100_uah", "expected_saving_uah", "expected_saving_no_wear_uah",
            "expected_grid_import_kwh", "soc_at_07", "min_soc_tomorrow_worst", "explanation", "status",
        ]
        values = [row[c] for c in cols] + [Jsonb(row["inputs"]), Jsonb(row["tou_plan"])]
        marks = ", ".join(["%s"] * len(values))
        found = self._one(
            f"INSERT INTO charge_decisions({', '.join(cols)}, inputs, tou_plan, is_final) VALUES ({marks}, TRUE) "
            "ON CONFLICT (night_date) WHERE is_final DO NOTHING RETURNING id",
            values,
        )
        return found is not None

    def final_decision_meta(self, night_date) -> dict | None:
        """id, статус и отпечаток настроек (inputs.settings_fp; None у старых решений) финального решения на ночь."""
        return self._one(
            "SELECT id, status, inputs->'settings_fp' AS settings_fp FROM charge_decisions "
            "WHERE night_date = %s AND is_final",
            (night_date,),
        )

    def replace_final_decision(self, old_id: int, row: dict, note: str) -> bool:
        """Заменить финальное решение новым в одной транзакции: старое → is_final=false, status='skipped'
        (только если оно не в очереди записи), новое вставляется финальным. False — ничего не изменено."""
        cols = [
            "created_at", "night_date", "soc_at_decision", "target_pct", "expected_cost_uah", "cost_no_charge_uah",
            "cost_to_ceiling_uah", "cost_to_100_uah", "expected_saving_uah", "expected_saving_no_wear_uah",
            "expected_grid_import_kwh", "soc_at_07", "min_soc_tomorrow_worst", "explanation", "status",
        ]
        values = [row[c] for c in cols] + [Jsonb(row["inputs"]), Jsonb(row["tou_plan"])]
        marks = ", ".join(["%s"] * len(values))

        def work(cur) -> bool:
            with self._conn.transaction():
                cur.execute(
                    "UPDATE charge_decisions SET is_final = FALSE, "
                    "status = CASE WHEN status = 'applied' THEN status ELSE 'skipped' END, "
                    "apply_note = CASE WHEN apply_note = '' THEN %s ELSE apply_note || '; ' || %s END "
                    "WHERE id = %s AND is_final AND status <> 'queued'",
                    (note, note, old_id),
                )
                if cur.rowcount != 1:
                    return False
                cur.execute(
                    f"INSERT INTO charge_decisions({', '.join(cols)}, inputs, tou_plan, is_final) "
                    f"VALUES ({marks}, TRUE)",
                    values,
                )
                return True

        return self._run(work, retry=False)

    _DECISION_COLS = (
        "id, created_at, night_date, is_final, soc_at_decision, target_pct, expected_cost_uah, cost_no_charge_uah, "
        "cost_to_ceiling_uah, cost_to_100_uah, expected_saving_uah, expected_saving_no_wear_uah, "
        "expected_grid_import_kwh, soc_at_07, min_soc_tomorrow_worst, explanation, inputs, tou_plan, status, "
        "actual_cost_uah, perfect_cost_uah, regret_uah, model_cost_decided_uah, actual_coverage_pct, evaluated_at, "
        "write_request_id, applied_at, apply_note"
    )

    def latest_final_decision(self) -> dict | None:
        return self._one(
            f"SELECT {self._DECISION_COLS} FROM charge_decisions WHERE is_final ORDER BY night_date DESC LIMIT 1"
        )

    def decisions_history(self, since_date) -> list[dict]:
        return self._all(
            f"SELECT {self._DECISION_COLS} FROM charge_decisions WHERE is_final AND night_date >= %s "
            "ORDER BY night_date DESC",
            (since_date,),
        )

    def decisions_to_evaluate(self, since_date) -> list[dict]:
        return self._all(
            f"SELECT {self._DECISION_COLS} FROM charge_decisions WHERE is_final AND night_date >= %s "
            "AND regret_uah IS NULL ORDER BY night_date",
            (since_date,),
        )

    def update_decision_evaluation(self, decision_id: int, result: dict, at: datetime | None) -> None:
        self._exec(
            "UPDATE charge_decisions SET actual_cost_uah = %s, actual_coverage_pct = %s, perfect_cost_uah = %s, "
            "regret_uah = %s, model_cost_decided_uah = %s, evaluated_at = %s WHERE id = %s",
            (result["actual"], result["coverage_pct"], result["perfect"], result["regret"], result["decided"], at,
             decision_id),
        )

    def claim_write_request(self) -> dict | None:
        """Захватить самый старый pending-запрос записи (pending → running); без повтора, чтобы не захватить два."""
        return self._run(
            lambda cur: cur.execute(
                "UPDATE inverter_write_requests SET status = 'running', started_at = now() WHERE id = ("
                "SELECT id FROM inverter_write_requests WHERE status = 'pending' ORDER BY id LIMIT 1 "
                "FOR UPDATE SKIP LOCKED) RETURNING id, changes, expected_old, source"
            ).fetchone(),
            retry=False,
        )

    def save_write_backup(self, request_id: int, backup: dict[int, int]) -> None:
        self._exec(
            "UPDATE inverter_write_requests SET backup = %s WHERE id = %s",
            (Jsonb({str(k): v for k, v in sorted(backup.items())}), request_id),
        )

    def finish_write_request(self, request_id: int, status: str, readback: dict[int, int] | None,
                             error: str | None) -> None:
        self._exec(
            "UPDATE inverter_write_requests SET status = %s, readback = %s, error = %s, finished_at = now() "
            "WHERE id = %s",
            (status, None if readback is None else Jsonb({str(k): v for k, v in sorted(readback.items())}),
             error, request_id),
        )

    def fail_interrupted_writes(self) -> int:
        """При старте сервиса: запросы, оставшиеся в running, помечаются failed."""
        rows = self._all(
            "UPDATE inverter_write_requests SET status = 'failed', error = 'прервано перезапуском', "
            "finished_at = now() WHERE status = 'running' RETURNING id"
        )
        return len(rows)

    def fail_stuck_writes(self, minutes: int) -> int:
        rows = self._all(
            "UPDATE inverter_write_requests SET status = 'failed', error = 'зависший запрос', finished_at = now() "
            "WHERE status = 'running' AND started_at < now() - make_interval(mins => %s) RETURNING id",
            (minutes,),
        )
        return len(rows)

    def expire_pending_writes(self, minutes: int) -> int:
        rows = self._all(
            "UPDATE inverter_write_requests SET status = 'stale', error = 'запрос устарел, не выполнен', "
            "finished_at = now() WHERE status = 'pending' AND created_at < now() - make_interval(mins => %s) "
            "RETURNING id",
            (minutes,),
        )
        return len(rows)

    # --- автозапись ночного target планировщика ---

    _APPLY_COLS = (
        "d.id, d.night_date, d.target_pct, d.status, d.write_request_id, d.applied_at, d.apply_note, "
        "w.status AS request_status, w.error AS request_error, w.finished_at AS request_finished_at, "
        "w.changes AS request_changes, w.expected_old AS request_expected_old, w.created_by AS request_created_by, "
        "COALESCE((d.inputs->>'late_apply')::boolean, false) AS late_apply"
    )

    def final_decision_apply(self, night_date) -> dict | None:
        """Финальное решение на ночь со статусом связанной заявки на запись (или None)."""
        return self._one(
            f"SELECT {self._APPLY_COLS} FROM charge_decisions d "
            "LEFT JOIN inverter_write_requests w ON w.id = d.write_request_id "
            "WHERE d.night_date = %s AND d.is_final",
            (night_date,),
        )

    def queued_decisions(self) -> list[dict]:
        return self._all(
            f"SELECT {self._APPLY_COLS} FROM charge_decisions d "
            "LEFT JOIN inverter_write_requests w ON w.id = d.write_request_id "
            "WHERE d.is_final AND d.status = 'queued' ORDER BY d.night_date"
        )

    def write_guard_state(self) -> dict:
        """Состояние для app/write_guard.guard_errors."""
        return self._one(
            "SELECT EXISTS (SELECT 1 FROM inverter_write_requests WHERE status IN ('pending', 'running')) AS active, "
            "(SELECT count(*) FROM inverter_write_requests WHERE created_at >= now() - interval '24 hours') AS last_24h, "
            "(SELECT max(ts) FROM samples) AS last_sample_ts"
        )

    def count_write_requests(self, source: str, created_by: str) -> int:
        return self._one(
            "SELECT count(*) AS n FROM inverter_write_requests WHERE source = %s AND created_by = %s",
            (source, created_by),
        )["n"]

    def last_write_request(self, source: str, created_by_prefix: str) -> dict | None:
        """Последняя заявка источника с created_by, начинающимся с префикса (или None)."""
        return self._one(
            "SELECT id, created_by, status, error, created_at, finished_at, changes, expected_old "
            "FROM inverter_write_requests WHERE source = %s AND created_by LIKE %s ORDER BY id DESC LIMIT 1",
            (source, created_by_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"),
        )

    def insert_write_request(self, changes: dict, expected_old: dict, source: str, created_by: str,
                             decision_id: int | None = None, expect_status: str | None = None,
                             expect_request_id: int | None = None, note: str = "") -> int:
        """Создать заявку (pending) и, если задан decision_id, в той же транзакции привязать её к решению
        (status='queued'). Привязка только если решение всё ещё в expect_status и со старой заявкой
        expect_request_id — иначе откат и RuntimeError (защита от дублей)."""

        def work(cur) -> int:
            with self._conn.transaction():
                rid = cur.execute(
                    "INSERT INTO inverter_write_requests(created_at, created_by, source, changes, expected_old, status) "
                    "VALUES (now(), %s, %s, %s, %s, 'pending') RETURNING id",
                    (created_by, source, Jsonb(changes), Jsonb(expected_old)),
                ).fetchone()["id"]
                if decision_id is not None:
                    cur.execute(
                        "UPDATE charge_decisions SET write_request_id = %s, status = 'queued', apply_note = %s "
                        "WHERE id = %s AND status = %s AND write_request_id IS NOT DISTINCT FROM %s",
                        (rid, note, decision_id, expect_status, expect_request_id),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(f"решение {decision_id} уже изменено — заявка не создана")
                return rid

        return self._run(work, retry=False)

    def set_decision_apply(self, decision_id: int, status: str, note: str, applied_at: datetime | None = None,
                           expect_status: str | None = None) -> bool:
        """Статус записи решения; если задан expect_status — только из него. True, если строка обновлена."""
        def work(cur) -> bool:
            cur.execute(
                "UPDATE charge_decisions SET status = %s, apply_note = %s, applied_at = COALESCE(%s, applied_at) "
                "WHERE id = %s AND (%s::text IS NULL OR status = %s::text)",
                (status, note, applied_at, decision_id, expect_status, expect_status),
            )
            return cur.rowcount == 1

        return self._run(work)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
