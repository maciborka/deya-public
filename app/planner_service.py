"""Сервис планировщика ночного заряда: сбор входов из БД, расчёт, сохранение решений, оценка задним числом.

В режиме «Авто» (control_mode='auto' и writes_enabled) ночной target решения ставится в очередь записи
(inverter_write_requests, source='planner') — только SOC ночных TOU-слотов с зарядом от сети; исполняет
app/inverter_writer.py. Ядро расчёта — `app/planner/`.
"""
import asyncio
import logging
import time
from dataclasses import asdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config
from .db import Database, find_zone
from .inverter_modes import decode_tou_slots
from .planner.load import build_load_profile
from .planner.optimize import Decision, _load_series, _series, evaluate_target, perfect_forecast_cost, plan_night
from .planner.params import PlannerParams
from .planner.pv import ForecastErr, consensus, provider_weights, scenarios
from .planner.sim import simulate
from .planner.timeutil import SLOT_SEC, at_local, local, slot_floor, zone
from .write_guard import guard_errors

log = logging.getLogger("deya.planner")

LOAD_DAYS = 14
HISTORY_DAYS = 14
MAX_SOC_AGE_SEC = 3600
ACTUAL_MIN_COVERAGE = 0.7
LOAD_MIN_COVERAGE = 0.9
NIGHT_START_HOUR = PlannerParams.night_start_hour
SHORT_LOAD_HISTORY_DAYS = 7
CHARGE_EPS_PCT = 0.5
TICK_SEC = 60
DECIDE_RETRY_SEC = 300
EVAL_RETRY_SEC = 1800
EVAL_LOOKBACK_DAYS = 7
EVAL_FULL_DELAY_SEC = 900
EVAL_PARTIAL_DELAY_SEC = 1800
APPLY_END = dtime(23, 30)  # окно автозаписи: от decision_time до 23:30 местного (отдельно от окна решения)
MANUAL_APPLY_END = dtime(6, 30)  # ручная ночная цель: окно пересчёта и записи — до 06:30 утра ночи
APPLY_RETRY_DELAY_SEC = 60  # пауза после неудачной заявки: коллектор успевает обновить снимок настроек
MAX_APPLY_ATTEMPTS = 2  # первая попытка + одна повторная
PLANNER_SOURCE = "planner"
SOC_REG = 166  # SOC слота n — регистр 166 + n − 1
FLOOR_PREFIX = "floor:"  # created_by заявок floor-sync: floor:<YYYY-MM-DD>
MAX_FLOOR_REQUESTS_PER_DAY = 3
FLOOR_RETRY_DELAY_SEC = 60


class TariffPrice:
    """Цена импорта на момент ts (грн/кВт·ч); тариф читается из БД один раз на локальную дату."""

    def __init__(self, db: Database, tz_name: str):
        self._db = db
        self._tz_name = tz_name
        self._tz = ZoneInfo(tz_name)
        self._zones: dict[date, list[dict]] = {}

    def __call__(self, ts: int) -> float:
        at = datetime.fromtimestamp(ts, self._tz)
        zones = self._zones.get(at.date())
        if zones is None:
            tariff = self._db.current_tariff(at)
            if tariff is None:
                raise ValueError("нет активного тарифного плана")
            zones = [z for z in tariff["zones"] if z["direction"] == "import"]
            self._zones[at.date()] = zones
        zone = find_zone(zones, at.time())
        if zone is None:
            raise ValueError("тарифная зона на момент %s не найдена" % at.isoformat())
        return float(zone["price_per_kwh"])


def _reg_number(regs: dict, addr: int) -> float | None:
    v = regs.get(str(addr))
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


GRID_SOURCES = (1, 3)


def tou_floor_slots(regs: dict) -> list[dict] | None:
    """TOU-слоты из снимка настроек (начало, конец, SOC, источник); None, если слоты в снимке неполные."""
    slots = decode_tou_slots(regs)
    if any(s["start"] is None or s["soc_pct"] is None for s in slots):
        return None
    return [
        {
            "start": s["start"], "end": s["end"], "soc_pct": float(s["soc_pct"]), "source": s["source"],
            "grid_charge": s["source"] in GRID_SOURCES,
        }
        for s in slots
    ]


def effective_floor(st: dict) -> float:
    """Пол разряда, который система выставляет: аварийный при режиме «всегда» (и пока что «по графику»,
    консервативно), иначе обычный."""
    if st["emergency_mode"] == "off":
        return float(st["normal_floor_pct"])
    return float(st["emergency_floor_pct"])


def floor_mismatch(slots: list[dict] | None, floor_pct: float) -> bool:
    """Пороги разряда в слотах без заряда от сети отличаются от пола, который выставит система."""
    return bool(slots) and any(not s["grid_charge"] and s["soc_pct"] != float(floor_pct) for s in slots)


def build_night_changes(regs: dict, target: float, st: dict, params: PlannerParams):
    """Изменения SOC ночных TOU-слотов с зарядом от сети под target решения.

    → (changes, expected_old) — {"рег": новое}, {"рег": старое}; пустые словари — всё уже установлено;
    → ("skip", причина) — записывать нечего. Времена, источники и мощность не трогаются.
    Значение = round(clamp(target, эффективный пол, max_grid_charge_soc_pct)).
    """
    slots = tou_floor_slots(regs)
    if slots is None:
        return "skip", "в снимке настроек нет полного набора TOU-слотов"
    night = [(i, s) for i, s in enumerate(slots) if s["grid_charge"] and params.is_night(int(s["start"][:2]))]
    if not night:
        return "skip", "в инверторе нет ночного слота с зарядом от сети"
    lo, hi = effective_floor(st), float(st["max_grid_charge_soc_pct"])
    value = int(round(min(hi, max(lo, float(target)))))
    changes, expected_old = {}, {}
    for i, _s in night:
        reg = str(SOC_REG + i)
        old = int(regs[reg])
        if old != value:
            changes[reg] = value
            expected_old[reg] = old
    return changes, expected_old


NIGHT_TARGET_FP_KEYS = ("night_target_mode", "night_target_manual_pct", "night_target_manual_night")
CLOUDY_FP_KEY = "cloudy_full_charge_kwh"


def _cloudy_threshold(st: dict) -> float | None:
    """Порог пасмурного дня из настроек: None/0 → правило выключено."""
    v = st.get("cloudy_full_charge_kwh")
    return float(v) if v else None


def manual_night_target(st: dict, night: date) -> float | None:
    """Ручная ночная цель SOC из настроек для ночи night или None (авто).

    always — всегда; once — только если night_target_manual_night == night (прошедшая ночь — истекло, авто).
    """
    pct = st.get("night_target_manual_pct")
    mode = st.get("night_target_mode") or "auto"
    if pct is None:
        return None
    if mode == "always" or (mode == "once" and st.get("night_target_manual_night") == night):
        return float(pct)
    return None


def settings_fingerprint(st: dict) -> dict:
    """Настройки, от которых зависит ночное решение (хранится в inputs.settings_fp; смена → пересчёт решения).

    Ручная цель: режим; значение — при once/always; ночь — только при once.
    """
    mode = st.get("night_target_mode") or "auto"
    night = st.get("night_target_manual_night")
    return {
        "floor": float(effective_floor(st)),
        "emergency_mode": st["emergency_mode"],
        "max_grid_charge_soc_pct": float(st["max_grid_charge_soc_pct"]),
        "risk": st["risk"],
        CLOUDY_FP_KEY: _cloudy_threshold(st),
        "night_target_mode": mode,
        "night_target_manual_pct": st.get("night_target_manual_pct") if mode != "auto" else None,
        "night_target_manual_night": night.isoformat() if mode == "once" and night is not None else None,
    }


