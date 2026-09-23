"""Tests for Ecowitt auth and pull_observations actions."""

import pytest
from unittest.mock import AsyncMock, MagicMock

import pydantic

from app.actions.configurations import AuthenticateEcowittConfig, PullObservationsConfiguration
from app.actions.handlers import action_auth, action_pull_observations

STATION_A = "AA:BB:CC:DD:EE:01"
STATION_B = "AA:BB:CC:DD:EE:02"

# Epoch seconds for 2026-07-01T01:00:00Z and one minute later
T0 = "1782867600"
T1 = "1782867660"


def realtime_data(time=T0, temperature_f="86.0", wind_mph="5.0"):
    return {
        "outdoor": {"temperature": {"time": time, "unit": "ºF", "value": temperature_f}},
        "wind": {"wind_speed": {"time": time, "unit": "mph", "value": wind_mph}},
    }


class InMemoryStateManager:
    """Stands in for IntegrationStateManager's Redis storage."""

    def __init__(self):
        self.states = {}

    async def get_state(self, integration_id, action_id, source_id="no-source"):
        return self.states.get((integration_id, action_id, source_id), {})

    async def set_state(self, integration_id, action_id, state, source_id="no-source"):
        self.states[(integration_id, action_id, source_id)] = state


@pytest.fixture
def integration_with_id():
    integration = MagicMock()
    integration.id = "550e8400-e29b-41d4-a716-446655440000"
    return integration


@pytest.fixture
def mock_ecowitt_auth_config():
    return AuthenticateEcowittConfig(
        application_key="test_app_key",
        api_key=pydantic.SecretStr("test_api_key"),
    )


@pytest.fixture
def pull_observations_config():
    return PullObservationsConfiguration(
        stations=[
            {"mac": STATION_A, "name": "North Ridge", "latitude": -1.234, "longitude": 36.789},
            {"mac": STATION_B, "name": "River Camp", "latitude": -1.5, "longitude": 36.9},
        ],
    )


@pytest.fixture
def pull_env(mocker, mock_ecowitt_auth_config):
    """Patch the pull action's collaborators; tests set `fetch.side_effect`."""
    mocker.patch("app.actions.handlers.get_auth_config", return_value=mock_ecowitt_auth_config)
    # Avoid activity_logger decorator publishing to Pub/Sub
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock())
    env = MagicMock()
    env.fetch = mocker.patch("app.actions.handlers.fetch_realtime_data", AsyncMock())
    env.send_observations = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock())
    env.send_events = mocker.patch("app.actions.handlers.send_events_to_gundi", AsyncMock())
    env.state = InMemoryStateManager()
    mocker.patch("app.actions.handlers.state_manager", env.state)
    return env


def by_mac(readings):
    """fetch_realtime_data side effect returning (or raising) a reading per MAC."""
    async def fetch(application_key, api_key, mac):
        result = readings[mac]
        if isinstance(result, Exception):
            raise result
        return result
    return fetch


def sent_observations(env):
    return [obs for call in env.send_observations.call_args_list for obs in call.kwargs["observations"]]


def sent_alert_types(env):
    return [
        (event["event_details"]["source_id"], event["event_details"]["alert_type"])
        for call in env.send_events.call_args_list
        for event in call.kwargs["events"]
    ]


# --- auth action ---

@pytest.mark.asyncio
async def test_action_auth_success(mocker, integration_with_id, mock_ecowitt_auth_config):
    """Auth action returns valid_credentials True when validate_ecowitt_credentials succeeds."""
    mocker.patch("app.actions.handlers.validate_ecowitt_credentials", AsyncMock(return_value=True))
    result = await action_auth(integration=integration_with_id, action_config=mock_ecowitt_auth_config)
    assert result["valid_credentials"] is True


@pytest.mark.asyncio
async def test_action_auth_failure(mocker, integration_with_id, mock_ecowitt_auth_config):
    """Auth action returns valid_credentials False when validate_ecowitt_credentials returns False."""
    mocker.patch("app.actions.handlers.validate_ecowitt_credentials", AsyncMock(return_value=False))
    result = await action_auth(integration=integration_with_id, action_config=mock_ecowitt_auth_config)
    assert result["valid_credentials"] is False


@pytest.mark.asyncio
async def test_action_auth_exception(mocker, integration_with_id, mock_ecowitt_auth_config):
    """Auth action returns valid_credentials False and message when validate raises."""
    mocker.patch(
        "app.actions.handlers.validate_ecowitt_credentials",
        AsyncMock(side_effect=Exception("Network error")),
    )
    result = await action_auth(integration=integration_with_id, action_config=mock_ecowitt_auth_config)
    assert result["valid_credentials"] is False
    assert "Network error" in result["message"]


# --- configuration ---

def test_station_coordinates_are_required():
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfiguration(stations=[{"mac": STATION_A, "name": "North Ridge"}])


def test_station_macs_are_normalized_to_upper_case():
    config = PullObservationsConfiguration(
        stations=[{"mac": " aa:bb:cc:dd:ee:01 ", "name": "North Ridge", "latitude": 0, "longitude": 0}]
    )
    assert config.stations[0].mac == STATION_A


# --- pull_observations action ---

