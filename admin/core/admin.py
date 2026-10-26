from datetime import datetime, timezone

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone as dj_timezone
from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.models import BaseInlineFormSet
from django.contrib.auth.admin import GroupAdmin as BaseGroupAdmin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group, User
from unfold.admin import ModelAdmin, TabularInline
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from unfold.widgets import UnfoldAdminPasswordWidget

from .overview import build_overview
from .tou import (
    WRITE_PERM,
    TouSlotsForm,
    compute_changes,
    create_request,
    fill_reserve,
    floor_warning,
    guard_errors,
    initial_from_regs,
)
from .models import (
    ChargeDecision,
    ForecastProvider,
    ForecastRaw,
    ForecastRun,
    InverterWriteRequest,
    PanelGroup,
    PollError,
    Sample,
    SettingsSnapshot,
    StrategySettings,
    TariffPlan,
    TariffZone,
    zone_coverage_errors,
)


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "-"


class ForecastProviderForm(forms.ModelForm):
    class Meta:
        model = ForecastProvider
        fields = ["code", "name", "enabled", "api_key", "params", "notes"]
        widgets = {"api_key": UnfoldAdminPasswordWidget(render_value=False)}
        help_texts = {
            "api_key": "Оставьте пустым, чтобы не менять сохранённый ключ.",
            "params": "Solcast: {\"sites\": {\"PV1 схід\": \"xxxx-xxxx-xxxx-xxxx\", \"PV2 захід\": \"…\"}} — по сайту на скат (старый формат {\"resource_id\": \"…\"} тоже работает).",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        key = self.instance.api_key if self.instance.pk else ""
        if key:
            field = self.fields["api_key"]
            field.help_text = f"Ключ сохранён (…{key[-4:]}). Оставьте пустым, чтобы не менять; введите новый, чтобы заменить."
            field.widget.attrs["placeholder"] = "•••••••• ключ сохранён"

    def clean_api_key(self):
        value = self.cleaned_data.get("api_key", "")
        if not value and self.instance.pk:
            return self.instance.api_key
        return value


@admin.register(ForecastProvider)
class ForecastProviderAdmin(ModelAdmin):
    form = ForecastProviderForm
    list_display = ("code", "name", "enabled", "has_key", "updated_at")
    list_editable = ("enabled",)

    @admin.display(description="ключ задан")
    def has_key(self, obj):
        return f"да (…{obj.api_key[-4:]})" if obj.api_key else "нет"


@admin.register(PanelGroup)
class PanelGroupAdmin(ModelAdmin):
    list_display = ("name", "enabled", "lat", "lon", "tilt", "azimuth", "kwp")
    list_editable = ("enabled",)


class TariffZoneFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        zones = []
        for form in self.forms:
            data = form.cleaned_data
            if not data or data.get("DELETE"):
                continue
            zones.append((data["name"], data["start_time"], data["end_time"], data["direction"]))
        errors = zone_coverage_errors(zones, TariffZone.DIRECTIONS)
        if errors:
            raise ValidationError(errors)


class TariffZoneInline(TabularInline):
    model = TariffZone
    formset = TariffZoneFormSet
    extra = 0


@admin.register(TariffPlan)
class TariffPlanAdmin(ModelAdmin):
    list_display = ("name", "valid_from", "valid_to", "is_active", "zones_summary")
    inlines = [TariffZoneInline]

    @admin.display(description="зоны")
    def zones_summary(self, obj):
        return obj.zones_summary()


@admin.register(StrategySettings)
class StrategySettingsAdmin(ModelAdmin):
    radio_fields = {
        "control_mode": admin.VERTICAL, "emergency_mode": admin.VERTICAL, "night_target_mode": admin.VERTICAL,
    }
    readonly_fields = ("reserve_soc_pct", "night_target_manual_night", "night_target_state", "planner_auto_target")
    fieldsets = (
        (None, {"fields": ("control_mode", "writes_enabled", "decision_time")}),
        (
            "Пороги АКБ",
            {"fields": ("normal_floor_pct", "emergency_mode", "emergency_floor_pct", "max_grid_charge_soc_pct",
                        "cloudy_full_charge_kwh")},
        ),
        (
            "Ночная цель",
            {"fields": ("night_target_mode", "night_target_manual_pct", "night_target_manual_night",
                        "night_target_state", "planner_auto_target")},
        ),
        ("Батарея", {"fields": ("battery_capacity_kwh", "battery_usable_pct", "round_trip_efficiency_pct")}),
        (
            "Планировщик",
            {"fields": ("export_price_per_kwh", "wear_cost_per_kwh", "risk", "default_plan_soc_pct")},
        ),
        ("Устаревшее", {"fields": ("reserve_soc_pct",), "classes": ("collapse",)}),
    )

    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj is not None and obj.night_target_expired:
            obj._expired_night = obj.night_target_manual_night
            # Истёкшая разовая цель показывается как «авто»: повторный выбор «только ближайшая ночь» — это
            # изменение поля, и save() назначит новую ночь; сохранение без него оставит авто.
            obj.night_target_mode = obj.NIGHT_TARGET_AUTO
            obj._night_target_orig = (obj.night_target_mode, obj.night_target_manual_pct)
        return obj

    def has_add_permission(self, request):
        return not StrategySettings.objects.exists()

    @admin.display(description="действие ручной цели")
    def night_target_state(self, obj):
        expired = getattr(obj, "_expired_night", None)
        if expired is not None:
            return (f"действие разовой цели {obj.night_target_manual_pct} % истекло (ночь {expired:%d.%m.%Y} прошла) — "
                    "сейчас авто; чтобы задать снова, выберите «только ближайшая ночь»")
        if obj is None or obj.night_target_mode == StrategySettings.NIGHT_TARGET_AUTO:
            return "не действует (авто)"
        if obj.night_target_mode == StrategySettings.NIGHT_TARGET_ALWAYS:
            return f"действует на все ночи: {obj.night_target_manual_pct} %"
        night = obj.night_target_manual_night
        if night is None:
            return "ночь будет определена при сохранении"
        if obj.night_target_expired:
            return f"истекло (ночь {night:%d.%m.%Y} прошла) — планировщик считает авто"
        return f"действует на ночь {night:%d.%m.%Y}: {obj.night_target_manual_pct} %"

    @admin.display(description="последняя расчётная цель планировщика")
    def planner_auto_target(self, obj):
        dec = ChargeDecision.objects.filter(is_final=True).order_by("-night_date").first()
        if dec is None:
            return "решений ещё нет"
        inputs = dec.inputs or {}
        manual = bool(inputs.get("manual"))
        target = inputs.get("auto_target_pct") if manual else dec.target_pct
        prob = inputs.get("auto_day_import_prob") if manual else inputs.get("day_import_prob")
        if target is None:
            return "—"
        text = f"{float(target):g} % (ночь {dec.night_date:%d.%m.%Y})"
        if prob is not None:
            text += f", вероятность дневной докупки {float(prob) * 100:.0f} %"
        if manual:
            text += f"; в решении ручная цель {dec.target_pct:g} %"
            if inputs.get("day_import_prob") is not None:
                text += f" (вероятность {float(inputs['day_import_prob']) * 100:.0f} %)"
        return text

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        custom = [
            path("mode/", self.admin_site.admin_view(self.mode_view), name="core_inverter_mode"),
        ]
        return custom + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        obj = StrategySettings.load()
        if obj is None:
            return HttpResponseRedirect(reverse("admin:core_strategysettings_add"))
        return HttpResponseRedirect(reverse("admin:core_strategysettings_change", args=[obj.pk]))

    def mode_view(self, request):
        snapshot = SettingsSnapshot.objects.order_by("-id").first()
        form = None
        preview = None
        errors = []
        strategy = StrategySettings.load()
        reserve = strategy.effective_floor_pct if strategy else None
        warning = floor_warning(snapshot.regs, reserve) if snapshot is not None else None
        if snapshot is not None:
            if request.method == "POST":
                if not request.user.has_perm(WRITE_PERM):
                    raise PermissionDenied
                data = request.POST
                if warning and request.POST.get("action") == "fill_reserve":
                    data = fill_reserve(request.POST, snapshot.regs, reserve)
                form = TouSlotsForm(data)
                if form.is_valid():
                    preview, errors = self._check_tou(request, form, snapshot)
                    if not errors and request.POST.get("action") == "write":
                        if not form.cleaned_data["confirm"]:
                            errors.append("Отметьте «Подтверждаю запись в инвертор».")
                        else:
                            _rows, changes, expected_old = compute_changes(snapshot.regs, form.cleaned_data)
                            req = create_request(request.user, changes, expected_old)
                            messages.success(
                                request, f"Запрос № {req.pk} поставлен в очередь: сервис запишет {len(changes)} рег. при ближайшем опросе."
                            )
                            return HttpResponseRedirect(reverse("admin:core_inverter_mode"))
            else:
                form = TouSlotsForm(initial=initial_from_regs(snapshot.regs, snapshot.pk))
        context = {
            **self.admin_site.each_context(request),
            "title": "Режим инвертора",
            "overview": build_overview(),
            "form": form,
            "preview": preview,
            "errors": errors,
            "floor_warning": warning,
            "requests": InverterWriteRequest.objects.order_by("-id")[:5],
            "can_write": request.user.has_perm(WRITE_PERM),
        }
        return TemplateResponse(request, "admin/inverter_mode.html", context)

    def _check_tou(self, request, form, snapshot):
        if form.cleaned_data["snapshot_id"] != snapshot.pk:
            return None, ["Снимок настроек обновился — нажмите «Проверить» ещё раз."]
        rows, changes, _expected_old = compute_changes(snapshot.regs, form.cleaned_data)
        if not changes:
            return None, ["Нет изменений."]
        return rows, guard_errors(snapshot)


class ReadOnlyAdmin(ModelAdmin):
    list_per_page = 100

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(InverterWriteRequest)
class InverterWriteRequestAdmin(ReadOnlyAdmin):
    list_display = ("id", "created_at", "created_by", "source", "status", "changes_summary", "started_at", "finished_at", "error")
    list_filter = ("status",)
    ordering = ("-id",)
    actions = ["cancel_pending"]

    @admin.display(description="изменения (рег: было → станет)")
    def changes_summary(self, obj):
        return obj.changes_summary()

    @admin.action(description="Отменить (только ожидающие)")
    def cancel_pending(self, request, queryset):
        if not request.user.has_perm(WRITE_PERM):
            raise PermissionDenied
        n = queryset.filter(status=InverterWriteRequest.PENDING).update(
            status=InverterWriteRequest.CANCELLED, finished_at=dj_timezone.now()
        )
        messages.info(request, f"Отменено запросов: {n}.")


@admin.register(Sample)
class SampleAdmin(ReadOnlyAdmin):
    list_display = ("when", "state", "soc_pct", "batt_power_w", "pv1_power_w", "pv2_power_w", "grid_power_w", "load_power_w")
    ordering = ("-ts",)
    show_full_result_count = False

    @admin.display(description="время", ordering="ts")
    def when(self, obj):
        return _fmt_ts(obj.ts)


@admin.register(ChargeDecision)
class ChargeDecisionAdmin(ReadOnlyAdmin):
    list_display = (
        "night_date", "is_final", "status", "write_request", "applied_at", "soc_at_decision", "target_pct", "expected_cost_uah",
        "expected_saving_uah", "actual_cost_uah", "actual_coverage_pct", "model_cost_decided_uah", "perfect_cost_uah", "regret_uah",
    )
    list_filter = ("status", "is_final", "night_date")
    ordering = ("-night_date", "-created_at")
    date_hierarchy = "night_date"


@admin.register(SettingsSnapshot)
class SettingsSnapshotAdmin(ReadOnlyAdmin):
    list_display = ("id", "when", "hash")
    ordering = ("-ts",)

    @admin.display(description="время", ordering="ts")
    def when(self, obj):
        return _fmt_ts(obj.ts)


@admin.register(PollError)
class PollErrorAdmin(ReadOnlyAdmin):
    list_display = ("when", "error")
    ordering = ("-ts",)

    @admin.display(description="время", ordering="ts")
    def when(self, obj):
        return _fmt_ts(obj.ts)


@admin.register(ForecastRaw)
class ForecastRawAdmin(ReadOnlyAdmin):
    list_display = ("provider", "panel", "fetched", "target", "power_w", "gti", "temp_c")
    ordering = ("-fetched_at", "-target_ts")
    show_full_result_count = False

    @admin.display(description="получен", ordering="fetched_at")
    def fetched(self, obj):
        return _fmt_ts(obj.fetched_at)

    @admin.display(description="на час", ordering="target_ts")
    def target(self, obj):
        return _fmt_ts(obj.target_ts)


@admin.register(ForecastRun)
class ForecastRunAdmin(ReadOnlyAdmin):
    list_display = ("when", "provider", "panel", "ok", "n", "error")
    ordering = ("-ts",)

    @admin.display(description="время", ordering="ts")
    def when(self, obj):
        return _fmt_ts(obj.ts)


admin.site.unregister(User)
admin.site.unregister(Group)


@admin.register(User)
class UserAdmin(BaseUserAdmin, ModelAdmin):
    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(Group)
class GroupAdmin(BaseGroupAdmin, ModelAdmin):
    pass
