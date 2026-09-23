# gundi-integration-ecowitt-weather

A [Gundi v2](https://gundiservice.org) integration that pulls readings from
[Ecowitt](https://www.ecowitt.com) weather stations through the Ecowitt Cloud
API v3. It sends each new reading to Gundi as an observation, and sends alerts
for severe weather as events.

It's built from the
[gundi-integration-action-runner](https://github.com/PADAS/gundi-integration-action-runner)
template. See the template's README for the framework itself: action
registration, activity logging, scheduling and webhooks.

## Actions

### `auth`: Authenticate

Checks the Ecowitt credentials by calling the real-time endpoint with a
placeholder MAC address. It returns `{"valid_credentials": true|false}`.

| Field | Description |
|---|---|
| `application_key` | Ecowitt Cloud API application key |
| `api_key` | Ecowitt Cloud API key (stored as a secret) |

Both keys come from the API settings of the Ecowitt account that owns the
stations.

### `pull_observations`: Pull Observations

For each configured station, the action:

1. Fetches the current reading from `GET /api/v3/device/real_time` with
   `call_back=all`.
2. Converts every reading to metric according to the unit Ecowitt returns
   with it. The API reports in the account's display units, often imperial.
3. Skips the reading if it isn't newer than the last one sent for that station.
4. Sends one observation per station with a new reading, and alert events for
   any threshold that has just been crossed.

| Field | Default | Description |
|---|---|---|
| `stations` | required | List of stations, each with `mac`, `name`, `latitude` and `longitude` |
| `subject_type` | `weather-station` | Subject type for the observations |
| `high_wind_speed_kmh` | `80.0` | Wind speed at or above which an alert is sent |
| `heavy_rain_mm_hr` | `50.0` | Rain rate at or above which an alert is sent |
| `extreme_temp_high_c` | `45.0` | Outdoor temperature at or above which an alert is sent |
| `extreme_temp_low_c` | `-20.0` | Outdoor temperature at or below which an alert is sent |
| `run_on_schedule` | `true` | Turn off to pause scheduled runs without deleting the configuration |

Coordinates are required because the Ecowitt real-time API doesn't return a
location. MAC addresses are normalized to upper case.

The code doesn't set a schedule. Set one when registering the integration,
e.g. `python -m app.register --schedule "pull_observations:*/5 * * * *"`.

The action returns `observations_sent`, `events_sent`, `stations_skipped`
(readings already sent) and `stations_failed`.

## Output

### Observations

```json
{
  "source": "D4:E9:F4:F5:66:CB",
  "source_name": "Weather Station",
  "type": "weather-station",
  "subject_type": "weather-station",
  "recorded_at": "2026-07-01T01:01:00+00:00",
  "location": {"lat": 0.845365, "lon": 104.70335},
  "additional": {
    "temperature_c": 30.0,
    "feels_like_c": 33.3,
    "dew_point_c": 25.6,
    "humidity_pct": 81,
    "indoor_temperature_c": 25.0,
    "indoor_humidity_pct": 60,
    "pressure_hpa": 1013.2,
    "wind_speed_kmh": 16.1,
    "wind_gust_kmh": 32.2,
    "wind_direction_deg": 185,
    "rain_rate_mm_hr": 2.5,
    "rain_daily_mm": 12.7,
    "rain_event_mm": 7.6,
    "solar_radiation_wm2": 830.5,
    "uv_index": 6,
    "battery": {"ws90_battery": "3.10 V"}
  }
}
```

- **`source`** is the station's MAC address.
- **`recorded_at`** is the newest per-reading `time` in the Ecowitt response.
  If the response has no usable time, the fetch time is used instead. In that
  case the reading can't be checked for duplicates.
- **Readings missing from the response are left out.** A station without an
  indoor sensor has no `indoor_*` fields, for example.
- **Rain** comes from `rainfall_piezo` when the station has a piezo gauge (for
  example the WS90), and from the traditional `rainfall` gauge otherwise.
- **Battery** readings are passed through as reported. Sensors report battery
  in different forms: a 0/1 flag, a 1–5 level or a voltage.

### Alert events

Events have `event_type: "ecowitt_alert"`. An alert is sent when a condition
**starts**, not on every reading while it lasts. It's sent again only after
the condition has cleared.

| `alert_type` | Condition |
|---|---|
| `high_wind_speed` | `wind_speed_kmh` ≥ `high_wind_speed_kmh` |
| `heavy_rain` | `rain_rate_mm_hr` ≥ `heavy_rain_mm_hr` |
| `extreme_temperature_high` | `temperature_c` ≥ `extreme_temp_high_c` |
| `extreme_temperature_low` | `temperature_c` ≤ `extreme_temp_low_c` |

`event_details` holds the `alert_type`, `current_value`, `threshold` and
`source_id` (the station MAC).

## Behaviour

- **State:** each station's state is stored under its MAC. It holds the time of
  the last reading sent and the alerts currently active. State is saved only
  after the send to Gundi succeeds, so a failed send is retried on the next run.
- **Failures:** a station that fails doesn't stop the others. Ecowitt requests
  are retried only on network errors, HTTP 429 and 5xx, with at most 3 attempts
  within 60 seconds. The next scheduled run covers longer outages.
- **Credentials:** Ecowitt only accepts its keys as query parameters. They are
  redacted from httpx's request logs and left out of error messages and retry
  logs.
- **Webhooks:** not supported. Ecowitt gateways' "customized upload" posts
  form-encoded data, and the template's webhook service only parses JSON.

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `ECOWITT_API_BASE_URL` | `https://api.ecowitt.net` | Ecowitt Cloud API base URL |

The Gundi settings are described in the template.

## Development

Use Python 3.10, matching CI:

```bash
pip install -r requirements.txt
pytest
```

After editing `requirements.in`, recompile the pinned requirements:

```bash
pip-compile --output-file=requirements.txt requirements-base.in requirements-dev.in requirements.in
```

To run the service against Gundi's stage environment with Docker Compose, see
[local/LOCAL_DEVELOPMENT.md](local/LOCAL_DEVELOPMENT.md).

### Checking a real station

`local/check_station.py` runs the pull logic against a real station and sends
nothing to Gundi. It prints the raw units, reading times and battery keys from
the Ecowitt response, and the observation and alerts the action would produce.

```bash
export ECOWITT_APPLICATION_KEY=... ECOWITT_API_KEY=...
python local/check_station.py --mac AA:BB:CC:DD:EE:FF --lat 0.1 --lon 36.5 --save response.json
```

`--save` writes the raw response to a file, which can then be used as a test
fixture.

## Syncing with the template

The template is tracked as the `template` remote:

```bash
git remote add template git@github.com:PADAS/gundi-integration-action-runner.git  # once
git fetch template
git merge template/main
```

This README replaces the template's, so keep this version if the merge
conflicts on it.