def _night_target_fp(fp: dict | None) -> tuple:
    """Часть отпечатка, относящаяся к ручной цели (у старых решений ключей нет — как авто)."""
    fp = fp or {}
    return (fp.get("night_target_mode") or "auto",) + tuple(fp.get(k) for k in NIGHT_TARGET_FP_KEYS[1:])


def _same_fp(old: dict | None, new: dict) -> bool:
    """Отпечатки совпадают; у решений до ручной цели её ключей нет — считаются авто, а до порога пасмурного дня
    его ключа нет — совпадают с любым (без лишнего пересчёта при выкатке)."""
    if old is None:
        return False
    skip = NIGHT_TARGET_FP_KEYS + (CLOUDY_FP_KEY,)
    rest = lambda fp: {k: v for k, v in fp.items() if k not in skip}
    if CLOUDY_FP_KEY in old and old[CLOUDY_FP_KEY] != new.get(CLOUDY_FP_KEY):
        return False
    return rest(old) == rest(new) and _night_target_fp(old) == _night_target_fp(new)


def build_floor_changes(regs: dict, st: dict, params: PlannerParams):
    """Изменения SOC для приведения порогов разряда инвертора к эффективному полу.

    → (changes, expected_old) — {"рег": новое}, {"рег": старое}; пустые словари — всё уже соответствует
    (или в снимке нет полного набора слотов). Слоты без заряда от сети → пол; ночные слоты с зарядом от сети
    с SOC ниже пола → пол (иначе при включении аварийного режима ночью АКБ разрядится ниже пола).
    """
    slots = tou_floor_slots(regs)
    if slots is None:
        return {}, {}
    floor = int(round(effective_floor(st)))
    changes, expected_old = {}, {}
    for i, s in enumerate(slots):
        night_grid = s["grid_charge"] and params.is_night(int(s["start"][:2]))
        if not s["grid_charge"]:
            need = int(s["soc_pct"]) != floor
        else:
            need = night_grid and s["soc_pct"] < floor
        if need:
            reg = str(SOC_REG + i)
            changes[reg] = floor
            expected_old[reg] = int(regs[reg])
    return changes, expected_old


def build_params(st: dict, regs: dict) -> PlannerParams:
    floor = effective_floor(st)
    kwargs: dict = {
        "capacity_kwh": float(st["battery_capacity_kwh"]),
        "usable_frac": st["battery_usable_pct"] / 100,
        "roundtrip_eff": st["round_trip_efficiency_pct"] / 100,
        "export_price_uah": float(st["export_price_per_kwh"]),
        "reserve_soc_pct": floor,  # эффективный пол: на нём floor_pct, сетка/clamp target, terminal V
        "floor_profile": [(0, 24 * 60, floor)],  # заготовка профиля; окна отключений (schedule) — позже
        "night_ceiling_pct": float(st["max_grid_charge_soc_pct"]),
        "wear_cost_per_kwh": float(st["wear_cost_per_kwh"]),
        "risk": st["risk"],
        "default_plan_soc": float(st["default_plan_soc_pct"]),
        "tz": config.TZ,
        "cloudy_full_charge_kwh": _cloudy_threshold(st),
    }
    shutdown = _reg_number(regs, 115)
    if shutdown is not None:
        kwargs["shutdown_soc_pct"] = shutdown
    amps = _reg_number(regs, 128)
    if amps:
        kwargs["grid_charge_current_a"] = amps
    return PlannerParams(**kwargs)


def night_date_for(now_ts: int, params: PlannerParams) -> date:
    """Локальная дата ночи (дата, в 23:00 которой она начинается), содержащей now или ближайшей следующей."""
    now = local(now_ts, params.tz)
    return now.date() - timedelta(days=1) if now.hour < params.night_end_hour else now.date()


def _tomorrow_kwh(series: dict[int, float], day: date, tz: str) -> float:
    return sum(v for ts, v in series.items() if local(ts, tz).date() == day) * SLOT_SEC / 3600 / 1000


def _pv_scenarios(db: Database, fc: dict, tz: str, today: date) -> tuple[dict, dict, dict]:
    """Веса провайдеров (по истории ошибок за HISTORY_DAYS до today), консенсус и сценарии p10/p50/p90 по слотам."""
    errors = [
        ForecastErr(r["provider"], r["target_ts"], r["fetched_at"], r["forecast_w"], r["actual_w"])
        for r in db.forecast_history_errors(at_local(today - timedelta(days=HISTORY_DAYS), 0, tz), at_local(today, 0, tz), tz)
        if r["provider"] in fc
    ]
    weights = provider_weights(errors, sorted(fc), tz)
    cons = consensus({p: {ts: v["p"] for ts, v in h.items()} for p, h in fc.items()}, weights)
    sol = fc.get("solcast")
    if sol:
        scen = scenarios(
            cons,
            {ts: v["p10"] for ts, v in sol.items()},
            {ts: v["p"] for ts, v in sol.items()},
            {ts: v["p90"] for ts, v in sol.items()},
        )
    else:
        scen = scenarios(cons)
    return weights, cons, scen


def _gather(db: Database, now_ts: int, until_ts: int | None = None) -> dict:
    """Собрать входы планировщика из БД. Бросает ValueError, если данных недостаточно.

    Окно запроса прогноза PV: от полуночи даты ночи до max(полночь d+3, until_ts + 1 ч).
    """
    st = db.strategy_settings()
    if st is None:
        raise ValueError("настройки стратегии не заданы")
    snapshot = db.latest_settings()
    regs = snapshot["regs"] if snapshot else {}
    params = build_params(st, regs)
    tz = params.tz

    sample = db.latest_sample()
    if sample is None or sample["soc_pct"] is None or now_ts - sample["ts"] > MAX_SOC_AGE_SEC:
        raise ValueError("нет свежего значения SOC (последний замер старше часа или отсутствует)")
    soc = float(sample["soc_pct"])

    price = TariffPrice(db, tz)
    d = night_date_for(now_ts, params)

    profile = build_load_profile(db.load_samples_15m(now_ts - LOAD_DAYS * 86400, now_ts), now_ts, tz, LOAD_DAYS)

    enabled = {r["code"] for r in db.forecast_providers() if r["enabled"]}
    from_ts = at_local(d, 0, tz)
    to_ts = at_local(d + timedelta(days=3), 0, tz)
    if until_ts is not None:
        to_ts = max(to_ts, until_ts + 3600)
    fc = {p: h for p, h in db.forecast_asof(from_ts, to_ts, now_ts).items() if p in enabled}
    if not fc:
        raise ValueError("нет прогноза PV от включённых провайдеров")
    weights, cons, scen = _pv_scenarios(db, fc, tz, local(now_ts, tz).date())
    return {
        "st": st, "regs": regs, "params": params, "soc": soc, "price": price, "night_date": d,
        "profile": profile, "fc": fc, "weights": weights, "cons": cons, "scen": scen,
    }


PV_HOURLY_MAX_GAP_SEC = 300  # интервалы между замерами длиннее — пропуск данных, не интегрируются


