import datetime

import httpx
import pytest
import stamina

from app.services.ecowitt import (
    EcowittHTTPError,
    build_observation,
    check_alert_conditions,
    fetch_realtime_data,
    latest_reading_time,
    parse_ecowitt_data,
    validate_ecowitt_credentials,
    _safe_float,
    _safe_int,
)


# --- Fixtures ---

# Epoch seconds for 2026-07-01T01:00:00Z and one minute later
T0 = "1782867600"
T1 = "1782867660"


@pytest.fixture
def ws90_realtime_data():
    """`data` of an Ecowitt v3 real_time response for a WS90 in imperial units."""
    return {
        "outdoor": {
            "temperature": {"time": T0, "unit": "ºF", "value": "86.0"},
            "feels_like": {"time": T0, "unit": "ºF", "value": "92.0"},
            "dew_point": {"time": T0, "unit": "ºF", "value": "78.0"},
            "humidity": {"time": T0, "unit": "%", "value": "81"},
        },
        "indoor": {
            "temperature": {"time": T0, "unit": "ºF", "value": "77.0"},
            "humidity": {"time": T0, "unit": "%", "value": "60"},
        },
        "solar_and_uvi": {
            "solar": {"time": T0, "unit": "W/m²", "value": "830.5"},
            "uvi": {"time": T0, "unit": "", "value": "6"},
        },
        "rainfall_piezo": {
            "rain_rate": {"time": T1, "unit": "in/hr", "value": "0.10"},
            "daily": {"time": T1, "unit": "in", "value": "0.50"},
            "event": {"time": T1, "unit": "in", "value": "0.30"},
        },
        "wind": {
            "wind_speed": {"time": T0, "unit": "mph", "value": "10.0"},
            "wind_gust": {"time": T0, "unit": "mph", "value": "20.0"},
            "wind_direction": {"time": T0, "unit": "º", "value": "185"},
        },
        "pressure": {
            "relative": {"time": T0, "unit": "inHg", "value": "29.92"},
            "absolute": {"time": T0, "unit": "inHg", "value": "29.80"},
        },
        "battery": {
            "ws90_battery": {"unit": "V", "value": "3.10"},
        },
    }


@pytest.fixture
def parsed_normal_data():
    return {
        "temperature_c": 25.3,
        "humidity_pct": 65,
        "wind_speed_kmh": 12.5,
        "rain_rate_mm_hr": 0.0,
    }


@pytest.fixture
def mock_ecowitt_http(mocker):
    """Route the module's httpx.AsyncClient through a MockTransport.

    Takes a list of httpx.Response objects returned in order and records the
    requests made, so tests exercise real status handling and retries.
    """
    real_client = httpx.AsyncClient
    requests = []

    def install(responses):
        remaining = list(responses)

        def handler(request):
            requests.append(request)
            return remaining.pop(0)

        mocker.patch(
            "app.services.ecowitt.httpx.AsyncClient",
            side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
        )
        return requests

    return install


@pytest.fixture
def stamina_retries():
    """Let stamina retry without sleeping, up to three attempts."""
    stamina.set_testing(True, attempts=3)
    yield
    stamina.set_testing(False)


# --- Tests for safe conversion helpers ---

class TestSafeConversions:
    def test_safe_float_valid(self):
        assert _safe_float("25.3") == 25.3
        assert _safe_float(10) == 10.0

    def test_safe_float_invalid(self):
        assert _safe_float(None) is None
        assert _safe_float("abc") is None

    def test_safe_int_valid(self):
        assert _safe_int("65") == 65
        assert _safe_int("6.0") == 6

    def test_safe_int_invalid(self):
        assert _safe_int(None) is None
        assert _safe_int("abc") is None


# --- Tests for parse_ecowitt_data ---

