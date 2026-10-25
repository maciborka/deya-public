import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.forecast import base, forecast_solar, open_meteo, solcast
from app.forecast.base import ForecastSample, PanelGroup, az_solcast, k_temp


def utc(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def test_open_meteo_parse_shifts_hour():
    data = {"hourly": {
        "time": ["2026-06-01T11:00", "2026-06-01T12:00"],
        "global_tilted_irradiance": [800.0, None],
        "shortwave_radiation": [700.0, 650.0],
        "temperature_2m": [25.0, 35.0],
    }}
    out = open_meteo.parse(data, kwp=5)
    assert [s.target_ts for s in out] == [utc("2026-06-01T10:00"), utc("2026-06-01T11:00")]
    assert out[0].power_w == pytest.approx(4000)
    assert out[0].ghi == 700.0
    assert out[1].gti is None and out[1].power_w is None


def test_open_meteo_fetch_uses_http(monkeypatch):
    seen = {}

    def fake(url, headers=None):
        seen["url"] = url
        return {"hourly": {"time": ["2026-06-01T01:00"], "global_tilted_irradiance": [0]}}

    monkeypatch.setattr(base, "http_get_json", fake)
    out = open_meteo.OpenMeteoProvider().fetch(PanelGroup("A", 50.45, 30.52, 30, 0, 5), 3)
    assert "forecast_days=3" in seen["url"] and "timezone=UTC" in seen["url"]
    assert out[0].power_w == 0


def test_forecast_solar_parse_local_tz():
    data = {
        "result": {"watts": {"2026-06-01 12:00:00": 3000, "2026-06-01 13:00:00": 3500}},
        "message": {"info": {"timezone": "Europe/Kyiv"}},
    }
    out = forecast_solar.parse(data)
    kyiv_noon = int(datetime(2026, 6, 1, 12, tzinfo=ZoneInfo("Europe/Kyiv")).timestamp())
    assert [s.target_ts for s in out] == [kyiv_noon, kyiv_noon + 3600]
    assert out[0].power_w == 3000


def test_solcast_parse_and_azimuth():
    data = {"forecasts": [{"period_end": "2026-06-01T11:00:00.0000000Z", "gti": 500, "gti10": 400,
                           "gti90": 600, "air_temp": 25}]}
    out = solcast.parse(data, kwp=10)
    assert out[0].target_ts == utc("2026-06-01T10:00")
    assert out[0].power_w == pytest.approx(5000)
    assert out[0].p10 == pytest.approx(4000) and out[0].p90 == pytest.approx(6000)
    assert az_solcast(0) == 180
    assert az_solcast(90) == 90
    assert az_solcast(-90) == -90
    assert az_solcast(-180) == 0
    assert az_solcast(30) == 150
    assert az_solcast(-30) == -150


def test_solcast_enabled_only_with_key(monkeypatch):
    monkeypatch.delenv("SOLCAST_API_KEY", raising=False)
    assert not solcast.SolcastProvider().enabled()
    monkeypatch.setenv("SOLCAST_API_KEY", "x")
    assert solcast.SolcastProvider().enabled()


def test_k_temp():
    assert k_temp(None) == 1.0
    assert k_temp(25) == 1.0
    assert k_temp(35) == pytest.approx(0.96)
    assert k_temp(-100) == 1.1
    assert k_temp(200) == 0.5


@pytest.fixture
def client(pg_url, monkeypatch):

    async def noop(self):
        return None

    monkeypatch.setattr(main.Collector, "run", noop)
    monkeypatch.setattr(main.ForecastScheduler, "run", noop)
    with TestClient(main.app) as c:
        yield c


def test_status_not_configured(client):
    r = client.get("/api/forecast/status").json()
    assert r["configured"] is False and r["panels"] == []


def test_status_with_panel_hides_keys(client):
    db = main.app.state.db
    db._exec(
        "INSERT INTO panel_groups(name, enabled, lat, lon, tilt, azimuth, kwp) VALUES ('A', true, 50, 30, 30, 0, 5)"
    )
    db._exec(
        "INSERT INTO forecast_providers(code, name, enabled, api_key, params, notes, updated_at) "
        "VALUES ('solcast', 'Solcast', true, 'k-secret', '{}', '', now()) "
        "ON CONFLICT (code) DO UPDATE SET api_key = 'k-secret', enabled = true"
    )
    try:
        r = client.get("/api/forecast/status")
        assert r.json()["configured"] is True
        assert "solcast" in r.json()["providers"]
        assert "k-secret" not in r.text
        assert {"code": "solcast", "name": "Solcast", "enabled": True, "has_key": True} in r.json()["provider_details"]
    finally:
        db._exec("UPDATE forecast_providers SET api_key = '', enabled = false WHERE code = 'solcast'")


def test_forecast_endpoint(client):
    db = main.app.state.db
    tz = ZoneInfo(config.TZ)
    day = datetime(2026, 6, 2, tzinfo=tz)
    t0 = int(day.timestamp()) + 12 * 3600
    db.insert_forecast("open_meteo", "A", 100, [ForecastSample(target_ts=t0, power_w=1000.0)])
    db.insert_forecast("open_meteo", "A", 200, [ForecastSample(target_ts=t0, power_w=2000.0)])
    db.insert_forecast("open_meteo", "B", 200, [ForecastSample(target_ts=t0, power_w=500.0)])
    db.insert_sample(t0 + 10, {"pv1_power_w": 1000, "pv2_power_w": 200})
    db.insert_sample(t0 + 40, {"pv1_power_w": 1400, "pv2_power_w": 400})
    r = client.get("/api/forecast", params={"date": "2026-06-02"}).json()
    assert r["providers"]["open_meteo"]["hourly"] == [{"ts": t0, "power_w": 2500.0}]
    assert r["providers"]["open_meteo"]["kwh"] == 2.5
    assert r["fact"]["hourly"] == [{"ts": t0, "power_w": 1500.0}]
    other = client.get("/api/forecast", params={"date": "2026-06-03"}).json()
    assert other["providers"] == {}
    assert client.get("/api/forecast").status_code == 200


def test_solcast_rooftop_parse():
    data = {
        "forecasts": [
            {"period_end": "2026-10-22T10:30:00.0000000Z", "period": "PT30M",
             "pv_estimate": 2.0, "pv_estimate10": 1.0, "pv_estimate90": 3.0},
            {"period_end": "2026-10-22T11:00:00.0000000Z", "period": "PT30M",
             "pv_estimate": 4.0, "pv_estimate10": 2.0, "pv_estimate90": 5.0},
        ]
    }
    out = solcast.parse_rooftop(data)
    assert len(out) == 1
    s = out[0]
    assert s.target_ts == utc("2026-10-22T10:00:00Z")
    assert s.power_w == 3000 and s.p10 == 1500 and s.p90 == 4000
    assert s.gti is None


def test_solcast_site_level_flag():
    assert solcast.SolcastProvider(api_key="k", params={"resource_id": "abc"}).site_level
    assert not solcast.SolcastProvider(api_key="k").site_level
    assert solcast.SolcastProvider().min_interval_sec == 3 * 3600


def test_scheduler_site_level_called_once(monkeypatch):
    import asyncio

    from app.forecast import registry, scheduler

    calls = []

    class Prov(base.BaseProvider):
        code = "x"
        site_level = True

        def fetch(self, panel, days):
            calls.append(panel.name)
            return []

    class FakeDb:
        def enabled_panels(self):
            return [PanelGroup("a", 1, 1, 1, 0, 1), PanelGroup("b", 1, 1, 1, 0, 1)]

        def forecast_providers(self):
            return [{}]

        def last_ok_run_ts(self, *a):
            return None

        def insert_forecast(self, *a):
            pass

        def log_forecast_run(self, *a):
            pass

    monkeypatch.setattr(registry, "get_enabled", lambda rows: [Prov()])
    sch = scheduler.ForecastScheduler(FakeDb(), "UTC", [5], 3)
    asyncio.run(sch.run_cycle())
    assert calls == ["site"]


def _run_site_provider(monkeypatch, prov):
    import asyncio

    from app.forecast import registry, scheduler

    runs = []

    class FakeDb:
        def enabled_panels(self):
            return []

        def forecast_providers(self):
            return [{}]

        def last_ok_run_ts(self, *a):
            return None

        def insert_forecast(self, *a):
            pass

        def log_forecast_run(self, *a):
            runs.append(a)

    monkeypatch.setattr(registry, "get_enabled", lambda rows: [prov])
    asyncio.run(scheduler.ForecastScheduler(FakeDb(), "UTC", [5], 3).run_cycle())
    return runs


def test_solcast_two_sites_two_calls(monkeypatch):
    urls = []

    def fake_get(url, headers=None):
        urls.append(url)
        return {"forecasts": []}

    monkeypatch.setattr(base, "http_get_json", fake_get)
    prov = solcast.SolcastProvider(api_key="secretkey", params={"sites": {"PV1": "aaaa", "PV2": "bbbb"}})
    runs = _run_site_provider(monkeypatch, prov)
    assert [r[2] for r in runs] == ["PV1", "PV2"]
    assert all(r[3] for r in runs)
    assert "aaaa" in urls[0] and "bbbb" in urls[1] and len(urls) == 2


def test_solcast_resource_id_single_site(monkeypatch):
    urls = []
    monkeypatch.setattr(base, "http_get_json", lambda url, headers=None: urls.append(url) or {"forecasts": []})
    prov = solcast.SolcastProvider(api_key="k", params={"resource_id": "abc"})
    runs = _run_site_provider(monkeypatch, prov)
    assert [r[2] for r in runs] == ["site"] and len(urls) == 1 and "abc" in urls[0]


def test_solcast_error_has_no_key(monkeypatch):
    def boom(url, headers=None):
        raise RuntimeError("GET x не удался: HTTPError: HTTP Error 403: Forbidden")

    monkeypatch.setattr(base, "http_get_json", boom)
    prov = solcast.SolcastProvider(api_key="secretkey", params={"sites": {"PV1": "aaaa"}})
    runs = _run_site_provider(monkeypatch, prov)
    assert not runs[0][3] and "secretkey" not in str(runs[0][5])
