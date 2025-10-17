from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

class TimePoint:
    def __init__(self, ts: datetime, intensity: float):
        self.ts = ts
        self.intensity = intensity

def parse_energycharts_series(json_data: Dict[str, Any]) -> List[TimePoint]:
    ts_list = json_data.get("unix_seconds", [])     # Unix seconds [https://api.energy-charts.info/]
    actual   = json_data.get("co2eq", [])
    forecast = json_data.get("co2eq_forecast", [])

    points: List[TimePoint] = []
    for i, ts_sec in enumerate(ts_list):
        ts = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        val = None
        if i < len(actual)   and actual[i]   is not None: val = float(actual[i])
        elif i < len(forecast) and forecast[i] is not None: val = float(forecast[i])
        if val is not None:
            points.append(TimePoint(ts, val))
    points.sort(key=lambda p: p.ts)
    return points

def best_start_from_points(points: List[TimePoint],
                           window_start: datetime,
                           window_end: datetime,
                           runtime_minutes: int,
                           allowed_hours: Optional[tuple] = None,
                           min_gain: Optional[float] = None,
                           now_intensity: Optional[float] = None) -> Dict[str, Any]:
    runtime = timedelta(minutes=runtime_minutes)

    def within_hours(ts: datetime, hours: Optional[tuple]) -> bool:
        if not hours: return True
        start_h, end_h = hours
        return start_h <= ts.hour < end_h

    candidates = [p for p in points if window_start <= p.ts < window_end]
    if not candidates:
        return {"status": "No Data"}

    best_avg, best_start = None, None
    for i, p in enumerate(candidates):
        t0 = p.ts
        if t0 + runtime > window_end: break
        if not within_hours(t0, allowed_hours): continue
        vals = [q.intensity for q in candidates[i:] if q.ts < t0 + runtime]
        if not vals: continue
        avg = sum(vals) / len(vals)
        if best_avg is None or avg < best_avg:
            best_avg, best_start = avg, t0

    if best_start is None:
        return {"status": "No Candidate"}
    if min_gain is not None and now_intensity is not None:
        if (now_intensity - best_avg) < min_gain:
            return {"status": "Gain Too Low", "best_start_time": best_start.isoformat(), "expected_avg_intensity": best_avg}

    return {
        "status": "OK",
        "best_start_time": best_start.isoformat(),
        "best_end_time": (best_start + runtime).isoformat(),
        "expected_avg_intensity": best_avg
    }
