import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_night_target_manual"),
    ]

    operations = [
        migrations.AddField(
            model_name="strategysettings",
            name="cloudy_full_charge_kwh",
            field=models.DecimalField(
                blank=True,
                decimal_places=1,
                default=12,
                help_text="Если прогноз солнца на завтра (p50, 07–23) ниже порога — ночью заряжаем от сети до потолка заряда. Пусто или 0 — выключено. Подобран по наблюдениям 22 и 26.10 (прогноз 5.5–5.8, факт 31–64 % от прогноза); корректировать по полю pv_p50_day_kwh в решениях.",
                max_digits=5,
                null=True,
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="порог пасмурного дня, кВт·ч",
            ),
        ),
    ]
