import logging
import async_timeout
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import discovery
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import as_local
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_LOCATION, REFRESH_INTERVAL_MINUTES

_LOGGER = logging.getLogger(__name__)

# --------------------------
# Provider Implementations
# --------------------------

class FraunhoferEnergyChartsProvider:
    CO2EQ_URL = "https://api.energy-charts.info/co2eq"

    def __init__(self, country: str, hass: Optional[HomeAssistant] = None):
        self.country = country
        self.hass = hass

    async def fetch_co2eq_series(self, session) -> Optional[Dict[str, Any]]:
        # Use cache if available and fresh
        cache = None
        cache_time = None
        if self.hass:
            cache_data = self.hass.data.get(DOMAIN, {}).get('energy_charts_cache', {})
            cache = cache_data.get('data')
            cache_time = cache_data.get('timestamp')
        now = datetime.now(timezone.utc)
        if cache and cache_time and (now - cache_time).total_seconds() < 3600:
            return cache
        # Fetch from API and update cache
        url = f"{self.CO2EQ_URL}?country={self.country}"
        backoffs = [5, 10, 15]
        for attempt in range(len(backoffs) + 1):
            try:
                _LOGGER.info("Calling Energy-Charts CO2eq API: %s (try %d)", url, attempt + 1)
                async with async_timeout.timeout(60):
                    async with session.get(url, headers={"accept": "application/json"}) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            _LOGGER.error("Energy-Charts CO2eq error (try %d): %s - %s", attempt + 1, resp.status, body)
                            if 400 <= resp.status < 500:
                                return None
                        else:
                            data = await resp.json()
                            if self.hass:
                                self.hass.data.setdefault(DOMAIN, {})['energy_charts_cache'] = {
                                    'data': data,
                                    'timestamp': now
                                }
                            return data
            except asyncio.TimeoutError as te:
                _LOGGER.error("Timeout (try %d) calling Energy-Charts CO2eq API: %s - %s", attempt + 1, url, te)
            except Exception as e:
                _LOGGER.exception("Error (try %d) calling Energy-Charts CO2eq API: %s - %s", attempt + 1, url, e)
            if attempt < len(backoffs):
                await asyncio.sleep(backoffs[attempt])
        return None

# --------------------------
# Raw Data Engine
# --------------------------

class TimePoint:
    def __init__(self, ts: datetime, intensity: float):
        self.ts = ts
        self.intensity = intensity

def parse_energycharts_series(json_data: Dict[str, Any]) -> List[TimePoint]:
    ts_list = json_data.get("unix_seconds", [])
    actual = json_data.get("co2eq", [])
    forecast = json_data.get("co2eq_forecast", [])

    points: List[TimePoint] = []
    for i, ts_sec in enumerate(ts_list):
        ts = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        val = None
        if i < len(actual) and actual[i] is not None:
            val = float(actual[i])
        elif i < len(forecast) and forecast[i] is not None:
            val = float(forecast[i])
        if val is not None:
            points.append(TimePoint(ts=ts, intensity=val))

    points.sort(key=lambda p: p.ts)
    return points

def parse_hours(hours_str: Optional[str]) -> Optional[Tuple[int, int]]:
    if not hours_str:
        return None
    try:
        s, e = hours_str.split("-")
        return int(s), int(e)
    except Exception:
        _LOGGER.warning("Invalid allowedHours format '%s'. Expected '8-21'. Ignoring.", hours_str)
        return None

def within_allowed_hours(ts_utc: datetime, allowed_hours: Optional[Tuple[int, int]]) -> bool:
    if not allowed_hours:
        return True
    start_h, end_h = allowed_hours
    local = as_local(ts_utc)
    return start_h <= local.hour < end_h

