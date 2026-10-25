from django.db import migrations

PROVIDERS = [
    ("open_meteo", "Open-Meteo", True),
    ("forecast_solar", "Forecast.Solar", True),
    ("solcast", "Solcast", False),
]


def seed(apps, schema_editor):
    model = apps.get_model("core", "ForecastProvider")
    for code, name, enabled in PROVIDERS:
        model.objects.get_or_create(code=code, defaults={"name": name, "enabled": enabled})


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
