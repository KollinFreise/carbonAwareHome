# custom_components/carbonAwareHome/provider.py
from typing import Dict, Any, Optional
from datetime import datetime
import requests

class IForecastProvider:
    def get_best_time(self, window_start: datetime, window_end: datetime, runtime_minutes: int) -> Dict[str, Any]:
        raise NotImplementedError

class IRawTimeseriesProvider:
    def fetch_timeseries(self) -> Dict[str, Any]:
        raise NotImplementedError

class IIntensityProvider:
    def get_current_intensity(self) -> Optional[float]:
        raise NotImplementedError

class BluehandsProvider(IForecastProvider, IIntensityProvider):
    # Endpoints are hardcoded internally (not configurable)
    FORECAST_URL = "https://forecast.carbon-aware-computing.com"
    INTENSITY_URL = "https://intensity.carbon-aware-computing.com"
    def __init__(self, api_key: str, location: str):
        self.api_key = api_key
        self.location = location

    def get_best_time(self, window_start: datetime, window_end: datetime, runtime_minutes: int) -> Dict[str, Any]:
        # Calls the forecast API, uses "optimalDataPoints"
        # See README including Swagger-UI
        # [Bluehands/Carbon-Aware-Computing]
        raise NotImplementedError

    def get_current_intensity(self) -> Optional[float]:
        # Calls the intensity API
        # [Bluehands/Carbon-Aware-Computing]
        raise NotImplementedError

class FraunhoferEnergyChartsProvider(IRawTimeseriesProvider):
    # Endpoint is hardcoded internally (not configurable)
    CO2EQ_URL = "https://api.energy-charts.info/co2eq"
    def __init__(self, country: str):
        self.country = country  # Mapping from HA-Location -> EC country code is done internally

    def fetch_timeseries(self) -> Dict[str, Any]:
        # Returns JSON with unix_seconds, co2eq, co2eq_forecast
        # Energy-Charts API overview and field types: https://api.energy-charts.info/ (unix seconds etc.)
        url = f"{self.CO2EQ_URL}?country={self.country}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return resp.json()
