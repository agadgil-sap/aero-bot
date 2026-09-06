"""Shared pytest configuration for asynchronous HTTP behavior tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch


@pytest.fixture(autouse=True)
def isolate_audit_storage(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep application audit writes inside each test's private temporary directory.

    Args:
        monkeypatch: Pytest environment mutation helper restored after the test.
        tmp_path: Per-test private filesystem location.

    Yields:
        Control after configuring isolated application persistence.
    """
    # Dedicated child lets AuditStore create and secure its own final parent directory.
    database_path = tmp_path / "aero-bot-audit" / "audit.sqlite3"
    # Environment override exercises the same settings boundary used by local operators.
    monkeypatch.setenv("AERO_BOT_AUDIT_DATABASE_PATH", str(database_path))
    yield


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio because it is the production ASGI server's event-loop backend."""
    return "asyncio"