class TestParseEcowittData:
    def test_converts_fahrenheit_readings_to_celsius(self, ws90_realtime_data):
        result = parse_ecowitt_data(ws90_realtime_data)
        assert result["temperature_c"] == 30.0
        assert result["indoor_temperature_c"] == 25.0

    def test_keeps_celsius_readings(self):
        data = {"outdoor": {"temperature": {"unit": "℃", "value": "25.3"}}}
        assert parse_ecowitt_data(data)["temperature_c"] == 25.3

    def test_parses_dew_point_and_feels_like(self, ws90_realtime_data):
        result = parse_ecowitt_data(ws90_realtime_data)
        assert result["dew_point_c"] == 25.6
        assert result["feels_like_c"] == 33.3

    def test_converts_mph_wind_to_kmh(self, ws90_realtime_data):
        result = parse_ecowitt_data(ws90_realtime_data)
        assert result["wind_speed_kmh"] == 16.1
        assert result["wind_gust_kmh"] == 32.2
        assert result["wind_direction_deg"] == 185

    @pytest.mark.parametrize(
        "unit, value, expected_kmh",
        [("m/s", "3.0", 10.8), ("knots", "10", 18.5), ("km/h", "12.5", 12.5)],
    )
    def test_converts_other_wind_units_to_kmh(self, unit, value, expected_kmh):
        data = {"wind": {"wind_speed": {"unit": unit, "value": value}}}
        assert parse_ecowitt_data(data)["wind_speed_kmh"] == expected_kmh

    def test_reads_piezo_rain_gauge_in_mm(self, ws90_realtime_data):
        result = parse_ecowitt_data(ws90_realtime_data)
        assert result["rain_rate_mm_hr"] == 2.5
        assert result["rain_daily_mm"] == 12.7
        assert result["rain_event_mm"] == 7.6

    def test_prefers_piezo_rain_gauge_over_traditional(self, ws90_realtime_data):
        ws90_realtime_data["rainfall"] = {"daily": {"unit": "mm", "value": "99.0"}}
        assert parse_ecowitt_data(ws90_realtime_data)["rain_daily_mm"] == 12.7

    def test_falls_back_to_traditional_rain_gauge(self):
        data = {"rainfall": {"daily": {"unit": "mm", "value": "2.4"}}}
        assert parse_ecowitt_data(data)["rain_daily_mm"] == 2.4

    def test_converts_inhg_pressure_to_hpa(self, ws90_realtime_data):
        assert parse_ecowitt_data(ws90_realtime_data)["pressure_hpa"] == 1013.2

    def test_falls_back_to_absolute_pressure(self):
        data = {"pressure": {"absolute": {"unit": "hPa", "value": "1009.1"}}}
        assert parse_ecowitt_data(data)["pressure_hpa"] == 1009.1

    def test_parses_solar_and_uv(self, ws90_realtime_data):
        result = parse_ecowitt_data(ws90_realtime_data)
        assert result["solar_radiation_wm2"] == 830.5
        assert result["uv_index"] == 6

    def test_passes_battery_readings_through_with_units(self, ws90_realtime_data):
        assert parse_ecowitt_data(ws90_realtime_data)["battery"] == {"ws90_battery": "3.10 V"}

    def test_parse_empty_data(self):
        assert parse_ecowitt_data({}) == {}

    def test_none_values_excluded(self):
        data = {"outdoor": {"temperature": {"unit": "ºF", "value": "not a number"}}}
        assert parse_ecowitt_data(data) == {}


# --- Tests for latest_reading_time ---

class TestLatestReadingTime:
    def test_returns_newest_reading_time(self, ws90_realtime_data):
        assert latest_reading_time(ws90_realtime_data) == datetime.datetime(
            2026, 7, 1, 1, 1, tzinfo=datetime.timezone.utc
        )

    def test_returns_none_without_reading_times(self):
        assert latest_reading_time({"outdoor": {"temperature": {"unit": "ºF", "value": "86"}}}) is None

    def test_accepts_iso_timestamps_with_offset(self):
        data = {"outdoor": {"temperature": {"time": "2026-07-01T08:00:00+07:00", "value": "1"}}}
        assert latest_reading_time(data) == datetime.datetime(2026, 7, 1, 1, 0, tzinfo=datetime.timezone.utc)

    def test_ignores_timestamps_without_timezone(self):
        data = {"outdoor": {"temperature": {"time": "2026-07-01 08:00:00", "value": "1"}}}
        assert latest_reading_time(data) is None


