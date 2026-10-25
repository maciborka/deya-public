"""Декодер регистров настроек Deye (98–177) в человекочитаемый вид. Чистые функции, без Django."""

ENERGY_PATTERNS = {0: "Battery First", 1: "Load First"}
WORK_MODES = {0: "Selling First (Export First)", 1: "Zero Export To Load", 2: "Zero Export To CT"}
SOURCES = {0: "нет", 1: "сеть", 2: "генератор", 3: "сеть и генератор"}
STATES = {0: "Standby", 1: "Self-test", 2: "Normal", 3: "Alarm", 4: "Fault"}
DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _reg(regs, addr):
    value = regs.get(str(addr), regs.get(addr))
    return value if isinstance(value, (int, float)) else None


def _label(mapping, value):
    if value is None:
        return None
    return mapping.get(int(value), f"неизвестно ({int(value)})")


def fmt_hhmm(value):
    """HHMM числом (2300) -> '23:00'."""
    if value is None:
        return None
    value = int(value)
    return f"{value // 100:02d}:{value % 100:02d}"


def decode_days(mask):
    """Биты 1–7 регистра 146 -> список дней Пн…Вс."""
    if mask is None:
        return []
    return [name for i, name in enumerate(DAY_NAMES) if int(mask) >> (i + 1) & 1]


def days_label(days):
    if len(days) == 7:
        return "все дни"
    return ", ".join(days) if days else "нет дней"


def decode_tou_slots(regs):
    times = [_reg(regs, 148 + i) for i in range(6)]
    slots = []
    for i in range(6):
        start = times[i]
        end = times[(i + 1) % 6]
        source = _reg(regs, 172 + i)
        voltage = _reg(regs, 160 + i)
        slots.append(
            {
                "n": i + 1,
                "start": fmt_hhmm(start),
                "end": fmt_hhmm(end),
                "power_w": _reg(regs, 154 + i),
                "voltage_v": round(voltage * 0.01, 2) if voltage is not None else None,
                "soc_pct": _reg(regs, 166 + i),
                "source": None if source is None else int(source),
                "source_label": _label(SOURCES, source),
            }
        )
    return slots


def decode_mode(regs):
    """regs: {"<адрес>": значение} из settings_snapshots."""
    energy = _reg(regs, 141)
    work = _reg(regs, 142)
    grid_charge = _reg(regs, 130)
    export = _reg(regs, 145)
    tou = _reg(regs, 146)
    days = decode_days(tou)
    return {
        "energy_pattern": _label(ENERGY_PATTERNS, energy),
        "energy_pattern_code": None if energy is None else int(energy),
        "work_mode": _label(WORK_MODES, work),
        "work_mode_code": None if work is None else int(work),
        "grid_charge": {"enabled": None if grid_charge is None else bool(grid_charge), "current_a": _reg(regs, 128)},
        "max_charge_a": _reg(regs, 108),
        "max_discharge_a": _reg(regs, 109),
        "soc_shutdown_pct": _reg(regs, 115),
        "soc_restart_pct": _reg(regs, 116),
        "soc_low_pct": _reg(regs, 117),
        "export": {"enabled": None if export is None else bool(export), "max_power_w": _reg(regs, 143)},
        "tou": {
            "enabled": None if tou is None else bool(int(tou) & 1),
            "days": days,
            "days_label": days_label(days),
            "slots": decode_tou_slots(regs),
        },
    }


def decode_state(state):
    return _label(STATES, state)


def slot_at(slots, hhmm):
    """Слот, действующий в момент hhmm (число HHMM); слот действует от своего начала до начала следующего."""
    timed = [s for s in slots if s["start"] is not None]
    if not timed:
        return None
    now = f"{hhmm // 100:02d}:{hhmm % 100:02d}"
    ordered = sorted(timed, key=lambda s: s["start"])
    current = ordered[-1]
    for s in ordered:
        if s["start"] <= now:
            current = s
    return current
