import datetime
import logging
from typing import List, Optional

import httpx
import stamina

from app.settings.integration import ECOWITT_API_BASE_URL

logger = logging.getLogger(__name__)

# Ask for every category so newer sensors (e.g. the WS90's piezo rain gauge,
# reported under "rainfall_piezo") are included without listing them here.
CALLBACK_CATEGORIES = "all"


def _safe_float(value, default=None):
    """Safely convert a value to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _safe_int(value, default=None):
    """Safely convert a value to int."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _reading(data: dict, path: str) -> Optional[dict]:
    """Return the {value, unit, time} reading at a dotted path, if present."""
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current if isinstance(current, dict) and "value" in current else None


def _normalized_unit(reading: dict) -> str:
    return (reading.get("unit") or "").strip().lower().replace("º", "").replace("°", "")


def _temperature_c(reading: Optional[dict]) -> Optional[float]:
    if reading is None or (value := _safe_float(reading["value"])) is None:
        return None
    if "f" in _normalized_unit(reading):
        value = (value - 32) * 5 / 9
    return round(value, 1)


def _wind_kmh(reading: Optional[dict]) -> Optional[float]:
    if reading is None or (value := _safe_float(reading["value"])) is None:
        return None
    unit = _normalized_unit(reading)
    if "mph" in unit:
        value *= 1.609344
    elif "m/s" in unit:
        value *= 3.6
    elif "knot" in unit:
        value *= 1.852
    return round(value, 1)


def _rain_mm(reading: Optional[dict]) -> Optional[float]:
    if reading is None or (value := _safe_float(reading["value"])) is None:
        return None
    if "in" in _normalized_unit(reading):
        value *= 25.4
    return round(value, 1)


def _pressure_hpa(reading: Optional[dict]) -> Optional[float]:
    if reading is None or (value := _safe_float(reading["value"])) is None:
        return None
    if "inhg" in _normalized_unit(reading):
        value *= 33.8639
    return round(value, 1)


def _first(*values):
    return next((v for v in values if v is not None), None)


def _battery(data: dict) -> Optional[dict]:
    """Pass battery readings through as "<value> <unit>" strings.

    Sensors report battery differently (a 0/1 flag, a 1-5 level or a voltage),
    so they are recorded as-is rather than interpreted.
    """
    battery = data.get("battery")
    if not isinstance(battery, dict):
        return None
    readings = {}
    for name, reading in battery.items():
        if isinstance(reading, dict) and "value" in reading:
            unit = (reading.get("unit") or "").strip()
            readings[name] = f"{reading['value']} {unit}".strip()
    return readings or None


def parse_ecowitt_data(data: dict) -> dict:
    """
    Normalize the `data` of an Ecowitt Cloud API v3 real_time response to
    metric weather fields, converting each reading according to its unit.
    """
    parsed = {
        "temperature_c": _temperature_c(_reading(data, "outdoor.temperature")),
        "feels_like_c": _temperature_c(_reading(data, "outdoor.feels_like")),
        "dew_point_c": _temperature_c(_reading(data, "outdoor.dew_point")),
        "humidity_pct": _safe_int((_reading(data, "outdoor.humidity") or {}).get("value")),
        "indoor_temperature_c": _temperature_c(_reading(data, "indoor.temperature")),
        "indoor_humidity_pct": _safe_int((_reading(data, "indoor.humidity") or {}).get("value")),
        "pressure_hpa": _first(
            _pressure_hpa(_reading(data, "pressure.relative")),
            _pressure_hpa(_reading(data, "pressure.absolute")),
        ),
        "wind_speed_kmh": _wind_kmh(_reading(data, "wind.wind_speed")),
        "wind_gust_kmh": _wind_kmh(_reading(data, "wind.wind_gust")),
        "wind_direction_deg": _safe_int((_reading(data, "wind.wind_direction") or {}).get("value")),
        "solar_radiation_wm2": _safe_float((_reading(data, "solar_and_uvi.solar") or {}).get("value")),
        "uv_index": _safe_int((_reading(data, "solar_and_uvi.uvi") or {}).get("value")),
        "battery": _battery(data),
    }
    # Stations with a piezo rain gauge (e.g. WS90) report it under
    # "rainfall_piezo"; fall back to the traditional tipping-bucket gauge.
    for field, key in (("rain_rate_mm_hr", "rain_rate"), ("rain_daily_mm", "daily"), ("rain_event_mm", "event")):
        parsed[field] = _first(
            _rain_mm(_reading(data, f"rainfall_piezo.{key}")),
            _rain_mm(_reading(data, f"rainfall.{key}")),
        )

    # Remove None values
    return {k: v for k, v in parsed.items() if v is not None}


