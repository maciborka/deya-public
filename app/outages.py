"""Отключения сети: состояние по напряжению фаз и поиск отрезков отключения в samples."""

GRID_MIN_V = 180
MAX_GAP_SEC = 300
MIN_OUTAGE_SEC = 30
PHASE_FIELDS = ("grid_l1_voltage_v", "grid_l2_voltage_v", "grid_l3_voltage_v")
OUTAGE_FIELDS = (*PHASE_FIELDS, "batt_power_w", "load_power_w", "soc_pct")


def _low_phases(sample: dict) -> list[int] | None:
    volts = [sample.get(f) for f in PHASE_FIELDS]
    if all(v is None for v in volts):
        return None
    return [i + 1 for i, v in enumerate(volts) if v is not None and v < GRID_MIN_V]


def grid_state(sample: dict) -> str | None:
    """"ok" — все три фазы >= GRID_MIN_V, "partial" — просела 1–2 фазы, "down" — все три; None — нет данных."""
    low = _low_phases(sample)
    if low is None:
        return None
    if len(low) >= 3:
        return "down"
    return "partial" if low else "ok"


def find_outages(rows: list[dict], max_gap: int = MAX_GAP_SEC) -> list[dict]:
    """rows — samples по возрастанию ts. Отключение — непрерывный отрезок partial/down. Разрыв данных
    больше max_gap закрывает отрезок и сам отключением не считается. end_ts None — идёт на последнем сэмпле."""
    result: list[dict] = []
    cur: dict | None = None

    def close(ongoing: bool) -> None:
        nonlocal cur
        if cur is None:
            return
        end = cur["last_ts"]
        duration = end - cur["start_ts"]
        if duration >= MIN_OUTAGE_SEC:
            result.append({
                "start_ts": cur["start_ts"],
                "end_ts": None if ongoing else end,
                "duration_sec": duration,
                "kind": "down" if cur["down"] else "partial",
                "phases_down": sorted(cur["phases"]),
                "batt_kwh": round(cur["batt_wh"] / 1000, 3),
                "load_kwh": round(cur["load_wh"] / 1000, 3),
                "soc_start": cur["soc_start"],
                "soc_end": cur["soc_end"],
            })
        cur = None

    prev: dict | None = None
    for r in rows:
        ts = r["ts"]
        if prev is not None and ts - prev["ts"] > max_gap:
            close(False)
        state = grid_state(r)
        if state in ("partial", "down"):
            if cur is None:
                cur = {"start_ts": ts, "last_ts": ts, "down": False, "phases": set(), "batt_wh": 0.0,
                       "load_wh": 0.0, "soc_start": r.get("soc_pct"), "soc_end": r.get("soc_pct")}
            elif prev is not None:
                dt = ts - prev["ts"]
                for key, acc in (("batt_power_w", "batt_wh"), ("load_power_w", "load_wh")):
                    a, b = prev.get(key), r.get(key)
                    if a is not None and b is not None and dt > 0:
                        cur[acc] += (max(a, 0.0) + max(b, 0.0)) / 2 * dt / 3600
                cur["last_ts"] = ts
                if r.get("soc_pct") is not None:
                    cur["soc_end"] = r["soc_pct"]
                    if cur["soc_start"] is None:
                        cur["soc_start"] = r["soc_pct"]
            if state == "down":
                cur["down"] = True
            cur["phases"].update(_low_phases(r))
        else:
            close(False)
        prev = r
    close(True)
    return result
