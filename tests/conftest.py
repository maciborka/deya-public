import os
from urllib.parse import urlparse

import psycopg
import pytest

TABLES = "samples, settings_snapshots, poll_errors, forecast_raw, forecast_runs, panel_groups, tariff_zones, tariff_plans, strategy_settings, charge_decisions, inverter_write_requests"


@pytest.fixture
def pg_url(monkeypatch):
    """DSN тестовой БД со схемой от Django-миграций; чистит таблицы перед тестом."""
    url = os.environ.get("DEYA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("DEYA_TEST_DATABASE_URL не задан")
    dbname = urlparse(url).path.lstrip("/")
    if "test" not in dbname:
        pytest.exit(f"DEYA_TEST_DATABASE_URL указывает на БД '{dbname}': имя должно содержать 'test'", returncode=2)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")
    monkeypatch.setenv("DEYA_DATABASE_URL", url)
    return url
