"""Консенсус прогнозов PV, коррекция bias и сценарии p10/p50/p90."""
import math
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .timeutil import SLOT_SEC, local, zone

MIN_HISTORY_DAYS = 7
RMSE_FLOOR_W = 1.0
BIAS_MIN, BIAS_MAX = 0.5, 1.5
RATIO_MIN, RATIO_MAX = 0.2, 2.0
DEFAULT_P10_K = 0.55
DEFAULT_P90_K = 1.2
SOLCAST_MIN_P50_W = 1.0


@dataclass
class ForecastErr:
    """Пара «прогноз провайдера — факт» для одного часа/слота. Время — unix UTC."""

    provider: str
    target_ts: int
    fetched_ts: int
    forecast_w: float
    actual_w: float


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, x))


def provider_weights(
    history: list[ForecastErr],
    providers: list[str] | None = None,
    tz: str | ZoneInfo = "Europe/Kyiv",
    min_days: int = MIN_HISTORY_DAYS,
) -> dict[str, float]:
    """Веса 1/RMSE² по провайдерам, нормированные к 1.

    Учитываются только прогнозы «на завтра»: сделанные в локальные сутки, предшествующие целевым
    (из нескольких таких прогнозов на один target_ts берётся самый поздний). Если у какого-то провайдера
    < min_days дней истории или истории нет — равные веса всем.
    """
    tzi = zone(tz)
    names = set(providers or []) | {h.provider for h in history}
    if not names:
        return {}
    equal = {p: 1.0 / len(names) for p in sorted(names)}
    latest: dict[tuple[str, int], ForecastErr] = {}
    for h in history:
        if (local(h.target_ts, tzi).date() - local(h.fetched_ts, tzi).date()).days != 1:
            continue
        key = (h.provider, h.target_ts)
        if key not in latest or h.fetched_ts > latest[key].fetched_ts:
            latest[key] = h
    errs: dict[str, list[float]] = {p: [] for p in names}
    days: dict[str, set] = {p: set() for p in names}
    for (p, ts), h in latest.items():
        errs[p].append(h.forecast_w - h.actual_w)
        days[p].add(local(ts, tzi).date())
    if any(len(days[p]) < min_days for p in names):
        return equal
    inv = {}
    for p in names:
        rmse = max(RMSE_FLOOR_W, math.sqrt(sum(e * e for e in errs[p]) / len(errs[p])))
        inv[p] = 1.0 / rmse**2
    total = sum(inv.values())
    return {p: inv[p] / total for p in sorted(names)}


def _to_slots(series: Mapping[int, float | None], slot_sec: int = SLOT_SEC) -> dict[int, float]:
    """Часовой ряд (ключи кратны 3600, шаг ≥ 1 ч) растянуть на слоты; иначе вернуть как есть."""
    keys = sorted(k for k, v in series.items() if v is not None)
    if not keys:
        return {}
    steps = [b - a for a, b in zip(keys, keys[1:])]
    hourly = all(k % 3600 == 0 for k in keys) and (not steps or min(steps) >= 3600)
    if not hourly:
        return {k: float(series[k]) for k in keys}
    out: dict[int, float] = {}
    for k in keys:
        for j in range(3600 // slot_sec):
            out[k + j * slot_sec] = float(series[k])
    return out


def consensus(
    forecasts: Mapping[str, Mapping[int, float | None]],
    weights: Mapping[str, float] | None = None,
    slot_sec: int = SLOT_SEC,
) -> dict[int, float]:
    """Взвешенный консенсус прогнозов по слотам (Вт).

    Часовые прогнозы растягиваются на 4 слота по 15 мин. Если у провайдера нет слота — веса
    перенормируются по имеющимся. Без weights — равные веса; провайдер, которого нет в weights, имеет вес 0
    (если у всех имеющихся в слоте вес 0 — простое среднее).
    """
    per_provider = {p: _to_slots(s, slot_sec) for p, s in forecasts.items()}
    slots = sorted({ts for s in per_provider.values() for ts in s})
    out: dict[int, float] = {}
    for ts in slots:
        vals = [(1.0 if not weights else weights.get(p, 0.0), s[ts]) for p, s in per_provider.items() if ts in s]
        sw = sum(w for w, _ in vals)
        if sw > 0:
            out[ts] = sum(w * v for w, v in vals) / sw
        else:
            out[ts] = sum(v for _, v in vals) / len(vals)
    return out


def bias_correction(hourly_ratio: Mapping[int, float] | None = None) -> dict[int, float]:
    """Множители по локальному часу 0..23 (по умолчанию 1), кламп 0.5–1.5."""
    ratios = hourly_ratio or {}
    return {h: _clamp(float(ratios.get(h, 1.0)), BIAS_MIN, BIAS_MAX) for h in range(24)}


def apply_bias(series: Mapping[int, float], factors: Mapping[int, float], tz: str | ZoneInfo = "Europe/Kyiv") -> dict[int, float]:
    """Применить почасовые множители bias_correction к ряду по слотам."""
    tzi = zone(tz)
    return {ts: v * factors.get(local(ts, tzi).hour, 1.0) for ts, v in series.items()}


def _hour_mean(series: Mapping[int, float | None], ts: int, slot_sec: int) -> float | None:
    hour = ts - ts % 3600
    vals = [series[k] for k in range(hour, hour + 3600, slot_sec) if series.get(k) is not None]
    return sum(vals) / len(vals) if vals else None


def scenarios(
    cons: Mapping[int, float],
    solcast_p10: Mapping[int, float | None] | None = None,
    solcast_p50: Mapping[int, float | None] | None = None,
    solcast_p90: Mapping[int, float | None] | None = None,
    slot_sec: int = SLOT_SEC,
) -> dict[str, dict[int, float]]:
    """Сценарии PV {"p10","p50","p90"} по слотам.

    p50 = консенсус. Если есть Solcast — консенсус масштабируется отношениями p10/p50 и p90/p50 Solcast
    по каждому часу (кламп 0.2–2.0); в часы без данных Solcast (или p50 ≈ 0) и без Solcast вовсе —
    p10 = 0.55×, p90 = 1.2×.
    """
    has_solcast = bool(solcast_p10) and bool(solcast_p50) and bool(solcast_p90)
    p10: dict[int, float] = {}
    p90: dict[int, float] = {}
    for ts, v in cons.items():
        k10, k90 = DEFAULT_P10_K, DEFAULT_P90_K
        if has_solcast:
            s10 = _hour_mean(solcast_p10, ts, slot_sec)
            s50 = _hour_mean(solcast_p50, ts, slot_sec)
            s90 = _hour_mean(solcast_p90, ts, slot_sec)
            if s10 is not None and s90 is not None and s50 is not None and s50 >= SOLCAST_MIN_P50_W:
                k10 = _clamp(s10 / s50, RATIO_MIN, RATIO_MAX)
                k90 = _clamp(s90 / s50, RATIO_MIN, RATIO_MAX)
        p10[ts] = v * k10
        p90[ts] = v * k90
    return {"p10": p10, "p50": dict(cons), "p90": p90}
