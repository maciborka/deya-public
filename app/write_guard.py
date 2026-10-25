"""Проверки перед созданием заявки на запись из сервиса (планировщик).

SQL-аналог admin/core/tou.py guard_errors. Общий модуль между app/ и admin/ невозможен (разные образы),
поэтому константы продублированы — держать синхронно с admin/core/tou.py.
"""

WRITE_LIMIT_24H = 10  # = admin/core/tou.py WRITE_LIMIT_24H
SNAPSHOT_MAX_AGE_SEC = 15 * 60  # = admin/core/tou.py SNAPSHOT_MAX_AGE_SEC


def guard_errors(state: dict, now_ts: float) -> list[str]:
    """Причины, по которым создавать заявку сейчас нельзя. state — Database.write_guard_state():
    active (pending/running), last_24h (создано за 24 ч), last_sample_ts (последняя телеметрия)."""
    errors = []
    if state["active"]:
        errors.append("уже есть запрос в очереди или в работе")
    if state["last_24h"] >= WRITE_LIMIT_24H:
        errors.append(f"лимит: не больше {WRITE_LIMIT_24H} запросов за 24 часа")
    # Как в админке: снимки настроек дедуплицируются, свежесть — по последней телеметрии;
    # совпадение «было» с живыми регистрами проверяет воркер перед записью (иначе stale).
    last = state["last_sample_ts"]
    if last is None or last < now_ts - SNAPSHOT_MAX_AGE_SEC:
        errors.append("сервис давно не получал данные с инвертора")
    return errors
