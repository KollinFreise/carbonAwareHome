import logging
from typing import Any, Dict, Optional, Tuple
from bisect import bisect_right

from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class CO2CurrentSensor(Entity):
    """CO2 Current Sensor reading robustly from cached Energy-Charts series."""

    def __init__(self, hass):
        self.hass = hass
        self._state: Optional[float] = None
        self._timestamp: Optional[str] = None  # ISO8601 (UTC)
        self._status: str = "OK"
        self._attrs: Dict[str, Any] = {}
        self._source: Optional[str] = None  # 'actual' | 'forecast' | None

    async def async_update(self):
        _LOGGER.info("async_update called for sensor.current_co2_intensity")
        """Fetch a robust current state from cache with sensible fallbacks."""
        cache = self.hass.data.get(DOMAIN, {}).get("energy_charts_cache", {})
        data = cache.get("data")
        cache_ts = cache.get("timestamp")

        if not data or not isinstance(data, dict):
            self._set_no_data("API not reachable or no data in cache")
            _LOGGER.info(
                "Sensor updated: no cache data available (status=%s)", self._status
            )
            return

        unix_seconds = data.get("unix_seconds") or []
        co2_actual = data.get("co2eq") or []
        co2_forecast = data.get("co2eq_forecast") or []

        # Current UTC time and epoch seconds
        now_utc = dt_util.utcnow()
        now_ts = int(now_utc.timestamp())

        # Robust: get last valid value (prefer actual, else forecast)
        value, ts_used, source, idx_used = self._pick_best_value(
            unix_seconds, co2_actual, co2_forecast, now_ts
        )

        if value is None or ts_used is None:
            self._set_no_data("No usable CO2 data (actual/forecast empty)")
            _LOGGER.warning(
                "Sensor updated: no usable value found (actual/forecast empty or mismatched)"
            )
            return

        # Set state and timestamp
        from datetime import datetime, timezone

        # Round value to 2 decimal places
        self._state = round(float(value), 2)
        self._timestamp = datetime.fromtimestamp(ts_used, tz=timezone.utc).isoformat()
        self._source = source
        self._status = "OK"

        # Additional attributes
        self._attrs = {
            "last_update": self._timestamp,  # Data timestamp (UTC)
            "status": self._status,
            "source": self._source,  # 'actual' or 'forecast'
            "index_used": idx_used,
            "series_length": len(unix_seconds),
            "series_step_seconds": (
                unix_seconds[1] - unix_seconds[0] if len(unix_seconds) > 1 else None
            ),
            "cache_timestamp": cache_ts.isoformat() if cache_ts else None,
            "cache_age_minutes": (
                round((now_utc - cache_ts).total_seconds() / 60, 1) if cache_ts else None
            ),
            "deprecated": data.get("deprecated", False),
            "updated_at_local": dt_util.as_local(now_utc).isoformat(),
        }

        _LOGGER.info(
            "Sensor updated: value=%s %s, time=%s (source=%s, idx=%s, status=%s)",
            self._state,
            self.unit_of_measurement,
            self._timestamp,
            self._source,
            idx_used,
            self._status,
        )

    def _pick_best_value(
            self,
            unix_seconds: list,
            co2_actual: list,
            co2_forecast: list,
            now_ts: int,
    ) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[int]]:
        n = len(unix_seconds)
        if n == 0:
            return None, None, None, None

        # Interpolation: find i0, i1 so that unix_seconds[i0] <= now_ts < unix_seconds[i1]
        for i in range(n - 1):
            t0, t1 = unix_seconds[i], unix_seconds[i + 1]
            v0 = co2_actual[i] if i < len(co2_actual) and co2_actual[i] is not None else co2_forecast[i] if i < len(co2_forecast) and co2_forecast[i] is not None else None
            v1 = co2_actual[i + 1] if i + 1 < len(co2_actual) and co2_actual[i + 1] is not None else co2_forecast[i + 1] if i + 1 < len(co2_forecast) and co2_forecast[i + 1] is not None else None
            if v0 is not None and v1 is not None and t0 <= now_ts < t1:
                # Linear interpolation
                f = (now_ts - t0) / (t1 - t0)
                interpolated = v0 + (v1 - v0) * f
                return round(interpolated, 2), now_ts, "interpolated", i  # Wert runden

        # Fallback: wie bisher
        # Finde den Index des letzten Zeitpunkts <= now
        i = bisect_right(unix_seconds, now_ts) - 1

        def actual_at(idx: int) -> Optional[float]:
            return round(float(co2_actual[idx]), 2) if 0 <= idx < len(co2_actual) and co2_actual[idx] is not None else None

        def forecast_at(idx: int) -> Optional[float]:
            return round(float(co2_forecast[idx]), 2) if 0 <= idx < len(co2_forecast) and co2_forecast[idx] is not None else None

        # 1) Suche rückwärts bis 'now' einen gültigen Actual-Wert
        if i >= 0:
            for j in range(i, -1, -1):
                val = actual_at(j)
                if val is not None:
                    return val, unix_seconds[j], "actual", j

        # 2) Suche vorwärts ab max(i, 0) einen Forecast-Wert
        start_fwd = max(i, 0)
        for j in range(start_fwd, n):
            val = forecast_at(j)
            if val is not None:
                return val, unix_seconds[j], "forecast", j

        # 3) Wenn 'now' vor Beginn (i < 0), nimm ersten Forecast-Wert überhaupt
        if i < 0:
            for j in range(0, n):
                val = forecast_at(j)
                if val is not None:
                    return val, unix_seconds[j], "forecast", j

        # 4) Wenn 'now' nach Ende (i >= n-1), nimm den letzten verfügbaren Wert (Actual bevorzugt)
        for j in range(n - 1, -1, -1):
            val = actual_at(j)
            if val is not None:
                return val, unix_seconds[j], "actual", j
            val_f = forecast_at(j)
            if val_f is not None:
                return val_f, unix_seconds[j], "forecast", j

        # Kein verwertbarer Wert gefunden
        return None, None, None, None

    def _set_no_data(self, status: str):
        self._state = None
        self._timestamp = None
        self._source = None
        self._status = status
        self._attrs = {
            "last_update": None,
            "status": self._status,
            "source": self._source,
            "updated_at_local": dt_util.as_local(dt_util.utcnow()).isoformat(),
        }

    @property
    def name(self):
        return "Current CO2 Intensity"

    @property
    def unique_id(self):
        return f"{DOMAIN}_current_co2_intensity"

    @property
    def state(self):
        return self._state

    @property
    def unit_of_measurement(self):
        return "gCO2eq/kWh"

    @property
    def extra_state_attributes(self):
        return self._attrs

    @property
    def icon(self):
        return "mdi:molecule-co2"

    @property
    def available(self) -> bool:
        # Entity gilt als verfügbar, wenn wir Daten zuweisen konnten
        return self._state is not None and self._status == "OK"

    @property
    def should_poll(self) -> bool:
        return False


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the CO2 sensor platform."""
    _LOGGER.info("Starte Registrierung des CO2 Sensors (Current CO2 Intensity)")
    async_add_entities([CO2CurrentSensor(hass)], True)
