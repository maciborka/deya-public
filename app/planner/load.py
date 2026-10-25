"""Профиль нагрузки по 15-минутным слотам суток."""
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .timeutil import SLOT_SEC, SLOTS_PER_DAY, local, slot_floor, slot_index, zone

FALLBACK_P50_W = 600.0
FALLBACK_P75_W = 750.0
MIN_HOURS = 6.0
MIN_DAYS_FOR_SPREAD = 3
P75_FALLBACK_K = 1.25


@dataclass
class LoadProfile:
    """Профиль нагрузки на 96 слотов: p50 и p75 (Вт)."""

    p50: list[float]
    p75: list[float]
    days: int
    fallback: bool


def _weighted_quantile(vals: list[tuple[float, float]], q: float) -> float:
    """Взвешенный квантиль: первое значение, на котором накопленный вес ≥ q·Σвес. vals — (вес, значение)."""
    ordered = sorted(vals, key=lambda wx: wx[1])
    total = sum(w for w, _ in ordered)
    acc = 0.0
    for w, x in ordered:
        acc += w
        if acc >= q * total - 1e-12:
            return x
    return ordered[-1][1]


def _interpolate(values: list[float | None]) -> list[float]:
    """Заполнить пропуски линейной интерполяцией по соседним слотам (по кругу суток)."""
    n = len(values)
    known = [i for i, v in enumerate(values) if v is not None]
    if not known:
        raise ValueError("нет ни одного известного слота")
    out = list(values)
    for i, v in enumerate(values):
        if v is not None:
            continue
        prev = max((k for k in known if k < i), default=known[-1] - n)
        nxt = min((k for k in known if k > i), default=known[0] + n)
        vp, vn = values[prev % n], values[nxt % n]
        out[i] = vp + (vn - vp) * (i - prev) / (nxt - prev)
    return out


def build_load_profile(
    samples: list[tuple[int, float]],
    now_ts: int,
    tz: str | ZoneInfo,
    days: int = 14,
    alpha: float = 0.15,
) -> LoadProfile:
    """Профиль нагрузки за последние days суток.

    Дневные значения слота взвешиваются экспоненциально: вес (1−alpha)^возраст_дня (свежие весомее).
    p50 — взвешенная медиана по дням, p75 — взвешенный 75-й перцентиль; если в слоте < 3 дней — p50 × 1.25.
    Данных < 6 ч — плоский fallback 600/750 Вт. Пустые слоты — интерполяция по соседям.
    """
    tzi = zone(tz)
    since = now_ts - days * 86400
    picked = [(ts, w) for ts, w in samples if since <= ts < now_ts and w is not None]
    covered = {slot_floor(ts, tzi) for ts, _ in picked}
    if len(covered) * SLOT_SEC / 3600 < MIN_HOURS:
        return LoadProfile([FALLBACK_P50_W] * SLOTS_PER_DAY, [FALLBACK_P75_W] * SLOTS_PER_DAY, 0, True)

    buckets: dict[tuple, list[float]] = {}
    for ts, w in picked:
        buckets.setdefault((local(ts, tzi).date(), slot_index(ts, tzi)), []).append(float(w))
    today = local(now_ts, tzi).date()
    per_slot: list[list[tuple[float, float]]] = [[] for _ in range(SLOTS_PER_DAY)]
    for (d, idx), vals in buckets.items():
        weight = (1 - alpha) ** max(0, (today - d).days)
        per_slot[idx].append((weight, sum(vals) / len(vals)))

    p50: list[float | None] = []
    p75: list[float | None] = []
    for vals in per_slot:
        if not vals:
            p50.append(None)
            p75.append(None)
            continue
        med = _weighted_quantile(vals, 0.5)
        q75 = med * P75_FALLBACK_K if len(vals) < MIN_DAYS_FOR_SPREAD else _weighted_quantile(vals, 0.75)
        p50.append(med)
        p75.append(max(q75, med))
    n_days = len({d for d, _ in buckets})
    return LoadProfile(_interpolate(p50), _interpolate(p75), n_days, False)


def expand_profile(profile: Sequence[float], start_ts: int, n_slots: int, tz: str | ZoneInfo, slot_sec: int = SLOT_SEC) -> list[float]:
    """Развернуть суточный профиль (96 слотов) в ряд на n_slots начиная со start_ts (по локальному времени)."""
    if len(profile) != SLOTS_PER_DAY:
        raise ValueError(f"профиль должен содержать {SLOTS_PER_DAY} слотов, получено {len(profile)}")
    tzi = zone(tz)
    return [float(profile[slot_index(start_ts + i * slot_sec, tzi)]) for i in range(n_slots)]
