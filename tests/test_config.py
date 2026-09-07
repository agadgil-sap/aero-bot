"""Behavior tests for security-sensitive application settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aero_bot.config import Settings
from aero_bot.sugar import DEFAULT_BASE_RPC_URL, LP_SUGAR_ADDRESS


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "8.8.8.8"])
def test_settings_reject_network_exposure(host: str) -> None:
    """Non-loopback bind addresses fail validation before server startup."""
    with pytest.raises(ValidationError, match="loopback"):
        Settings(bind_host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_settings_accept_loopback_addresses(host: str) -> None:
    """IPv4 and IPv6 loopback addresses remain valid local choices."""
    # The validated settings retain the explicit address for Uvicorn.
    settings = Settings(bind_host=host)

    assert settings.bind_host == host


def test_settings_requires_absolute_audit_database_path() -> None:
    """Relative durable storage paths fail before application startup."""
    with pytest.raises(ValidationError, match="must be absolute"):
        Settings(audit_database_path=Path("repository-local-audit.sqlite3"))


def test_settings_default_to_public_base_rpc_and_pinned_sugar() -> None:
    """Read-only enumeration defaults to the public endpoint and pinned contract."""
    # Defaults keep the application usable without any environment configuration.
    settings = Settings()

    assert settings.base_rpc_url == DEFAULT_BASE_RPC_URL
    # The pinned deployment address is normalized to its lowercase form.
    assert settings.lp_sugar_address == LP_SUGAR_ADDRESS.lower()
    # Live discovery stays opt-in so default startup performs no network reads.
    assert settings.pool_discovery_enabled is False


@pytest.mark.parametrize(
    "url",
    [
        "ftp://mainnet.base.org",
        "http://mainnet.base.org",
        "http://192.168.1.20:8545",
        "not-a-url",
    ],
)
def test_settings_reject_unsafe_rpc_endpoints(url: str) -> None:
    """Only HTTPS, or plaintext HTTP to loopback, may carry read-only RPC traffic."""
    with pytest.raises(ValidationError, match="base_rpc_url"):
        Settings(base_rpc_url=url)


@pytest.mark.parametrize("url", ["https://mainnet.base.org", "http://127.0.0.1:8545"])
def test_settings_accept_https_and_loopback_rpc_endpoints(url: str) -> None:
    """Remote HTTPS and local node endpoints are both valid read-only choices."""
    # The validated settings retain the configured endpoint for the Sugar backend.
    settings = Settings(base_rpc_url=url)

    assert settings.base_rpc_url == url


def test_settings_reject_malformed_sugar_address() -> None:
    """A malformed Sugar override fails closed before any request is attempted."""
    with pytest.raises(ValidationError, match="40 hexadecimal"):
        Settings(lp_sugar_address="0x1234")
