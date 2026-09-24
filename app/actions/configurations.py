from typing import List

import pydantic

from app.actions.core import AuthActionConfiguration, ExecutableActionMixin, PullActionConfiguration
from app.services.errors import ConfigurationNotFound
from app.services.utils import find_config_for_action


class AuthenticateEcowittConfig(AuthActionConfiguration, ExecutableActionMixin):
    """Configuration for the Ecowitt auth action (portal credential verification)."""

    application_key: str = pydantic.Field(
        ...,
        title="Application Key",
        description="Ecowitt Cloud API application key",
    )
    api_key: pydantic.SecretStr = pydantic.Field(
        ...,
        title="API Key",
        description="Ecowitt Cloud API key",
        format="password",
    )


def get_auth_config(integration):
    """Get Ecowitt API credentials from the integration's auth action config."""
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth",
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            "are missing. Please configure the Auth action in the portal."
        )
    return AuthenticateEcowittConfig.parse_obj(auth_config.data)


class EcowittStation(pydantic.BaseModel):
    mac: str = pydantic.Field(
        ...,
        title="MAC Address",
        description="The station's MAC address as shown in the Ecowitt app, e.g. AA:BB:CC:DD:EE:FF.",
    )
    name: str = pydantic.Field(
        ...,
        title="Name",
        description="Station name shown in EarthRanger.",
    )
    latitude: float = pydantic.Field(
        ...,
        title="Latitude",
        description="Station latitude in decimal degrees. The Ecowitt API doesn't report a location.",
        ge=-90,
        le=90,
    )
    longitude: float = pydantic.Field(
        ...,
        title="Longitude",
        description="Station longitude in decimal degrees.",
        ge=-180,
        le=180,
    )

    @pydantic.validator("mac")
    def normalize_mac(cls, value):
        return value.strip().upper()


class PullObservationsConfiguration(PullActionConfiguration, ExecutableActionMixin):
    stations: List[EcowittStation] = pydantic.Field(
        ...,
        title="Weather Stations",
        description="Ecowitt stations to pull readings from. Each needs its MAC address, a name and coordinates.",
        min_items=1,
    )
    subject_type: str = pydantic.Field(
        "weather-station",
        title="Subject Type",
        description="Subject type given to the stations' observations.",
    )
    # Alert thresholds
    high_wind_speed_kmh: float = pydantic.Field(
        80.0,
        title="High Wind Speed Alert (km/h)",
        description="Send an alert when the wind speed reaches this value.",
    )
    heavy_rain_mm_hr: float = pydantic.Field(
        50.0,
        title="Heavy Rain Alert (mm/hr)",
        description="Send an alert when the rain rate reaches this value.",
    )
    extreme_temp_high_c: float = pydantic.Field(
        45.0,
        title="High Temperature Alert (°C)",
        description="Send an alert when the outdoor temperature reaches this value.",
    )
    extreme_temp_low_c: float = pydantic.Field(
        -20.0,
        title="Low Temperature Alert (°C)",
        description="Send an alert when the outdoor temperature falls to this value.",
    )