# --- Tests for build_observation ---

class TestBuildObservation:
    def test_build_observation_full(self, parsed_normal_data):
        obs = build_observation(
            parsed_data=parsed_normal_data,
            source_id="AA:BB:CC:DD:EE:FF",
            source_name="North Ridge Station",
            subject_type="weather-station",
            lat=-1.234,
            lon=36.789,
            recorded_at="2026-07-01T01:00:00+00:00",
        )
        assert obs == {
            "source": "AA:BB:CC:DD:EE:FF",
            "source_name": "North Ridge Station",
            "type": "weather-station",
            "subject_type": "weather-station",
            "recorded_at": "2026-07-01T01:00:00+00:00",
            "location": {"lat": -1.234, "lon": 36.789},
            "additional": parsed_normal_data,
        }


# --- Tests for check_alert_conditions ---

def _alert_types(events):
    return [e["event_details"]["alert_type"] for e in events]


class TestCheckAlertConditions:
    def _check(self, parsed_data):
        return check_alert_conditions(
            parsed_data=parsed_data,
            source_id="AA:BB:CC:DD:EE:FF",
            lat=-1.234,
            lon=36.789,
            recorded_at="2026-07-01T01:00:00+00:00",
        )

    def test_no_alerts_normal_conditions(self, parsed_normal_data):
        assert self._check(parsed_normal_data) == []

    def test_high_wind_alert(self):
        events = self._check({"wind_speed_kmh": 95.0})
        assert _alert_types(events) == ["high_wind_speed"]
        assert events[0]["event_type"] == "ecowitt_alert"
        assert events[0]["location"] == {"lat": -1.234, "lon": 36.789}
        assert events[0]["event_details"]["current_value"] == 95.0

    def test_wind_at_threshold_triggers_alert(self):
        assert _alert_types(self._check({"wind_speed_kmh": 80.0})) == ["high_wind_speed"]

    def test_heavy_rain_alert(self):
        assert _alert_types(self._check({"rain_rate_mm_hr": 55.0})) == ["heavy_rain"]

    def test_extreme_high_temp_alert(self):
        assert _alert_types(self._check({"temperature_c": 47.0})) == ["extreme_temperature_high"]

    def test_extreme_low_temp_alert(self):
        assert _alert_types(self._check({"temperature_c": -25.0})) == ["extreme_temperature_low"]

    def test_multiple_alerts(self):
        events = self._check({"wind_speed_kmh": 95.0, "rain_rate_mm_hr": 60.0, "temperature_c": 46.0})
        assert sorted(_alert_types(events)) == ["extreme_temperature_high", "heavy_rain", "high_wind_speed"]

    def test_battery_readings_never_raise_alerts(self):
        assert self._check({"battery": {"ws90_battery": "0 V"}}) == []


# --- Tests for validate_ecowitt_credentials ---

class TestValidateEcowittCredentials:
    @pytest.mark.asyncio
    async def test_valid_credentials_code_zero(self, mock_ecowitt_http):
        mock_ecowitt_http([httpx.Response(200, json={"code": 0, "msg": "success", "data": {}})])
        assert await validate_ecowitt_credentials(application_key="app", api_key="api") is True

    @pytest.mark.asyncio
    async def test_invalid_api_key_returns_false(self, mock_ecowitt_http):
        mock_ecowitt_http([httpx.Response(200, json={"code": 40011, "msg": "Illegal Api_Key Parameter"})])
        assert await validate_ecowitt_credentials(application_key="bad", api_key="bad") is False

    @pytest.mark.asyncio
    async def test_http_401_returns_false(self, mock_ecowitt_http):
        mock_ecowitt_http([httpx.Response(401)])
        assert await validate_ecowitt_credentials(application_key="bad", api_key="bad") is False