def best_start_from_points(points: List[TimePoint],
                           window_start_utc: datetime,
                           window_end_utc: datetime,
                           runtime_minutes: int,
                           allowed_hours: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    runtime = timedelta(minutes=runtime_minutes)
    candidates = [p for p in points if window_start_utc <= p.ts < window_end_utc]
    if not candidates:
        return {"status": "No Data"}

    best_avg = None
    best_start = None

    for i, p in enumerate(candidates):
        t0 = p.ts
        if t0 + runtime > window_end_utc:
            break
        if not within_allowed_hours(t0, allowed_hours):
            continue
        vals = [q.intensity for q in candidates[i:] if q.ts < t0 + runtime]
        if not vals:
            continue
        avg = sum(vals) / len(vals)
        if best_avg is None or avg < best_avg:
            best_avg = avg
            best_start = t0

    if best_start is None:
        return {"status": "No Candidate"}

    return {
        "status": "OK",
        "best_start_time_utc": best_start.isoformat(),
        "best_end_time_utc": (best_start + runtime).isoformat(),
        "expected_avg_intensity": best_avg
    }

# --------------------------
# Service Schemas (Validation)
# --------------------------

GET_SCHEMA = vol.Schema({
    vol.Required("dataStartAt"): cv.string,
    vol.Required("dataEndAt"): cv.string,
    vol.Required("expectedRuntime"): cv.positive_int,
    vol.Optional("allowedHours"): cv.string,
})

# --------------------------
# HA Setup and Service Handlers
# --------------------------

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Carbon Aware component from configuration.yaml."""
    _LOGGER.warning(">>> Carbon Aware Home: __init__ loaded")

    conf = config.get(DOMAIN)
    if conf is None:
        hass.async_create_task(
            discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
        )
        return True

    location = conf.get(CONF_LOCATION, "de")

    hass.data[DOMAIN] = {
        CONF_LOCATION: location,
    }

    # --- Cache Refresh Scheduler ---
    async def refresh_energy_charts_cache(_now=None):
        session = async_get_clientsession(hass)
        provider = FraunhoferEnergyChartsProvider(country=location, hass=hass)
        data = await provider.fetch_co2eq_series(session)
        _LOGGER.warning(">>> Cache refresh attempted")
        if data is not None:
            _LOGGER.info("Energy-Charts cache refreshed with keys: %s", list(data.keys()))
            await hass.services.async_call(
                "homeassistant", "update_entity",
                {"entity_id": "sensor.current_co2_intensity"},
                blocking=False
            )
        else:
            _LOGGER.warning("Energy-Charts cache refresh failed or returned no data")

        next_run = dt_util.utcnow() + timedelta(minutes=REFRESH_INTERVAL_MINUTES)
        async_track_point_in_time(hass, refresh_energy_charts_cache, next_run)

    # Starte den ersten Refresh beim Setup
    hass.async_create_task(refresh_energy_charts_cache())

    # --- Minütliches Entity-Update unabhängig vom Cache ---
    async def _update_sensor_every_minute(_now):
        await hass.services.async_call(
            "homeassistant", "update_entity",
            {"entity_id": "sensor.current_co2_intensity"},
            blocking=False
        )
    async_track_time_interval(hass, _update_sensor_every_minute, timedelta(minutes=1))
    _LOGGER.info("Per-minute sensor update scheduled")

    # --- Service: get_best_time_raw (supports_response=True) ---
    async def handle_get_best_time_raw(call: ServiceCall):
        session = async_get_clientsession(hass)
        data_start_at = call.data.get("dataStartAt")
        data_end_at   = call.data.get("dataEndAt")
        runtime       = call.data.get("expectedRuntime", 60)
        allowed_hours = parse_hours(call.data.get("allowedHours"))

        if not isinstance(data_start_at, str) or not isinstance(data_end_at, str):
            return {"status": "MissingParam"}

        try:
            runtime = int(runtime)
        except Exception:
            runtime = 60

        ws = dt_util.parse_datetime(data_start_at)
        we = dt_util.parse_datetime(data_end_at)
        if ws is None or we is None:
            return {"status": "InvalidDatetime"}

        ws_utc = dt_util.as_utc(ws)
        we_utc = dt_util.as_utc(we)

        provider = FraunhoferEnergyChartsProvider(country=location, hass=hass)
        raw_json = await provider.fetch_co2eq_series(session)
        if raw_json is None:
            return {
                "status": "Error",
                "source": "raw",
                "best_start": None,
                "avg_intensity": None,
                "start": data_start_at,
                "end": data_end_at,
                "runtime_minutes": runtime,
                "location": location
            }

        points = parse_energycharts_series(raw_json)
        result = best_start_from_points(points, ws_utc, we_utc, runtime, allowed_hours=allowed_hours)
        status = result.get("status", "Error")
        if status != "OK":
            return {
                "status": status,
                "source": "raw",
                "best_start": None,
                "avg_intensity": None,
                "start": data_start_at,
                "end": data_end_at,
                "runtime_minutes": runtime,
                "location": location
            }

        return {
            "status": "OK",
            "source": "raw",
            "best_start": result["best_start_time_utc"],  # ISO-String UTC
            "avg_intensity": result["expected_avg_intensity"],
            "start": data_start_at,
            "end": data_end_at,
            "runtime_minutes": runtime,
            "location": location
        }

    hass.services.async_register(
        DOMAIN, "get_best_time_raw", handle_get_best_time_raw, schema=GET_SCHEMA, supports_response=True
    )

    # --- Discovery der Sensor-Plattform ---
    hass.async_create_task(
        discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )

    return True
