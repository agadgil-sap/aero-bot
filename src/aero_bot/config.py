"""Typed configuration with local-only security constraints."""

from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aero_bot.domain import EvmAddress
from aero_bot.sugar import DEFAULT_BASE_RPC_URL, LP_SUGAR_ADDRESS

# The default product title appears in API metadata and the dashboard.
DEFAULT_APP_NAME = "Aero Bot"
# The fixed environment label prevents this local release implying remote deployment support.
LOCAL_ENVIRONMENT: Literal["local"] = "local"
# The default IPv4 loopback address keeps the service on the user's Mac.
DEFAULT_BIND_HOST = "127.0.0.1"
# The default unprivileged port is stable for local bookmarks and operating instructions.
DEFAULT_BIND_PORT = 8765
# The default audit file lives in a dedicated private macOS application-data directory.
DEFAULT_AUDIT_DATABASE_PATH = (
    Path.home() / "Library" / "Application Support" / "Aero Bot" / "audit.sqlite3"
)
# The default pool-pin cache lives beside the audit store: local state, never
# inside the source repository.
DEFAULT_LP_POOL_PINS_PATH = (
    Path.home() / "Library" / "Application Support" / "Aero Bot" / "lp_pool_pins.json"
)
# Loopback host names are the only names permitted with plaintext HTTP RPC endpoints.
PLAINTEXT_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


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
    # The audit path keeps durable evidence outside the source repository by default.
    audit_database_path: Path = DEFAULT_AUDIT_DATABASE_PATH
    # The pool-pin cache stores Sugar-verified pool identities for the
    # known-pool fast path; it is a local cache, never a trust root.
    lp_pool_pins_path: Path = DEFAULT_LP_POOL_PINS_PATH
    # The Base JSON-RPC endpoint used only for read-only eth_call enumeration.
    base_rpc_url: str = DEFAULT_BASE_RPC_URL
    # The LP Sugar contract is the authoritative complete Aerodrome pool inventory.
    lp_sugar_address: EvmAddress = LP_SUGAR_ADDRESS
    # Live pool enumeration is opt-in so default startup stays deterministic and offline.
    pool_discovery_enabled: bool = False

    @field_validator("bind_host")
    @classmethod
    def require_loopback_host(cls, value: str) -> str:
        """Reject bind addresses that would expose the first release to a network."""
        # The parsed address provides reliable IPv4 and IPv6 loopback classification.
        parsed_address = ip_address(value)
        if not parsed_address.is_loopback:
            raise ValueError("bind_host must be a loopback IP address")
        return value

    @field_validator("audit_database_path")
    @classmethod
    def require_absolute_audit_path(cls, value: Path) -> Path:
        """Expand and require an absolute path for durable audit storage."""
        # Expansion permits the environment setting to use an operator-local home path.
        expanded_path = value.expanduser()
        if not expanded_path.is_absolute():
            raise ValueError("audit_database_path must be absolute")
        return expanded_path

    @field_validator("lp_pool_pins_path")
    @classmethod
    def require_absolute_pool_pins_path(cls, value: Path) -> Path:
        """Expand and require an absolute path for the pool-pin cache."""
        # The pin cache is local application state and follows the audit path
        # convention of an expanded, absolute, operator-configurable path.
        expanded_path = value.expanduser()
        if not expanded_path.is_absolute():
            raise ValueError("lp_pool_pins_path must be absolute")
        return expanded_path

    @field_validator("base_rpc_url")
    @classmethod
    def require_read_only_rpc_url(cls, value: str) -> str:
        """Accept only HTTPS RPC endpoints, or plaintext endpoints on loopback hosts."""
        # Parsed URL components distinguish remote HTTPS endpoints from local nodes.
        parsed_url = urlparse(value)
        # A remote plaintext endpoint would expose read traffic to network observers.
        plaintext_to_remote_host = (
            parsed_url.scheme == "http" and parsed_url.hostname not in PLAINTEXT_HTTP_HOSTS
        )
        if parsed_url.scheme not in {"http", "https"} or plaintext_to_remote_host:
            raise ValueError("base_rpc_url must use HTTPS, or HTTP only on a loopback host")
        if not parsed_url.netloc:
            raise ValueError("base_rpc_url must include a host")
        return value
