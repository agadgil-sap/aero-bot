"""Shared pytest configuration for asynchronous HTTP behavior tests."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio because it is the production ASGI server's event-loop backend."""
    return "asyncio"
