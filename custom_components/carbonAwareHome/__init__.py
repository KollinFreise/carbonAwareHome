import logging
import urllib.parse
import async_timeout
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import discovery
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util
from homeassistant.util.dt import now as hass_now, as_local
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_API_KEY, CONF_LOCATION

_LOGGER = logging.getLogger(__name__)

# --------------------------
# Provider Implementations
# --------------------------

class BluehandsProvider:
    FORECAST_URL = "https://forecast.carbon-aware-computing.com/emissions/forecasts/current"

    def __init__(self, api_key: str, location: str):
        self.api_key = api_key
        self.location = location

    async def get_best_time(self, session, window_start_iso: str, window_end_iso: str, runtime_minutes: int) -> Tuple[Optional[str], Optional[float], Dict[str, Any]]:
        query_params = {
            "location": self.location,
            "dataStartAt": window_start_iso,
            "dataEndAt": window_end_iso,
            "windowSize": runtime_minutes
        }
        url = f"{self.FORECAST_URL}?{urllib.parse.urlencode(query_params)}"

        headers = {"accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key

        try:
            async with async_timeout.timeout(20):
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        _LOGGER.error("Forecast API error: %s - %s", resp.status, body)
                        return None, None, {"status": "Error", "http_status": resp.status}
                    data = await resp.json()
        except asyncio.TimeoutError:
            _LOGGER.error("Timeout calling Bluehands forecast API")
            return None, None, {"status": "Timeout"}
        except Exception as e:
            _LOGGER.exception("Error calling Bluehands forecast API: %s", e)
            return None, None, {"status": "Error", "exception": str(e)}

        if (not data) or (not isinstance(data, list)) or ("optimalDataPoints" not in data[0]) or (not data[0]["optimalDataPoints"]):
            _LOGGER.warning("No optimalDataPoints in API response")
            return None, None, {"status": "No Data"}

        best_point = data[0]["optimalDataPoints"][0]
        best_time = best_point.get("timestamp")
        best_value = best_point.get("value")
        return best_time, best_value, {"status": "OK", "raw": data}


class FraunhoferEnergyChartsProvider:
    CO2EQ_URL = "https://api.energy-charts.info/co2eq"

    def __init__(self, country: str):
        self.country = country

    async def fetch_co2eq_series(self, session) -> Optional[Dict[str, Any]]:
        url = f"{self.CO2EQ_URL}?country={self.country}"
        backoffs = [5, 10, 15]  # retry wait times in seconds
        for attempt in range(len(backoffs) + 1):
            try:
                async with async_timeout.timeout(30):
                    async with session.get(url, headers={"accept": "application/json"}) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            _LOGGER.error("Energy-Charts CO2eq error (try %d): %s - %s", attempt + 1, resp.status, body)
                            if 400 <= resp.status < 500:
                                return None
                        else:
                            return await resp.json()
            except asyncio.TimeoutError:
                _LOGGER.warning("Timeout (try %d) calling Energy-Charts CO2eq API", attempt + 1)
            except Exception as e:
                _LOGGER.exception("Error (try %d) calling Energy-Charts CO2eq API: %s", attempt + 1, e)

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
# Sensor Update Helper
# --------------------------

def update_forecast_sensor(hass: HomeAssistant,
                           state_time_utc_iso: Optional[str],
                           attrs: Dict[str, Any]) -> None:
    entity_id = "sensor.co2_intensity_forecast"

    if state_time_utc_iso:
        try:
            dt_utc = datetime.fromisoformat(state_time_utc_iso.replace("Z", "+00:00"))
            local = as_local(dt_utc)
            state_value = local.isoformat()
        except Exception:
            state_value = state_time_utc_iso
    else:
        state_value = attrs.get("status", "No Data")

    hass.states.async_set(entity_id, state_value, attrs)

# --------------------------
# Scheduling Helper (non-blocking)
# --------------------------

async def schedule_execution(hass: HomeAssistant, optimal_time_iso: str,
                             target_service: Optional[str] = None,
                             target_data: Optional[Dict[str, Any]] = None):
    dt_opt = dt_util.parse_datetime(optimal_time_iso.replace("Z", "+00:00"))
    if dt_opt is None:
        _LOGGER.error("Cannot parse optimal time: %s", optimal_time_iso)
        return
    dt_opt_utc = dt_util.as_utc(dt_opt)

    async def _run_at_time(_now):
        _LOGGER.info("Executing scheduled task at %s", dt_util.as_local(dt_opt_utc).isoformat())
        if target_service:
            try:
                domain, service = target_service.split(".")
                await hass.services.async_call(domain, service, target_data or {}, blocking=False)
            except Exception as e:
                _LOGGER.exception("Scheduled service call failed: %s", e)
        else:
            await hass.services.async_call("logbook", "log", {
                "name": "Carbon Aware",
                "message": f"Scheduled time reached: {dt_util.as_local(dt_opt_utc).isoformat()}"
            }, blocking=False)

    async_track_point_in_time(hass, _run_at_time, dt_opt_utc)
    _LOGGER.info("Scheduled execution for %s", dt_util.as_local(dt_opt_utc).isoformat())

# --------------------------
# Service Schemas (Validation)
# --------------------------

GET_SCHEMA = vol.Schema({
    vol.Required("dataStartAt"): cv.string,
    vol.Required("dataEndAt"): cv.string,
    vol.Required("expectedRuntime"): cv.positive_int,
    vol.Optional("allowedHours"): cv.string,
})

DELAY_SCHEMA = vol.Schema({
    vol.Required("dataStartAt"): cv.string,
    vol.Required("dataEndAt"): cv.string,
    vol.Required("expectedRuntime"): cv.positive_int,
    vol.Optional("allowedHours"): cv.string,
    vol.Optional("targetService"): cv.string,  # e.g. "switch.turn_on"
    vol.Optional("targetData"): dict,          # e.g. {"entity_id": "switch.washer"}
})

# --------------------------
# HA Setup and Service Handlers
# --------------------------

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Carbon Aware component from configuration.yaml."""
    conf = config.get(DOMAIN)
    if conf is None:
        return True

    api_key = conf.get(CONF_API_KEY)
    location = conf.get(CONF_LOCATION, "de")

    hass.data[DOMAIN] = {
        CONF_API_KEY: api_key,
        CONF_LOCATION: location,
    }

    session = async_get_clientsession(hass)

    # --- Non-blocking Services with Response Data ---

    async def handle_get_best_time_raw(call: ServiceCall):
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

        provider = FraunhoferEnergyChartsProvider(country=location)
        raw_json = await provider.fetch_co2eq_series(session)
        if raw_json is None:
            # Fallback: Bluehands API
            api_provider = BluehandsProvider(api_key=api_key, location=location)
            best_time, best_value, meta = await api_provider.get_best_time(session, data_start_at, data_end_at, runtime)
            return {
                "status": meta.get("status", "Error"),
                "source": "raw_fallback_api",
                "best_start": best_time,
                "avg_intensity": best_value,
                "start": data_start_at,
                "end": data_end_at,
                "runtime_minutes": runtime,
                "location": location,
                "fallback": "api"
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

    async def handle_get_best_time_api(call: ServiceCall):
        data_start_at = call.data.get("dataStartAt")
        data_end_at   = call.data.get("dataEndAt")
        runtime       = call.data.get("expectedRuntime", 60)

        if not isinstance(data_start_at, str) or not isinstance(data_end_at, str):
            return {"status": "MissingParam"}

        try:
            runtime = int(runtime)
        except Exception:
            runtime = 60

        provider = BluehandsProvider(api_key=api_key, location=location)
        best_time, best_value, meta = await provider.get_best_time(session, data_start_at, data_end_at, runtime)

        return {
            "status": meta.get("status", "Error"),
            "source": "api",
            "best_start": best_time,
            "avg_intensity": best_value,
            "start": data_start_at,
            "end": data_end_at,
            "runtime_minutes": runtime,
            "location": location
        }

    hass.services.async_register(
        DOMAIN, "get_best_time_raw", handle_get_best_time_raw, schema=GET_SCHEMA, supports_response=True
    )
    hass.services.async_register(
        DOMAIN, "get_best_time_api", handle_get_best_time_api, schema=GET_SCHEMA, supports_response=True
    )

    # --- Sensor Update Services (set sensor.co2_intensity_forecast) ---

    async def handle_get_forecast_api(call: ServiceCall):
        data_start_at = call.data.get("dataStartAt")
        data_end_at = call.data.get("dataEndAt")
        runtime = int(call.data.get("expectedRuntime", 60))
        allowed_hours = parse_hours(call.data.get("allowedHours"))

        provider = BluehandsProvider(api_key=api_key, location=location)
        best_time, best_value, meta = await provider.get_best_time(session, data_start_at, data_end_at, runtime)

        attrs = {
            "status": meta.get("status", "Unknown"),
            "source": "api",
            "optimal_co2": best_value if best_value is not None else "NA",
            "expectedRuntime": runtime,
            "start": data_start_at,
            "end": data_end_at,
            "location": location
        }
        update_forecast_sensor(hass, best_time, attrs)
        return best_time, best_value

    async def handle_get_forecast_raw(call: ServiceCall):
        data_start_at = call.data.get("dataStartAt")
        data_end_at = call.data.get("dataEndAt")
        runtime = int(call.data.get("expectedRuntime", 60))
        allowed_hours = parse_hours(call.data.get("allowedHours"))

        ws = dt_util.parse_datetime(data_start_at)
        we = dt_util.parse_datetime(data_end_at)
        if ws is None or we is None:
            update_forecast_sensor(hass, None, {
                "status": "Error",
                "source": "raw",
                "expectedRuntime": runtime,
                "start": data_start_at,
                "end": data_end_at,
                "location": location
            })
            return None, None
        ws_utc = dt_util.as_utc(ws)
        we_utc = dt_util.as_utc(we)

        provider = FraunhoferEnergyChartsProvider(country=location)
        raw_json = await provider.fetch_co2eq_series(session)
        if raw_json is None:
            _LOGGER.warning("Raw data unavailable, falling back to Bluehands API")
            api_provider = BluehandsProvider(api_key=api_key, location=location)
            best_time, best_value, meta = await api_provider.get_best_time(session, data_start_at, data_end_at, runtime)
            attrs = {
                "status": meta.get("status", "Error"),
                "source": "raw_fallback_api",
                "optimal_co2": best_value if best_value is not None else "NA",
                "expectedRuntime": runtime,
                "start": data_start_at,
                "end": data_end_at,
                "location": location,
                "fallback": "api"
            }
            update_forecast_sensor(hass, best_time, attrs)
            return best_time, best_value

        points = parse_energycharts_series(raw_json)
        result = best_start_from_points(points, ws_utc, we_utc, runtime, allowed_hours=allowed_hours)
        status = result.get("status")
        if status != "OK":
            _LOGGER.warning("RAW result status=%s, falling back to Bluehands API", status)
            api_provider = BluehandsProvider(api_key=api_key, location=location)
            best_time, best_value, meta = await api_provider.get_best_time(session, data_start_at, data_end_at, runtime)
            attrs = {
                "status": meta.get("status", status),
                "source": "raw_fallback_api",
                "optimal_co2": best_value if best_value is not None else "NA",
                "expectedRuntime": runtime,
                "start": data_start_at,
                "end": data_end_at,
                "location": location,
                "fallback": "api"
            }
            update_forecast_sensor(hass, best_time, attrs)
            return best_time, best_value

        best_start_utc = result["best_start_time_utc"]
        expected_avg_intensity = result["expected_avg_intensity"]
        attrs = {
            "status": "OK",
            "source": "raw",
            "expected_avg_intensity": expected_avg_intensity,
            "expectedRuntime": runtime,
            "start": data_start_at,
            "end": data_end_at,
            "location": location
        }
        update_forecast_sensor(hass, best_start_utc, attrs)
        return best_start_utc, expected_avg_intensity

    hass.services.async_register(DOMAIN, "get_co2_intensity_forecast_api", handle_get_forecast_api, schema=GET_SCHEMA)
    hass.services.async_register(DOMAIN, "get_co2_intensity_forecast_raw", handle_get_forecast_raw, schema=GET_SCHEMA)
    # Alias for compatibility
    hass.services.async_register(DOMAIN, "get_co2_intensity_forecast", handle_get_forecast_api, schema=GET_SCHEMA)

    # --- Non-blocking Delay Services (schedule instead of wait) ---

    async def handle_delay_api(call: ServiceCall):
        best_time, _ = await handle_get_forecast_api(call)
        if best_time:
            await schedule_execution(
                hass,
                best_time,
                call.data.get("targetService"),
                call.data.get("targetData")
            )
        else:
            _LOGGER.warning("No optimal time available (API), task not scheduled.")

    async def handle_delay_raw(call: ServiceCall):
        # Calculate via RAW (with internal fallback), update sensor, schedule execution
        best_time, _ = await handle_get_forecast_raw(call)
        if best_time:
            await schedule_execution(
                hass,
                best_time,
                call.data.get("targetService"),
                call.data.get("targetData")
            )
        else:
            _LOGGER.warning("No optimal time available (RAW), trying API as last resort")
            best_time_api, _ = await handle_get_forecast_api(call)
            if best_time_api:
                await schedule_execution(
                    hass,
                    best_time_api,
                    call.data.get("targetService"),
                    call.data.get("targetData")
                )
            else:
                _LOGGER.error("No optimal time available at all; task not scheduled.")

    hass.services.async_register(DOMAIN, "delay_execution_by_co2_forecast_api", handle_delay_api, schema=DELAY_SCHEMA)
    hass.services.async_register(DOMAIN, "delay_execution_by_co2_forecast_raw", handle_delay_raw, schema=DELAY_SCHEMA)

    # Discovery of the sensor platform
    hass.async_create_task(
        discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )

    return True