def _hour_bounds(day: date, tz: str) -> list[int]:
    """Границы локальных часов суток day (24/23/25 часов; в Киеве смещение кратно часу)."""
    start, end = at_local(day, 0, tz), at_local(day + timedelta(days=1), 0, tz)
    return list(range(start, end + 1, 3600))


def _bin_kwh(series: dict[int, float], bounds: list[int]) -> list[float | None]:
    """Ряд слотов (Вт) → кВт·ч по часам bounds; None, если в часе нет слотов."""
    sums: list[float | None] = [None] * (len(bounds) - 1)
    for ts, w in series.items():
        i = (ts - bounds[0]) // 3600
        if 0 <= i < len(sums):
            sums[i] = (sums[i] or 0.0) + w * SLOT_SEC / 3600 / 1000
    return sums


def _actual_hourly_kwh(rows: list[dict], bounds: list[int]) -> list[float | None]:
    """Выработка PV (pv1+pv2) по часам: трапеции между замерами, интервал делится по границам часов; без данных → None."""
    sums: list[float | None] = [None] * (len(bounds) - 1)
    for a, b in zip(rows, rows[1:]):
        dt = b["ts"] - a["ts"]
        if dt <= 0 or dt > PV_HOURLY_MAX_GAP_SEC:
            continue
        pa = [r for r in (a.get("pv1_power_w"), a.get("pv2_power_w")) if r is not None]
        pb = [r for r in (b.get("pv1_power_w"), b.get("pv2_power_w")) if r is not None]
        if not pa or not pb:
            continue
        w = max((sum(pa) + sum(pb)) / 2, 0.0)
        for i in range(len(sums)):
            lo, hi = max(a["ts"], bounds[i]), min(b["ts"], bounds[i + 1])
            if hi > lo:
                sums[i] = (sums[i] or 0.0) + w * (hi - lo) / 3600 / 1000
    return sums


def _rnd(v: float | None, n: int = 3) -> float | None:
    return None if v is None else round(v, n)


def compute_pv_hourly(db: Database, day: date, now_ts: int) -> dict:
    """Почасовой прогноз PV vs факт за локальные сутки day.

    Прогноз — как его видел планировщик: на момент decision_time вечером накануне (если он ещё впереди — на now).
    Факт — интеграл pv1+pv2 по локальным часам; для не начавшихся часов None.
    """
    tz = config.TZ
    st = db.strategy_settings()
    decision_time = st["decision_time"] if st else dtime(22, 45)
    asof = int(datetime.combine(day - timedelta(days=1), decision_time, tzinfo=zone(tz)).timestamp())
    asof = min(asof, now_ts)
    bounds = _hour_bounds(day, tz)
    n = len(bounds) - 1
    warnings: list[str] = []

    enabled = {r["code"] for r in db.forecast_providers() if r["enabled"]}
    fc = {p: h for p, h in db.forecast_asof(bounds[0], bounds[-1], asof).items() if p in enabled}
    p10 = p50 = p90 = [None] * n
    prov: dict[str, list[float | None]] = {}
    if fc:
        _, cons, scen = _pv_scenarios(db, fc, tz, local(asof, tz).date())
        p10, p50, p90 = (_bin_kwh(scen[k], bounds) for k in ("p10", "p50", "p90"))
        for p, h in sorted(fc.items()):
            prov[p] = _bin_kwh(consensus({p: {ts: v["p"] for ts, v in h.items()}}), bounds)
    else:
        warnings.append("нет прогноза PV на этот день от включённых провайдеров")

    rows = db.samples_between(bounds[0] - PV_HOURLY_MAX_GAP_SEC, bounds[-1] + PV_HOURLY_MAX_GAP_SEC, 400000,
                              ["pv1_power_w", "pv2_power_w"])
    actual = _actual_hourly_kwh(rows, bounds)
    actual = [a if bounds[i] <= now_ts else None for i, a in enumerate(actual)]

    hours = [{
        "ts": bounds[i],
        "hour": local(bounds[i], tz).strftime("%H:00"),
        "actual_kwh": _rnd(actual[i]),
        "p50_kwh": _rnd(p50[i]),
        "p10_kwh": _rnd(p10[i]),
        "p90_kwh": _rnd(p90[i]),
        "providers": {p: _rnd(v[i]) for p, v in prov.items() if v[i] is not None},
    } for i in range(n)]

    def total(vals) -> float | None:
        got = [v for v in vals if v is not None]
        return round(sum(got), 3) if got else None

    done = [i for i in range(n) if bounds[i + 1] <= now_ts and actual[i] is not None and p50[i] is not None]
    done_p50 = sum(p50[i] for i in done)
    totals = {
        "actual_kwh": total(actual),
        "p50_kwh": total(p50),
        "p10_kwh": total(p10),
        "p90_kwh": total(p90),
        "providers": {p: total(v) for p, v in prov.items()},
        "actual_vs_p50_pct": round(sum(actual[i] for i in done) / done_p50 * 100, 1) if done_p50 > 0 else None,
    }
    return {"date": day.isoformat(), "forecast_asof_ts": asof, "hours": hours, "totals": totals, "warnings": warnings}


def compute(db: Database, now_ts: int, _inputs: dict | None = None) -> dict:
    """Собрать входы и посчитать решение. Бросает ValueError, если данных недостаточно."""
    g = _inputs or _gather(db, now_ts)
    st, regs, params, soc, price, d = g["st"], g["regs"], g["params"], g["soc"], g["price"], g["night_date"]
    profile, fc, weights, scen = g["profile"], g["fc"], g["weights"], g["scen"]

    dec = plan_night(now_ts, soc, scen, profile.p50, profile.p75, params, price,
                     manual_target=manual_night_target(st, d))
    slots = tou_floor_slots(regs) or []
    day_grid = [s["start"] for s in slots if s["grid_charge"] and not params.is_night(int(s["start"][:2]))]
    if day_grid:
        # Дневной дозаряд от сети модель не симулирует — предупреждаем, что импорт занижен.
        dec.explanation += (f" Внимание: в дневных слотах ({', '.join(day_grid)}) включён заряд от сети — "
                            "этот импорт в плане не учтён.")
    return {
        "decision": dec, "params": params, "soc": soc, "night_date": d, "now_ts": now_ts,
        "profile": profile, "weights": weights, "scenarios": scen, "providers": sorted(fc),
        "tou_slots": tou_floor_slots(regs), "reserve_soc_pct": params.reserve_soc_pct,
        "emergency_mode": st["emergency_mode"], "settings_fp": settings_fingerprint(st),
    }


