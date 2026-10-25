"""Выбор target ночного заряда по ожидаемой стоимости в грн."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta

from .load import expand_profile
from .params import PlannerParams
from .pv import _to_slots
from .sim import PriceFn, SimResult, simulate
from .timeutil import SLOTS_PER_DAY, at_local, local, slot_floor, zone

# Веса сценариев PV по режиму риска
RISK_PV_WEIGHTS: dict[str, dict[str, float]] = {
    "economy": {"p10": 0.15, "p50": 0.60, "p90": 0.25},
    "balanced": {"p10": 0.25, "p50": 0.55, "p90": 0.20},
    "safe": {"p10": 0.45, "p50": 0.45, "p90": 0.10},
}
# Вес сценария нагрузки p75 (p50 получает остаток)
RISK_LOAD_P75_WEIGHT: dict[str, float] = {"economy": 0.1, "balanced": 0.3, "safe": 0.5}
FULL_SOC_PCT = 99.5
V_GRID_PCT = 5.0
# «Ядро дня» для проверки покрытия прогноза PV (светло круглый год), местное время
PV_CORE_HOURS = (9, 15)
PV_MIN_COVERAGE = 0.8
# Ограничение «днём (дорогой тариф) не покупать из сети»: дневной импорт 07–23 завтра > DAY_IMPORT_TOL_KWH
# допустим с вероятностью не выше допуска режима риска; пол при расчёте поднят на SAFETY_MARGIN_PCT (погрешность BMS).
DAY_IMPORT_TOL_KWH = 0.5
SAFETY_MARGIN_PCT = 3.0


def _pct(p: float) -> int:
    """Доля → целые % с округлением половины вверх (как Math.round на дашборде; f"{22.5:.0f}" дало бы 22)."""
    return int(p * 100 + 0.5)
RISK_DAY_IMPORT_TOL: dict[str, float] = {"economy": 0.33, "balanced": 0.2, "safe": 0.05}

Series = Mapping[int, float] | Sequence[float]


@dataclass
class TouSlot:
    """Слот TOU Deye: время начала HH:MM, заряд от сети, SOC слота, мощность (Вт)."""

    time: str
    grid_charge: bool
    soc_pct: float
    power_w: float


@dataclass
class Decision:
    """Решение планировщика на ночь. Стоимости — грн: импорт − экспорт + износ за ночь и завтрашний день
    плюс терминальная V(SOC на 23:00 завтра) — оптимальная стоимость ночи₂ + дня₂."""

    target_pct: float
    expected_cost_uah: float
    scenario_costs: dict[str, float]
    candidate_costs: dict[float, float]
    counterfactuals: dict[str, float]
    savings_vs_no_charge_uah: float
    savings_vs_no_charge_no_wear_uah: float
    expected_grid_import_kwh: float
    soc_at_23_pct: float
    soc_at_07_pct: float
    min_soc_tomorrow_pct: float
    min_soc_tomorrow_worst_pct: float
    margin_uah_per_kwh: float
    epsilon_applied: bool
    tomorrow_full_p50: bool
    day2_forecast_missing: bool
    pv_coverage_incomplete: bool
    horizon_start_ts: int
    horizon_end_ts: int
    n_slots: int
    explanation: str
    tou_plan: list[TouSlot] = field(default_factory=list)
    hourly: list[dict] = field(default_factory=list)
    # Ограничение дневного импорта (07–23 завтра, пол + SAFETY_MARGIN_PCT): вероятность выбранного target,
    # взвешенный ожидаемый объём, выполнено ли (P ≤ допуска), допуск режима риска
    day_import_prob: float = 0.0
    expected_day_import_kwh: float = 0.0
    day_import_constraint_met: bool = True
    day_import_tol: float = 0.0
    candidate_day_import_prob: dict[float, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Ручная ночная цель (настройки): target_pct = ручное значение (clamp пол..потолок); для сравнения —
    # target, который выбрал бы расчёт, и его вероятность дневного импорта
    manual: bool = False
    auto_target_pct: float | None = None
    auto_day_import_prob: float | None = None


def hourly_plan(r: SimResult, params: PlannerParams, price: PriceFn) -> list[dict]:
    """Почасовая сводка симуляции: PV и потребление (кВт·ч), SOC на конец часа (%), покупка из сети (кВт·ч)
    и её стоимость по тарифу (грн). Час — локальный, ts — начало часа (или первого слота горизонта в нём)."""
    tzi = zone(params.tz)
    hours: list[dict] = []
    for i, ts in enumerate(r.slot_ts):
        lt = local(ts, tzi)
        key = (lt.date(), lt.hour, lt.utcoffset())
        if not hours or hours[-1]["_key"] != key:
            hours.append({"_key": key, "ts": ts, "hour": f"{lt.hour:02d}:00", "pv_kwh": 0.0, "load_kwh": 0.0,
                          "soc_end_pct": 0.0, "grid_import_kwh": 0.0, "cost_uah": 0.0})
        h = hours[-1]
        h["pv_kwh"] += r.pv_by_slot[i]
        h["load_kwh"] += r.load_by_slot[i]
        h["grid_import_kwh"] += r.import_by_slot[i]
        h["cost_uah"] += r.import_by_slot[i] * price(ts)
        h["soc_end_pct"] = r.soc_pct[i + 1]
    for h in hours:
        del h["_key"]
    return hours


def tou_plan(target_pct: float, params: PlannerParams) -> list[TouSlot]:
    """6 слотов TOU: 00:00 сеть→target; 07:00, 17:00, 21:00, 22:00 — без заряда, SOC = резерв; 23:00 сеть→target."""
    hold = params.inverter_max_w
    grid = params.night_charge_limit_w
    r = params.reserve_soc_pct
    return [
        TouSlot("00:00", True, target_pct, grid),
        TouSlot("07:00", False, r, hold),
        TouSlot("17:00", False, r, hold),
        TouSlot("21:00", False, r, hold),
        TouSlot("22:00", False, r, hold),
        TouSlot("23:00", True, target_pct, grid),
    ]


def default_tou_plan(params: PlannerParams) -> list[TouSlot]:
    """План по умолчанию (target = default_plan_soc) — если решения нет."""
    return tou_plan(params.default_plan_soc, params)


def _series(src: Series, slots: list[int], params: PlannerParams, missing: float) -> list[float]:
    """Ряд по слотам: словарь {ts: Вт} (почасовой растягивается на 15-мин слоты; пропуск → missing)
    или суточный профиль из 96 значений."""
    if isinstance(src, Mapping):
        m = _to_slots(src, params.slot_sec)
        return [m.get(ts, missing) for ts in slots]
    if len(src) == SLOTS_PER_DAY:
        return expand_profile(src, slots[0], len(slots), params.tz, params.slot_sec) if slots else []
    raise ValueError("ряд должен быть словарём {slot_ts: Вт} или профилем из 96 слотов")


def _load_series(src: Series, slots: list[int], params: PlannerParams) -> list[float]:
    if isinstance(src, Mapping):
        vals = [float(v) for v in src.values() if v is not None]
        return _series(src, slots, params, sum(vals) / len(vals) if vals else 0.0)
    return _series(src, slots, params, 0.0)


def _pv_coverage(src: Series, a: int, b: int, params: PlannerParams) -> float:
    """Доля слотов «ядра дня» (PV_CORE_HOURS по местному времени) в [a, b), для которых есть прогноз PV."""
    if not isinstance(src, Mapping):
        return 1.0
    m = _to_slots(src, params.slot_sec)
    core = [ts for ts in range(a, b, params.slot_sec) if PV_CORE_HOURS[0] <= local(ts, params.tz).hour < PV_CORE_HOURS[1]]
    if not core:
        return 1.0
    return sum(1 for ts in core if ts in m) / len(core)


def _night_window(now_ts: int, params: PlannerParams):
    """Локальная дата начала ночи, содержащей now или ближайшей следующей."""
    now = local(now_ts, params.tz)
    d = now.date()
    if now.hour < params.night_end_hour:
        d -= timedelta(days=1)
    return d


def _grid(lo: float, hi: float, step: float) -> list[float]:
    out: list[float] = []
    t = lo
    while t <= hi + 1e-9:
        out.append(round(t, 6))
        t += step
    return out


def plan_night(
    now_ts: int,
    soc_now_pct: float,
    pv_scenarios: Mapping[str, Series],
    load_p50: Series,
    load_p75: Series,
    params: PlannerParams,
    price: PriceFn,
    _only_target: float | None = None,
    manual_target: float | None = None,
) -> "Decision | float":
    """Target ночного заряда: самый дешёвый из допустимых по ограничению дневного импорта.

    Горизонт: ближайшие 23:00 → 23:00 завтра (ночь + день). Если now < 23:00, SOC на 23:00 получается
    прогоном now→23:00 без сетевого заряда (PV p50, нагрузка p50).
    Кандидаты: сетка от резерва до потолка с шагом target_step_pct плюс «удержание» clamp(SOC_23 − ε)
    и clamp(SOC_23) (clamp в [резерв, 100]). ε влияет только на набор кандидатов и пояснение.
    Сценарии: PV p10/p50/p90 × нагрузка p50/p75; веса по risk:
      economy  — PV .15/.60/.25, нагрузка p75 .1;
      balanced — PV .25/.55/.20, нагрузка p75 .3;
      safe     — PV .45/.45/.10, нагрузка p75 .5.
    Стоимость сценария = импорт×цена − экспорт×цена_экспорта + износ + V(SOC на 23:00 завтра).
    V — таблица по сетке SOC 5 % (линейная интерполяция): min по target₂ (сетка 5 % от резерва) стоимости
    ночи₂ + дня₂ при PV p50 дня 2 и тех же весах нагрузки. Нет прогноза на день 2 (ядро 09–15 покрыто < 80 %) —
    PV дня 2 берётся как PV p50 завтра в то же местное время, day2_forecast_missing=True.
    Покрытие прогноза PV на завтра < 80 % ядра дня — ValueError; 80–100 % — pv_coverage_incomplete=True.
    Ограничение: для кандидата и сценария D — импорт из сети в слотах 07:00–23:00 завтра при поле, поднятом на
    SAFETY_MARGIN_PCT; P = Σ весов сценариев с D > DAY_IMPORT_TOL_KWH. Допустимы кандидаты с P ≤
    RISK_DAY_IMPORT_TOL[risk]; из них — argmin стоимости. Допустимых нет → min P, затем min ожидаемого D, затем
    меньший target (на практике — потолок) и предупреждение.
    Экономическая проверка: маржа = дневная_цена − ночная_цена/(η_charge·η_discharge) − износ; при маржа ≤ 0
    из допустимых берутся только кандидаты без заряда от сети (≤ SOC_23), если такие есть; иначе ночной заряд
    остаётся (дневной импорт запрещён независимо от цены).
    manual_target (ручная цель из настроек): target = clamp(manual_target, пол, потолок) вместо выбранного;
    траектории, стоимости и вероятность дневного импорта считаются для него, выбранный расчётом target
    сохраняется в auto_target_pct; P ручной цели > допуска — предупреждение.
    """
    tzi = zone(params.tz)
    step = params.slot_sec
    d = _night_window(now_ts, params)
    t23 = at_local(d, params.night_start_hour, tzi)
    t07 = at_local(d + timedelta(days=1), params.night_end_hour, tzi)
    t23_next = at_local(d + timedelta(days=1), params.night_start_hour, tzi)
    t07_day2 = at_local(d + timedelta(days=2), params.night_end_hour, tzi)
    end2 = at_local(d + timedelta(days=2), params.night_start_hour, tzi)
    now_slot = slot_floor(now_ts, tzi)
    start = max(t23, now_slot)

    pv_map = {k: pv_scenarios.get(k, pv_scenarios["p50"]) for k in ("p10", "p50", "p90")}
    cov1 = _pv_coverage(pv_map["p50"], t07, t23_next, params)
    if cov1 < PV_MIN_COVERAGE:
        raise ValueError(
            f"Прогноз PV на завтра покрывает {cov1:.0%} слотов {PV_CORE_HOURS[0]:02d}:00–{PV_CORE_HOURS[1]:02d}:00 "
            f"(нужно ≥ {PV_MIN_COVERAGE:.0%}) — решение не принимается."
        )
    pv_incomplete = cov1 < 1.0
    day2_missing = _pv_coverage(pv_map["p50"], t07_day2, end2, params) < PV_MIN_COVERAGE

    soc23 = min(100.0, max(0.0, soc_now_pct))
    if now_slot < t23:
        pre_slots = list(range(now_slot, t23, step))
        pre = simulate(
            soc23, now_slot, _series(pv_map["p50"], pre_slots, params, 0.0),
            _load_series(load_p50, pre_slots, params), params, price, params.reserve_soc_pct, tou=False,
        )
        soc23 = pre.end_soc

    w_pv = RISK_PV_WEIGHTS[params.risk]
    w75 = RISK_LOAD_P75_WEIGHT[params.risk]
    w_load = {"p50": 1 - w75, "p75": w75}
    combos = [(pk, lk, w_pv[pk] * w_load[lk]) for pk in ("p10", "p50", "p90") for lk in ("p50", "p75")]

    # Терминальная функция V(SOC на 23:00 завтра): оптимум ночи₂ + дня₂
    slots2 = list(range(t23_next, end2, step))
    if day2_missing:
        day1_ts = [slot_floor(int((local(ts, tzi) - timedelta(days=1)).timestamp()), tzi) for ts in slots2]
        pv2 = _series(pv_map["p50"], day1_ts, params, 0.0)
    else:
        pv2 = _series(pv_map["p50"], slots2, params, 0.0)
    load2 = {"p50": _load_series(load_p50, slots2, params), "p75": _load_series(load_p75, slots2, params)}
    soc_grid = _grid(0.0, 100.0, V_GRID_PCT)
    v_table: list[float] = []
    for g in soc_grid:
        best_v = None
        for t2 in _grid(params.reserve_soc_pct, max(params.night_ceiling_pct, g), V_GRID_PCT):
            v = sum(
                w * (r.cost_uah + r.wear_uah)
                for lk, w in w_load.items()
                for r in [simulate(g, t23_next, pv2, load2[lk], params, price, t2)]
            )
            best_v = v if best_v is None else min(best_v, v)
        v_table.append(best_v)

    def terminal_v(soc: float) -> float:
        x = min(100.0, max(0.0, soc)) / V_GRID_PCT
        i = min(len(v_table) - 2, int(x))
        return v_table[i] + (v_table[i + 1] - v_table[i]) * (x - i)

    slots = list(range(start, t23_next, step))
    n = len(slots)
    pv = {k: _series(v, slots, params, 0.0) for k, v in pv_map.items()}
    load = {lk: _load_series(src, slots, params) for lk, src in (("p50", load_p50), ("p75", load_p75))}

    night_price = price(start)
    day_slots = [ts for ts in slots if ts >= t07] or slots
    day_price = max(price(ts) for ts in day_slots)
    margin = day_price - night_price / (params.eta_charge * params.eta_discharge) - params.wear_cost_per_kwh
    no_charge = min(params.reserve_soc_pct, soc23)

    def idx(ts: int) -> int:
        return max(0, min(n, (ts - start) // step))

    def _raised(x: float) -> float:
        return min(100.0, max(x, params.shutdown_soc_pct, 100.0 - params.usable_pct) + SAFETY_MARGIN_PCT)

    safe_params = replace(
        params,
        reserve_soc_pct=_raised(params.floor_pct),
        floor_profile=[(a, b, _raised(float(f))) for a, b, f in params.floor_profile] if params.floor_profile else None,
    )
    day_seg = slice(idx(t07), idx(t23_next))
    tol = RISK_DAY_IMPORT_TOL[params.risk]

    cache: dict[float, dict] = {}

    def evaluate(target: float) -> dict:
        target = round(target, 6)
        if target in cache:
            return cache[target]
        costs, costs_nw, sims, day_imp = {}, {}, {}, {}
        for pk, lk, _ in combos:
            r = simulate(soc23, start, pv[pk], load[lk], params, price, target)
            sims[(pk, lk)] = r
            v = terminal_v(r.end_soc)
            costs[f"{pk}/{lk}"] = r.cost_uah + r.wear_uah + v
            costs_nw[f"{pk}/{lk}"] = r.cost_uah + v
            if _only_target is None:
                rs = simulate(soc23, start, pv[pk], load[lk], safe_params, price, target)
                day_imp[(pk, lk)] = sum(rs.import_by_slot[day_seg])
        out = {
            "day_p": sum(w for pk, lk, w in combos if day_imp.get((pk, lk), 0.0) > DAY_IMPORT_TOL_KWH),
            "day_kwh": sum(w * day_imp.get((pk, lk), 0.0) for pk, lk, w in combos),
            "cost": sum(w * costs[f"{pk}/{lk}"] for pk, lk, w in combos),
            "cost_nw": sum(w * costs_nw[f"{pk}/{lk}"] for pk, lk, w in combos),
            "import": sum(w * sims[(pk, lk)].grid_import_kwh for pk, lk, w in combos),
            "costs": costs,
            "sims": sims,
        }
        cache[target] = out
        return out

    if _only_target is not None:
        return evaluate(_only_target)["cost"]

    def clamp(x: float) -> float:
        return round(min(100.0, max(params.reserve_soc_pct, x)), 6)

    grid = _grid(params.reserve_soc_pct, params.night_ceiling_pct, params.target_step_pct)
    hold_eps = clamp(soc23 - params.epsilon_pct)
    hold = clamp(soc23)
    candidates = sorted(set(grid) | {hold_eps, hold, round(no_charge, 6)})

    def no_grid(c: float) -> bool:
        return c <= soc23 + 1e-9 or c == round(no_charge, 6)

    def cheapest(cs: list[float]) -> float:
        best = cs[0]
        for c in cs[1:]:
            if evaluate(c)["cost"] < evaluate(best)["cost"] - 1e-6:
                best = c
        return best

    feasible = [c for c in candidates if evaluate(c)["day_p"] <= tol + 1e-9]
    feasible_nc = [c for c in feasible if no_grid(c)]
    constraint_met = bool(feasible)
    if feasible:
        target = cheapest(feasible_nc if margin <= 0 and feasible_nc else feasible)
    else:
        target = min(candidates, key=lambda c: (round(evaluate(c)["day_p"], 9), round(evaluate(c)["day_kwh"], 6), c))
    unconstrained = cheapest([c for c in candidates if no_grid(c)] if margin <= 0 else candidates)
    binding = evaluate(unconstrained)["day_p"] > tol + 1e-9
    margin_override = margin <= 0 and not no_grid(target)
    manual = manual_target is not None
    auto_target = target
    if manual:
        target = round(min(params.night_ceiling_pct, max(params.reserve_soc_pct, float(manual_target))), 6)

    chosen = evaluate(target)
    nc = evaluate(no_charge)
    r50 = chosen["sims"][("p50", "p50")]
    tomorrow_seg = slice(idx(t07), idx(t23_next) + 1)
    # SOC после первого дневного слота: 100 % на 07:00 от ночного заряда — не «заполнение солнцем»
    tomorrow_full = max(r50.soc_pct[idx(t07) + 1: idx(t23_next) + 1] or [r50.end_soc]) >= FULL_SOC_PCT
    eps_applied = not manual and target == hold_eps and target not in grid and target <= soc23

    reasons: list[str] = []
    warnings: list[str] = []
    if manual:
        auto_p = evaluate(auto_target)["day_p"]
        constraint_met = chosen["day_p"] <= tol + 1e-9
        reasons.append(
            f"Ручная ночная цель {target:g} % (настройки); расчёт выбрал бы {auto_target:g} % "
            f"(вероятность дневной докупки {_pct(auto_p)} %)."
        )
        if constraint_met:
            reasons.append(f"Вероятность дневной докупки при ручной цели {_pct(chosen['day_p'])} % "
                           f"(допуск {_pct(tol)} %).")
        else:
            msg = (f"ручная цель: вероятность дневной докупки {_pct(chosen['day_p'])} % "
                   f"(авто дало бы {_pct(auto_p)} %)")
            reasons.append(msg[0].upper() + msg[1:] + ".")
            warnings.append(msg)
    else:
        if margin <= 0:
            reasons.append(
                f"Маржа ночного заряда {margin:.2f} грн/кВт·ч ≤ 0 ({day_price:.2f} − {night_price:.2f}/η − износ "
                f"{params.wear_cost_per_kwh:.2f}) — заряд от сети невыгоден"
                + (", но без него днём придётся покупать из сети — заряжаем." if margin_override else ".")
            )
        if constraint_met:
            reasons.append(
                f"{'Минимальный заряд' if binding else 'Самый дешёвый target'}, при котором днём не покупаем "
                f"из сети с вероятностью ≥ {_pct((1 - tol))} %: {target:g} % (вероятность дневного импорта "
                f"{_pct(chosen['day_p'])} %, запас пола +{SAFETY_MARGIN_PCT:g} %)."
            )
        else:
            msg = (f"Даже при заряде до {target:g} % днём вероятно докупим ~{chosen['day_kwh']:.1f} кВт·ч "
                   f"(вероятность {_pct(chosen['day_p'])} %).")
            reasons.append(msg)
            warnings.append(msg)
    reasons.append(
        f"Ожидаемая стоимость ({params.risk}): target {target:g} % — "
        f"{chosen['cost']:.2f} грн против {nc['cost']:.2f} грн без заряда."
    )
    if not manual:
        if target in (hold_eps, hold) and target <= soc23 + 1e-9 and target > no_charge:
            reasons.append(
                f"Выгоднее удержать ночью SOC ≈ {target:.1f} % (SOC_23 {soc23:.1f}, ε {params.epsilon_pct:g}) — "
                "запас АКБ дороже днём, чем дешёвой ночью."
            )
        elif tomorrow_full:
            reasons.append("Завтра по p50 АКБ заполняется солнцем — лишний ночной заряд вытеснил бы бесплатную PV.")
        elif target > soc23:
            reasons.append("Завтра солнца мало — ночной заряд от сети окупается.")
    if day2_missing:
        reasons.append("Прогноза на послезавтра нет — для оценки остатка взят PV завтрашнего дня.")
    if pv_incomplete:
        reasons.append(f"Прогноз PV на завтра неполный ({cov1:.0%} ядра дня).")

    def min_tomorrow(r: SimResult) -> float:
        return min(r.soc_pct[tomorrow_seg] or [r.end_soc])

    return Decision(
        target_pct=target,
        expected_cost_uah=chosen["cost"],
        scenario_costs=dict(chosen["costs"]),
        candidate_costs={c: evaluate(c)["cost"] for c in sorted(set(candidates) | set(grid) | {target})},
        counterfactuals={
            "no_charge": nc["cost"],
            "hold": evaluate(hold_eps)["cost"],
            "ceiling": evaluate(params.night_ceiling_pct)["cost"],
            "full": evaluate(100.0)["cost"],
        },
        savings_vs_no_charge_uah=nc["cost"] - chosen["cost"],
        savings_vs_no_charge_no_wear_uah=nc["cost_nw"] - chosen["cost_nw"],
        expected_grid_import_kwh=chosen["import"],
        soc_at_23_pct=soc23,
        soc_at_07_pct=r50.soc_pct[idx(t07)],
        min_soc_tomorrow_pct=min_tomorrow(r50),
        min_soc_tomorrow_worst_pct=min(min_tomorrow(r) for r in chosen["sims"].values()),
        margin_uah_per_kwh=margin,
        epsilon_applied=eps_applied,
        tomorrow_full_p50=tomorrow_full,
        day2_forecast_missing=day2_missing,
        pv_coverage_incomplete=pv_incomplete,
        horizon_start_ts=start,
        horizon_end_ts=t23_next,
        n_slots=n,
        explanation=" ".join(reasons),
        tou_plan=tou_plan(target, params),
        hourly=hourly_plan(r50, params, price),
        day_import_prob=chosen["day_p"],
        expected_day_import_kwh=chosen["day_kwh"],
        day_import_constraint_met=constraint_met,
        day_import_tol=tol,
        candidate_day_import_prob={c: evaluate(c)["day_p"] for c in sorted(set(candidates) | set(grid) | {target})},
        warnings=warnings,
        manual=manual,
        auto_target_pct=auto_target if manual else None,
        auto_day_import_prob=evaluate(auto_target)["day_p"] if manual else None,
    )


def perfect_forecast_cost(
    now_ts: int,
    soc_now_pct: float,
    pv_actual: Series,
    load_actual: Series,
    params: PlannerParams,
    price: PriceFn,
) -> Decision:
    """Та же оптимизация при известных факте PV и нагрузки (все сценарии = факт) — для расчёта regret."""
    return plan_night(
        now_ts, soc_now_pct, {"p10": pv_actual, "p50": pv_actual, "p90": pv_actual},
        load_actual, load_actual, params, price,
    )


def evaluate_target(
    now_ts: int,
    soc_now_pct: float,
    pv_actual: Series,
    load_actual: Series,
    params: PlannerParams,
    price: PriceFn,
    target_pct: float,
) -> float:
    """Стоимость по модели (импорт − экспорт + износ + V) заданного target при известных факте PV и нагрузки."""
    return plan_night(
        now_ts, soc_now_pct, {"p10": pv_actual, "p50": pv_actual, "p90": pv_actual},
        load_actual, load_actual, params, price, _only_target=target_pct,
    )
