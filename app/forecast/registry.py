"""Реестр провайдеров прогноза.

Добавить новый источник: создать файл app/forecast/<имя>.py с классом-наследником
BaseProvider (code, requires_key, enabled(), fetch(panel, days) -> list[ForecastSample])
и добавить одну строку в PROVIDERS ниже, затем завести запись в админке
(таблица forecast_providers). Включение/выключение, ключ и параметры — в админке;
провайдер с requires_key без ключа (enabled() == False) пропускается.
"""

from .base import BaseProvider
from .forecast_solar import ForecastSolarProvider
from .open_meteo import OpenMeteoProvider
from .solcast import SolcastProvider

PROVIDERS: dict[str, type[BaseProvider]] = {
    OpenMeteoProvider.code: OpenMeteoProvider,
    ForecastSolarProvider.code: ForecastSolarProvider,
    SolcastProvider.code: SolcastProvider,
}


def build(row: dict) -> BaseProvider | None:
    cls = PROVIDERS.get(row["code"])
    if cls is None:
        return None
    return cls(api_key=row.get("api_key") or "", params=row.get("params") or {})


def get_enabled(rows: list[dict]) -> list[BaseProvider]:
    result = []
    for row in rows:
        if not row.get("enabled"):
            continue
        provider = build(row)
        if provider is not None and provider.enabled():
            result.append(provider)
    return result
