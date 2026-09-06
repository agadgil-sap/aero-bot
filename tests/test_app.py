"""Behavior tests for the local dashboard and safety metadata."""

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from aero_bot.app import create_app
from aero_bot.audit import AuditEventType, AuditStore, AuditVerificationStatus
from aero_bot.config import Settings
from aero_bot.domain import RiskPolicy
from aero_bot.registry import B20RegistryResult, RegistryStatus
from aero_bot.transactions import TransactionPlanner, TransactionPolicy
from aero_bot.venues import (
    PoolCandidate,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
    PoolKind,
    VenueId,
)


def risk_request_payload() -> dict[str, object]:
    """Build complete public fixture evidence for the risk HTTP boundary."""
    return {
        "token_address": "0xb20000000000000000000078ee7ce2fe4908108c",
        "pool_address": "0x2222222222222222222222222222222222222222",
        "token_paused": False,
        "market_regime": "market_open",
        "oracle_healthy": True,
        "oracle_age_seconds": 60,
        "oracle_deviation_bps": "25",
        "pool_tvl_usd": "2000000",
        "exit_depth_usd": "50000",
        "compensation_mode": "unstaked_fees",
        "fee_apr": "4",
        "fee_retention_fraction": "0.9",
        "emissions_apr": "20",
        "impermanent_loss_apr": "0",
        "adverse_selection_apr": "0",
        "proposed_capital_usd": "5000",
        "realized_daily_loss_usd": "0",
    }


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
        "audit_status": "empty",
        "audit_record_count": 0,
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
    assert "Wallet onboarding unavailable" in response.text
    assert "Simulation backend unavailable" in response.text
    assert "Immutable audit chain" in response.text
    assert "Ready, no records" in response.text
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


@pytest.mark.anyio
async def test_transaction_api_is_wallet_free_and_emergency_halted_by_default() -> None:
    """Public capabilities remain disabled and default planning produces no payload."""
    # Default application uses an emergency-halted planner with no contract allowlists.
    transport = httpx.ASGITransport(app=create_app(Settings()))
    # The HTTP client exercises public capability and planning boundaries together.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Capability request provides the machine-readable first-release safety contract.
        capabilities_response = await client.get("/api/transactions/capabilities")
        # Planning request contains only public fixture identities and no wallet credential.
        planning_response = await client.post(
            "/api/transactions/plan/exact-allowance",
            json={
                "owner_address": "0x1111111111111111111111111111111111111111",
                "token_address": "0xb20000000000000000000078ee7ce2fe4908108c",
                "spender_address": "0x2222222222222222222222222222222222222222",
                "amount_raw": 1_250_000,
                "current_allowance_raw": 0,
                "block_number": 35_000_000,
            },
        )

    # Capabilities make absence of all key-bearing operations explicit.
    capabilities = capabilities_response.json()
    assert capabilities_response.status_code == 200
    assert capabilities["execution_mode"] == "simulation_only"
    assert capabilities["private_key_input_available"] is False
    assert capabilities["signing_available"] is False
    assert capabilities["broadcast_available"] is False
    assert capabilities["wallet_onboarding_available"] is False
    # Default policy returns no unsigned payload while emergency halt remains enabled.
    assert planning_response.status_code == 200
    assert planning_response.json()["status"] == "blocked"
    assert planning_response.json()["plan"] is None


