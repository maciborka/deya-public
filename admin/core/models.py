from datetime import time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class Sample(models.Model):
    ts = models.BigIntegerField(primary_key=True, verbose_name="время (unix UTC)")
    state = models.IntegerField(null=True, blank=True)
    soc_pct = models.FloatField(null=True, blank=True)
    batt_voltage_v = models.FloatField(null=True, blank=True)
    batt_power_w = models.FloatField(null=True, blank=True)
    batt_current_a = models.FloatField(null=True, blank=True)
    batt_temp_c = models.FloatField(null=True, blank=True)
    dc_temp_c = models.FloatField(null=True, blank=True)
    ac_temp_c = models.FloatField(null=True, blank=True)
    pv1_power_w = models.FloatField(null=True, blank=True)
    pv2_power_w = models.FloatField(null=True, blank=True)
    pv3_power_w = models.FloatField(null=True, blank=True)
    pv4_power_w = models.FloatField(null=True, blank=True)
    pv1_voltage_v = models.FloatField(null=True, blank=True)
    pv1_current_a = models.FloatField(null=True, blank=True)
    pv2_voltage_v = models.FloatField(null=True, blank=True)
    pv2_current_a = models.FloatField(null=True, blank=True)
    grid_power_w = models.FloatField(null=True, blank=True)
    grid_l1_power_w = models.FloatField(null=True, blank=True)
    grid_l2_power_w = models.FloatField(null=True, blank=True)
    grid_l3_power_w = models.FloatField(null=True, blank=True)
    grid_l1_voltage_v = models.FloatField(null=True, blank=True)
    grid_l2_voltage_v = models.FloatField(null=True, blank=True)
    grid_l3_voltage_v = models.FloatField(null=True, blank=True)
    grid_freq_hz = models.FloatField(null=True, blank=True)
    load_power_w = models.FloatField(null=True, blank=True)
    inverter_power_w = models.FloatField(null=True, blank=True)
    bms_soc_pct = models.FloatField(null=True, blank=True)
    pv_today_kwh = models.FloatField(null=True, blank=True)
    batt_charge_today_kwh = models.FloatField(null=True, blank=True)
    batt_discharge_today_kwh = models.FloatField(null=True, blank=True)
    grid_import_today_kwh = models.FloatField(null=True, blank=True)
    grid_export_today_kwh = models.FloatField(null=True, blank=True)
    load_today_kwh = models.FloatField(null=True, blank=True)
    batt_charge_total_kwh = models.FloatField(null=True, blank=True)
    batt_discharge_total_kwh = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "samples"
        verbose_name = "замер"
        verbose_name_plural = "замеры"


class SettingsSnapshot(models.Model):
    ts = models.BigIntegerField()
    regs = models.JSONField()
    hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        db_table = "settings_snapshots"
        verbose_name = "снимок настроек"
        verbose_name_plural = "снимки настроек"


class PollError(models.Model):
    ts = models.BigIntegerField(db_index=True)
    error = models.TextField()

    class Meta:
        db_table = "poll_errors"
        verbose_name = "ошибка опроса"
        verbose_name_plural = "ошибки опроса"


class ForecastProvider(models.Model):
    code = models.SlugField(unique=True, verbose_name="код")
    name = models.CharField(max_length=100, verbose_name="название")
    enabled = models.BooleanField(default=False, verbose_name="включён")
    api_key = models.CharField(max_length=255, blank=True, default="", verbose_name="API-ключ")
    params = models.JSONField(default=dict, blank=True, verbose_name="параметры",
                              help_text="Например, для Solcast: {\"resource_id\": \"...\"}")
    notes = models.TextField(blank=True, default="", verbose_name="заметки")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="обновлён")

    class Meta:
        db_table = "forecast_providers"
        verbose_name = "провайдер прогноза"
        verbose_name_plural = "провайдеры прогноза"

    def __str__(self):
        return self.name


