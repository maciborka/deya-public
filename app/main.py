import asyncio
import base64
import binascii
import secrets
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, weather
from .collector import Collector
from .db import Database
from .decoder import METRIC_NAMES
from .flows import FLOW_FIELDS, LOAD_BUCKETS, MAX_GAP_SEC, bucket_bounds, integrate_day_import, integrate_flows, load_sources
from .inverter_modes import decode_mode
from .outages import OUTAGE_FIELDS, find_outages, grid_state
from .forecast import registry
from .forecast.scheduler import ForecastScheduler, next_run_ts, parse_hours
from .logger_client import LoggerClient
from .planner_service import (
    PlannerScheduler, TariffPrice, apply_status, compute_forecast24, compute_pv_hourly, compute_preview, floor_sync_status, serialize,
)
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(config.database_url())
    interrupted = db.fail_interrupted_writes()
    if interrupted:
        logging.getLogger("deya.writer").warning("запросов записи прервано перезапуском: %d", interrupted)
    client = LoggerClient(config.LOGGER_IP, config.LOGGER_SERIAL, config.LOGGER_PORT)
    collector = Collector(client, db, config.POLL_SEC, config.FULL_POLL_SEC)
    app.state.db = db
    app.state.collector = collector
    app.state.preview_cache = None
    app.state.forecast_cache = {}
    app.state.pv_hourly_cache = {}
    scheduler = ForecastScheduler(db, config.TZ, parse_hours(config.FORECAST_HOURS), config.FORECAST_DAYS)
    planner = PlannerScheduler(db, config.TZ)
    tasks = [
        asyncio.create_task(collector.run()),
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(planner.run()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        await client.close()
        db.close()


STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_POINTS = 1000
RAW_MAX_STEP = 10  # сырые точки, если нужный шаг не больше опроса (10 с)
PREVIEW_CACHE_SEC = 60
NO_PANELS_REASON = "нет включённых групп панелей с kwp > 0 (добавьте в админке)"

app = FastAPI(title="Deya", lifespan=lifespan)


def _authorized(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user.encode(), config.WEB_USER.encode()) & secrets.compare_digest(
        password.encode(), config.WEB_PASSWORD.encode()
    )


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Пароль на всё (дашборд доступен извне через Cloudflare Tunnel). Без пароля — только healthcheck
    изнутри контейнера."""
    if not config.WEB_PASSWORD:
        return await call_next(request)
    local_health = request.url.path == "/api/health" and request.client and request.client.host == "127.0.0.1"
    if local_health or _authorized(request.headers.get("authorization")):
        return await call_next(request)
    return Response("Требуется вход", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Deya", charset="UTF-8"'})


@app.get("/api/health")
async def health():
    db: Database = app.state.db
    return {
        "last_success_ts": app.state.collector.last_success_ts,
        "samples_count": await asyncio.to_thread(db.count_samples),
        "last_error": await asyncio.to_thread(db.last_error),
    }


@app.get("/api/latest")
async def latest():
    row = await asyncio.to_thread(app.state.db.latest_sample)
    if row is None:
        raise HTTPException(status_code=404, detail="нет данных")
    state = grid_state(row)
    since = None
    if state in ("partial", "down"):
        since = await _outage_since(int(row["ts"]))
    return {**row, "grid_state": state, "outage_since_ts": since}


_OUTAGE_SINCE_TTL = 60
_outage_since_cache: dict = {"at": 0.0, "since": None}


async def _outage_since(last_ts: int) -> int | None:
    """Начало текущего отключения (поиск по 48 ч); найденное значение кэшируется на минуту."""
    now = time.monotonic()
    c = _outage_since_cache
    if c["since"] is not None and now - c["at"] < _OUTAGE_SINCE_TTL:
        return c["since"]
    rows = await asyncio.to_thread(app.state.db.samples_between, last_ts - 48 * 3600, last_ts, 400000, list(OUTAGE_FIELDS))
    found = find_outages(rows)
    since = found[-1]["start_ts"] if found and found[-1]["end_ts"] is None else None
    c.update(at=now, since=since)
    return since


@app.get("/api/outages")
async def outages(days: int = Query(30, ge=1, le=90)):
    now = int(time.time())
    rows = await asyncio.to_thread(
        app.state.db.samples_between, now - days * 86400, now, days * 9000, list(OUTAGE_FIELDS))
    items = find_outages(rows)[::-1]
    return {"items": items, "total_sec": sum(i["duration_sec"] for i in items), "count": len(items)}


@app.get("/api/samples")
async def samples(
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    hours: float | None = Query(None, gt=0, le=24 * 400),
    limit: int = Query(5000, ge=1, le=5000),
    step: int | None = Query(None, ge=1),
    fields: str | None = Query(None, description="через запятую, из колонок samples; ts всегда"),
):
    now = int(time.time())
    to_ts = now if to_ts is None else to_ts
    if from_ts is None:
        from_ts = to_ts - int(hours * 3600) if hours is not None else to_ts - 86400
    span = max(to_ts - from_ts, 0)
    cols = None
    if fields:
        cols = [f.strip() for f in fields.split(",") if f.strip() and f.strip() != "ts"]
        unknown = [f for f in cols if f not in METRIC_NAMES]
        if unknown:
            raise HTTPException(status_code=422, detail=f"неизвестные поля: {', '.join(unknown)}")
    min_step = -(-span // MAX_POINTS)  # ≤ MAX_POINTS точек
    if step is None and min_step <= RAW_MAX_STEP:
        return await asyncio.to_thread(app.state.db.samples_between, from_ts, to_ts, min(limit, MAX_POINTS), cols)
    step = max(step or 1, min_step)
    return await asyncio.to_thread(app.state.db.samples_bucketed, from_ts, to_ts, step, cols)


@app.get("/api/flows")
async def flows(day: date | None = Query(None, alias="date")):
    tz = ZoneInfo(config.TZ)
    now = int(time.time())
    if day is None:
        day = datetime.fromtimestamp(now, tz).date()
    start = int(datetime.combine(day, datetime.min.time(), tz).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).timestamp())
    end = min(end, now) if start <= now else start
    rows = await asyncio.to_thread(app.state.db.samples_between, start, end, 200000, list(FLOW_FIELDS))
    return {"date": day.isoformat(), "from_ts": start, "to_ts": end, **integrate_flows(rows, start, end)}


@app.get("/api/load-sources")
async def load_sources_api(hours: int = Query(24, ge=1, le=720)):
    """Чем питался дом: кВт·ч из PV / АКБ / сети по бакетам (≤48 ч → 5 мин, ≤7 сут → 1 ч, иначе локальные сутки)."""
    now = int(time.time())
    kind = "5m" if hours <= 48 else "1h" if hours <= 168 else "1d"
    from_ts = now - hours * 3600
    if kind == "1d":
        tz = ZoneInfo(config.TZ)
        today = datetime.fromtimestamp(now, tz).date()
        first = datetime.combine(today - timedelta(days=hours // 24 - 1), datetime.min.time(), tz)
        from_ts = int(first.timestamp())

    def work():
        bounds = bucket_bounds(kind, from_ts, now, config.TZ)
        # запас по краям: интервал, пересекающий границу окна, делится интерполяцией
        rows = app.state.db.samples_between(from_ts - MAX_GAP_SEC, now + MAX_GAP_SEC, 400000, list(FLOW_FIELDS))
        return load_sources(rows, bounds)

    points = await asyncio.to_thread(work)
    tot = {k: round(sum(p[k] or 0.0 for p in points), 3) for k in ("pv_kwh", "batt_kwh", "grid_kwh")}
    whole = sum(tot.values())
    share = {k[:-4]: (round(v / whole, 4) if whole > 0 else 0.0) for k, v in tot.items()}
    return {"bucket": kind, "bucket_sec": LOAD_BUCKETS[kind], "points": points, "totals": tot, "share": share}


@app.get("/api/day-import")
async def day_import(day: date | None = Query(None, alias="date")):
    """Покупка из сети в слотах дорогого тарифа (цена = максимальной цене импорта плана) за локальные сутки."""
    tz = ZoneInfo(config.TZ)
    now = int(time.time())
    if day is None:
        day = datetime.fromtimestamp(now, tz).date()
    start = int(datetime.combine(day, datetime.min.time(), tz).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).timestamp())
    end = min(end, now) if start <= now else start
    db: Database = app.state.db

    def work():
        tariff = db.current_tariff(datetime.fromtimestamp(start, tz))
        prices = [z["price_per_kwh"] for z in (tariff["zones"] if tariff else []) if z["direction"] == "import"]
        if not prices:
            return None
        rows = db.samples_between(start, end, 200000, ["grid_power_w"])
        return integrate_day_import(
            rows, start, end, TariffPrice(db, config.TZ), max(prices),
            lambda ts: datetime.fromtimestamp(ts, tz).hour,
        )

    res = await asyncio.to_thread(work)
    if res is None:
        raise HTTPException(status_code=404, detail="нет активного тарифного плана")
    return {"date": day.isoformat(), **res}


@app.get("/api/daily")
async def daily(days: int = Query(30, ge=1, le=366)):
    return await asyncio.to_thread(app.state.db.daily_max, days, config.TZ)


@app.get("/api/settings/latest")
async def settings_latest():
    row = await asyncio.to_thread(app.state.db.latest_settings)
    if row is None:
        raise HTTPException(status_code=404, detail="нет данных")
    return row


@app.get("/api/forecast/status")
async def forecast_status():
    db: Database = app.state.db
    panels = await asyncio.to_thread(db.enabled_panels)
    rows = await asyncio.to_thread(db.forecast_providers)
    runs = await asyncio.to_thread(db.recent_forecast_runs, 20)
    details = []
    for row in rows:
        provider = registry.build(row)
        details.append({
            "code": row["code"],
            "name": row["name"],
            "enabled": row["enabled"],
            "has_key": bool(provider.api_key) if provider else bool(row["api_key"]),
        })
    return {
        "configured": bool(panels),
        "reason": None if panels else NO_PANELS_REASON,
        "panels": [vars(p) for p in panels],
        "providers": [p.code for p in registry.get_enabled(rows)],
        "provider_details": details,
        "runs": runs,
        "next_run_ts": int(next_run_ts(datetime.now(ZoneInfo(config.TZ)), parse_hours(config.FORECAST_HOURS)).timestamp()),
    }


@app.get("/api/forecast")
async def forecast(day: date | None = Query(None, alias="date")):
    tz = ZoneInfo(config.TZ)
    if day is None:
        day = datetime.now(tz).date() + timedelta(days=1)
    start = int(datetime.combine(day, datetime.min.time(), tz).timestamp())
    end = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).timestamp())
    db: Database = app.state.db
    panels = await asyncio.to_thread(db.enabled_panels)
    fc = await asyncio.to_thread(db.forecast_hourly, start, end)
    fact = await asyncio.to_thread(db.fact_hourly, start, end)

    def pack(hourly: dict[int, float]) -> dict:
        return {
            "hourly": [{"ts": t, "power_w": round(w, 1)} for t, w in sorted(hourly.items())],
            "kwh": round(sum(hourly.values()) / 1000, 2),
        }

    return {
        "date": day.isoformat(),
        "configured": bool(panels),
        "reason": None if panels else NO_PANELS_REASON,
        "providers": {code: pack(h) for code, h in fc.items()},
        "fact": pack(fact),
    }


@app.get("/api/weather")
async def weather_forecast():
    db: Database = app.state.db
    panels = await asyncio.to_thread(db.enabled_panels)
    lat, lon = (panels[0].lat, panels[0].lon) if panels else (weather.DEFAULT_LAT, weather.DEFAULT_LON)
    try:
        return await asyncio.to_thread(weather.get_weather, lat, lon)
    except weather.WeatherUnavailable:
        raise HTTPException(status_code=503, detail="прогноз погоды недоступен")


PV_HOURLY_PAST_DAYS = 7
PV_HOURLY_FUTURE_DAYS = 2


@app.get("/api/pv/hourly")
async def pv_hourly(day: date | None = Query(None, alias="date")):
    """Почасовой прогноз PV (как у планировщика) против факта за локальные сутки; сегодня−7 … сегодня+2."""
    now_ts = int(time.time())
    today = datetime.fromtimestamp(now_ts, ZoneInfo(config.TZ)).date()
    if day is None:
        day = today
    if not today - timedelta(days=PV_HOURLY_PAST_DAYS) <= day <= today + timedelta(days=PV_HOURLY_FUTURE_DAYS):
        raise HTTPException(status_code=400, detail="дата вне диапазона: сегодня−7 … сегодня+2")
    now = time.monotonic()
    cache = app.state.pv_hourly_cache
    cached = cache.get(day)
    if cached is not None and now - cached[0] < PREVIEW_CACHE_SEC:
        return cached[1]
    result = await asyncio.to_thread(compute_pv_hourly, app.state.db, day, now_ts)
    for k in [k for k, v in cache.items() if now - v[0] >= PREVIEW_CACHE_SEC]:
        del cache[k]
    cache[day] = (now, result)
    return result


@app.get("/api/tariff")
async def tariff():
    db: Database = app.state.db
    plan = await asyncio.to_thread(db.current_tariff)
    if plan is None:
        raise HTTPException(status_code=404, detail="нет активного тарифного плана")
    current = await asyncio.to_thread(db.price_at)
    return {
        "plan": {k: v for k, v in plan.items() if k != "zones"},
        "zones": plan["zones"],
        "current": current,
    }


@app.get("/api/strategy")
async def strategy():
    row = await asyncio.to_thread(app.state.db.strategy_settings)
    if row is None:
        raise HTTPException(status_code=404, detail="настройки стратегии не заданы")
    return {
        "reserve_soc_pct": row["reserve_soc_pct"],
        "max_grid_charge_soc_pct": row["max_grid_charge_soc_pct"],
        "normal_floor_pct": row["normal_floor_pct"],
        "emergency_mode": row["emergency_mode"],
        "emergency_floor_pct": row["emergency_floor_pct"],
        "cloudy_full_charge_kwh": row["cloudy_full_charge_kwh"],
        "battery_capacity_kwh": float(row["battery_capacity_kwh"]),
        "battery_usable_pct": row["battery_usable_pct"],
        "round_trip_efficiency_pct": row["round_trip_efficiency_pct"],
        "control_mode": row["control_mode"],
        "writes_enabled": row["writes_enabled"],
        "decision_time": row["decision_time"].strftime("%H:%M"),
        "night_target_mode": row["night_target_mode"],
        "night_target_manual_pct": row["night_target_manual_pct"],
        "night_target_manual_night": row["night_target_manual_night"].isoformat() if row["night_target_manual_night"] else None,
        "updated_at": row["updated_at"].isoformat(),
    }


async def _control_mode() -> str:
    row = await asyncio.to_thread(app.state.db.strategy_settings)
    return row["control_mode"] if row else "off"


async def _night_target() -> dict:
    row = await asyncio.to_thread(app.state.db.strategy_settings)
    return {
        "night_target_mode": row["night_target_mode"] if row else "auto",
        "night_target_manual_pct": row["night_target_manual_pct"] if row else None,
    }


@app.get("/api/plan")
async def plan():
    row = await asyncio.to_thread(app.state.db.latest_final_decision)
    return {
        "control_mode": await _control_mode(),
        **await _night_target(),
        "decision": serialize(row) if row else None,
        "apply": await asyncio.to_thread(apply_status, app.state.db),
        "floor_sync": await asyncio.to_thread(floor_sync_status, app.state.db),
    }


@app.get("/api/plan/preview")
async def plan_preview():
    cached = app.state.preview_cache
    if cached is not None and time.monotonic() - cached[0] < PREVIEW_CACHE_SEC:
        decision = cached[1]
    else:
        try:
            decision = await asyncio.to_thread(compute_preview, app.state.db)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        app.state.preview_cache = (time.monotonic(), decision)
    return {
        "control_mode": await _control_mode(),
        "decision": decision,
        "apply": await asyncio.to_thread(apply_status, app.state.db),
        "floor_sync": await asyncio.to_thread(floor_sync_status, app.state.db),
    }

FORECAST_CACHE_MAX = 32


@app.get("/api/plan/forecast")
async def plan_forecast(
    target: float | None = Query(None, ge=0, le=100),
    hours: int = Query(24, ge=6, le=48),
):
    if target is not None:
        target = round(target)
    key = (target, hours)
    now = time.monotonic()
    cache = app.state.forecast_cache
    cached = cache.get(key)
    if cached is not None and now - cached[0] < PREVIEW_CACHE_SEC:
        return cached[1]
    try:
        result = await asyncio.to_thread(compute_forecast24, app.state.db, None, target, hours)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    for k in [k for k, v in cache.items() if now - v[0] >= PREVIEW_CACHE_SEC]:
        del cache[k]
    cache[key] = (now, result)
    while len(cache) > FORECAST_CACHE_MAX:
        del cache[next(iter(cache))]
    return result


@app.get("/api/plan/history")
async def plan_history(days: int = Query(30, ge=1, le=366)):
    since = datetime.now(ZoneInfo(config.TZ)).date() - timedelta(days=days)
    rows = await asyncio.to_thread(app.state.db.decisions_history, since)
    return {"decisions": [serialize(r) for r in rows]}


@app.get("/api/mode")
async def mode():
    row = await asyncio.to_thread(app.state.db.latest_settings)
    if row is None:
        raise HTTPException(status_code=404, detail="нет данных")
    return {"snapshot_ts": row["ts"], **decode_mode(row["regs"])}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