@pytest.mark.anyio
async def test_transaction_routes_plan_then_report_simulation_unavailable() -> None:
    """An enabled fixture policy can plan but cannot sign, broadcast, or fake simulation."""
    # Explicit fixture policy allows only one token and one spender for this app instance.
    planner = TransactionPlanner(
        TransactionPolicy(
            emergency_halt=False,
            allowed_token_addresses=frozenset({"0xb20000000000000000000078ee7ce2fe4908108c"}),
            allowed_spender_addresses=frozenset({"0x2222222222222222222222222222222222222222"}),
        )
    )
    # Injected planner exercises ready behavior without changing safe application defaults.
    transport = httpx.ASGITransport(app=create_app(Settings(), transaction_planner=planner))
    # The client passes the serialized plan directly into stateless revalidation and simulation.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Planning input contains public state only.
        planning_response = await client.post(
            "/api/transactions/plan/exact-allowance",
            json={
                "owner_address": "0x1111111111111111111111111111111111111111",
                "token_address": "0xb20000000000000000000078ee7ce2fe4908108c",
                "spender_address": "0x2222222222222222222222222222222222222222",
                "amount_raw": 1_250_000,
                "current_allowance_raw": 0,
                "block_number": 35_000_000,
            },
        )
        # Returned plan is untrusted input again at the simulation endpoint.
        simulation_response = await client.post(
            "/api/transactions/simulate", json=planning_response.json()["plan"]
        )

    assert planning_response.status_code == 200
    assert planning_response.json()["status"] == "ready"
    assert simulation_response.status_code == 200
    assert simulation_response.json()["status"] == "unavailable"
    assert simulation_response.json()["observations"] == []


@pytest.mark.anyio
async def test_concentrated_position_endpoint_exposes_position_aware_loss() -> None:
    """The HTTP analysis boundary returns inventory and hold-relative loss evidence."""
    # Default application provides the pure analyzer without enabling transaction execution.
    transport = httpx.ASGITransport(app=create_app(Settings()))
    # The request exercises validation, Decimal math, routing, and JSON serialization together.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Exact-square fixture moves from a mixed entry position to token1-only above range.
        response = await client.post(
            "/api/analysis/concentrated-position",
            json={
                "liquidity": "100",
                "lower_price": "1",
                "upper_price": "4",
                "entry_price": "2.25",
                "current_price": "9",
                "entry_token0_usd": "2.25",
                "entry_token1_usd": "1",
                "current_token0_usd": "9",
                "current_token1_usd": "1",
                "holding_period_seconds": 31_536_000,
            },
        )

    # API result keeps values as JSON decimal strings or exact-compatible serialized numbers.
    payload = response.json()
    assert response.status_code == 200
    assert payload["range_state"] == "above_range"
    assert payload["current_amounts"] == {"token0": "0", "token1": "100"}
    assert payload["current_position_value_usd"] == "100"
    assert Decimal(payload["hold_value_usd"]) == Decimal(200)
    assert Decimal(payload["impermanent_loss_fraction"]) == Decimal("0.5")
    assert Decimal(payload["impermanent_loss_apr"]) == Decimal("0.5")


@pytest.mark.anyio
async def test_risk_endpoint_holds_and_never_combines_exclusive_returns() -> None:
    """Default API policy holds while exposing the selected compensation stream only."""
    # Default application keeps the risk engine emergency-halted and contract allowlists empty.
    transport = httpx.ASGITransport(app=create_app(Settings()))
    # The request exercises validation, compensation comparison, and risk routing together.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Unstaked fixture quotes both fee and emission alternatives for explicit comparison.
        response = await client.post(
            "/api/risk/evaluate",
            json=risk_request_payload(),
        )

    # Default safety gates hold while compensation remains mathematically inspectable.
    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "hold"
    assert payload["reasons"][:3] == [
        "emergency_halt",
        "token_not_allowlisted",
        "pool_not_allowlisted",
    ]
    assert Decimal(payload["compensation"]["selected_apr"]) == Decimal("3.6")
    assert Decimal(payload["compensation"]["alternative_apr"]) == Decimal("10")
    assert Decimal(payload["net_apr"]) == Decimal("3.6")