def _parse_reading_time(value) -> Optional[datetime.datetime]:
    """Parse a reading's time: epoch seconds, or an ISO timestamp with an offset.

    Timestamps without a timezone are ignored, since the station's local
    timezone is unknown.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return datetime.datetime.fromtimestamp(int(text), tz=datetime.timezone.utc)
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def latest_reading_time(data: dict) -> Optional[datetime.datetime]:
    """Return the newest per-reading `time` in a real_time response's `data`."""
    times = []

    def collect(node):
        for key, value in node.items():
            if isinstance(value, dict):
                collect(value)
            elif key == "time" and (parsed := _parse_reading_time(value)):
                times.append(parsed)

    collect(data)
    return max(times, default=None)


def build_observation(
    parsed_data: dict,
    source_id: str,
    source_name: str,
    subject_type: str,
    lat: float,
    lon: float,
    recorded_at: str,
) -> dict:
    """Build a Gundi observation dict from parsed weather data."""
    return {
        "source": source_id,
        "source_name": source_name,
        "type": "weather-station",
        "subject_type": subject_type,
        "recorded_at": recorded_at,
        "location": {"lat": lat, "lon": lon},
        "additional": parsed_data,
    }


def check_alert_conditions(
    parsed_data: dict,
    source_id: str,
    lat: float,
    lon: float,
    recorded_at: str,
    high_wind_speed_kmh: float = 80.0,
    heavy_rain_mm_hr: float = 50.0,
    extreme_temp_high_c: float = 45.0,
    extreme_temp_low_c: float = -20.0,
) -> List[dict]:
    """Check weather data against alert thresholds and return event dicts."""
    events = []
    location = {"lat": lat, "lon": lon}

    def alert(title, alert_type, current_value, threshold):
        events.append({
            "title": title,
            "event_type": "ecowitt_alert",
            "recorded_at": recorded_at,
            "location": location,
            "event_details": {
                "alert_type": alert_type,
                "current_value": current_value,
                "threshold": threshold,
                "source_id": source_id,
            },
        })

    wind = parsed_data.get("wind_speed_kmh")
    if wind is not None and wind >= high_wind_speed_kmh:
        alert(f"High Wind Speed - {wind} km/h", "high_wind_speed", wind, high_wind_speed_kmh)

    rain = parsed_data.get("rain_rate_mm_hr")
    if rain is not None and rain >= heavy_rain_mm_hr:
        alert(f"Heavy Rainfall - {rain} mm/hr", "heavy_rain", rain, heavy_rain_mm_hr)

    temp = parsed_data.get("temperature_c")
    if temp is not None and temp >= extreme_temp_high_c:
        alert(f"Extreme High Temperature - {temp} C", "extreme_temperature_high", temp, extreme_temp_high_c)
    if temp is not None and temp <= extreme_temp_low_c:
        alert(f"Extreme Low Temperature - {temp} C", "extreme_temperature_low", temp, extreme_temp_low_c)

    return events


async def validate_ecowitt_credentials(application_key: str, api_key: str) -> bool:
    """
    Validate Ecowitt API credentials by calling the real_time endpoint with a placeholder MAC.
    Returns True if the API accepts the keys (code 0 or non-auth error), False otherwise.
    """
    url = f"{ECOWITT_API_BASE_URL}/api/v3/device/real_time"
    params = {
        "application_key": application_key,
        "api_key": api_key,
        "mac": "00:00:00:00:00:00",
        "call_back": CALLBACK_CATEGORIES,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)
            if response.status_code in (401, 403):
                return False
            result = response.json()
            code = result.get("code", -1)
            msg = (result.get("msg") or "").lower()
            if code == 0:
                return True
            # Auth-related error messages indicate invalid credentials
            if any(term in msg for term in ("key", "auth", "invalid", "unauthorized", "forbidden")):
                return False
            # Other errors (e.g. device not found) mean the request reached the API with valid keys
            return True
    except httpx.HTTPError:
        return False


def _is_retryable(exc: Exception) -> bool:
    """Retry network failures, rate limiting and server errors, not client errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


# Bounded so a failing station can't hold up the other stations in a run;
# the next scheduled pull is the longer-term retry.
@stamina.retry(on=_is_retryable, attempts=3, timeout=60.0, wait_initial=2.0, wait_max=10.0)
async def fetch_realtime_data(application_key: str, api_key: str, mac: str) -> dict:
    """Fetch real-time data from Ecowitt Cloud API v3."""
    url = f"{ECOWITT_API_BASE_URL}/api/v3/device/real_time"
    params = {
        "application_key": application_key,
        "api_key": api_key,
        "mac": mac,
        "call_back": CALLBACK_CATEGORIES,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            raise ValueError(f"Ecowitt API error: {result.get('msg', 'Unknown error')} (code: {result.get('code')})")
        return result.get("data", {})