def _hourly_energy(plan, slots: list[int], params: PlannerParams, price, dt: float) -> list[dict]:
    """Почасовая (локальные часы) разбивка энергии траектории plan, кВт·ч; см. docstring compute_forecast24."""
    tzi = zone(params.tz)
    keys = ("pv_kwh", "load_kwh", "grid_import_kwh", "batt_charge_kwh", "batt_discharge_kwh",
            "batt_charge_from_pv_kwh", "batt_charge_from_grid_kwh", "load_from_pv_kwh",
            "load_from_batt_kwh", "load_from_grid_kwh", "cost_uah")
    hours: list[dict] = []
    last_key = None
    for i, ts in enumerate(slots):
        lt = local(ts, tzi)
        key = (lt.date(), lt.hour, lt.utcoffset())
        if key != last_key:
            hours.append({"ts": ts, "hour": f"{lt.hour:02d}:00", **{k: 0.0 for k in keys}, "soc_end_pct": 0.0})
            last_key = key
        h = hours[-1]
        pv_k, load_k, imp = plan.pv_by_slot[i], plan.load_by_slot[i], plan.import_by_slot[i]
        db_k = (plan.soc_pct[i + 1] - plan.soc_pct[i]) * params.kwh_per_pct
        chg, dis = max(0.0, db_k), max(0.0, -db_k)
        chg_pv = min(chg, max(0.0, pv_k - load_k))
        l_pv = min(pv_k, load_k)
        l_batt = min(load_k - l_pv, dis)
        h["pv_kwh"] += pv_k
        h["load_kwh"] += load_k
        h["grid_import_kwh"] += imp
        h["batt_charge_kwh"] += chg
        h["batt_discharge_kwh"] += dis
        h["batt_charge_from_pv_kwh"] += chg_pv
        h["batt_charge_from_grid_kwh"] += chg - chg_pv
        h["load_from_pv_kwh"] += l_pv
        h["load_from_batt_kwh"] += l_batt
        h["load_from_grid_kwh"] += max(0.0, load_k - l_pv - l_batt)
        h["cost_uah"] += imp * price(ts)
        h["soc_end_pct"] = plan.soc_pct[i + 1]
    for h in hours:
        for k in keys:
            h[k] = round(h[k], 3)
        h["soc_end_pct"] = round(h["soc_end_pct"], 2)
    return hours


def compute_forecast24(
    db: Database, now_ts: int | None = None, target_pct: float | None = None, hours: int = 24
) -> dict:
    """Прогноз SOC/потоков АКБ на скользящие `hours` ч от now (шаг slot_sec). Ничего не пишет.

    points[i] — слот [ts, ts+slot_sec): soc_* — SOC на НАЧАЛО слота, потоки — средние за слот;
    SOC на конец окна — в soc_end. Траектории: as_is (ночной target из TOU-слота инвертора с зарядом от сети;
    нет такого слота — заряда нет), plan (target_pct из запроса или решение plan_night), полоса soc_lo/soc_hi —
    plan при PV min/max по провайдерам. Ошибки данных — ValueError.

    hourly / energy_24h — разбивка энергии плана (кВт·ч) по локальным часам / за всё окно, считается по слотам.
    Заряд/разряд АКБ берётся из дельты SOC (энергия в АКБ, уже после КПД), поэтому источники заряда и покрытия
    нагрузки — приближение: заряд из PV = min(заряд, PV−нагрузка), остальное — из сети; нагрузка покрывается
    сначала PV, затем разрядом АКБ, остаток — сеть.
    """
    now_ts = now_ts or int(time.time())
    g = _gather(db, now_ts, now_ts + hours * 3600)
    dec: Decision | None = None if target_pct is not None else compute(db, now_ts, _inputs=g)["decision"]
    params: PlannerParams = g["params"]
    price, regs, fc, soc = g["price"], g["regs"], g["fc"], g["soc"]
    step = params.slot_sec
    dt = step / 3600
    start = slot_floor(now_ts, params.tz, step)
    n = hours * 3600 // step
    slots = [start + i * step for i in range(n)]
    warnings: list[str] = []

    load = _load_series(g["profile"].p50, slots, params)
    pv_raw = _series(g["scen"]["p50"], slots, params, -1.0)
    miss = sum(1 for v in pv_raw if v < 0)
    if miss:
        warnings.append(f"Нет прогноза PV на {miss} из {n} слотов окна — там взят 0.")
    pv = [max(0.0, v) for v in pv_raw]
    per_prov = {p: _series({ts: x["p"] for ts, x in h.items()}, slots, params, -1.0) for p, h in fc.items()}
    p10 = _series(g["scen"]["p10"], slots, params, -1.0)
    p90 = _series(g["scen"]["p90"], slots, params, -1.0)
    pv_min, pv_max = [], []
    for i in range(n):
        vals = [v[i] for v in per_prov.values() if v[i] >= 0]
        if not vals:
            pv_min.append(pv[i])
            pv_max.append(pv[i])
            continue
        vals.append(pv[i])
        lo_v = vals + [p10[i]] if p10[i] >= 0 else vals
        hi_v = vals + [p90[i]] if p90[i] >= 0 else vals
        pv_min.append(min(lo_v))
        pv_max.append(max(hi_v))

    tou = tou_floor_slots(regs)
    as_is_target, as_is_tou = 0.0, False
    if tou is None:
        warnings.append("Нет снимка TOU инвертора — as_is без ночного заряда.")
    else:
        night_all = sorted((s for s in tou if params.is_night(int(s["start"][:2]))), key=lambda s: s["start"])
        if night_all:
            ref = 23 * 60 + 30
            mins = lambda s: int(s["start"][:2]) * 60 + int(s["start"][3:])
            chosen = max((s for s in night_all if mins(s) <= ref), key=mins, default=night_all[-1])
            if len({s["soc_pct"] for s in night_all}) > 1 or len({s["grid_charge"] for s in night_all}) > 1:
                desc = ", ".join(f"{s['start']} {s['soc_pct']:g} %{'' if s['grid_charge'] else ' без сети'}" for s in night_all)
                warnings.append(f"Ночные слоты TOU различаются ({desc}) — as_is по слоту {chosen['start']}.")
        else:
            chosen = None
        if chosen is not None and chosen["grid_charge"]:
            as_is_target, as_is_tou = chosen["soc_pct"], True
        else:
            warnings.append("В инверторе нет ночного слота с зарядом от сети — траектория as_is без ночного заряда.")
    if g["profile"].fallback:
        warnings.append("Нет достаточной истории нагрузки — взят плоский профиль.")
    if target_pct is not None:
        plan_target, source = min(100.0, max(0.0, float(target_pct))), "what_if"
    else:
        plan_target, source = dec.target_pct, "planner"  # dec задан, когда target_pct is None

    as_is = simulate(soc, start, pv, load, params, price, as_is_target, tou=as_is_tou)
    plan = simulate(soc, start, pv, load, params, price, plan_target)
    lo = simulate(soc, start, pv_min, load, params, price, plan_target)
    hi = simulate(soc, start, pv_max, load, params, price, plan_target)

    tzi = zone(params.tz)
    points = []
    for i, ts in enumerate(slots):
        lt = local(ts, tzi)
        floor = params.floor_pct_at(lt.hour * 60 + lt.minute)
        p = plan.soc_pct[i]
        points.append({
            "ts": ts,
            "pv_w": round(pv[i], 1), "pv_min_w": round(pv_min[i], 1), "pv_max_w": round(pv_max[i], 1),
            "load_w": round(load[i], 1),
            "soc_as_is": round(as_is.soc_pct[i], 2),
            "soc_plan": round(p, 2),
            "soc_lo": round(min(lo.soc_pct[i], p), 2),
            "soc_hi": round(max(hi.soc_pct[i], p), 2),
            "batt_kw_plan": round((plan.soc_pct[i + 1] - p) * params.kwh_per_pct / dt, 3),
            "grid_import_kw_plan": round(plan.import_by_slot[i] / dt, 3),
            "floor_pct": round(floor, 2),
            "price": round(price(ts), 4),
        })
    hourly = _hourly_energy(plan, slots, params, price, dt)
    energy_keys = [k for k in hourly[0] if k not in ("ts", "hour", "soc_end_pct")] if hourly else []
    energy_24h = {k: round(sum(h[k] for h in hourly), 3) for k in energy_keys}
    return {
        "now_ts": now_ts, "slot_sec": step,
        "hourly": hourly, "energy_24h": energy_24h,
        "target_as_is_pct": round(as_is_target, 2), "target_plan_pct": round(plan_target, 2),
        "target_source": source,
        "soc_end": {"as_is": round(as_is.end_soc, 2), "plan": round(plan.end_soc, 2)},
        "points": points,
        "totals": {
            "as_is": {"import_kwh": round(as_is.grid_import_kwh, 3), "cost_uah": round(as_is.cost_uah, 2)},
            "plan": {"import_kwh": round(plan.grid_import_kwh, 3), "cost_uah": round(plan.cost_uah, 2)},
        },
        "providers_kwh": {p: round(sum(x for x in v if x >= 0) * dt / 1000, 2) for p, v in sorted(per_prov.items())},
        "warnings": warnings,
    }


