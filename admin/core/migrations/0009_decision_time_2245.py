from datetime import time

from django.db import migrations


def to_2245(apps, schema_editor):
    model = apps.get_model("core", "StrategySettings")
    model.objects.filter(decision_time=time(21, 0)).update(decision_time=time(22, 45))


def to_2100(apps, schema_editor):
    model = apps.get_model("core", "StrategySettings")
    model.objects.filter(decision_time=time(22, 45)).update(decision_time=time(21, 0))


class Migration(migrations.Migration):
    dependencies = [("core", "0008_strategy_planner_chargedecision")]

    operations = [migrations.RunPython(to_2245, to_2100)]
