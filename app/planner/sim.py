"""Симулятор АКБ/инвертора Deye в режиме TOU по 15-минутным слотам."""
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .params import PlannerParams
from .timeutil import local, zone

PriceFn = Callable[[int], float]
EPS = 1e-9


@dataclass
class SimResult:
    """Итог симуляции. soc_pct — SOC на начало каждого слота плюс финальный (n+1 точка), энергия — кВт·ч."""

    slot_ts: list[int]
    soc_pct: list[float]
    grid_import_kwh: float = 0.0
    grid_charge_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    pv_kwh: float = 0.0
    curtailed_kwh: float = 0.0
    load_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0
    battery_throughput_kwh: float = 0.0
    cost_uah: float = 0.0
    wear_uah: float = 0.0
    end_soc: float = 0.0
    min_soc: float = 0.0
    min_soc_ts: int = 0
    reserve_hits_slots: int = 0
    import_by_slot: list[float] = field(default_factory=list)
    pv_by_slot: list[float] = field(default_factory=list)
    load_by_slot: list[float] = field(default_factory=list)


def simulate(
    soc0_pct: float,
    start_ts: int,
    pv_w: Sequence[float],
    load_w: Sequence[float],
    params: PlannerParams,
    price: PriceFn,
    night_target_pct: float,
    tou: bool | Sequence[bool] = True,
) -> SimResult:
    """Прогон по слотам начиная со start_ts (длина ряда = len(load_w)).

    tou — разрешён ли заряд от сети в ночных слотах (23:00–07:00): bool для всех слотов или по слоту.
    Ночной слот с зарядом: SOC < target — сеть дозаряжает до target (≤ night_charge_limit_w, с η_charge),
    АКБ нагрузку не питает; SOC ≥ target — АКБ отдаёт нагрузке, но не ниже target. PV-излишек всегда
    заряжает АКБ. Остальные слоты: PV → нагрузка → АКБ (до 100 %, ≤ max_charge_w) → экспорт
    (≤ inverter_max_w, остальное — curtailed); дефицит — из АКБ до пола, дальше сеть.
    Пол — max(резерв, shutdown, 100 − usable) или, если задан params.floor_profile, пол профиля на это время
    суток (params.floor_pct_at); в ночных слотах с зарядом — max(пол, target).
    reserve_hits_slots — число слотов, закончившихся на полу.
    battery_throughput_kwh — энергия, отданная АКБ на шину; износ = throughput × wear_cost_per_kwh.
    cost_uah = Σ импорт × price(ts) − экспорт × export_price_uah.
    """
    n = len(load_w)
    if len(pv_w) != n:
        raise ValueError("pv_w и load_w должны быть одной длины")
    mask = [bool(tou)] * n if isinstance(tou, bool) else [bool(x) for x in tou]
    if len(mask) != n:
        raise ValueError("длина tou не совпадает с рядом")
    tzi = zone(params.tz)
    kpp = params.kwh_per_pct
    eta_c, eta_d = params.eta_charge, params.eta_discharge
    dt = params.slot_sec / 3600
    cap_e = 100.0 * kpp
    tgt_e = min(100.0, max(0.0, night_target_pct)) * kpp
    e = min(100.0, max(0.0, soc0_pct)) * kpp

    res = SimResult(slot_ts=[start_ts + i * params.slot_sec for i in range(n)], soc_pct=[e / kpp])
    for i, ts in enumerate(res.slot_ts):
        pv = min(max(0.0, pv_w[i]), params.pv_max_w) * dt / 1000
        load = max(0.0, load_w[i]) * dt / 1000
        lt = local(ts, tzi)
        night_tou = mask[i] and params.is_night(lt.hour)
        # Пол — целевой профиль (система сама выставляет пороги слотов); ночные слоты с зарядом — max(пол, target).
        floor_e = params.floor_pct_at(lt.hour * 60 + lt.minute) * kpp
        slot_floor_e = max(floor_e, tgt_e) if night_tou else floor_e
        charge_room = params.max_charge_w * dt / 1000
        imp = exp = cur = chg = dis = 0.0
        if pv >= load:
            take = min(pv - load, charge_room, max(0.0, cap_e - e) / eta_c)
            e += take * eta_c
            chg += take
            charge_room -= take
            exp = pv - load - take
            exp_lim = params.inverter_max_w * dt / 1000
            if exp > exp_lim:
                cur = exp - exp_lim
                exp = exp_lim
        else:
            deficit = load - pv
            give = min(deficit, params.inverter_max_w * dt / 1000, max(0.0, e - slot_floor_e) * eta_d)
            e -= give / eta_d
            dis += give
            imp += deficit - give
        if night_tou and e < tgt_e - EPS:
            g = max(0.0, min(params.night_charge_limit_w * dt / 1000, charge_room, (tgt_e - e) / eta_c))
            e += g * eta_c
            chg += g
            imp += g
            res.grid_charge_kwh += g
        e = min(cap_e, max(0.0, e))
        if e <= floor_e + EPS:
            res.reserve_hits_slots += 1

        res.grid_import_kwh += imp
        res.grid_export_kwh += exp
        res.pv_kwh += pv
        res.curtailed_kwh += cur
        res.load_kwh += load
        res.battery_charge_kwh += chg
        res.battery_discharge_kwh += dis
        res.cost_uah += imp * price(ts) - exp * params.export_price_uah
        res.import_by_slot.append(imp)
        res.pv_by_slot.append(pv)
        res.load_by_slot.append(load)
        res.soc_pct.append(e / kpp)

    res.battery_throughput_kwh = res.battery_discharge_kwh
    res.wear_uah = res.battery_throughput_kwh * params.wear_cost_per_kwh
    res.end_soc = res.soc_pct[-1]
    min_i = min(range(len(res.soc_pct)), key=lambda k: res.soc_pct[k])
    res.min_soc = res.soc_pct[min_i]
    res.min_soc_ts = start_ts + min_i * params.slot_sec
    return res