def _inputs_summary(res: dict) -> dict:
    dec: Decision = res["decision"]
    params: PlannerParams = res["params"]
    profile = res["profile"]
    weights = res["weights"]
    tomorrow = res["night_date"] + timedelta(days=1)
    grid_kwh = max(0.0, dec.target_pct - dec.soc_at_23_pct) * params.kwh_per_pct / params.eta_charge
    warnings = []
    if profile.fallback:
        warnings.append("Нет достаточной истории нагрузки — взят плоский профиль.")
    elif profile.days < SHORT_LOAD_HISTORY_DAYS:
        warnings.append(f"Мало истории нагрузки: {profile.days} дн.")
    equal = len(weights) > 1 and max(weights.values()) - min(weights.values()) < 1e-9
    if equal:
        warnings.append("Мало истории ошибок прогноза — веса провайдеров равные.")
    if dec.day2_forecast_missing:
        warnings.append("Нет прогноза на послезавтра.")
    if dec.pv_coverage_incomplete:
        warnings.append("Прогноз PV на завтра неполный.")
    slots = res["tou_slots"]
    reserve = res["reserve_soc_pct"]
    mismatch = floor_mismatch(slots, reserve)
    if mismatch:
        day = sorted({s["soc_pct"] for s in slots if not s["grid_charge"]})
        warnings.append(
            f"Порог разряда в инверторе {'/'.join(f'{v:g}' for v in day)} %, система выставит {reserve:g} % — "
            "план посчитан по порогу из настроек."
        )
    for w in dec.warnings:
        if w not in warnings:
            warnings.append(w)
    hours = [
        {k: (round(v, 3) if isinstance(v, float) else v) for k, v in h.items()} for h in dec.hourly
    ]
    return {
        "day_import_prob": round(float(dec.day_import_prob), 4),
        "expected_day_import_kwh": round(float(dec.expected_day_import_kwh), 3),
        "day_import_constraint_met": dec.day_import_constraint_met,
        "day_import_tol": round(float(dec.day_import_tol), 4),
        "candidate_day_import_prob": {f"{k:g}": round(v, 4) for k, v in dec.candidate_day_import_prob.items()},
        "manual": dec.manual,
        "auto_target_pct": dec.auto_target_pct,
        "auto_day_import_prob": round(float(dec.auto_day_import_prob), 4) if dec.auto_day_import_prob is not None else None,
        "late_apply": bool(res.get("late_apply")) or dec.manual,
        "params": asdict(params),
        "providers": res["providers"],
        "provider_weights": {p: round(w, 4) for p, w in weights.items()},
        "weights_equal": equal,
        "pv_tomorrow_kwh": {k: round(_tomorrow_kwh(v, tomorrow, params.tz), 2) for k, v in res["scenarios"].items()},
        "load_tomorrow_kwh": round(sum(profile.p50) * SLOT_SEC / 3600 / 1000, 2),
        "load_profile_days": profile.days,
        "load_fallback": profile.fallback,
        "day2_forecast_missing": dec.day2_forecast_missing,
        "pv_coverage_incomplete": dec.pv_coverage_incomplete,
        "soc_at_23_pct": round(dec.soc_at_23_pct, 2),
        "margin_uah_per_kwh": round(dec.margin_uah_per_kwh, 3),
        "tomorrow_full_p50": dec.tomorrow_full_p50,
        "cloudy_full": dec.cloudy_full,
        "cloudy_pre_rule_target_pct": dec.cloudy_pre_rule_target_pct,
        "pv_p50_day_kwh": round(dec.pv_p50_day_kwh, 2) if dec.pv_p50_day_kwh is not None else None,
        "counterfactuals": {k: round(v, 2) for k, v in dec.counterfactuals.items()},
        "charge_from_grid": dec.target_pct > dec.soc_at_23_pct + CHARGE_EPS_PCT,
        "grid_charge_kwh": round(grid_kwh, 2),
        "floor_by_slot": slots,
        "reserve_soc_pct": reserve,
        "floor_mismatch": mismatch,
        "settings_fp": res.get("settings_fp"),
        "hourly_plan": {
            "hours": hours,
            "total_import_kwh": round(sum(h["grid_import_kwh"] for h in dec.hourly), 3),
            "total_cost_uah": round(sum(h["cost_uah"] for h in dec.hourly), 2),
        },
        "warnings": warnings,
    }


def to_row(res: dict) -> dict:
    dec: Decision = res["decision"]
    return {
        "created_at": datetime.fromtimestamp(res["now_ts"], timezone.utc),
        "night_date": res["night_date"],
        "soc_at_decision": res["soc"],
        "target_pct": dec.target_pct,
        "expected_cost_uah": dec.expected_cost_uah,
        "cost_no_charge_uah": dec.counterfactuals["no_charge"],
        "cost_to_ceiling_uah": dec.counterfactuals["ceiling"],
        "cost_to_100_uah": dec.counterfactuals["full"],
        "expected_saving_uah": dec.savings_vs_no_charge_uah,
        "expected_saving_no_wear_uah": dec.savings_vs_no_charge_no_wear_uah,
        "expected_grid_import_kwh": dec.expected_grid_import_kwh,
        "soc_at_07": dec.soc_at_07_pct,
        "min_soc_tomorrow_worst": dec.min_soc_tomorrow_worst_pct,
        "explanation": dec.explanation,
        "inputs": _inputs_summary(res),
        "tou_plan": [asdict(t) for t in dec.tou_plan],
        "status": "advice",
    }


def serialize(row: dict, preview: bool = False) -> dict:
    """Строка решения → JSON-ответ API."""
    out = dict(row)
    out["preview"] = preview
    out["is_final"] = row.get("is_final", False) and not preview
    out.setdefault("id", None)
    for key in ("created_at", "evaluated_at", "applied_at"):
        out[key] = out[key].isoformat() if out.get(key) else None
    out["night_date"] = row["night_date"].isoformat()
    out["warnings"] = (row.get("inputs") or {}).get("warnings", [])
    out["hourly_plan"] = (row.get("inputs") or {}).get("hourly_plan")
    out["manual"] = bool((row.get("inputs") or {}).get("manual"))
    out["auto_target_pct"] = (row.get("inputs") or {}).get("auto_target_pct")
    out["cloudy_full"] = bool((row.get("inputs") or {}).get("cloudy_full"))
    out["pv_p50_day_kwh"] = (row.get("inputs") or {}).get("pv_p50_day_kwh")
    return out


def compute_preview(db: Database, now_ts: int | None = None) -> dict:
    return serialize(to_row(compute(db, now_ts or int(time.time()))), preview=True)


