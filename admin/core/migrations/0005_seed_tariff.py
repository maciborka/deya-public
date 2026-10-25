from datetime import date, time

from django.db import migrations

ZONES = [
    ("День", time(7, 0), time(23, 0), "4.32"),
    ("Ночь", time(23, 0), time(7, 0), "2.16"),
]


def seed(apps, schema_editor):
    plan_model = apps.get_model("core", "TariffPlan")
    zone_model = apps.get_model("core", "TariffZone")
    if plan_model.objects.exists():
        return
    plan = plan_model.objects.create(name="Двухзонный", valid_from=date(2026, 1, 1), is_active=True)
    for name, start, end, price in ZONES:
        zone_model.objects.create(
            plan=plan, name=name, start_time=start, end_time=end, price_per_kwh=price, direction="import"
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0004_tariffplan_tariffzone")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
