"""Декодирование регистров Deye (LV 3-phase hybrid). Только чистые функции."""

S16 = "s16"
U16 = "u16"
TEMP = "temp"
U32 = "u32"

# (имя, регистр, тип, масштаб)
METRICS = [
    ("state", 500, U16, 1),
    ("soc_pct", 588, U16, 1),
    ("batt_voltage_v", 587, U16, 0.01),
    ("batt_power_w", 590, S16, 1),
    ("batt_current_a", 591, S16, 0.01),
    ("batt_temp_c", 586, TEMP, 0.1),
    ("dc_temp_c", 540, TEMP, 0.1),
    ("ac_temp_c", 541, TEMP, 0.1),
    ("pv1_power_w", 672, U16, 1),
    ("pv2_power_w", 673, U16, 1),
    ("pv3_power_w", 674, U16, 1),
    ("pv4_power_w", 675, U16, 1),
    ("pv1_voltage_v", 676, U16, 0.1),
    ("pv1_current_a", 677, U16, 0.1),
    ("pv2_voltage_v", 678, U16, 0.1),
    ("pv2_current_a", 679, U16, 0.1),
    ("grid_power_w", 625, S16, 1),
    ("grid_l1_power_w", 622, S16, 1),
    ("grid_l2_power_w", 623, S16, 1),
    ("grid_l3_power_w", 624, S16, 1),
    ("grid_l1_voltage_v", 598, U16, 0.1),
    ("grid_l2_voltage_v", 599, U16, 0.1),
    ("grid_l3_voltage_v", 600, U16, 0.1),
    ("grid_freq_hz", 609, U16, 0.01),
    ("load_power_w", 653, U16, 1),
    ("inverter_power_w", 636, S16, 1),
    ("bms_soc_pct", 214, U16, 1),
    ("pv_today_kwh", 529, U16, 0.1),
    ("batt_charge_today_kwh", 514, U16, 0.1),
    ("batt_discharge_today_kwh", 515, U16, 0.1),
    ("grid_import_today_kwh", 520, U16, 0.1),
    ("grid_export_today_kwh", 521, U16, 0.1),
    ("load_today_kwh", 526, U16, 0.1),
    ("batt_charge_total_kwh", 516, U32, 0.1),
    ("batt_discharge_total_kwh", 518, U32, 0.1),
]

METRIC_NAMES = [m[0] for m in METRICS]
INT_METRICS = {"state"}


def _s16(v: int) -> int:
    return v - 0x10000 if v >= 0x8000 else v


def _round(x: float) -> float:
    return round(x, 4)


def decode(regs: dict[int, int]) -> dict:
    out: dict = {}
    for name, reg, kind, scale in METRICS:
        lo = regs.get(reg)
        if lo is None:
            out[name] = None
            continue
        if kind == U32:
            hi = regs.get(reg + 1)
            if hi is None:
                out[name] = None
                continue
            out[name] = _round((hi << 16 | lo) * scale)
        elif kind == S16:
            out[name] = _round(_s16(lo) * scale) if scale != 1 else _s16(lo)
        elif kind == TEMP:
            out[name] = round((lo - 1000) * scale, 1)
        else:
            out[name] = lo if scale == 1 else _round(lo * scale)
    return out


def settings_regs(regs: dict[int, int]) -> dict[int, int]:
    return {r: regs[r] for r in range(98, 178) if r in regs}
