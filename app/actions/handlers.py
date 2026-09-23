import datetime
import logging

from app.actions.configurations import AuthenticateEcowittConfig, PullObservationsConfiguration, get_auth_config
from app.services.activity_logger import activity_logger
from app.services.gundi import send_observations_to_gundi, send_events_to_gundi
from app.services.ecowitt import (
    fetch_realtime_data,
    latest_reading_time,
    parse_ecowitt_data,
    build_observation,
    check_alert_conditions,
    validate_ecowitt_credentials,
)
from app.services.state import IntegrationStateManager

logger = logging.getLogger(__name__)

state_manager = IntegrationStateManager()


async def action_auth(integration, action_config: AuthenticateEcowittConfig):
    """
    Verify Ecowitt API credentials by calling the API.
    Used by the portal to validate credentials without running pull_observations.
    """
    try:
        valid = await validate_ecowitt_credentials(
            application_key=action_config.application_key,
            api_key=action_config.api_key.get_secret_value(),
        )
        return {"valid_credentials": valid}
    except Exception as e:
        logger.exception("Ecowitt auth failed")
        return {"valid_credentials": False, "message": str(e)}


async def _pull_station(integration, station, action_config, application_key, api_key):
    """Fetch one station's latest reading and build its observation and new alerts.

    Returns None when the reading was already sent in an earlier run.
    """
    integration_id = str(integration.id)
    raw_data = await fetch_realtime_data(application_key=application_key, api_key=api_key, mac=station.mac)
    parsed_data = parse_ecowitt_data(raw_data)
    if not parsed_data:
        raise ValueError("No weather readings in the Ecowitt response")

    state = await state_manager.get_state(
        integration_id=integration_id, action_id="pull_observations", source_id=station.mac
    )
    reading_time = latest_reading_time(raw_data)
    if reading_time is not None:
        last_sent = state.get("latest_recorded_at")
        if last_sent and reading_time <= datetime.datetime.fromisoformat(last_sent):
            return None
        recorded_at = reading_time.isoformat()
    else:
        logger.warning(f"No reading time for station {station.mac}; using the time it was fetched")
        recorded_at = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

    observation = build_observation(
        parsed_data=parsed_data,
        source_id=station.mac,
        source_name=station.name,
        subject_type=action_config.subject_type,
        lat=station.latitude,
        lon=station.longitude,
        recorded_at=recorded_at,
    )
    alerts = check_alert_conditions(
        parsed_data=parsed_data,
        source_id=station.mac,
        lat=station.latitude,
        lon=station.longitude,
        recorded_at=recorded_at,
        high_wind_speed_kmh=action_config.high_wind_speed_kmh,
        heavy_rain_mm_hr=action_config.heavy_rain_mm_hr,
        extreme_temp_high_c=action_config.extreme_temp_high_c,
        extreme_temp_low_c=action_config.extreme_temp_low_c,
    )
    # Alert only when a condition starts, not on every reading while it lasts
    previously_active = set(state.get("active_alerts", []))
    new_alerts = [a for a in alerts if a["event_details"]["alert_type"] not in previously_active]
    new_state = {
        "latest_recorded_at": recorded_at if reading_time is not None else state.get("latest_recorded_at"),
        "active_alerts": sorted(a["event_details"]["alert_type"] for a in alerts),
    }
    return observation, new_alerts, new_state


@activity_logger()
async def action_pull_observations(integration, action_config: PullObservationsConfiguration):
    integration_id = str(integration.id)
    logger.info(
        f"Pulling observations for integration {integration_id}, "
        f"stations: {[station.mac for station in action_config.stations]}"
    )

    auth_config = get_auth_config(integration)
    application_key = auth_config.application_key
    api_key = auth_config.api_key.get_secret_value()

    observations, events, new_states = [], [], {}
    stations_skipped = stations_failed = 0

    for station in action_config.stations:
        try:
            result = await _pull_station(integration, station, action_config, application_key, api_key)
        except Exception as e:
            logger.warning(f"Failed to pull Ecowitt station {station.mac}, integration {integration_id}: {e}")
            stations_failed += 1
            continue
        if result is None:
            stations_skipped += 1
            continue
        observation, new_alerts, new_state = result
        observations.append(observation)
        events.extend(new_alerts)
        new_states[station.mac] = new_state

    if observations:
        await send_observations_to_gundi(observations=observations, integration_id=integration_id)
        logger.info(f"Sent {len(observations)} observation(s) to Gundi for integration {integration_id}")
    if events:
        await send_events_to_gundi(events=events, integration_id=integration_id)
        logger.info(f"Sent {len(events)} alert event(s) to Gundi for integration {integration_id}")

    # Saved only after sending, so a failed send is retried on the next run
    for mac, new_state in new_states.items():
        await state_manager.set_state(
            integration_id=integration_id, action_id="pull_observations", state=new_state, source_id=mac
        )

    return {
        "observations_sent": len(observations),
        "events_sent": len(events),
        "stations_skipped": stations_skipped,
        "stations_failed": stations_failed,
    }
