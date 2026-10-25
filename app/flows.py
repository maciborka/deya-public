"""Энергопотоки за период: интегрирование samples с правилом распределения на каждом интервале."""
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MAX_GAP_SEC = 300
FLOW_KEYS = ("pv_to_load", "pv_to_batt", "pv_to_grid", "grid_to_load", "grid_to_batt", "batt_to_load")
TOTAL_KEYS = ("pv", "load", "grid_import", "grid_export", "batt_charge", "batt_discharge")
FLOW_FIELDS = ("pv1_power_w", "pv2_power_w", "load_power_w", "grid_power_w", "batt_power_w")


def split_power(pv: float, load: float, grid: float, batt: float) -> dict[str, float]:
    """Мощности (Вт) одного интервала → потоки. Знаки: batt + разряд / − заряд; grid + импорт / − экспорт.
    PV сначала в дом, остаток в АКБ (если заряд), остаток в сеть; дом добирает из АКБ, затем из сети;
    заряд АКБ сверх PV — из сети."""
    pv, load = max(pv, 0.0), max(load, 0.0)
    charge, discharge = max(-batt, 0.0), max(batt, 0.0)
    pv_to_load = min(pv, load)
    rest_pv = pv - pv_to_load
    pv_to_batt = min(rest_pv, charge)
    # Не больше измеренного экспорта: остаток — потери инвертора/ограничение (zero export).
    pv_to_grid = min(rest_pv - pv_to_batt, max(-grid, 0.0))
    need = load - pv_to_load
    batt_to_load = min(discharge, need)
    return {
        "pv_to_load": pv_to_load,
        "pv_to_batt": pv_to_batt,
        "pv_to_grid": pv_to_grid,
        "grid_to_load": need - batt_to_load,
        "grid_to_batt": charge - pv_to_batt,
        "batt_to_load": batt_to_load,
    }


def integrate_flows(rows: list[dict], from_ts: int, to_ts: int, max_gap: int = MAX_GAP_SEC) -> dict:
    """rows — samples по возрастанию ts (с полями FLOW_FIELDS). Интервалы длиннее max_gap или с пропусками
    значений не интегрируются. Возвращает кВт·ч и coverage — долю окна [from_ts, to_ts], покрытую данными."""
    flows = dict.fromkeys(FLOW_KEYS, 0.0)
    totals = dict.fromkeys(TOTAL_KEYS, 0.0)
    covered = 0.0

    def power(r: dict) -> tuple[float, float, float, float] | None:
        load, grid, batt = r.get("load_power_w"), r.get("grid_power_w"), r.get("batt_power_w")
        if load is None or grid is None or batt is None:
            return None
        return (r.get("pv1_power_w") or 0.0) + (r.get("pv2_power_w") or 0.0), load, grid, batt

    for a, b in zip(rows, rows[1:]):
        dt = b["ts"] - a["ts"]
        if dt <= 0 or dt > max_gap or a["ts"] < from_ts or b["ts"] > to_ts:
            continue
        pa, pb = power(a), power(b)
        if pa is None or pb is None:
            continue
        pv, load, grid, batt = ((x + y) / 2 for x, y in zip(pa, pb))
        k = dt / 3600 / 1000
        for key, val in split_power(pv, load, grid, batt).items():
            flows[key] += val * k
        totals["pv"] += max(pv, 0.0) * k
        totals["load"] += max(load, 0.0) * k
        totals["grid_import"] += max(grid, 0.0) * k
        totals["grid_export"] += max(-grid, 0.0) * k
        totals["batt_charge"] += max(-batt, 0.0) * k
        totals["batt_discharge"] += max(batt, 0.0) * k
        covered += dt
    window = max(to_ts - from_ts, 1)
    return {
        "flows_kwh": {k: round(v, 3) for k, v in flows.items()},
        "totals_kwh": {k: round(v, 3) for k, v in totals.items()},
        "coverage": round(min(covered / window, 1.0), 3),
    }


LOAD_BUCKETS = {"5m": 300, "1h": 3600, "1d": 86400}


