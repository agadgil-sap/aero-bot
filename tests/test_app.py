"""Behavior tests for the local dashboard and safety metadata."""

import httpx
import pytest

from aero_bot.app import create_app
from aero_bot.config import Settings
from aero_bot.registry import B20RegistryResult, RegistryStatus


@pytest.mark.anyio
async def test_health_exposes_wallet_free_operating_boundary() -> None:
    """Health output identifies the chain, venue, and simulation-only mode."""
    # A local settings instance represents the supported first-release environment.
    settings = Settings()
    # The ASGI transport exercises the complete application without opening a network socket.
    transport = httpx.ASGITransport(app=create_app(settings))
    # The HTTP client follows the same protocol boundary used by local monitoring.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The request covers routing, serialization, and response validation together.
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "environment": "local",
        "chain_id": 8453,
        "enabled_venues": ["aerodrome"],
        "execution_mode": "simulation_only",
    }


@pytest.mark.anyio
async def test_dashboard_states_truthful_initial_status() -> None:
    """Dashboard clearly distinguishes hard safety guarantees from pending data."""
    # The ASGI transport renders the page as a user would receive it from localhost.
    transport = httpx.ASGITransport(app=create_app(Settings()))
    # The HTTP client provides a realistic request and response lifecycle.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The request covers the complete server-side dashboard response.
        response = await client.get("/")

    assert response.status_code == 200
    assert "Simulation only" in response.text
    assert "No wallet, signer, private key, or broadcast path is present." in response.text
    assert "Aerodrome" in response.text
    assert "Source verified" in response.text
    assert "Verified 10 Coinbase-issued B20 listings" in response.text
    assert "Not connected" in response.text
    assert "Hold USDC is always a valid outcome." in response.text


@pytest.mark.anyio
async def test_dashboard_escapes_custom_application_name() -> None:
    """A locally configured application title cannot inject dashboard markup."""
    # The custom title represents untrusted text entering from an environment override.
    settings = Settings(app_name='<script id="injected">bad()</script>')
    # The ASGI transport exercises the rendered HTML boundary.
    transport = httpx.ASGITransport(app=create_app(settings))
    # The HTTP client provides the complete rendering behavior under test.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The request renders the customized dashboard title.
        response = await client.get("/")

    assert '<script id="injected">' not in response.text
    assert "&lt;script id=&quot;injected&quot;&gt;bad()&lt;/script&gt;" in response.text


@pytest.mark.anyio
async def test_b20_registry_endpoint_exposes_source_provenance() -> None:
    """The registry API exposes issuer evidence rather than ticker-only claims."""
    # The ASGI transport exercises the complete registry response boundary.
    transport = httpx.ASGITransport(app=create_app(Settings()))
    # The HTTP client performs the same JSON request used by the future dashboard client.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The request reads the validated package snapshot through the application route.
        response = await client.get("/api/registry/b20")

    # The decoded response supports assertions about public API behavior.
    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "verified"
    assert payload["source_url"] == "https://www.base.org/stocks"
    assert payload["source_observed_at"] == "2026-09-06"
    assert len(payload["assets"]) == 10


@pytest.mark.anyio
async def test_dashboard_displays_registry_failure_diagnostic() -> None:
    """Invalid official evidence is visibly blocked rather than silently omitted."""
    # The explicit result simulates registry corruption without altering the packaged evidence.
    failed_registry = B20RegistryResult(
        status=RegistryStatus.INVALID,
        source_url="https://www.base.org/stocks",
        source_observed_at=None,
        assets=(),
        diagnostic="Evidence <failed> validation",
    )
    # The ASGI transport injects the deterministic diagnostic into the user-facing page.
    transport = httpx.ASGITransport(app=create_app(Settings(), failed_registry))
    # The HTTP client requests the complete rendered dashboard.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The response captures the same blocked state a local operator would see.
        response = await client.get("/")

    assert "Blocked" in response.text
    assert "Evidence &lt;failed&gt; validation" in response.text
