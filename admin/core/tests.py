import time as time_mod
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import ForecastProviderForm
from .tou import WRITE_LIMIT_24H, TouSlotsForm, initial_from_regs
from .inverter_modes import decode_mode, decode_tou_slots, slot_at
from .models import (
    Sample,
    ChargeDecision,
    ForecastProvider,
    InverterWriteRequest,
    SettingsSnapshot,
    StrategySettings,
    TariffPlan,
    TariffZone,
    zone_coverage_errors,
)


class ProviderKeyTests(TestCase):
    def test_empty_key_keeps_existing(self):
        p = ForecastProvider.objects.create(code="test_prov", name="Test", api_key="secret-1")
        form = ForecastProviderForm(
            {"code": "test_prov", "name": "Test2", "enabled": "on", "api_key": "", "params": "{}", "notes": ""},
            instance=p,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        p.refresh_from_db()
        self.assertEqual(p.api_key, "secret-1")
        self.assertEqual(p.name, "Test2")

    def test_new_key_replaces(self):
        p = ForecastProvider.objects.create(code="test_prov", name="Test", api_key="secret-1")
        form = ForecastProviderForm(
            {"code": "test_prov", "name": "Test", "api_key": "secret-2", "params": "{}", "notes": ""},
            instance=p,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        p.refresh_from_db()
        self.assertEqual(p.api_key, "secret-2")


class TariffTests(TestCase):
    def _errors(self, zones):
        return zone_coverage_errors(
            [(n, time(*s), time(*e), d) for n, s, e, d in zones], TariffZone.DIRECTIONS
        )

    def test_seed_migration(self):
        plan = TariffPlan.objects.get(name="Двухзонный")
        self.assertTrue(plan.is_active)
        self.assertEqual(plan.zones.count(), 2)
        self.assertEqual(plan.zones_summary(), "День 07:00–23:00 4.32 · Ночь 23:00–07:00 2.16")

    def test_midnight_wrap_ok(self):
        self.assertEqual(self._errors([("Д", (7, 0), (23, 0), "import"), ("Н", (23, 0), (7, 0), "import")]), [])

    def test_overlap(self):
        errors = self._errors([("Д", (7, 0), (23, 0), "import"), ("Н", (22, 0), (7, 0), "import")])
        self.assertTrue(any("пересекаются" in e for e in errors))

    def test_gap(self):
        errors = self._errors([("Д", (7, 0), (22, 0), "import"), ("Н", (23, 0), (7, 0), "import")])
        self.assertTrue(any("не покрывают" in e and "22:00–23:00" in e for e in errors))

    def test_directions_independent(self):
        zones = [
            ("Д", (7, 0), (23, 0), "import"),
            ("Н", (23, 0), (7, 0), "import"),
            ("Э", (0, 0), (0, 0), "export"),
        ]
        self.assertEqual(self._errors(zones), [])

    def test_active_plans_overlap(self):
        plan = TariffPlan(name="X", valid_from=date(2026, 6, 1), is_active=True)
        with self.assertRaises(ValidationError):
            plan.clean()
        plan.is_active = False
        plan.clean()


REGS = {
    "141": 0, "142": 1, "130": 1, "128": 100, "108": 150, "109": 150,
    "115": 5, "116": 50, "117": 10, "143": 12000, "145": 1, "146": 255,
    "148": 0, "149": 655, "150": 700, "151": 1700, "152": 1800, "153": 2305,
    "154": 12000, "155": 12000, "156": 12000, "157": 12000, "158": 12000, "159": 12000,
    "160": 4900, "161": 4900, "162": 4900, "163": 4900, "164": 4900, "165": 4900,
    "166": 50, "167": 50, "168": 80, "169": 50, "170": 50, "171": 50,
    "172": 1, "173": 1, "174": 0, "175": 0, "176": 0, "177": 1,
}


class DecoderTests(TestCase):
    def test_modes(self):
        m = decode_mode(REGS)
        self.assertEqual(m["energy_pattern"], "Battery First")
        self.assertEqual(m["work_mode"], "Zero Export To Load")
        self.assertTrue(m["grid_charge"]["enabled"])
        self.assertEqual(m["grid_charge"]["current_a"], 100)

    def test_tou_all_week(self):
        tou = decode_mode(REGS)["tou"]
        self.assertTrue(tou["enabled"])
        self.assertEqual(len(tou["days"]), 7)
        self.assertEqual(tou["days_label"], "все дни")

    def test_tou_partial_days(self):
        tou = decode_mode({"146": 0b00000111})["tou"]
        self.assertEqual(tou["days"], ["Пн", "Вт"])
        self.assertTrue(tou["enabled"])

    def test_slots_and_wrap(self):
        slots = decode_tou_slots(REGS)
        self.assertEqual(len(slots), 6)
        self.assertEqual((slots[1]["start"], slots[1]["end"]), ("06:55", "07:00"))
        self.assertEqual((slots[5]["start"], slots[5]["end"]), ("23:05", "00:00"))
        self.assertEqual(slots[0]["source_label"], "сеть")
        self.assertEqual(slots[2]["source_label"], "нет")
        self.assertEqual(slots[2]["soc_pct"], 80)
        self.assertEqual(slots[0]["voltage_v"], 49.0)

    def test_slot_at(self):
        slots = decode_tou_slots(REGS)
        self.assertEqual(slot_at(slots, 2330)["n"], 6)
        self.assertEqual(slot_at(slots, 1000)["n"], 3)
        self.assertEqual(slot_at(slots, 30)["n"], 1)


class StrategySettingsTests(TestCase):
    def test_seeded_single(self):
        self.assertEqual(StrategySettings.objects.count(), 1)
        s = StrategySettings.load()
        self.assertEqual((s.reserve_soc_pct, s.control_mode, s.writes_enabled), (30, "off", False))

    def test_migration_sets_emergency_defaults(self):
        s = StrategySettings.load()
        self.assertEqual(
            (s.emergency_mode, s.emergency_floor_pct, s.normal_floor_pct, s.max_grid_charge_soc_pct),
            ("always", 50, 15, 100),
        )
        self.assertEqual(s.effective_floor_pct, 50)
        s.emergency_mode = "off"
        self.assertEqual(s.effective_floor_pct, 15)
        s.full_clean()

    def test_schedule_mode_forbidden(self):
        s = StrategySettings.load()
        s.emergency_mode = "schedule"
        with self.assertRaises(ValidationError) as cm:
            s.full_clean()
        self.assertIn("emergency_mode", cm.exception.message_dict)

    def test_emergency_floor_not_below_normal(self):
        s = StrategySettings.load()
        s.normal_floor_pct, s.emergency_floor_pct = 40, 30
        with self.assertRaises(ValidationError):
            s.full_clean()
        s.emergency_floor_pct = 40
        s.full_clean()

    def test_emergency_floor_not_above_ceiling(self):
        s = StrategySettings.load()
        s.max_grid_charge_soc_pct = 40
        with self.assertRaises(ValidationError):
            s.full_clean()
        s.emergency_mode = "off"
        s.full_clean()

    def test_admin_change_page_has_threshold_group(self):
        User.objects.create_superuser("root2", "r2@example.com", "pw-for-test-1")
        self.client.login(username="root2", password="pw-for-test-1")
        resp = self.client.get(reverse("admin:core_strategysettings_change", args=[StrategySettings.load().pk]))
        self.assertContains(resp, "Пороги АКБ")
        self.assertContains(resp, 'name="emergency_mode"', count=3)  # radio: 3 варианта

    def test_second_record_forbidden(self):
        with self.assertRaises(ValidationError):
            StrategySettings.objects.create()
        self.assertEqual(StrategySettings.objects.count(), 1)

    def test_normal_floor_not_above_max(self):
        s = StrategySettings.load()
        s.normal_floor_pct, s.emergency_floor_pct, s.max_grid_charge_soc_pct = 90, 95, 80
        with self.assertRaises(ValidationError):
            s.full_clean()

    def test_decision_time_range(self):
        s = StrategySettings.load()
        for bad in (time(6, 59), time(23, 0), time(0, 0)):
            s.decision_time = bad
            with self.assertRaises(ValidationError):
                s.full_clean()
        for ok in (time(7, 0), time(22, 45)):
            s.decision_time = ok
            s.full_clean()

    def test_normal_floor_not_below_shutdown(self):
        s = StrategySettings.load()
        s.normal_floor_pct = 3
        s.full_clean()
        SettingsSnapshot.objects.create(ts=1, regs=REGS, hash="h")
        with self.assertRaises(ValidationError):
            s.full_clean()
        s.normal_floor_pct = 5
        s.full_clean()


class ModePageTests(TestCase):
    def test_pages_ok_for_superuser(self):
        SettingsSnapshot.objects.create(ts=1792000000, regs=REGS, hash="h")
        User.objects.create_superuser("root", "r@example.com", "pw-for-test-1")
        self.client.login(username="root", password="pw-for-test-1")
        resp = self.client.get(reverse("admin:core_inverter_mode"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Battery First")
        self.assertContains(resp, "23:05–00:00")
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)
        resp = self.client.get(reverse("admin:core_strategysettings_changelist"))
        self.assertEqual(resp.status_code, 302)


class PlannerSettingsTests(TestCase):
    def test_new_fields_and_decision_time(self):
        s = StrategySettings.load()
        self.assertEqual(s.decision_time, time(22, 45))
        self.assertEqual((s.export_price_per_kwh, s.wear_cost_per_kwh), (Decimal("0"), Decimal("0.4")))
        self.assertEqual((s.risk, s.default_plan_soc_pct), ("safe", 60))  # 0016: строгий режим владельца
        s.full_clean()


def make_decision(night_date, **extra):
    values = dict(
        night_date=night_date, target_pct=60, expected_cost_uah=100, cost_no_charge_uah=120,
        cost_to_ceiling_uah=105, cost_to_100_uah=130, expected_saving_uah=20, expected_saving_no_wear_uah=25,
        expected_grid_import_kwh=10, soc_at_07=50, min_soc_tomorrow_worst=30,
    )
    values.update(extra)
    return ChargeDecision.objects.create(**values)


class ChargeDecisionTests(TestCase):
    def test_one_final_per_night(self):
        make_decision(date(2026, 6, 15))
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_decision(date(2026, 6, 15))
        make_decision(date(2026, 6, 15), is_final=False)
        make_decision(date(2026, 6, 16))
        self.assertEqual(ChargeDecision.objects.count(), 3)

    def test_admin_read_only(self):
        d = make_decision(date(2026, 6, 15), actual_cost_uah=110, regret_uah=5)
        User.objects.create_superuser("root", "r@example.com", "pw-for-test-1")
        self.client.login(username="root", password="pw-for-test-1")
        self.assertEqual(self.client.get(reverse("admin:core_chargedecision_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:core_chargedecision_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:core_chargedecision_delete", args=[d.pk])).status_code, 403)
        resp = self.client.post(reverse("admin:core_chargedecision_change", args=[d.pk]), {"target_pct": "99"})
        self.assertEqual(resp.status_code, 403)
        d.refresh_from_db()
        self.assertEqual(d.target_pct, 60)


class TouFormTests(TestCase):
    def data(self, **overrides):
        data = {k: v for k, v in initial_from_regs(REGS, 1).items()}
        data.update(overrides)
        return {k: "" if v is None else str(v) for k, v in data.items()}

    def test_initial_valid(self):
        form = TouSlotsForm(self.data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["s6_start"], 2305)
        self.assertEqual(form.cleaned_data["s1_source"], 1)

    def test_invalid_values(self):
        cases = {
            "s2_start": "06:60", "s3_start": "24:00", "s4_start": "abc",
            "s1_power": "12001", "s2_soc": "4", "s3_soc": "101", "s4_source": "4",
        }
        form = TouSlotsForm(self.data(**cases))
        self.assertFalse(form.is_valid())
        for name in cases:
            self.assertIn(name, form.errors)

    def test_any_minute_and_all_sources(self):
        form = TouSlotsForm(self.data(s2_start="06:57", s6_start="23:59", s3_source="2", s4_source="3"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data["s2_start"], form.cleaned_data["s6_start"]), (657, 2359))

    def test_times_strictly_increase(self):
        form = TouSlotsForm(self.data(s3_start="06:55"))
        self.assertFalse(form.is_valid())
        self.assertIn("s3_start", form.errors)


class TouWriteTests(TestCase):
    def setUp(self):
        self.snapshot = SettingsSnapshot.objects.create(ts=1792000000, regs=REGS, hash="h")
        Sample.objects.create(ts=int(time_mod.time()))
        User.objects.create_superuser("root", "r@example.com", "pw-for-test-1")
        self.client.login(username="root", password="pw-for-test-1")
        self.url = reverse("admin:core_inverter_mode")

    def post(self, action, confirm=False, **overrides):
        data = {k: "" if v is None else str(v) for k, v in initial_from_regs(REGS, self.snapshot.pk).items()}
        data.update(overrides)
        data["action"] = action
        if confirm:
            data["confirm"] = "on"
        return self.client.post(self.url, data)

    def test_get_shows_form_with_initial(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="23:05"')
        self.assertContains(resp, "Проверить")

    def test_check_shows_diff_without_creating(self):
        resp = self.post("check", s6_start="23:00", s6_soc="90")
        self.assertContains(resp, "23:05 → <b>23:00</b>")
        self.assertContains(resp, "Подтверждаю запись в инвертор")
        self.assertEqual(InverterWriteRequest.objects.count(), 0)

    def test_write_requires_confirm(self):
        resp = self.post("write", s6_soc="90")
        self.assertContains(resp, "Отметьте")
        self.assertEqual(InverterWriteRequest.objects.count(), 0)

    def test_write_creates_pending_with_changed_only(self):
        resp = self.post("write", confirm=True, s6_start="23:00", s6_soc="90")
        self.assertEqual(resp.status_code, 302)
        req = InverterWriteRequest.objects.get()
        self.assertEqual(req.status, InverterWriteRequest.PENDING)
        self.assertEqual(req.created_by, "root")
        self.assertEqual(req.changes, {"153": 2300, "171": 90})
        self.assertEqual(req.expected_old, {"153": 2305, "171": 50})

    def test_no_changes(self):
        resp = self.post("write", confirm=True)
        self.assertContains(resp, "Нет изменений")
        self.assertEqual(InverterWriteRequest.objects.count(), 0)

    def test_refuse_when_pending_exists(self):
        InverterWriteRequest.objects.create(changes={"171": 60}, expected_old={"171": 50})
        resp = self.post("write", confirm=True, s6_soc="90")
        self.assertContains(resp, "Уже есть запрос")
        self.assertEqual(InverterWriteRequest.objects.count(), 1)

    def test_refuse_over_daily_limit(self):
        for _ in range(WRITE_LIMIT_24H):
            InverterWriteRequest.objects.create(status=InverterWriteRequest.DONE)
        old = InverterWriteRequest.objects.first()
        old.created_at = timezone.now() - timedelta(hours=25)
        old.save()
        resp = self.post("write", confirm=True, s6_soc="90")
        self.assertEqual(resp.status_code, 302)
        InverterWriteRequest.objects.create(status=InverterWriteRequest.DONE)
        InverterWriteRequest.objects.filter(status=InverterWriteRequest.PENDING).update(status=InverterWriteRequest.DONE)
        resp = self.post("write", confirm=True, s6_soc="95")
        self.assertContains(resp, "Лимит")

    def test_refuse_when_no_fresh_telemetry(self):
        Sample.objects.all().update(ts=int(time_mod.time()) - 16 * 60)
        resp = self.post("write", confirm=True, s6_soc="90")
        self.assertContains(resp, "давно не получал данные")
        self.assertEqual(InverterWriteRequest.objects.count(), 0)

    def test_refuse_when_snapshot_changed(self):
        SettingsSnapshot.objects.create(ts=int(time_mod.time()), regs=REGS, hash="h2")
        resp = self.post("write", confirm=True, s6_soc="90")
        self.assertContains(resp, "Снимок настроек обновился")
        self.assertEqual(InverterWriteRequest.objects.count(), 0)

    def test_request_admin_read_only_and_cancel(self):
        pending = InverterWriteRequest.objects.create(changes={"171": 60}, expected_old={"171": 50})
        done = InverterWriteRequest.objects.create(status=InverterWriteRequest.DONE)
        self.assertEqual(self.client.get(reverse("admin:core_inverterwriterequest_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:core_inverterwriterequest_add")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("admin:core_inverterwriterequest_delete", args=[pending.pk])).status_code, 403
        )
        self.client.post(
            reverse("admin:core_inverterwriterequest_changelist"),
            {"action": "cancel_pending", "_selected_action": [pending.pk, done.pk], "index": 0},
        )
        pending.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(pending.status, InverterWriteRequest.CANCELLED)
        self.assertIsNotNone(pending.finished_at)
        self.assertEqual(done.status, InverterWriteRequest.DONE)

    def test_staff_without_perm_forbidden(self):
        from django.contrib.auth.models import Permission

        staff = User.objects.create_user("staff", "s@example.com", "pw-for-test-2", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_inverterwriterequest"))
        pending = InverterWriteRequest.objects.create(changes={"171": 60}, expected_old={"171": 50})
        self.client.login(username="staff", password="pw-for-test-2")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'value="check"')
        self.assertEqual(self.post("check", s6_soc="90").status_code, 403)
        self.assertEqual(self.post("write", confirm=True, s6_soc="90").status_code, 403)
        resp = self.client.post(
            reverse("admin:core_inverterwriterequest_changelist"),
            {"action": "cancel_pending", "_selected_action": [pending.pk], "index": 0},
        )
        self.assertEqual(resp.status_code, 403)
        pending.refresh_from_db()
        self.assertEqual(pending.status, InverterWriteRequest.PENDING)
        self.assertEqual(InverterWriteRequest.objects.count(), 1)


class FloorWarningTests(TestCase):
    def setUp(self):
        self.snapshot = SettingsSnapshot.objects.create(ts=1792000000, regs=REGS, hash="h")
        Sample.objects.create(ts=int(time_mod.time()))
        User.objects.create_superuser("root", "r@example.com", "pw-for-test-1")
        self.client.login(username="root", password="pw-for-test-1")
        self.url = reverse("admin:core_inverter_mode")
        StrategySettings.objects.update(emergency_mode="off", normal_floor_pct=30)  # пол 30 %

    def post(self, action, confirm=False):
        data = {k: "" if v is None else str(v) for k, v in initial_from_regs(REGS, self.snapshot.pk).items()}
        data["action"] = action
        if confirm:
            data["confirm"] = "on"
        return self.client.post(self.url, data)

    def test_warning_shown_on_mismatch(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, "В инверторе порог 50/80 %, в настройках 30 %")
        self.assertContains(resp, "Подставить порог из настроек")

    def test_warning_uses_emergency_floor(self):
        StrategySettings.objects.update(emergency_mode="always", emergency_floor_pct=60)
        self.assertContains(self.client.get(self.url), "В инверторе порог 50/80 %, в настройках 60 %")

    def test_no_warning_when_equal(self):
        s = StrategySettings.load()
        s.emergency_mode, s.emergency_floor_pct = "always", 50
        s.save()
        regs = dict(REGS, **{"168": 50})
        SettingsSnapshot.objects.create(ts=1792000100, regs=regs, hash="h2")
        resp = self.client.get(self.url)
        self.assertNotContains(resp, "В инверторе порог")
        self.assertNotContains(resp, "Подставить порог из настроек")

    def test_fill_reserve_fills_form_without_request(self):
        resp = self.post("fill_reserve", confirm=True)
        self.assertEqual(resp.status_code, 200)
        form = resp.context["form"]
        self.assertEqual([form.cleaned_data[f"s{n}_soc"] for n in range(1, 7)], [50, 50, 30, 30, 30, 50])
        self.assertContains(resp, "80 → <b>30</b>")
        self.assertContains(resp, "50 → <b>30</b>")
        self.assertContains(resp, "Подтверждаю запись в инвертор")
        self.assertNotContains(resp, 'name="confirm" checked')
        self.assertEqual(InverterWriteRequest.objects.count(), 0)

    def test_fill_then_write_as_usual(self):
        data = {k: "" if v is None else str(v) for k, v in initial_from_regs(REGS, self.snapshot.pk).items()}
        data.update({"s3_soc": "30", "s4_soc": "30", "s5_soc": "30", "action": "write", "confirm": "on"})
        resp = self.client.post(self.url, data)
        self.assertEqual(resp.status_code, 302)
        req = InverterWriteRequest.objects.get()
        self.assertEqual(req.changes, {"168": 30, "169": 30, "170": 30})


class ControlModeCleanTests(TestCase):
    def _settings(self, mode, writes):
        s = StrategySettings.load() or StrategySettings.objects.create()
        s.control_mode = mode
        s.writes_enabled = writes
        return s

    def test_auto_requires_writes_enabled(self):
        with self.assertRaises(ValidationError) as cm:
            self._settings(StrategySettings.AUTO, False).clean()
        self.assertIn("control_mode", cm.exception.message_dict)

    def test_auto_with_writes_ok(self):
        self._settings(StrategySettings.AUTO, True).clean()

    def test_advise_ok(self):
        self._settings(StrategySettings.ADVISE, False).clean()


class NightTargetTests(TestCase):
    KYIV = ZoneInfo("Europe/Kyiv")

    def _at(self, *args):
        return patch("django.utils.timezone.now", return_value=datetime(*args, tzinfo=self.KYIV))

    def _settings(self, mode, pct, **extra):
        s = StrategySettings.load()
        s.night_target_mode, s.night_target_manual_pct = mode, pct
        for k, v in extra.items():
            setattr(s, k, v)
        return s

    def test_defaults_auto(self):
        s = StrategySettings.load()
        self.assertEqual((s.night_target_mode, s.night_target_manual_pct, s.night_target_manual_night), ("auto", None, None))
        s.night_target_manual_pct = 3  # в авто значение не используется, но валидатор 5..100 действует
        with self.assertRaises(ValidationError):
            s.full_clean()
        s.night_target_manual_pct = 10
        s.full_clean()

    def test_manual_requires_value(self):
        for mode in ("once", "always"):
            with self.assertRaises(ValidationError) as cm:
                self._settings(mode, None).full_clean()
            self.assertIn("night_target_manual_pct", cm.exception.message_dict)

    def test_manual_not_below_effective_floor(self):
        with self.assertRaises(ValidationError) as cm:
            self._settings("always", 40).full_clean()  # аварийный «всегда», пол 50
        self.assertIn("ниже пола аварийного режима (50 %)", cm.exception.message_dict["night_target_manual_pct"][0])
        self._settings("always", 50).full_clean()
        with self.assertRaises(ValidationError) as cm:
            self._settings("once", 10, emergency_mode="off").full_clean()  # обычный пол 15
        self.assertIn("обычного", cm.exception.message_dict["night_target_manual_pct"][0])
        self._settings("once", 15, emergency_mode="off").full_clean()

    def test_manual_not_above_ceiling(self):
        with self.assertRaises(ValidationError) as cm:
            self._settings("once", 90, max_grid_charge_soc_pct=80, emergency_mode="off").full_clean()
        self.assertIn("выше потолка заряда (80 %)", cm.exception.message_dict["night_target_manual_pct"][0])
        with self.assertRaises(ValidationError):
            self._settings("always", 101).full_clean()

    def test_once_night_is_nearest_night(self):
        with self._at(2026, 10, 24, 3, 0):  # до 07:00 — текущая ночь (вчерашний вечер)
            s = self._settings("once", 70)
            s.save()
        self.assertEqual(StrategySettings.load().night_target_manual_night, date(2026, 10, 23))
        with self._at(2026, 10, 24, 6, 29):
            s = self._settings("once", 72)
            s.save()
        self.assertEqual(StrategySettings.load().night_target_manual_night, date(2026, 10, 23))
        with self._at(2026, 10, 24, 6, 45):  # после 06:30 запись на текущую ночь не идёт — наступающая ночь
            s = self._settings("once", 75)
            s.save()
        self.assertEqual(StrategySettings.load().night_target_manual_night, date(2026, 10, 24))
        with self._at(2026, 10, 24, 23, 50):
            s = self._settings("always", 75)
            s.save()
        self.assertIsNone(StrategySettings.load().night_target_manual_night)

    def test_once_kept_while_active_and_reset_when_expired(self):
        with self._at(2026, 10, 24, 8, 0):
            self._settings("once", 70).save()
        with self._at(2026, 10, 25, 3, 0):  # ночь 24.10 ещё идёт — повторное сохранение ночь не сдвигает
            s = StrategySettings.load()
            self.assertFalse(s.night_target_expired)
            s.risk = "balanced"
            s.save()
            self.assertEqual(StrategySettings.load().night_target_manual_night, date(2026, 10, 24))
        with self._at(2026, 10, 26, 8, 0):
            s = StrategySettings.load()
            self.assertTrue(s.night_target_expired)
            s.save()  # сохранение без повторного выбора режима — истёкшая разовая цель уходит в авто
            s = StrategySettings.load()
            self.assertEqual(s.night_target_mode, "auto")
            self.assertIsNone(s.night_target_manual_night)
            s.night_target_mode = "once"  # повторный выбор — цель на ближайшую ночь
            s.save()
            s = StrategySettings.load()
            self.assertEqual(s.night_target_manual_night, date(2026, 10, 26))
            self.assertFalse(s.night_target_expired)

    def test_admin_shows_night_target_and_planner_target(self):
        make_decision(date(2026, 10, 23), target_pct=55, inputs={"day_import_prob": 0.04})
        with self._at(2026, 10, 20, 8, 0):
            self._settings("once", 70).save()
        User.objects.create_superuser("root3", "r3@example.com", "pw-for-test-1")
        self.client.login(username="root3", password="pw-for-test-1")
        resp = self.client.get(reverse("admin:core_strategysettings_change", args=[StrategySettings.load().pk]))
        self.assertContains(resp, "Ночная цель")
        self.assertContains(resp, 'name="night_target_mode"', count=3)
        self.assertContains(resp, "55 % (ночь 23.10.2026), вероятность дневной докупки 4 %")
        self.assertContains(resp, "истекло")
