import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Tuple

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import discovery
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CONF_LOCATION, REFRESH_INTERVAL_MINUTES
from .engine import parse_energycharts_series, best_start_from_points
from .provider import FraunhoferEnergyChartsProvider

_LOGGER = logging.getLogger(__name__)

CACHE_MAX_AGE_SECONDS = 3600
API_TIMEOUT_SECONDS = 60
API_BACKOFF_SECONDS = (5, 10, 15)

GET_SCHEMA = vol.Schema({
    vol.Required("dataStartAt"): cv.string,
    vol.Required("dataEndAt"): cv.string,
    vol.Required("expectedRuntime"): cv.positive_int,
    vol.Optional("allowedHours"): cv.string,
})

# Hilfsfunktion für allowedHours (Format "8-21")
def parse_hours(hours_str: Optional[str]) -> Optional[Tuple[int, int]]:
    if not hours_str:
        return None
    try:
        s, e = hours_str.split("-")
        start_h, end_h = int(s), int(e)
        if 0 <= start_h < 24 and 0 <= end_h < 24 and start_h < end_h:
            return start_h, end_h
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Ungültiges allowedHours Format '%s' – erwartet z.B. '8-21'", hours_str)
    return None

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    _LOGGER.info("Carbon Aware Home: __init__ geladen")

    conf = config.get(DOMAIN)
    if conf is None:
        hass.async_create_task(discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config))
        return True

    location = conf.get(CONF_LOCATION, "de")
    refresh_cfg = conf.get("refresh_interval_minutes")
    try:
        refresh_interval_minutes = int(refresh_cfg) if refresh_cfg is not None else REFRESH_INTERVAL_MINUTES
        if refresh_interval_minutes <= 0:
            raise ValueError
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Ungültiger refresh_interval_minutes '%s' – verwende Default %d", refresh_cfg, REFRESH_INTERVAL_MINUTES)
        refresh_interval_minutes = REFRESH_INTERVAL_MINUTES

    hass.data[DOMAIN] = {
        CONF_LOCATION: location,
        "refresh_interval_minutes": refresh_interval_minutes,
    }

    async def refresh_energy_charts_cache(_now: Optional[datetime] = None) -> None:
        session = async_get_clientsession(hass)
        provider = FraunhoferEnergyChartsProvider(country=location, hass=hass)
        data = await provider.fetch_co2eq_series(session)
        if data is not None:
            _LOGGER.debug("Energy-Charts Cache aktualisiert: Keys=%s", list(data.keys()))
            await hass.services.async_call(
                "homeassistant",
                "update_entity",
                {"entity_id": "sensor.current_co2_intensity"},
                blocking=False,
            )
        else:
            _LOGGER.debug("Energy-Charts Cache konnte nicht aktualisiert werden (keine Daten)")
        next_run = dt_util.utcnow() + timedelta(minutes=hass.data[DOMAIN]["refresh_interval_minutes"])
        async_track_point_in_time(hass, refresh_energy_charts_cache, next_run)

    hass.async_create_task(refresh_energy_charts_cache())

    async def _update_sensor_every_minute(_now: datetime) -> None:
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": "sensor.current_co2_intensity"},
            blocking=False,
        )

    async_track_time_interval(hass, _update_sensor_every_minute, timedelta(minutes=1))
    _LOGGER.debug("Minütliches Sensor-Update geplant (refresh_interval_minutes=%d)", hass.data[DOMAIN]["refresh_interval_minutes"])

    async def handle_carbon_aware_best_time(call: ServiceCall) -> Dict[str, Any]:
        session = async_get_clientsession(hass)
        data_start_at = call.data.get("dataStartAt")
        data_end_at = call.data.get("dataEndAt")
        runtime_raw = call.data.get("expectedRuntime", 60)
        hours_str = call.data.get("allowedHours")
        allowed_hours = parse_hours(hours_str)

        if not isinstance(data_start_at, str) or not isinstance(data_end_at, str):
            return {"status": "MissingParam"}
        try:
            runtime = int(runtime_raw)
        except Exception:  # noqa: BLE001
            runtime = 60

        ws = dt_util.parse_datetime(data_start_at)
        we = dt_util.parse_datetime(data_end_at)
        if ws is None or we is None:
            return {"status": "InvalidDatetime"}
        ws_utc = dt_util.as_utc(ws)
        we_utc = dt_util.as_utc(we)
        if ws_utc >= we_utc:
            return {"status": "InvalidWindow"}

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
                "location": location,
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
                "location": location,
            }

        return {
            "status": "OK",
            "source": "raw",
            "best_start": result["best_start_time"],
            "avg_intensity": round(result["expected_avg_intensity"], 2),
            "start": data_start_at,
            "end": data_end_at,
            "runtime_minutes": runtime,
            "location": location,
        }

    # Neuer Service-Name
    hass.services.async_register(
        DOMAIN,
        "carbon_aware_best_time",
        handle_carbon_aware_best_time,
        schema=GET_SCHEMA,
        supports_response=True,
    )

    # Abwärtskompatibler Alias (Deprecated)
    async def _deprecated_get_best_time_raw(call: ServiceCall) -> Dict[str, Any]:
        _LOGGER.warning("Service get_best_time_raw ist veraltet – bitte carbon_aware_best_time verwenden.")
        return await handle_carbon_aware_best_time(call)

    hass.services.async_register(
        DOMAIN,
        "get_best_time_raw",
        _deprecated_get_best_time_raw,
        schema=GET_SCHEMA,
        supports_response=True,
    )

    hass.async_create_task(discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config))
    return True