def bucket_bounds(kind: str, from_ts: int, to_ts: int, tz_name: str) -> list[tuple[int, int]]:
    """Границы бакетов [start, end) в окне до to_ts. 5m/1h — по сетке эпохи; 1d — по локальным суткам
    (через zoneinfo, сутки бывают 23/25 ч). Последний бакет обрезается по to_ts."""
    out: list[tuple[int, int]] = []
    if kind == "1d":
        tz = ZoneInfo(tz_name)
        day = datetime.fromtimestamp(from_ts, tz).date()
        while True:
            start = int(datetime.combine(day, datetime.min.time(), tz).timestamp())
            if start >= to_ts:
                break
            end = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).timestamp())
            out.append((start, min(end, to_ts)))
            day += timedelta(days=1)
        return out
    step = LOAD_BUCKETS[kind]
    start = from_ts // step * step
    while start < to_ts:
        out.append((start, min(start + step, to_ts)))
        start += step
    return out


def _lerp_row(a: dict, b: dict, ts: int) -> dict | None:
    k = (ts - a["ts"]) / (b["ts"] - a["ts"])
    row = {"ts": ts}
    for f in FLOW_FIELDS:
        x, y = a.get(f), b.get(f)
        if x is None or y is None:
            if f == "pv1_power_w" or f == "pv2_power_w":
                row[f] = x if y is None else y
                continue
            return None
        row[f] = x + (y - x) * k
    return row


def load_sources(rows: list[dict], bounds: list[tuple[int, int]], max_gap: int = MAX_GAP_SEC) -> list[dict]:
    """Питание дома по бакетам: кВт·ч из PV / АКБ / сети (pv_to_load, batt_to_load, grid_to_load).
    Интервалы, пересекающие границу бакета, делятся интерполяцией мощности — сумма бакетов = integrate_flows.
    Бакет без данных (coverage 0) → значения None. dur_sec — длина окна бакета."""
    ts_list = [r["ts"] for r in rows]
    points = []
    for start, end in bounds:
        lo, hi = bisect_left(ts_list, start), bisect_right(ts_list, end)
        part = rows[lo:hi]
        if lo > 0 and (not part or part[0]["ts"] > start):
            a, b = rows[lo - 1], rows[lo] if lo < len(rows) else None
            if b is not None and 0 < b["ts"] - a["ts"] <= max_gap:
                edge = _lerp_row(a, b, start)
                if edge:
                    part = [edge, *part]
        if hi < len(rows) and (not part or part[-1]["ts"] < end):
            a, b = rows[hi - 1] if hi > 0 else None, rows[hi]
            if a is not None and 0 < b["ts"] - a["ts"] <= max_gap:
                edge = _lerp_row(a, b, end)
                if edge:
                    part = [*part, edge]
        res = integrate_flows(part, start, end, max_gap)
        fl = res["flows_kwh"]
        has = res["coverage"] > 0
        points.append({
            "ts": start,
            "dur_sec": end - start,
            "pv_kwh": fl["pv_to_load"] if has else None,
            "batt_kwh": fl["batt_to_load"] if has else None,
            "grid_kwh": fl["grid_to_load"] if has else None,
            "coverage": res["coverage"],
        })
    return points


def integrate_day_import(rows: list[dict], from_ts: int, to_ts: int, price_at, expensive_price: float,
                         hour_of, max_gap: int = MAX_GAP_SEC) -> dict:
    """Импорт из сети (grid_power_w > 0) только в интервалах дорогого тарифа (цена >= expensive_price).
    price_at(ts) -> грн/кВт·ч, hour_of(ts) -> локальный час. Разрывы > max_gap не интегрируются."""
    by_hour = [0.0] * 24
    cost = 0.0
    covered = 0.0
    for a, b in zip(rows, rows[1:]):
        dt = b["ts"] - a["ts"]
        if dt <= 0 or dt > max_gap or a["ts"] < from_ts or b["ts"] > to_ts:
            continue
        ga, gb = a.get("grid_power_w"), b.get("grid_power_w")
        if ga is None or gb is None:
            continue
        covered += dt
        price = price_at(a["ts"])
        if price < expensive_price:
            continue
        kwh = max((ga + gb) / 2, 0.0) * dt / 3600 / 1000
        by_hour[hour_of(a["ts"])] += kwh
        cost += kwh * price
    return {
        "import_kwh": round(sum(by_hour), 3),
        "cost_uah": round(cost, 2),
        "by_hour": [{"hour": h, "import_kwh": round(v, 3)} for h, v in enumerate(by_hour)],
        "coverage": round(min(covered / max(to_ts - from_ts, 1), 1.0), 3),
    }
