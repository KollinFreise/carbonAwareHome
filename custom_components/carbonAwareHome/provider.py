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

class FraunhoferEnergyChartsProvider(IRawTimeseriesProvider):
    # Endpoint is hardcoded internally (not configurable)
    CO2EQ_URL = "https://api.energy-charts.info/co2eq"
    def __init__(self, country: str):

    def fetch_timeseries(self) -> Dict[str, Any]:
        # Returns JSON with unix_seconds, co2eq, co2eq_forecast
        # Energy-Charts API overview and field types: https://api.energy-charts.info/ (unix seconds etc.)
        url = f"{self.CO2EQ_URL}?country={self.country}"
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return resp.json()
