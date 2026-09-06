"""Typed configuration with local-only security constraints."""

from ipaddress import ip_address
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The default product title appears in API metadata and the dashboard.
DEFAULT_APP_NAME = "Aero Bot"
# The fixed environment label prevents this local release implying remote deployment support.
LOCAL_ENVIRONMENT: Literal["local"] = "local"
# The default IPv4 loopback address keeps the service on the user's Mac.
DEFAULT_BIND_HOST = "127.0.0.1"
# The default unprivileged port is stable for local bookmarks and operating instructions.
DEFAULT_BIND_PORT = 8765


class Settings(BaseSettings):
    """Define environment-backed settings for the local application."""

    # Environment variables use this prefix and nested keys are not currently needed.
    model_config = SettingsConfigDict(env_prefix="AERO_BOT_", extra="forbid")

    # The application name gives API clients a stable human-readable identity.
    app_name: str = DEFAULT_APP_NAME
    # Only the local environment is valid until a separately secured deployment mode exists.
    environment: Literal["local"] = LOCAL_ENVIRONMENT
    # The bind host is validated as a loopback IP address to prevent network exposure.
    bind_host: str = DEFAULT_BIND_HOST
    # The bind port must be an unprivileged TCP port.
    bind_port: Annotated[int, Field(ge=1024, le=65535)] = DEFAULT_BIND_PORT

    @field_validator("bind_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Reject bind addresses that would expose the first release to a network."""
        # The parsed address provides reliable IPv4 and IPv6 loopback classification.
        parsed_address = ip_address(value)
        if not parsed_address.is_loopback:
            raise ValueError("bind_host must be a loopback IP address")
        return value
