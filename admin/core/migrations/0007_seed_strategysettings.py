from django.db import migrations


def seed(apps, schema_editor):
    model = apps.get_model("core", "StrategySettings")
    if not model.objects.exists():
        model.objects.create()


class Migration(migrations.Migration):
    dependencies = [("core", "0006_strategysettings")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
