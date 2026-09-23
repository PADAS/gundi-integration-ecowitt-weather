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
    mac: str = pydantic.Field(..., title="MAC Address", description="Station MAC address, e.g. AA:BB:CC:DD:EE:FF")
    name: str = pydantic.Field(..., title="Name", description="Station name shown in EarthRanger")
    latitude: float = pydantic.Field(..., title="Latitude")
    longitude: float = pydantic.Field(..., title="Longitude")

    @pydantic.validator("mac")
    def normalize_mac(cls, value):
        return value.strip().upper()


class PullObservationsConfiguration(PullActionConfiguration, ExecutableActionMixin):
    stations: List[EcowittStation] = pydantic.Field(..., title="Weather Stations")
    subject_type: str = "weather-station"
    # Alert thresholds
    high_wind_speed_kmh: float = 80.0
    heavy_rain_mm_hr: float = 50.0
    extreme_temp_high_c: float = 45.0
    extreme_temp_low_c: float = -20.0