class PanelGroup(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name="название")
    enabled = models.BooleanField(default=True, verbose_name="включена")
    lat = models.FloatField(verbose_name="широта")
    lon = models.FloatField(verbose_name="долгота")
    tilt = models.FloatField(verbose_name="наклон, °")
    azimuth = models.FloatField(
        verbose_name="азимут, °",
        help_text="Конвенция Open-Meteo: 0 = юг, восток < 0, запад > 0.",
    )
    kwp = models.FloatField(verbose_name="мощность, кВт пик")

    class Meta:
        db_table = "panel_groups"
        verbose_name = "группа панелей"
        verbose_name_plural = "группы панелей"

    def __str__(self):
        return self.name


class ForecastRaw(models.Model):
    provider = models.CharField(max_length=50, db_index=True)
    panel = models.CharField(max_length=100)
    fetched_at = models.BigIntegerField()
    target_ts = models.BigIntegerField()
    gti = models.FloatField(null=True, blank=True)
    ghi = models.FloatField(null=True, blank=True)
    power_w = models.FloatField(null=True, blank=True)
    p10 = models.FloatField(null=True, blank=True)
    p90 = models.FloatField(null=True, blank=True)
    temp_c = models.FloatField(null=True, blank=True)

    class Meta:
        db_table = "forecast_raw"
        indexes = [models.Index(fields=["provider", "target_ts", "fetched_at"], name="idx_forecast_raw")]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "panel", "fetched_at", "target_ts"], name="uniq_forecast_raw"
            )
        ]
        verbose_name = "сырой прогноз"
        verbose_name_plural = "сырые прогнозы"


class ForecastRun(models.Model):
    ts = models.BigIntegerField()
    provider = models.CharField(max_length=50)
    panel = models.CharField(max_length=100)
    ok = models.BooleanField()
    n = models.IntegerField()
    error = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "forecast_runs"
        verbose_name = "запуск прогноза"
        verbose_name_plural = "запуски прогноза"


MINUTES_IN_DAY = 24 * 60


def _zone_minutes(start, end):
    """Минуты суток [0, 1440), покрытые зоной; end <= start означает переход через полночь."""
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute
    if e > s:
        return list(range(s, e))
    return list(range(s, MINUTES_IN_DAY)) + list(range(0, e))


