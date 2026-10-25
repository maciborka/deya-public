import pytest

from app import weather


def sample(n=7):
    return {
        "current": {"time": "2026-10-22T12:00", "temperature_2m": 11.5, "weather_code": 3, "is_day": 1,
                    "cloud_cover": 80, "wind_speed_10m": 2.5},
        "daily": {
            "time": [f"2026-10-{22 + i}" for i in range(n)],
            "weather_code": [3] * n,
            "temperature_2m_max": [14.0] * n,
            "temperature_2m_min": [5.0] * n,
            "precipitation_sum": [0.2] * n,
            "precipitation_probability_max": [30] * n,
            "cloud_cover_mean": [70] * n,
            "wind_speed_10m_max": [6.1] * n,
            "sunrise": ["2026-10-22T07:30"] * n,
            "sunset": ["2026-10-22T17:50"] * n,
        },
    }


@pytest.fixture(autouse=True)
def clear_cache():
    weather._cache.clear()
    yield
    weather._cache.clear()


def test_parse_maps_fields():
    r = weather.parse(sample(), 48.44, 22.72, 1000)
    assert r["stale"] is False and r["fetched_at"] == 1000
    assert r["location"] == {"lat": 48.44, "lon": 22.72}
    assert r["current"]["temp"] == 11.5 and r["current"]["is_day"] is True and r["current"]["wind"] == 2.5
    assert len(r["days"]) == 7
    d = r["days"][0]
    assert d == {"date": "2026-10-22", "code": 3, "t_max": 14.0, "t_min": 5.0, "precip_mm": 0.2, "precip_prob": 30,
                 "cloud": 70, "wind_max": 6.1, "sunrise": "2026-10-22T07:30", "sunset": "2026-10-22T17:50"}


def test_parse_empty():
    r = weather.parse({}, 1, 2, 5)
    assert r["days"] == [] and r["current"]["temp"] is None and r["current"]["is_day"] is None


def test_cache_hit_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(weather, "fetch", lambda lat, lon: calls.append(1) or sample())
    a = weather.get_weather(48.0, 22.0, now=1000)
    b = weather.get_weather(48.0, 22.0, now=1000 + weather.CACHE_SEC - 1)
    assert len(calls) == 1 and a is b and b["stale"] is False
    weather.get_weather(48.0, 22.0, now=1000 + weather.CACHE_SEC)
    assert len(calls) == 2


def test_stale_on_error_and_503_without_cache(monkeypatch):
    def boom(lat, lon):
        raise RuntimeError("net")

    monkeypatch.setattr(weather, "fetch", boom)
    with pytest.raises(weather.WeatherUnavailable):
        weather.get_weather(48.0, 22.0, now=1000)
    monkeypatch.setattr(weather, "fetch", lambda lat, lon: sample())
    weather.get_weather(48.0, 22.0, now=1000)
    monkeypatch.setattr(weather, "fetch", boom)
    r = weather.get_weather(48.0, 22.0, now=1000 + weather.CACHE_SEC + 5)
    assert r["stale"] is True and r["fetched_at"] == 1000
    assert weather._cache[(48.0, 22.0)]["stale"] is False
