import aiohttp
import async_timeout
import logging
from homeassistant.helpers.entity import Entity
from .const import DOMAIN, CONF_API_KEY, CONF_LOCATION

_LOGGER = logging.getLogger(__name__)

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the CO2 sensor."""
    if discovery_info is None:
        return

    api_key = hass.data[DOMAIN][CONF_API_KEY]
    location = hass.data[DOMAIN][CONF_LOCATION]
    async_add_entities([CO2CurrentSensor(api_key, location)], True)

class CO2CurrentSensor(Entity):
    """Representation of a CO2 Current Sensor."""

    def __init__(self, api_key, location):
        self._api_key = api_key
        self._location = location
        self._state = None
        self._timestamp = None

    async def async_update(self):
        """Fetch new state data for the sensor."""
        _LOGGER.info("Start fetch new stat data")
        url = f"https://intensity.carbon-aware-computing.com/emissions/current?location={self._location}"
        headers = {"accept": "application/json", "x-api-key": self._api_key}

        async with aiohttp.ClientSession() as session:
            try:
                async with async_timeout.timeout(10):
                    _LOGGER.info("Timeout set")

                    async with session.get(url, headers=headers) as response:
                        _LOGGER.info("Start fetching new stat data: %s", response)

                        if response.status == 200:
                            data = await response.json()
                            self._state = data.get("value", "N/A")
                            self._timestamp = data.get("time", None)
                            _LOGGER.info("Fetched CO2 data: %s at %s", self._state, self._timestamp)
                        else:
                            _LOGGER.error("Error fetching current CO2 data: %s - %s", response.status, await response.text())
            except asyncio.TimeoutError:
                _LOGGER.error("Timeout while fetching current CO2 data")

    @property
    def name(self):
        return "Current CO2 Intensity"

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return "gCO2eq/kWh"

    @property
    def extra_state_attributes(self):
        return {"last_update": self._timestamp}

    @property
    def icon(self):
        return "mdi:molecule-co2"