def _fmt_minute(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _ranges(minutes):
    result = []
    for m in sorted(minutes):
        if result and result[-1][1] == m:
            result[-1][1] = m + 1
        else:
            result.append([m, m + 1])
    return ", ".join(f"{_fmt_minute(a)}–{_fmt_minute(b % MINUTES_IN_DAY)}" for a, b in result)


def zone_coverage_errors(zones, directions):
    """zones: iterable из (name, start_time, end_time, direction). Возвращает список текстов ошибок."""
    zones = list(zones)
    labels = dict(directions)
    errors = []
    for direction in sorted({z[3] for z in zones}):
        counts = [0] * MINUTES_IN_DAY
        for _name, start, end, d in zones:
            if d != direction:
                continue
            for m in _zone_minutes(start, end):
                counts[m] += 1
        label = labels.get(direction, direction)
        overlap = [m for m, c in enumerate(counts) if c > 1]
        gaps = [m for m, c in enumerate(counts) if c == 0]
        if overlap:
            errors.append(f"Зоны направления «{label}» пересекаются: {_ranges(overlap)}.")
        if gaps:
            errors.append(
                f"Зоны направления «{label}» не покрывают сутки целиком, нет зоны для: {_ranges(gaps)}."
            )
    return errors


class TariffPlan(models.Model):
    name = models.CharField(max_length=100, verbose_name="название")
    valid_from = models.DateField(verbose_name="действует с")
    valid_to = models.DateField(null=True, blank=True, verbose_name="действует по")
    is_active = models.BooleanField(default=True, verbose_name="активен")
    currency = models.CharField(max_length=8, default="UAH", verbose_name="валюта")
    notes = models.TextField(blank=True, default="", verbose_name="заметки")

    class Meta:
        db_table = "tariff_plans"
        verbose_name = "тарифный план"
        verbose_name_plural = "тарифные планы"
        ordering = ["-valid_from"]

    def __str__(self):
        return self.name

    def clean(self):
        if self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError("Дата окончания раньше даты начала.")
        if not self.is_active:
            return
        others = TariffPlan.objects.filter(is_active=True).exclude(pk=self.pk)
        for other in others:
            starts_before_other_ends = other.valid_to is None or self.valid_from <= other.valid_to
            ends_after_other_starts = self.valid_to is None or self.valid_to >= other.valid_from
            if starts_before_other_ends and ends_after_other_starts:
                raise ValidationError(f"Период действия пересекается с активным планом «{other.name}».")

    def zones_summary(self):
        return " · ".join(
            f"{z.name} {z.start_time:%H:%M}–{z.end_time:%H:%M} {z.price_per_kwh:.2f}"
            for z in self.zones.all()
        )


class TariffZone(models.Model):
    IMPORT = "import"
    EXPORT = "export"
    DIRECTIONS = [(IMPORT, "покупка из сети"), (EXPORT, "продажа в сеть")]

    plan = models.ForeignKey(
        TariffPlan, on_delete=models.CASCADE, related_name="zones", verbose_name="тарифный план"
    )
    name = models.CharField(max_length=50, verbose_name="название")
    start_time = models.TimeField(verbose_name="начало")
    end_time = models.TimeField(
        verbose_name="конец", help_text="Если конец ≤ начала, зона переходит через полночь."
    )
    price_per_kwh = models.DecimalField(max_digits=8, decimal_places=4, verbose_name="цена за кВт·ч")
    direction = models.CharField(max_length=10, choices=DIRECTIONS, default=IMPORT, verbose_name="направление")

    class Meta:
        db_table = "tariff_zones"
        verbose_name = "тарифная зона"
        verbose_name_plural = "тарифные зоны"
        ordering = ["start_time"]

    def __str__(self):
        return f"{self.name} {self.start_time:%H:%M}–{self.end_time:%H:%M}"


NIGHT_SWITCH = time(6, 30)  # = app/planner_service.py MANUAL_APPLY_END (конец позднего окна записи)


def nearest_night(now=None):
    """Дата ночи (вечера, в 23:00 которого она начинается), к которой относится ручная цель, сохранённая в now:
    до 06:30 местного — текущая ночь (вчерашний вечер), иначе сегодняшний вечер. Граница 06:30, а не 07:00
    (night_date_for в app/planner_service.py): после 06:30 запись на текущую ночь уже не идёт, поэтому
    сохранённое в 06:30–07:00 относится к наступающей ночи; в 22:45 даты совпадают."""
    local = timezone.localtime(now)
    return local.date() - timedelta(days=1) if local.time() < NIGHT_SWITCH else local.date()


class StrategySettings(models.Model):
    """Синглтон: параметры стратегии заряда АКБ. Только БД, в инвертор ничего не пишется."""

    OFF = "off"
    ADVISE = "advise"
    AUTO = "auto"
    CONTROL_MODES = [
        (OFF, "Ручной — инвертор работает по TOU, заданному вручную"),
        (ADVISE, "Рекомендации — планировщик считает план на ночь, в инвертор не пишет"),
        (AUTO, "Авто — планировщик записывает план в инвертор (нужна «запись разрешена»)"),
    ]

    reserve_soc_pct = models.IntegerField(
        default=30,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name="порог разряда АКБ (устарело), %",
        help_text="Устарело: используется пол из режима (обычный / аварийный). Колонка остаётся в БД для совместимости.",
    )
    EMERGENCY_OFF = "off"
    EMERGENCY_ALWAYS = "always"
    EMERGENCY_SCHEDULE = "schedule"
    EMERGENCY_MODES = [
        (EMERGENCY_OFF, "Выключен"),
        (EMERGENCY_ALWAYS, "Всегда (отключение в любой момент)"),
        (EMERGENCY_SCHEDULE, "По графику (позже)"),
    ]
    normal_floor_pct = models.IntegerField(
        default=15,
        db_default=15,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name="пол в обычном режиме, %",
        help_text="До какого SOC разряжать АКБ, когда аварийный режим выключен. "
        "Ниже SOC low (рег. 117) инвертор переходит на сеть, поэтому по умолчанию 15 %.",
    )
    emergency_mode = models.CharField(
        max_length=10,
        choices=EMERGENCY_MODES,
        default=EMERGENCY_OFF,
        db_default=EMERGENCY_OFF,
        verbose_name="аварийный режим",
        help_text="Аварийный режим держит повышенный пол разряда на случай отключения света.",
    )
    emergency_floor_pct = models.IntegerField(
        default=50,
        db_default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name="пол в аварийном режиме, %",
        help_text="Минимальный SOC, до которого разряжать АКБ в аварийном режиме.",
    )
    max_grid_charge_soc_pct = models.IntegerField(
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name="потолок заряда, %",
        help_text="Потолок ночного заряда от сети.",
    )
    battery_capacity_kwh = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=24,
        validators=[MinValueValidator(0)],
        verbose_name="ёмкость АКБ, кВт·ч",
        help_text="Номинальная ёмкость батареи.",
    )
    battery_usable_pct = models.IntegerField(
        default=95,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        verbose_name="полезная ёмкость, %",
        help_text="Какую долю номинальной ёмкости считаем доступной.",
    )
    round_trip_efficiency_pct = models.IntegerField(
        default=90,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        verbose_name="КПД цикла, %",
        help_text="КПД заряд-разряд (round trip).",
    )
    control_mode = models.CharField(
        max_length=10,
        choices=CONTROL_MODES,
        default=OFF,
        verbose_name="режим управления",
        help_text="Ручной: TOU задаётся вручную. Рекомендации: планировщик считает план, в инвертор не пишет. "
        "Авто: планировщик пишет план в инвертор (нужна «запись разрешена»).",
    )
    writes_enabled = models.BooleanField(
        default=False,
        verbose_name="запись в инвертор разрешена",
        help_text="Запись в инвертор; включать только по решению владельца. Вместе с режимом «Авто» включает "
        "автозапись ночного target планировщика в SOC ночных слотов TOU с зарядом от сети.",
    )
    decision_time = models.TimeField(
        default=time(22, 45),
        verbose_name="время решения",
        help_text="Когда принимать решение на ночь.",
    )
    export_price_per_kwh = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name="цена экспорта, грн/кВт·ч",
        help_text="Цена продажи излишков в сеть (0 — не продаём).",
    )
    wear_cost_per_kwh = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal("0.4"),
        validators=[MinValueValidator(0)],
        verbose_name="износ АКБ, грн/кВт·ч",
        help_text="Стоимость износа АКБ на 1 кВт·ч, прошедший через батарею.",
    )
    RISK_ECONOMY = "economy"
    RISK_BALANCED = "balanced"
    RISK_SAFE = "safe"
    RISKS = [
        (RISK_ECONOMY, "экономно"),
        (RISK_BALANCED, "сбалансировано"),
        (RISK_SAFE, "надёжно"),
    ]
    risk = models.CharField(
        max_length=10,
        choices=RISKS,
        default=RISK_BALANCED,
        verbose_name="риск-профиль",
        help_text="Насколько осторожно планировать ночной заряд при неопределённом прогнозе.",
    )
    default_plan_soc_pct = models.IntegerField(
        default=60,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name="план по умолчанию, %",
        help_text="Целевой SOC, если решения на ночь нет.",
    )
    NIGHT_TARGET_AUTO = "auto"
    NIGHT_TARGET_ONCE = "once"
    NIGHT_TARGET_ALWAYS = "always"
    NIGHT_TARGET_MODES = [
        (NIGHT_TARGET_AUTO, "Авто — цель считает планировщик"),
        (NIGHT_TARGET_ONCE, "Вручную — только ближайшая ночь"),
        (NIGHT_TARGET_ALWAYS, "Вручную — всегда"),
    ]
    night_target_mode = models.CharField(
        max_length=10,
        choices=NIGHT_TARGET_MODES,
        default=NIGHT_TARGET_AUTO,
        db_default=NIGHT_TARGET_AUTO,
        verbose_name="ночная цель SOC: авто / вручную",
        help_text="Авто — ночную цель SOC выбирает планировщик. Вручную — планировщик берёт значение ниже "
        "вместо расчётного: только на ближайшую ночь или на все ночи. В режиме «Авто» управления цель сразу "
        "записывается в инвертор, в том числе ночью (до 06:30); возврат на «Авто» ночью так же сразу пересчитывает "
        "и записывает расчётную цель.",
    )
    night_target_manual_pct = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(100)],
        verbose_name="ночная цель SOC вручную, %",
        help_text="Пишется в SOC ночных слотов TOU 1, 2 и 6 (с зарядом от сети). Если АКБ вечером выше этого "
        "уровня — ночью разряжается до него; если ниже — дозаряжается от сети по ночному тарифу. "
        "Не ниже действующего пола разряда и не выше потолка заряда.",
    )
    night_target_manual_night = models.DateField(
        null=True,
        blank=True,
        verbose_name="ночь ручной цели",
        help_text="Для режима «только ближайшая ночь»: дата вечера ночи, заполняется автоматически при сохранении.",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="обновлено")

    class Meta:
        db_table = "strategy_settings"
        verbose_name = "Настройки стратегии"
        verbose_name_plural = "Настройки стратегии"

    def __str__(self):
        return "Настройки стратегии"

    def clean(self):
        if self.decision_time is not None and not time(7, 0) <= self.decision_time < time(23, 0):
            raise ValidationError("Время решения должно быть в диапазоне от 07:00 до 23:00 (не включая 23:00).")
        if self.emergency_mode == self.EMERGENCY_SCHEDULE:
            raise ValidationError({"emergency_mode": "График отключений ещё не реализован."})
        normal, emergency, ceiling = self.normal_floor_pct, self.emergency_floor_pct, self.max_grid_charge_soc_pct
        if normal is not None and ceiling is not None and normal > ceiling:
            raise ValidationError("Пол обычного режима не может быть больше потолка заряда.")
        if normal is not None and emergency is not None and emergency < normal:
            raise ValidationError("Пол аварийного режима не может быть ниже пола обычного режима.")
        if emergency is not None and ceiling is not None and self.emergency_mode != self.EMERGENCY_OFF and emergency > ceiling:
            raise ValidationError("Пол аварийного режима не может быть больше потолка заряда.")
        if normal is not None:
            snapshot = SettingsSnapshot.objects.order_by("-id").first()
            shutdown = snapshot.regs.get("115") if snapshot else None
            if isinstance(shutdown, (int, float)) and normal < shutdown:
                raise ValidationError(
                    f"Пол обычного режима ниже SOC shutdown инвертора (рег. 115 = {int(shutdown)} %)."
                )
        if self.night_target_mode in (self.NIGHT_TARGET_ONCE, self.NIGHT_TARGET_ALWAYS):
            manual = self.night_target_manual_pct
            if manual is None:
                raise ValidationError({"night_target_manual_pct": "Укажите ночную цель SOC для ручного режима."})
            floor = self.effective_floor_pct
            if floor is not None and manual < floor:
                kind = "аварийного" if self.emergency_mode != self.EMERGENCY_OFF else "обычного"
                raise ValidationError({"night_target_manual_pct": (
                    f"Ночная цель {manual} % ниже пола {kind} режима ({floor} %): АКБ не разряжается ниже пола."
                )})
            if ceiling is not None and manual > ceiling:
                raise ValidationError({"night_target_manual_pct": (
                    f"Ночная цель {manual} % выше потолка заряда ({ceiling} %)."
                )})
        if self.control_mode == self.AUTO and not self.writes_enabled:
            raise ValidationError(
                {"control_mode": "Режим «Авто» нельзя выбрать, пока не включена «запись в инвертор разрешена»."}
            )
        if not self.pk and StrategySettings.objects.exists():
            raise ValidationError("Запись настроек стратегии уже существует.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._night_target_orig = (self.night_target_mode, self.night_target_manual_pct)

    def save(self, *args, **kwargs):
        if not self.pk and StrategySettings.objects.exists():
            raise ValidationError("Запись настроек стратегии уже существует.")
        if self.night_target_mode == self.NIGHT_TARGET_ONCE:
            changed = (self.night_target_mode, self.night_target_manual_pct) != self._night_target_orig
            if changed or self.night_target_manual_night is None:
                self.night_target_manual_night = nearest_night()
            elif self.night_target_expired:
                # Сохранение других полей не должно молча оживить старую разовую цель — возвращаем авто;
                # чтобы снова задать цель на ночь, владелец выбирает «только ближайшая ночь» ещё раз.
                self.night_target_mode = self.NIGHT_TARGET_AUTO
                self.night_target_manual_night = None
        else:
            self.night_target_manual_night = None
        super().save(*args, **kwargs)
        self._night_target_orig = (self.night_target_mode, self.night_target_manual_pct)

    @property
    def night_target_expired(self):
        """Режим «только ближайшая ночь» относится к уже прошедшей ночи — планировщик считает авто."""
        return (
            self.night_target_mode == self.NIGHT_TARGET_ONCE
            and self.night_target_manual_night is not None
            and self.night_target_manual_night < nearest_night()
        )

    @property
    def effective_floor_pct(self):
        """Пол разряда, который система выставляет в инвертор: аварийный при режиме «всегда», иначе обычный."""
        if self.emergency_mode == self.EMERGENCY_OFF:
            return self.normal_floor_pct
        return self.emergency_floor_pct

    @classmethod
    def load(cls):
        return cls.objects.order_by("pk").first()


class ChargeDecision(models.Model):
    """Решение планировщика на ночь (рекомендация). Создаётся сервисом, в админке только чтение."""

    ADVICE = "advice"
    QUEUED = "queued"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"
    STATUSES = [
        (ADVICE, "рекомендация"),
        (QUEUED, "в очереди на запись"),
        (APPLIED, "применено"),
        (FAILED, "ошибка"),
        (SKIPPED, "пропущено"),
    ]

    created_at = models.DateTimeField(default=timezone.now, verbose_name="создано")
    night_date = models.DateField(verbose_name="ночь (дата начала)")
    is_final = models.BooleanField(default=True, verbose_name="финальное")
    soc_at_decision = models.FloatField(null=True, blank=True, verbose_name="SOC на момент решения, %")
    target_pct = models.FloatField(verbose_name="цель SOC, %")
    expected_cost_uah = models.FloatField(verbose_name="ожидаемая стоимость, грн")
    cost_no_charge_uah = models.FloatField(verbose_name="без заряда, грн")
    cost_to_ceiling_uah = models.FloatField(verbose_name="до потолка, грн")
    cost_to_100_uah = models.FloatField(verbose_name="до 100 %, грн")
    expected_saving_uah = models.FloatField(verbose_name="экономия, грн")
    expected_saving_no_wear_uah = models.FloatField(verbose_name="экономия без износа, грн")
    expected_grid_import_kwh = models.FloatField(verbose_name="импорт из сети, кВт·ч")
    soc_at_07 = models.FloatField(verbose_name="SOC в 07:00, %")
    min_soc_tomorrow_worst = models.FloatField(verbose_name="мин. SOC завтра (худший), %")
    explanation = models.TextField(blank=True, verbose_name="объяснение")
    inputs = models.JSONField(default=dict, verbose_name="входы")
    tou_plan = models.JSONField(default=list, verbose_name="план TOU")
    status = models.CharField(max_length=10, choices=STATUSES, default=ADVICE, verbose_name="статус")
    write_request = models.ForeignKey(
        "InverterWriteRequest", null=True, blank=True, on_delete=models.SET_NULL, related_name="decisions",
        verbose_name="заявка на запись",
    )
    applied_at = models.DateTimeField(null=True, blank=True, verbose_name="записано в инвертор")
    apply_note = models.TextField(blank=True, default="", db_default="", verbose_name="запись: примечание")
    actual_cost_uah = models.FloatField(null=True, blank=True, verbose_name="Факт: покупка из сети, грн")
    model_cost_decided_uah = models.FloatField(
        null=True, blank=True, verbose_name="Модель (с износом и остатком АКБ): выбранный target при факте, грн"
    )
    perfect_cost_uah = models.FloatField(null=True, blank=True, verbose_name="Модель (с износом и остатком АКБ): идеальный прогноз, грн")
    actual_coverage_pct = models.FloatField(null=True, blank=True, verbose_name="покрытие данными факта, %")
    regret_uah = models.FloatField(null=True, blank=True, verbose_name="Упущено против идеального прогноза, грн")
    evaluated_at = models.DateTimeField(null=True, blank=True, verbose_name="оценено")

    class Meta:
        db_table = "charge_decisions"
        verbose_name = "решение на ночь"
        verbose_name_plural = "решения на ночь"
        ordering = ["-night_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["night_date"],
                condition=models.Q(is_final=True),
                name="charge_decision_one_final_per_night",
            ),
        ]

    def __str__(self):
        return f"{self.night_date}: {self.target_pct:g} %"