@pytest.mark.anyio
async def test_risk_endpoint_persists_reproducible_audit_evidence(tmp_path: Path) -> None:
    """A risk response is durably recorded with its input, policy, and output."""
    # Explicit store makes durable records inspectable through the real HTTP service boundary.
    audit_store = AuditStore(tmp_path / "risk-audit" / "audit.sqlite3")

    def fixed_clock() -> datetime:
        """Return one deterministic aware event time for the audited request."""
        # Fixed UTC evidence keeps the complete persisted envelope exactly assertable.
        return datetime(2026, 9, 6, 13, 15, tzinfo=UTC)

    # Injected storage and clock isolate persistence without changing application behavior.
    transport = httpx.ASGITransport(
        app=create_app(Settings(), audit_store=audit_store, clock=fixed_clock)
    )
    # One client checks the decision and both audit-health views after the append.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Request contains the complete public evidence required by deterministic evaluation.
        decision_response = await client.post(
            "/api/risk/evaluate",
            json=risk_request_payload(),
        )
        # Audit endpoint verifies the complete chain after the risk route returns.
        audit_response = await client.get("/api/audit/health")
        # Process health reports the same verified durable record count.
        health_response = await client.get("/health")
        # Dashboard must render the newly durable record with polished status copy.
        dashboard_response = await client.get("/")

    # Direct read confirms the API did not merely increment an in-memory counter.
    records = audit_store.read_records()
    # Canonical payload decoding exposes the exact persisted decision envelope.
    persisted_payload = json.loads(records[0].payload_json)
    assert decision_response.status_code == 200
    assert len(records) == 1
    assert records[0].event_type is AuditEventType.RISK_DECISION
    assert records[0].created_at == fixed_clock()
    assert persisted_payload["snapshot"]["token_address"] == (
        "0xb20000000000000000000078ee7ce2fe4908108c"  # noqa: S105
    )
    assert persisted_payload["policy"]["emergency_halt"] is True
    assert persisted_payload["decision"] == decision_response.json()
    assert audit_response.json()["status"] == AuditVerificationStatus.VERIFIED
    assert audit_response.json()["record_count"] == 1
    assert health_response.json()["audit_status"] == AuditVerificationStatus.VERIFIED
    assert health_response.json()["audit_record_count"] == 1
    assert "1 record verified" in dashboard_response.text


@pytest.mark.anyio
async def test_corrupt_audit_chain_degrades_health_and_dashboard(tmp_path: Path) -> None:
    """Local health surfaces durable audit corruption instead of reporting success."""
    # One valid record provides durable content for the tampering fixture.
    audit_store = AuditStore(tmp_path / "corrupt-audit" / "audit.sqlite3")
    audit_store.append(
        AuditEventType.SYSTEM_STATE,
        RiskPolicy(),
        datetime(2026, 9, 6, 14, 0, tzinfo=UTC),
    )
    # Privileged direct SQL models compromise beyond the ordinary append-only trigger boundary.
    connection = sqlite3.connect(audit_store.database_path)
    try:
        connection.execute("DROP TRIGGER audit_records_no_update")
        connection.execute("UPDATE audit_records SET payload_json = '{}' WHERE sequence = 1")
        connection.commit()
    finally:
        connection.close()

    # Injected corrupt store must remain visible so the operator can inspect its diagnostic.
    transport = httpx.ASGITransport(app=create_app(Settings(), audit_store=audit_store))
    # Health and dashboard exercise machine-readable and user-visible failure behavior.
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Both views independently verify the complete persistent chain.
        health_response = await client.get("/health")
        dashboard_response = await client.get("/")
        # Risk route must block rather than return a decision that cannot be audited.
        risk_response = await client.post("/api/risk/evaluate", json=risk_request_payload())

    assert health_response.status_code == 200
    assert health_response.json()["status"] == "degraded"
    assert health_response.json()["audit_status"] == AuditVerificationStatus.CORRUPT
    assert "Integrity failure" in dashboard_response.text
    assert "Audit record hash does not match its durable content." in dashboard_response.text
    assert risk_response.status_code == 503
    assert "audit integrity failed" in risk_response.json()["detail"]
    assert audit_store.verify_chain().record_count == 1
