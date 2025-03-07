import logging
import aiohttp
import urllib.parse
import async_timeout
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import discovery
from homeassistant.util.dt import now as hass_now, as_local
from .const import DOMAIN, CONF_API_KEY, CONF_LOCATION
import asyncio
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

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

    async def delay_until_optimal_time(optimal_time_str):
        """Delay task execution until the optimal time."""
        optimal_time = datetime.fromisoformat(optimal_time_str)
        
        # Wenn optimal_time keine Zeitzone hat, ergänze sie mit der Home Assistant Zeitzone
        if optimal_time.tzinfo is None:
            optimal_time = as_local(optimal_time)

        now = hass_now()

        if now < optimal_time:
            delay = (optimal_time - now).total_seconds()
            _LOGGER.info(f"Delaying task for {delay} seconds until optimal time.")
            await asyncio.sleep(delay)

        _LOGGER.info("Executing task now.")

    async def delay_execution_service(call: ServiceCall):
        """Service to delay execution until the optimal time."""
        try:
            data_start_at = call.data["dataStartAt"]
            data_end_at = call.data["dataEndAt"]
            window_size = call.data["expectedRuntime"]
        except KeyError as e:
            _LOGGER.error("Missing required parameter: %s", e)
            return

        location = hass.data[DOMAIN][CONF_LOCATION]
        api_key = hass.data[DOMAIN][CONF_API_KEY]
        best_time, _ = await handle_get_co2_forecast(call)

        if best_time:
            await delay_until_optimal_time(best_time)
        else:
            _LOGGER.warning("No optimal time available, task execution not delayed.")

    async def handle_get_co2_forecast(call: ServiceCall):
        """Handle the service call to get CO2 forecast."""
        try:
            data_start_at = call.data["dataStartAt"]
            data_end_at = call.data["dataEndAt"]
            window_size = call.data["expectedRuntime"]
        except KeyError as e:
            _LOGGER.error("Missing required parameter: %s", e)
            return None, None

        query_params = {
            "location": location,
            "dataStartAt": data_start_at,
            "dataEndAt": data_end_at,
            "windowSize": window_size
        }
        encoded_params = urllib.parse.urlencode(query_params)
        url = f"https://forecast.carbon-aware-computing.com/emissions/forecasts/current?{encoded_params}"

        async with aiohttp.ClientSession() as session:
            try:
                async with async_timeout.timeout(10):
                    async with session.get(url, headers={"accept": "application/json", "x-api-key": api_key}) as response:
                        if response.status == 200:
                            data = await response.json()
                            if data and len(data) > 0 and "optimalDataPoints" in data[0]:
                                best_point = data[0]["optimalDataPoints"][0]
                                best_time = best_point["timestamp"]
                                best_value = best_point["value"]

                                # Konvertiere den Zeitstempel in die lokale Zeit
                                local_best_time = as_local(datetime.fromisoformat(best_time))

                                # Setze den Sensor mit gültigen Daten
                                hass.states.async_set("sensor.co2_intensity_forecast", local_best_time.isoformat(), {
                                    "optimal_co2": best_value,
                                    "expectedRuntime": window_size,
                                    "start": data_start_at,
                                    "end": data_end_at,
                                    "location": location
                                })
                                return best_time, best_value
                            else:
                                hass.states.async_set("sensor.co2_intensity_forecast", "No Data", {
                                    "optimal_co2": "NA",
                                    "expectedRuntime": window_size,
                                    "start": data_start_at,
                                    "end": data_end_at,
                                    "location": location
                                })
                                _LOGGER.warning("No forecast data available")
                        else:
                            hass.states.async_set("sensor.co2_intensity_forecast", "Error", {
                                "optimal_co2": "NA",
                                "expectedRuntime": window_size,
                                "start": data_start_at,
                                "end": data_end_at,
                                "location": location
                            })
                            _LOGGER.error("Error fetching forecast: %s - %s", response, await response.text())

            except asyncio.TimeoutError:
                hass.states.async_set("sensor.co2_intensity_forecast", "Timeout", {
                    "optimal_co2": "NA",
                    "expectedRuntime": window_size,
                    "start": data_start_at,
                    "end": data_end_at,
                    "location": location
                })
                _LOGGER.error("Timeout fetching CO2 forecast")

        return None, None

    hass.services.async_register(DOMAIN, "get_co2_intensity_forecast", handle_get_co2_forecast)
    hass.services.async_register(DOMAIN, "delay_execution_by_co2_forecast", delay_execution_service)

    # Discovery of the sensor platform
    hass.async_create_task(
        discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )

    return True