def compute_and_save(db: Database, now_ts: int) -> bool:
    row = to_row(compute(db, now_ts))
    return db.insert_final_decision(row)


def actual_import_cost(
    imports: dict[int, float], slots: list[int], price: TariffPrice
) -> tuple[float | None, float]:
    """(стоимость покупки из сети, покрытие 0..1). Слоты без данных восполняются масштабированием по тарифным зонам.

    Покрытие < ACTUAL_MIN_COVERAGE — стоимость None.
    """
    if not slots:
        return None, 0.0
    have = [s for s in slots if s in imports]
    coverage = len(have) / len(slots)
    if coverage < ACTUAL_MIN_COVERAGE:
        return None, coverage
    total_by_price: dict[float, int] = {}
    have_by_price: dict[float, int] = {}
    cost_by_price: dict[float, float] = {}
    for s in slots:
        total_by_price[price(s)] = total_by_price.get(price(s), 0) + 1
    for s in have:
        p = price(s)
        have_by_price[p] = have_by_price.get(p, 0) + 1
        cost_by_price[p] = cost_by_price.get(p, 0.0) + imports[s] * p
    cost = sum(c * total_by_price[p] / have_by_price[p] for p, c in cost_by_price.items())
    return cost, coverage


def evaluate_decision(db: Database, row: dict, now_ts: int) -> dict | None:
    """Оценка решения: dict(actual, coverage_pct, perfect, regret, decided); None, если оценивать рано или считать нечего.

    После окончания горизонта (начало ночи → начало ночи следующих суток) — полная оценка;
    после конца ночи — только actual за ночь.
    """
    stored = (row.get("inputs") or {}).get("params")
    params = PlannerParams(**stored) if stored else None
    base = params or PlannerParams()
    tz = base.tz if params else config.TZ
    d = row["night_date"]
    t23 = at_local(d, base.night_start_hour, tz)
    t07 = at_local(d + timedelta(days=1), base.night_end_hour, tz)
    t23_next = at_local(d + timedelta(days=1), base.night_start_hour, tz)
    full = now_ts >= t23_next + EVAL_FULL_DELAY_SEC
    if not full and (now_ts < t07 + EVAL_PARTIAL_DELAY_SEC or row.get("actual_cost_uah") is not None):
        return None
    end = t23_next if full else t07
    price = TariffPrice(db, tz)
    slots = list(range(t23, end, SLOT_SEC))
    actual, coverage = actual_import_cost(db.grid_import_15m(t23, end), slots, price)
    out = {"actual": actual, "coverage_pct": round(coverage * 100, 1), "perfect": None, "regret": None, "decided": None}
    if not full:
        return out if actual is not None else None
    if params is None or row["soc_at_decision"] is None:
        return out
    created_ts = int(row["created_at"].timestamp())
    pv = db.pv_samples_15m(created_ts, t23_next)
    load = dict(db.load_samples_15m(created_ts, t23_next))
    day_slots = range(t23, t23_next, SLOT_SEC)
    if sum(1 for s in day_slots if s in load) < LOAD_MIN_COVERAGE * len(day_slots):
        return out
    try:
        soc = float(row["soc_at_decision"])
        perfect = perfect_forecast_cost(created_ts, soc, pv, load, params, price).expected_cost_uah
        decided = evaluate_target(created_ts, soc, pv, load, params, price, float(row["target_pct"]))
    except ValueError as e:
        log.info("perfect_cost для %s не посчитан: %s", d, e)
        return out
    perfect = min(perfect, decided)  # выбранный target сам является допустимым кандидатом
    out.update(perfect=perfect, regret=decided - perfect, decided=decided)
    return out


def _apply_end_ts(night: date, tz: str, late: bool = False) -> int:
    """Конец окна автозаписи: 23:30 вечера ночи; late (ручная цель) — 06:30 утра после ночи."""
    if late:
        return int(datetime.combine(night + timedelta(days=1), MANUAL_APPLY_END, ZoneInfo(tz)).timestamp())
    return int(datetime.combine(night, APPLY_END, ZoneInfo(tz)).timestamp())


def _row_apply_end_ts(row: dict, tz: str) -> int:
    return _apply_end_ts(row["night_date"], tz, bool(row.get("late_apply")))


def _planner_author(night: date, decision_id: int) -> str:
    # Попытки записи считаются на решение: пересчёт (redecide) получает свои MAX_APPLY_ATTEMPTS.
    return f"planner:{night.isoformat()}#{decision_id}"


def _fmt_changes(changes: dict, expected_old: dict) -> str:
    return ", ".join(f"рег {r}: {expected_old.get(r)} → {v}" for r, v in sorted((changes or {}).items(), key=lambda kv: int(kv[0])))


def _auto_enabled(st: dict) -> bool:
    return st["control_mode"] == "auto" and bool(st["writes_enabled"])


def _wait(db: Database, row: dict, reason: str) -> None:
    """Запись сейчас невозможна (guard, нет снимка) — статус не меняем, причину пишем в apply_note."""
    note = f"ожидание: {reason}"
    if row["apply_note"] != note:
        log.info("решение на ночь %s: запись отложена — %s", row["night_date"], reason)
        db.set_decision_apply(row["id"], row["status"], note, expect_status=row["status"])


def _snapshot_reflects(snap: dict, last: dict) -> bool:
    """Снимок настроек отражает выполненную заявку: снят позже её завершения или уже содержит её значения.
    ts снимка — целые секунды, а снимок пишется только при изменении регистров, поэтому снимок,
    снятый в ту же секунду (readback), иначе считался бы старым навсегда."""
    if last["finished_at"] is None or snap["ts"] > last["finished_at"].timestamp():
        return True
    regs = snap["regs"]
    return all(regs.get(str(k)) == v for k, v in (last["changes"] or {}).items())


def _try_apply(db: Database, row: dict, st: dict, now_ts: int, attempt: int) -> None:
    """Попытка поставить ночной target решения в очередь записи (по свежему снимку настроек)."""
    night = row["night_date"]
    snap = db.latest_settings()
    if snap is None:
        return _wait(db, row, "нет снимка настроек инвертора")
    guard = guard_errors(db.write_guard_state(), now_ts)
    if guard:
        return _wait(db, row, "; ".join(guard))
    last = db.last_write_request(PLANNER_SOURCE, "")
    if last is not None and last["status"] == "done" and not _snapshot_reflects(snap, last):
        # снимок ещё не отражает выполненную запись (например, прошлого решения) — ждём, попытка не тратится
        return _wait(db, row, f"снимок настроек старше выполненной заявки {last['id']}")
    regs = snap["regs"]
    res = build_night_changes(regs, row["target_pct"], st, build_params(st, regs))
    if res[0] == "skip":
        log.warning("решение на ночь %s: запись пропущена — %s", night, res[1])
        db.set_decision_apply(row["id"], "skipped", res[1], expect_status=row["status"])
        return
    changes, expected_old = res
    if not changes:
        log.info("решение на ночь %s: target %.1f %% уже установлен в инверторе", night, row["target_pct"])
        db.set_decision_apply(row["id"], "applied", "уже в инверторе", datetime.fromtimestamp(now_ts, timezone.utc),
                              expect_status=row["status"])
        return
    desc = _fmt_changes(changes, expected_old)
    rid = db.insert_write_request(
        changes, expected_old, PLANNER_SOURCE, _planner_author(night, row["id"]), decision_id=row["id"],
        expect_status=row["status"], expect_request_id=row["write_request_id"], note=f"попытка {attempt}: {desc}",
    )
    log.info("решение на ночь %s: заявка %d на запись (попытка %d, снимок %d): %s",
             night, rid, attempt, snap["id"], desc)


