from django.db import migrations, models


def dedupe_forecast(apps, schema_editor):
    schema_editor.execute(
        "DELETE FROM forecast_raw a USING forecast_raw b "
        "WHERE a.provider = b.provider AND a.panel = b.panel "
        "AND a.fetched_at = b.fetched_at AND a.target_ts = b.target_ts AND a.id > b.id"
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0002_seed_providers")]

    operations = [
        migrations.RunPython(dedupe_forecast, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="forecastraw",
            constraint=models.UniqueConstraint(
                fields=("provider", "panel", "fetched_at", "target_ts"), name="uniq_forecast_raw"
            ),
        ),
        migrations.AlterField(
            model_name="panelgroup",
            name="name",
            field=models.CharField(max_length=100, unique=True, verbose_name="название"),
        ),
    ]