# --- Tests for fetch_realtime_data ---

class TestFetchRealtimeData:
    @pytest.mark.asyncio
    async def test_returns_data_and_requests_all_categories(self, mock_ecowitt_http, ws90_realtime_data):
        requests = mock_ecowitt_http(
            [httpx.Response(200, json={"code": 0, "msg": "success", "time": T1, "data": ws90_realtime_data})]
        )
        result = await fetch_realtime_data(application_key="app", api_key="api", mac="AA:BB:CC:DD:EE:FF")
        assert result == ws90_realtime_data
        assert requests[0].url.params["call_back"] == "all"
        assert requests[0].url.params["mac"] == "AA:BB:CC:DD:EE:FF"

    @pytest.mark.asyncio
    async def test_api_error_code_raises(self, mock_ecowitt_http):
        mock_ecowitt_http([httpx.Response(200, json={"code": 40012, "msg": "Illegal MAC/IMEI Parameter"})])
        with pytest.raises(ValueError, match="Illegal MAC"):
            await fetch_realtime_data(application_key="app", api_key="api", mac="bad")

    @pytest.mark.asyncio
    async def test_retries_server_errors(self, mock_ecowitt_http, stamina_retries, ws90_realtime_data):
        requests = mock_ecowitt_http(
            [httpx.Response(503), httpx.Response(200, json={"code": 0, "data": ws90_realtime_data})]
        )
        result = await fetch_realtime_data(application_key="app", api_key="api", mac="AA:BB:CC:DD:EE:FF")
        assert result == ws90_realtime_data
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_does_not_retry_client_errors(self, mock_ecowitt_http, stamina_retries):
        requests = mock_ecowitt_http([httpx.Response(403), httpx.Response(403), httpx.Response(403)])
        with pytest.raises(EcowittHTTPError):
            await fetch_realtime_data(application_key="app", api_key="api", mac="AA:BB:CC:DD:EE:FF")
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_api_keys_are_redacted_from_request_logs(self, mock_ecowitt_http, caplog, ws90_realtime_data):
        mock_ecowitt_http([httpx.Response(200, json={"code": 0, "data": ws90_realtime_data})])
        with caplog.at_level("INFO", logger="httpx"):
            await fetch_realtime_data(application_key="secret-app-key", api_key="secret-api-key", mac="AA:BB:CC:DD:EE:FF")
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "HTTP Request" in logged
        assert "secret-app-key" not in logged
        assert "secret-api-key" not in logged

    @pytest.mark.asyncio
    async def test_http_errors_do_not_include_api_keys(self, mock_ecowitt_http):
        mock_ecowitt_http([httpx.Response(403)])
        with pytest.raises(EcowittHTTPError) as exc_info:
            await fetch_realtime_data(application_key="secret-app-key", api_key="secret-api-key", mac="AA:BB:CC:DD:EE:FF")
        assert exc_info.value.status_code == 403
        assert "secret" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_api_keys_are_not_attached_to_retry_logs(
        self, mock_ecowitt_http, stamina_retries, caplog, ws90_realtime_data
    ):
        mock_ecowitt_http([httpx.Response(503), httpx.Response(200, json={"code": 0, "data": ws90_realtime_data})])
        with caplog.at_level("INFO"):
            await fetch_realtime_data(application_key="secret-app-key", api_key="secret-api-key", mac="AA:BB:CC:DD:EE:FF")
        retry_records = [r for r in caplog.records if r.name == "stamina"]
        assert retry_records, "expected stamina to log the retry"
        assert all("secret" not in repr(vars(r)) for r in retry_records)