def _sync_queued(db: Database, row: dict, st: dict, now_ts: int) -> None:
    """Решение в очереди: перенести итог заявки в решение; после неудачи — одна повторная попытка."""
    night, rs = row["night_date"], row["request_status"]
    if rs in ("pending", "running"):
        return
    if rs == "done":
        applied_at = row["request_finished_at"] or datetime.fromtimestamp(now_ts, timezone.utc)
        desc = _fmt_changes(row["request_changes"], row["request_expected_old"])
        log.info("решение на ночь %s: заявка %s выполнена — %s", night, row["write_request_id"], desc)
        db.set_decision_apply(row["id"], "applied", f"записано: {desc}", applied_at, expect_status="queued")
        return
    if rs == "cancelled":
        # Отмена (вручную в админке или воркером при выключенной автозаписи) — без повтора.
        log.warning("решение на ночь %s: заявка %s отменена", night, row["write_request_id"])
        db.set_decision_apply(row["id"], "skipped", f"заявка {row['write_request_id']} отменена"
                              + (f" — {row['request_error']}" if row["request_error"] else ""), expect_status="queued")
        return
    error = f"заявка {row['write_request_id']}: {rs or 'нет'}" + (f" — {row['request_error']}" if row["request_error"] else "")
    attempts = db.count_write_requests(PLANNER_SOURCE, _planner_author(night, row["id"]))
    if attempts < MAX_APPLY_ATTEMPTS and _auto_enabled(st) and now_ts < _row_apply_end_ts(row, config.TZ):
        finished = row["request_finished_at"]
        if finished is not None and finished.timestamp() + APPLY_RETRY_DELAY_SEC > now_ts:
            return  # ждём, пока коллектор обновит снимок настроек
        log.warning("решение на ночь %s: %s — повторная попытка", night, error)
        _try_apply(db, row, st, now_ts, attempts + 1)
        return
    log.error("решение на ночь %s: запись не выполнена (%s), попыток %d", night, error, attempts)
    db.set_decision_apply(row["id"], "failed", f"ошибка записи: {error}", expect_status="queued")


def auto_apply(db: Database, st: dict, night: date, now_ts: int) -> None:
    """Шаг автозаписи (каждый tick): синхронизация заявок в очереди и первая попытка для решения на ночь night.

    Решение не пересчитывается — пишется его target_pct. Окно первой и повторной попытки — от decision_time
    до 23:30 местного (решение с ручной целью или пересчитанное из-за её смены, inputs.late_apply — до 06:30
    утра ночи). Если guard блокирует — ждём (status не меняется), по истечении окна — skipped.
    """
    for row in db.queued_decisions():
        try:
            _sync_queued(db, row, st, now_ts)
        except Exception:
            log.exception("синхронизация записи решения на ночь %s", row["night_date"])
    if not _auto_enabled(st):
        return
    row = db.final_decision_apply(night)
    if row is None or row["status"] != "advice":
        return
    start_ts = int(datetime.combine(night, st["decision_time"], ZoneInfo(config.TZ)).timestamp())
    end_ts = _row_apply_end_ts(row, config.TZ)
    if now_ts < start_ts:
        return
    if now_ts >= end_ts:
        if row["apply_note"]:  # пытались, но guard блокировал до конца окна
            end = MANUAL_APPLY_END if row.get("late_apply") else APPLY_END
            log.warning("решение на ночь %s: окно записи истекло — %s", night, row["apply_note"])
            db.set_decision_apply(row["id"], "skipped", f"окно записи до {end:%H:%M} истекло ({row['apply_note']})",
                                  expect_status="advice")
        return
    attempts = db.count_write_requests(PLANNER_SOURCE, _planner_author(night, row["id"]))
    if attempts >= MAX_APPLY_ATTEMPTS:
        db.set_decision_apply(row["id"], "failed", f"исчерпаны попытки записи ({attempts})", expect_status="advice")
        return
    _try_apply(db, row, st, now_ts, attempts + 1)


def _floor_author(now_ts: int) -> str:
    return FLOOR_PREFIX + datetime.fromtimestamp(now_ts, ZoneInfo(config.TZ)).date().isoformat()


def floor_sync(db: Database, st: dict, now_ts: int) -> None:
    """Привести пороги разряда инвертора к эффективному полу (отдельная заявка, без привязки к решению).

    Работает при control_mode=auto и writes_enabled; не более MAX_FLOOR_REQUESTS_PER_DAY заявок на календарный день;
    если guard блокирует, ждёт. После выполненной заявки ждёт нового снимка настроек, после неудачной — паузу.
    """
    if not _auto_enabled(st):
        return
    snap = db.latest_settings()
    if snap is None:
        return
    regs = snap["regs"]
    changes, expected_old = build_floor_changes(regs, st, build_params(st, regs))
    if not changes:
        return
    last = db.last_write_request(PLANNER_SOURCE, FLOOR_PREFIX)
    if last is not None and last["finished_at"] is not None:
        finished = last["finished_at"].timestamp()
        if last["status"] == "done" and not _snapshot_reflects(snap, last):
            return  # ждём снимок настроек, отражающий выполненную запись
        if last["status"] != "done" and finished + FLOOR_RETRY_DELAY_SEC > now_ts:
            return
    author = _floor_author(now_ts)
    if db.count_write_requests(PLANNER_SOURCE, author) >= MAX_FLOOR_REQUESTS_PER_DAY:
        log.warning("floor-sync: лимит %d заявок на день исчерпан, пороги не приведены: %s",
                    MAX_FLOOR_REQUESTS_PER_DAY, _fmt_changes(changes, expected_old))
        return
    guard = guard_errors(db.write_guard_state(), now_ts)
    if guard:
        log.info("floor-sync: запись отложена — %s", "; ".join(guard))
        return
    desc = _fmt_changes(changes, expected_old)
    rid = db.insert_write_request(changes, expected_old, PLANNER_SOURCE, author, note=f"floor-sync: {desc}")
    log.info("floor-sync: заявка %d (снимок %d): %s", rid, snap["id"], desc)


def floor_sync_status(db: Database, now_ts: int | None = None) -> dict:
    """Блок floor_sync для API: целевой пол, пол в инверторе по слотам без заряда от сети, состояние синхронизации."""
    now_ts = now_ts or int(time.time())
    st = db.strategy_settings()
    snap = db.latest_settings()
    slots = tou_floor_slots(snap["regs"]) if snap else None
    day = sorted({s["soc_pct"] for s in slots if not s["grid_charge"]}) if slots else []
    out: dict = {
        "target_floor_pct": effective_floor(st) if st else None,
        "inverter_floor_pct": day[0] if len(day) == 1 else None,
        "inverter_day_floors_pct": day,
        "auto_enabled": bool(st) and _auto_enabled(st),
        "status": "pending", "last_request": None,
    }
    last = db.last_write_request(PLANNER_SOURCE, FLOOR_PREFIX)
    if last is not None:
        out["last_request"] = {
            "id": last["id"], "status": last["status"], "error": last["error"], "changes": last["changes"],
            "created_at": last["created_at"].isoformat(), "created_by": last["created_by"],
            "finished_at": last["finished_at"].isoformat() if last["finished_at"] else None,
        }
    if st is None or slots is None:
        return out
    changes, _ = build_floor_changes(snap["regs"], st, build_params(st, snap["regs"]))
    if not changes:
        out["status"] = "ok"
    elif last is not None and last["status"] in ("pending", "running"):
        out["status"] = "queued"
    elif (last is not None and last["status"] != "done"
          and db.count_write_requests(PLANNER_SOURCE, _floor_author(now_ts)) >= MAX_FLOOR_REQUESTS_PER_DAY):
        out["status"] = "failed"
    return out


