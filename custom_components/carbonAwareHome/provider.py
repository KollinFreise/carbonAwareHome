# custom_components/carbonAwareHome/provider.py
from __future__ import annotations

from typing import Dict, Any, Optional
from datetime import datetime, timezone
import asyncio
import logging
import async_timeout
from aiohttp import ClientSession
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CACHE_MAX_AGE_SECONDS = 3600
API_TIMEOUT_SECONDS = 60
API_BACKOFF_SECONDS = (5, 10, 15)

# Vereinfachte Interface Platzhalter (verhindert alte fehlerhafte Reste)
class IForecastProvider:
    def get_best_time(self, window_start: datetime, window_end: datetime, runtime_minutes: int) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

class IRawTimeseriesProvider:
    async def fetch_co2eq_series(self, session: ClientSession) -> Optional[Dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

class IIntensityProvider:
    async def get_current_intensity(self) -> Optional[float]:  # pragma: no cover
        raise NotImplementedError

class FraunhoferEnergyChartsProvider(IRawTimeseriesProvider):
    """Asynchroner Provider für CO2-Daten (Fraunhofer Energy Charts)."""
    CO2EQ_URL = "https://api.energy-charts.info/co2eq"

    def __init__(self, country: str, hass: Optional[HomeAssistant] = None):
        self.country = country
        self.hass = hass

    async def fetch_co2eq_series(self, session: ClientSession) -> Optional[Dict[str, Any]]:
        """Lädt JSON mit unix_seconds, co2eq, co2eq_forecast. Nutzt Cache wenn frisch."""
        cache: Optional[Dict[str, Any]] = None
        cache_time: Optional[datetime] = None
        if self.hass:
            cache_data = self.hass.data.get(DOMAIN, {}).get("energy_charts_cache", {})
            cache = cache_data.get("data")
            cache_time = cache_data.get("timestamp")
        now = datetime.now(timezone.utc)
        if cache and cache_time and (now - cache_time).total_seconds() < CACHE_MAX_AGE_SECONDS:
            return cache

        url = f"{self.CO2EQ_URL}?country={self.country}"
        for attempt, backoff in enumerate(list(API_BACKOFF_SECONDS) + [None], start=1):
            try:
                _LOGGER.debug("Energy-Charts API Call: %s (Versuch %d)", url, attempt)
                async with async_timeout.timeout(API_TIMEOUT_SECONDS):
                    async with session.get(url, headers={"accept": "application/json"}) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            _LOGGER.warning("Energy-Charts Fehler (Versuch %d): %s - %s", attempt, resp.status, body)
                            if 400 <= resp.status < 500:
                                return None
                        else:
                            data = await resp.json()
                            if self.hass:
                                self.hass.data.setdefault(DOMAIN, {})["energy_charts_cache"] = {
                                    "data": data,
                                    "timestamp": now,
                                }
                            return data
            except asyncio.TimeoutError:
                _LOGGER.warning("Timeout beim Abruf der Energy-Charts API (Versuch %d)", attempt)
            except Exception as e:  # noqa: BLE001
                _LOGGER.exception("Unerwarteter Fehler beim Abruf der Energy-Charts API (Versuch %d): %s", attempt, e)
            if backoff is not None:
                await asyncio.sleep(backoff)
        return None
