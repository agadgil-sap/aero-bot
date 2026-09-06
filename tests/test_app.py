"""Behavior tests for the local dashboard and safety metadata."""

from datetime import UTC, datetime

import httpx
import pytest

from aero_bot.app import create_app
from aero_bot.config import Settings
from aero_bot.registry import B20RegistryResult, RegistryStatus
from aero_bot.venues import (
    PoolCandidate,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
    PoolKind,
    VenueId,
)


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
    assert "Discovery blocked" in response.text
    assert "No read-only Base RPC discovery backend is configured" in response.text
    assert "Health unavailable" in response.text
    assert "no reviewed proxy-address snapshot" in response.text
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


@pytest.mark.anyio
async def test_pool_endpoint_and_dashboard_expose_verified_discovery() -> None:
    """Injected verified discovery is visible through both JSON and the user-facing page."""
    # The candidate is valid fixture evidence and does not claim a real deployed pool.
    candidate = PoolCandidate(
        pool_address="0x1111111111111111111111111111111111111111",
        factory_address="0xf8f2eb4940cfe7d13603dddd87f123820fc061ef",
        token0_address="0xb20000000000000000000078ee7ce2fe4908108c",
        token1_address="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        pool_kind=PoolKind.SLIPSTREAM,
        fee_bps=30,
        gauge_address=None,
    )
    # The discovery result simulates a completed read-only backend after adapter validation.
    pool_discovery = PoolDiscoveryResult(
        venue=VenueId.AERODROME,
        status=PoolDiscoveryStatus.VERIFIED,
        source="fixture:block-123",
        observed_at=datetime(2026, 9, 6, 10, 30, tzinfo=UTC),
        pools=(candidate,),
        diagnostics=("Accepted one fixture pool.",),
    )
    # The transport carries one coherent injected result across both routes.
    transport = httpx.ASGITransport(app=create_app(Settings(), pool_discovery=pool_discovery))
    # The HTTP client exercises JSON serialization and HTML rendering together.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Both requests represent the operator's machine-readable and visual views.
        api_response = await client.get("/api/venues/aerodrome/pools")
        dashboard_response = await client.get("/")

    assert api_response.status_code == 200
    assert api_response.json()["status"] == "verified"
    assert len(api_response.json()["pools"]) == 1
    assert "1 pools verified" in dashboard_response.text
    assert "Accepted one fixture pool." in dashboard_response.text


@pytest.mark.anyio
async def test_chainlink_endpoint_exposes_evidence_backed_unavailable_state() -> None:
    """The default oracle route names missing trust inputs and makes no feed claims."""
    # The default application has verified B20 identities but no reviewed live feed backend.
    transport = httpx.ASGITransport(app=create_app(Settings()))
    # The HTTP client exercises routing and response-model serialization together.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # The request represents the machine-readable diagnostic used by the dashboard.
        response = await client.get("/api/oracles/chainlink")

    # The payload proves that documented semantics are not confused with live observations.
    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "unavailable"
    assert payload["expected_assets"] == 10
    assert payload["configured_feeds"] == 0
    assert payload["healthy_feeds"] == 0
    assert payload["assessments"] == []
    assert payload["source_url"].endswith("/tokenized-equity-feeds/coinbase")
