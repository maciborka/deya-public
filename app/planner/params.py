"""Параметры планировщика ночного заряда."""
import math
from dataclasses import dataclass
from typing import Literal

Risk = Literal["economy", "balanced", "safe"]
RISKS = ("economy", "balanced", "safe")


@dataclass
class PlannerParams:
    """АКБ, инвертор, тариф и стратегия. Мощность — Вт, энергия — кВт·ч, SOC — %, цены — грн/кВт·ч.

    SOC — доля номинальной ёмкости (1 % = capacity_kwh/100). usable_frac не масштабирует энергию,
    а ограничивает диапазон SOC снизу: floor = max(резерв, shutdown, 100 − usable_frac·100).

    reserve_soc_pct — эффективный пол разряда из настроек (аварийный или обычный, см. planner_service.effective_floor).
    floor_profile — целевой профиль пола [(начало_мин, конец_мин, SOC %), …] (окна внутри суток, конец не включая):
    система сама выставляет в инвертор дневные пороги по профилю; вне окон действует reserve_soc_pct.
    Для «всегда»/«выкл» — одно окно 00:00–24:00; окна отключений (schedule) лягут в тот же список.
    cloudy_full_charge_kwh — порог пасмурного дня: прогноз PV p50 на завтра (07–23) ниже него → ночная цель = потолок;
    None/0 — правило выключено.
    floor_slots — устарело (пороги TOU из снимка), на пол не влияет; поле оставлено для сохранённых решений.

    eta_charge/eta_discharge по умолчанию = sqrt(roundtrip_eff); night_charge_limit_w по умолчанию
    = grid_charge_current_a × battery_voltage_v. Тариф импорта передаётся функцией price(ts).
    """

    capacity_kwh: float = 24.0
    usable_frac: float = 0.95
    roundtrip_eff: float = 0.9
    eta_charge: float | None = None
    eta_discharge: float | None = None
    battery_voltage_v: float = 51.0
    max_charge_current_a: float = 150.0
    grid_charge_current_a: float = 100.0
    night_charge_limit_w: float | None = None
    pv_max_w: float = 12000.0
    inverter_max_w: float = 12000.0
    export_price_uah: float = 0.0
    reserve_soc_pct: float = 30.0
    night_ceiling_pct: float = 80.0
    shutdown_soc_pct: float = 5.0
    tz: str = "Europe/Kyiv"
    night_start_hour: int = 23
    night_end_hour: int = 7
    wear_cost_per_kwh: float = 0.4
    risk: Risk = "balanced"
    target_step_pct: float = 5.0
    epsilon_pct: float = 3.0
    default_plan_soc: float = 60.0
    slot_sec: int = 900
    floor_slots: list | None = None
    floor_profile: list | None = None
    cloudy_full_charge_kwh: float | None = None

    def __post_init__(self) -> None:
        if self.risk not in RISKS:
            raise ValueError(f"risk должен быть одним из {RISKS}, получено {self.risk!r}")
        if self.eta_charge is None:
            self.eta_charge = math.sqrt(self.roundtrip_eff)
        if self.eta_discharge is None:
            self.eta_discharge = math.sqrt(self.roundtrip_eff)
        if self.night_charge_limit_w is None:
            self.night_charge_limit_w = self.grid_charge_current_a * self.battery_voltage_v

    @property
    def usable_pct(self) -> float:
        """Полезная доля ёмкости, %."""
        return self.usable_frac * 100.0

    @property
    def kwh_per_pct(self) -> float:
        """кВт·ч на 1 % SOC (доля номинала)."""
        return self.capacity_kwh / 100.0

    @property
    def max_charge_w(self) -> float:
        """Лимит мощности заряда АКБ (ток × напряжение)."""
        return self.max_charge_current_a * self.battery_voltage_v

    @property
    def floor_pct(self) -> float:
        """Нижняя граница SOC: max(резерв, shutdown, 100 − usable_pct)."""
        return max(self.reserve_soc_pct, self.shutdown_soc_pct, 100.0 - self.usable_pct)

    def floor_pct_at(self, minute_of_day: int) -> float:
        """Нижняя граница SOC в момент суток: пол окна floor_profile, содержащего минуту (при пересечении окон —
        наибольший), иначе floor_pct; не ниже shutdown и 100 − usable_pct."""
        hits = [float(p) for start, end, p in self.floor_profile or [] if start <= minute_of_day < end]
        if not hits:
            return self.floor_pct
        return max(max(hits), self.shutdown_soc_pct, 100.0 - self.usable_pct)

    def is_night(self, hour: int) -> bool:
        """Локальный час попадает в ночную зону (23:00–07:00)."""
        if self.night_start_hour > self.night_end_hour:
            return hour >= self.night_start_hour or hour < self.night_end_hour
        return self.night_start_hour <= hour < self.night_end_hour