@pytest.mark.asyncio
async def test_pull_fetches_each_station_with_auth_credentials(
    pull_env, integration_with_id, pull_observations_config
):
    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(), STATION_B: realtime_data()})

    await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    calls = pull_env.fetch.call_args_list
    assert [c.kwargs["mac"] for c in calls] == [STATION_A, STATION_B]
    assert all(c.kwargs["application_key"] == "test_app_key" for c in calls)
    assert all(c.kwargs["api_key"] == "test_api_key" for c in calls)


@pytest.mark.asyncio
async def test_pull_sends_one_observation_per_station(pull_env, integration_with_id, pull_observations_config):
    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(), STATION_B: realtime_data()})

    result = await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    observations = sent_observations(pull_env)
    assert result["observations_sent"] == 2
    assert observations[0]["source"] == STATION_A
    assert observations[0]["source_name"] == "North Ridge"
    assert observations[0]["location"] == {"lat": -1.234, "lon": 36.789}
    assert observations[0]["additional"]["temperature_c"] == 30.0
    assert observations[1]["source"] == STATION_B
    assert observations[1]["location"] == {"lat": -1.5, "lon": 36.9}


@pytest.mark.asyncio
async def test_pull_records_observation_at_reading_time(pull_env, integration_with_id, pull_observations_config):
    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(time=T1), STATION_B: realtime_data()})

    await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    assert sent_observations(pull_env)[0]["recorded_at"] == "2026-07-01T01:01:00+00:00"


@pytest.mark.asyncio
async def test_pull_skips_readings_already_sent(pull_env, integration_with_id, pull_observations_config):
    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(time=T0), STATION_B: realtime_data(time=T0)})
    await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)
    pull_env.send_observations.reset_mock()

    # Station A has a new reading; station B still reports the one already sent
    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(time=T1), STATION_B: realtime_data(time=T0)})
    result = await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    assert [obs["source"] for obs in sent_observations(pull_env)] == [STATION_A]
    assert result["observations_sent"] == 1
    assert result["stations_skipped"] == 1


@pytest.mark.asyncio
async def test_pull_continues_when_a_station_fails(pull_env, integration_with_id, pull_observations_config):
    pull_env.fetch.side_effect = by_mac({STATION_A: ValueError("Ecowitt API error"), STATION_B: realtime_data()})

    result = await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    assert [obs["source"] for obs in sent_observations(pull_env)] == [STATION_B]
    assert result["stations_failed"] == 1


@pytest.mark.asyncio
async def test_pull_raises_alert_when_threshold_first_crossed(
    pull_env, integration_with_id, pull_observations_config
):
    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(wind_mph="60.0"), STATION_B: realtime_data()})

    result = await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    assert sent_alert_types(pull_env) == [(STATION_A, "high_wind_speed")]
    assert result["events_sent"] == 1


@pytest.mark.asyncio
async def test_pull_does_not_repeat_alert_while_condition_persists(
    pull_env, integration_with_id, pull_observations_config
):
    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(time=T0, wind_mph="60.0"), STATION_B: realtime_data()})
    await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)
    pull_env.send_events.reset_mock()

    pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(time=T1, wind_mph="65.0"), STATION_B: realtime_data()})
    await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    assert sent_alert_types(pull_env) == []


@pytest.mark.asyncio
async def test_pull_raises_alert_again_after_condition_clears(
    pull_env, integration_with_id, pull_observations_config
):
    for time, wind in ((T0, "60.0"), (T1, "5.0"), ("1782867720", "60.0")):
        pull_env.fetch.side_effect = by_mac({STATION_A: realtime_data(time=time, wind_mph=wind), STATION_B: realtime_data()})
        await action_pull_observations(integration=integration_with_id, action_config=pull_observations_config)

    assert sent_alert_types(pull_env) == [(STATION_A, "high_wind_speed"), (STATION_A, "high_wind_speed")]


def test_pull_observations_is_scheduled_every_five_minutes():
    from app.services.action_scheduler import CrontabSchedule

    # Self-registration sends this attribute to Gundi as the action's schedule
    assert action_pull_observations.crontab_schedule == CrontabSchedule.parse_obj_from_crontab("*/5 * * * *")


@pytest.mark.parametrize(
    "handler, title",
    [(action_auth, "Authenticate with Ecowitt"), (action_pull_observations, "Pull Weather Observations")],
)
def test_actions_register_with_display_titles(handler, title):
    # Self-registration uses this attribute as the action's name in Gundi
    assert getattr(handler, "action_title", None) == title


@pytest.mark.parametrize("config_model", [AuthenticateEcowittConfig, PullObservationsConfiguration])
def test_every_config_field_is_described_for_the_portal(config_model):
    schema = config_model.schema()
    models = [schema, *schema.get("definitions", {}).values()]
    undescribed = [
        f"{model['title']}.{name}"
        for model in models
        for name, field in model["properties"].items()
        if not field.get("description")
    ]
    assert undescribed == []


def test_at_least_one_station_is_required():
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfiguration(stations=[])


@pytest.mark.parametrize("latitude, longitude", [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_station_coordinates_must_be_in_range(latitude, longitude):
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfiguration(
            stations=[{"mac": STATION_A, "name": "North Ridge", "latitude": latitude, "longitude": longitude}]
        )