_NO_DECISION_WARNED: set[date] = set()  # ночи, по которым уже предупредили «ручная цель без решения»


def redecide_if_settings_changed(db: Database, st: dict, night: date, now_ts: int) -> bool:
    """Пересчитать финальное решение на ночь, если настройки (пол, аварийный режим, потолок, риск, ручная
    ночная цель) изменились.

    Только в окне [decision_time, 23:30] местного и если решение не в очереди записи (queued — ждём итога
    заявки, пересчёт на следующем tick). Если новое решение с ручной целью или изменились поля ручной цели —
    окно до 06:30 утра ночи, а новое решение помечается inputs.late_apply (такое же окно автозаписи).
    Старое решение уходит из финальных (status='skipped', applied сохраняется; apply_note «заменено…»), новое
    пишется финальным (advice) и дальше обрабатывается обычным auto_apply. Бросает ValueError при нехватке
    данных. True — решение заменено.
    """
    start_ts = int(datetime.combine(night, st["decision_time"], ZoneInfo(config.TZ)).timestamp())
    if not start_ts <= now_ts < _apply_end_ts(night, config.TZ, late=True):
        return False
    meta = db.final_decision_meta(night)
    if meta is None:
        if (manual_night_target(st, night) is not None and now_ts >= at_local(night, NIGHT_START_HOUR, config.TZ)
                and night not in _NO_DECISION_WARNED):
            _NO_DECISION_WARNED.add(night)
            log.warning("ручная ночная цель на ночь %s задана, но финального решения нет — в инвертор не пишется", night)
        return False
    if meta["status"] == "queued":
        return False
    fp = settings_fingerprint(st)
    if _same_fp(meta["settings_fp"], fp):
        return False
    late = manual_night_target(st, night) is not None or _night_target_fp(meta["settings_fp"]) != _night_target_fp(fp)
    if not late and now_ts >= _apply_end_ts(night, config.TZ):
        return False
    res = compute(db, now_ts)
    if res["settings_fp"] != fp:  # настройки поменялись между чтением st и расчётом — решим на следующем tick
        return False
    res["late_apply"] = late
    replaced = db.replace_final_decision(meta["id"], to_row(res), "заменено: изменились настройки")
    log.info("решение на ночь %s пересчитано из-за смены настроек (%s → %s): %s",
             night, meta["settings_fp"], fp, "заменено" if replaced else "не заменено")
    return replaced


def apply_status(db: Database, now_ts: int | None = None) -> dict:
    """Статус записи решения на текущую/ближайшую ночь для дашборда и ночной target, действующий в инверторе."""
    now_ts = now_ts or int(time.time())
    params = PlannerParams(tz=config.TZ)
    night = night_date_for(now_ts, params)
    row = db.final_decision_apply(night)
    snap = db.latest_settings()
    slots = tou_floor_slots(snap["regs"]) if snap else None
    targets = None
    if slots is not None:
        targets = sorted({s["soc_pct"] for s in slots if s["grid_charge"] and params.is_night(int(s["start"][:2]))})
    out = {"night_date": night.isoformat(), "has_decision": row is not None, "inverter_night_targets_pct": targets}
    # без решения: "pending" пока окно решения (до начала ночи) не закончилось, затем "none"
    window_end = at_local(night, NIGHT_START_HOUR, ZoneInfo(config.TZ))
    out["status"] = "pending" if now_ts < window_end else "none"
    st = db.strategy_settings()
    # ручная цель задана, а решения на ночь нет (окно решения прошло) — в инвертор она не попадёт
    out["manual_no_decision"] = (row is None and now_ts >= window_end and st is not None
                                 and manual_night_target(st, night) is not None)
    if row is not None:
        out.update(
            decision_id=row["id"], status=row["status"],
            applied_at=row["applied_at"].isoformat() if row["applied_at"] else None,
            apply_note=row["apply_note"], write_request_id=row["write_request_id"],
            write_request_status=row["request_status"],
        )
    return out


class PlannerScheduler:
    def __init__(self, db: Database, tz_name: str):
        self._db = db
        self._tz_name = tz_name
        self._tz = ZoneInfo(tz_name)
        self._last_try: dict[object, float] = {}

    def _throttled(self, key: object, interval: float, now_ts: float) -> bool:
        last = self._last_try.get(key)
        if last is not None and now_ts - last < interval:
            return True
        self._last_try[key] = now_ts
        return False

    async def tick(self, now_ts: int | None = None) -> None:
        now_ts = now_ts or int(time.time())
        now = datetime.fromtimestamp(now_ts, self._tz)
        st = await asyncio.to_thread(self._db.strategy_settings)
        night = night_date_for(now_ts, PlannerParams(tz=self._tz_name))
        if st is not None and st["control_mode"] != "off":
            start_ts = int(datetime.combine(night, st["decision_time"], self._tz).timestamp())
            end_ts = at_local(night, NIGHT_START_HOUR, self._tz)
            if start_ts <= now_ts < end_ts and not self._throttled("decide", DECIDE_RETRY_SEC, now_ts):
                if not await asyncio.to_thread(self._db.final_decision_exists, night):
                    try:
                        saved = await asyncio.to_thread(compute_and_save, self._db, now_ts)
                        log.info("решение на ночь %s %s", night, "сохранено" if saved else "уже есть")
                    except ValueError as e:
                        log.warning("решение на ночь %s не принято: %s", night, e)
        if st is not None and st["control_mode"] != "off" and not self._throttled("redecide", DECIDE_RETRY_SEC, now_ts):
            try:
                await asyncio.to_thread(redecide_if_settings_changed, self._db, st, night, now_ts)
            except ValueError as e:
                log.warning("пересчёт решения на ночь %s не выполнен: %s", night, e)
            except Exception:
                log.exception("пересчёт решения на ночь %s", night)
        if st is not None:
            try:
                await asyncio.to_thread(auto_apply, self._db, st, night, now_ts)
            except Exception:
                log.exception("автозапись решения на ночь %s", night)
            try:
                await asyncio.to_thread(floor_sync, self._db, st, now_ts)
            except Exception:
                log.exception("floor-sync порогов разряда")
        pending = await asyncio.to_thread(self._db.decisions_to_evaluate, now.date() - timedelta(days=EVAL_LOOKBACK_DAYS))
        for row in pending:
            if self._throttled(("eval", row["id"]), EVAL_RETRY_SEC, now_ts):
                continue
            try:
                result = await asyncio.to_thread(evaluate_decision, self._db, row, now_ts)
                if result is not None:
                    evaluated_at = datetime.fromtimestamp(now_ts, timezone.utc) if result["regret"] is not None else None
                    await asyncio.to_thread(self._db.update_decision_evaluation, row["id"], result, evaluated_at)
            except Exception:
                log.exception("оценка решения %s", row["night_date"])

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка цикла планировщика")
            await asyncio.sleep(TICK_SEC)