class InverterWriteRequest(models.Model):
    """Запрос на запись регистров TOU. Создаёт админка, выполняет только сервис (владелец соединения с логгером)."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"
    STATUSES = [
        (PENDING, "ожидает"),
        (RUNNING, "выполняется"),
        (DONE, "записано"),
        (FAILED, "ошибка"),
        (STALE, "устарел"),
        (CANCELLED, "отменён"),
    ]

    created_at = models.DateTimeField(default=timezone.now, verbose_name="создан")
    created_by = models.CharField(max_length=150, blank=True, default="", verbose_name="автор")
    SOURCE_ADMIN = "admin"
    SOURCE_PLANNER = "planner"
    SOURCES = [(SOURCE_ADMIN, "админка"), (SOURCE_PLANNER, "планировщик")]
    source = models.CharField(
        max_length=10, choices=SOURCES, default=SOURCE_ADMIN, db_default=SOURCE_ADMIN, verbose_name="источник"
    )
    changes = models.JSONField(default=dict, verbose_name="изменения", help_text="{\"рег\": новое значение}")
    expected_old = models.JSONField(default=dict, verbose_name="ожидаемые старые", help_text="{\"рег\": значение из снимка}")
    backup = models.JSONField(null=True, blank=True, verbose_name="копия 146–177 до записи")
    readback = models.JSONField(null=True, blank=True, verbose_name="чтение 146–177 после записи")
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING, db_index=True, verbose_name="статус")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="начато")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="завершено")
    error = models.TextField(null=True, blank=True, verbose_name="ошибка")

    class Meta:
        db_table = "inverter_write_requests"
        verbose_name = "запись в инвертор"
        verbose_name_plural = "записи в инвертор"
        ordering = ["-id"]

    def __str__(self):
        return f"#{self.pk} {self.get_status_display()}"

    def changes_summary(self):
        return ", ".join(
            f"{reg}: {self.expected_old.get(reg)} → {value}" for reg, value in sorted(self.changes.items(), key=lambda kv: int(kv[0]))
        )
