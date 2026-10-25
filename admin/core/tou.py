"""Форма редактирования 6 слотов TOU и подготовка запроса на запись. К логгеру не подключается."""

from datetime import timedelta

from django import forms
from django.utils import timezone
from unfold.widgets import UnfoldAdminIntegerFieldWidget, UnfoldAdminSelectWidget, UnfoldAdminTextInputWidget

from .inverter_modes import fmt_hhmm
from .models import InverterWriteRequest, Sample

SLOTS = 6
START_REG, POWER_REG, SOC_REG, SOURCE_REG = 148, 154, 166, 172
MAX_POWER_W = 12000
MIN_SOC, MAX_SOC = 5, 100
SOURCE_CHOICES = [(0, "нет"), (1, "сеть"), (2, "генератор"), (3, "сеть и генератор")]
GRID_SOURCES = (1, 3)
WRITE_LIMIT_24H = 10  # = app/write_guard.py WRITE_LIMIT_24H (SQL-копия guard_errors для планировщика)
WRITE_PERM = "core.add_inverterwriterequest"
SNAPSHOT_MAX_AGE_SEC = 15 * 60  # = app/write_guard.py SNAPSHOT_MAX_AGE_SEC

FIELDS = [
    ("start", "начало", START_REG),
    ("power", "мощность, Вт", POWER_REG),
    ("soc", "SOC, %", SOC_REG),
    ("source", "источник", SOURCE_REG),
]


def parse_hhmm(text):
    """'23:05' -> 2305; ValueError при неверном формате или вне 00:00–23:59."""
    parts = text.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError("время в формате ЧЧ:ММ")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("время вне диапазона 00:00–23:59")
    return h * 100 + m


def _fmt(kind, value):
    if value is None:
        return "-"
    if kind == "start":
        return fmt_hhmm(value)
    if kind == "source":
        return dict(SOURCE_CHOICES).get(int(value), f"неизвестно ({int(value)})")
    return str(int(value))


class TouSlotsForm(forms.Form):
    snapshot_id = forms.IntegerField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(required=False, label="Подтверждаю запись в инвертор")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for n in range(1, SLOTS + 1):
            self.fields[f"s{n}_start"] = forms.CharField(
                label=f"Слот {n}: начало", widget=UnfoldAdminTextInputWidget(attrs={"placeholder": "ЧЧ:ММ"})
            )
            self.fields[f"s{n}_power"] = forms.IntegerField(
                label=f"Слот {n}: мощность, Вт", min_value=0, max_value=MAX_POWER_W,
                widget=UnfoldAdminIntegerFieldWidget,
            )
            self.fields[f"s{n}_soc"] = forms.IntegerField(
                label=f"Слот {n}: SOC, %", min_value=MIN_SOC, max_value=MAX_SOC,
                widget=UnfoldAdminIntegerFieldWidget,
            )
            self.fields[f"s{n}_source"] = forms.TypedChoiceField(
                label=f"Слот {n}: источник", choices=SOURCE_CHOICES, coerce=int,
                widget=UnfoldAdminSelectWidget,
            )

    def clean(self):
        data = super().clean()
        starts = []
        for n in range(1, SLOTS + 1):
            name = f"s{n}_start"
            if name not in data:
                continue
            try:
                data[name] = parse_hhmm(data[name])
            except ValueError as e:
                self.add_error(name, str(e))
                continue
            starts.append((n, data[name]))
        if len(starts) == SLOTS:
            for (_, prev), (n, cur) in zip(starts, starts[1:]):
                if cur <= prev:
                    self.add_error(f"s{n}_start", "начало слота должно быть позже начала предыдущего")
        return data

    def rows(self):
        return [
            {"n": n, "fields": [self[f"s{n}_{kind}"] for kind, _, _ in FIELDS]}
            for n in range(1, SLOTS + 1)
        ]


def initial_from_regs(regs, snapshot_id):
    initial = {"snapshot_id": snapshot_id}
    for i in range(SLOTS):
        n = i + 1
        values = {kind: regs.get(str(base + i)) for kind, _, base in FIELDS}
        initial[f"s{n}_start"] = fmt_hhmm(values["start"]) if values["start"] is not None else ""
        initial[f"s{n}_power"] = values["power"]
        initial[f"s{n}_soc"] = values["soc"]
        initial[f"s{n}_source"] = values["source"]
    return initial


def discharge_slots(regs):
    """Номера слотов без заряда от сети (источник не «сеть»/«сеть и генератор») с их SOC из снимка."""
    out = []
    for i in range(SLOTS):
        source, soc = regs.get(str(SOURCE_REG + i)), regs.get(str(SOC_REG + i))
        if source is None or soc is None or int(source) in GRID_SOURCES:
            continue
        out.append((i + 1, int(soc)))
    return out


def floor_warning(regs, reserve):
    """Пороги разряда в инверторе (слоты без заряда от сети), если они отличаются от порога из настроек; иначе None."""
    socs = sorted({soc for _, soc in discharge_slots(regs)})
    if reserve is None or not socs or socs == [int(reserve)]:
        return None
    return {"inverter": "/".join(str(v) for v in socs), "settings": int(reserve)}


def fill_reserve(data, regs, reserve):
    """Копия данных формы: SOC слотов без заряда от сети = порог из настроек, без подтверждения записи."""
    filled = data.copy()
    for n, _soc in discharge_slots(regs):
        filled[f"s{n}_soc"] = str(int(reserve))
    filled.pop("confirm", None)
    return filled


def compute_changes(regs, cleaned):
    """Изменённые регистры: (строки для таблицы, {"рег": новое}, {"рег": старое})."""
    rows, changes, expected_old = [], {}, {}
    for i in range(SLOTS):
        n = i + 1
        for kind, label, base in FIELDS:
            reg = base + i
            old = regs.get(str(reg))
            new = int(cleaned[f"s{n}_{kind}"])
            if old is not None and int(old) == new:
                continue
            rows.append({"slot": n, "field": label, "old": _fmt(kind, old), "new": _fmt(kind, new), "reg": reg})
            changes[str(reg)] = new
            expected_old[str(reg)] = None if old is None else int(old)
    return rows, changes, expected_old


def guard_errors(snapshot, now=None):
    """Причины, по которым создавать запрос сейчас нельзя (кроме «нет изменений»).
    Те же правила для автозаписи планировщика — app/write_guard.py (держать синхронно)."""
    now = now or timezone.now()
    errors = []
    if InverterWriteRequest.objects.filter(
        status__in=[InverterWriteRequest.PENDING, InverterWriteRequest.RUNNING]
    ).exists():
        errors.append("Уже есть запрос в очереди или в работе — дождитесь его завершения.")
    if InverterWriteRequest.objects.filter(created_at__gte=now - timedelta(hours=24)).count() >= WRITE_LIMIT_24H:
        errors.append(f"Лимит: не больше {WRITE_LIMIT_24H} запросов за 24 часа.")
    # Снимки настроек дедуплицируются (пишутся только при изменении), поэтому свежесть
    # проверяем по последней телеметрии: сервис жив и опрашивает. Совпадение «было» с живыми
    # регистрами всё равно проверяет воркер перед записью (иначе status=stale).
    last = Sample.objects.order_by("-ts").values_list("ts", flat=True).first()
    if last is None or last < now.timestamp() - SNAPSHOT_MAX_AGE_SEC:
        errors.append("Сервис давно не получал данные с инвертора — запись недоступна.")
    return errors


def create_request(user, changes, expected_old):
    return InverterWriteRequest.objects.create(
        created_by=user.get_username(), changes=changes, expected_old=expected_old
    )
