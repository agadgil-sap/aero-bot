"""Behavior tests for security-sensitive application settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aero_bot.config import Settings


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
