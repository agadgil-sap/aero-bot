"""Behavior tests for the strict Aerodrome venue boundary."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    AERODROME_CLASSIC_FACTORY_ADDRESS,
    BASE_USDC_ADDRESS,
    REPUTABLE_VENUE_ALLOWLIST,
    SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
    AerodromeVenueAdapter,
    PoolCandidate,
    PoolDiscoveryBatch,
    PoolDiscoveryStatus,
    PoolDiscoveryUnavailableError,
    PoolKind,
    VenueId,
    aerodrome_contract_evidence,
)

# A fixture token matches one officially listed B20 contract.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
# A fixture pool represents a source-returned contract without making an onchain claim.
POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
# A fixture gauge represents optional gauge linkage without making an onchain claim.
GAUGE_ADDRESS = "0x2222222222222222222222222222222222222222"


class FixtureBackend:
    """Return deterministic read-only discovery observations for adapter tests."""

    def __init__(self, candidates: tuple[PoolCandidate, ...]) -> None:
        """Store the exact candidates returned by this fixture.

        Args:
            candidates: Untrusted pair-scoped pool observations for adapter validation.
        """
        # Candidates remain immutable so repeated discovery calls are deterministic.
        self._candidates = candidates
        # Calls capture the adapter's query boundary for scope assertions.
        self.calls: list[tuple[frozenset[str], str]] = []

    def discover(
        self,
        b20_addresses: frozenset[str],
        quote_token_address: str,
    ) -> PoolDiscoveryBatch:
        """Return the fixture batch and record every constrained query input.

        Args:
            b20_addresses: Issuer identities the adapter permits this backend to match.
            quote_token_address: Native USDC contract required by the adapter.

        Returns:
            A deterministic source-stamped candidate batch.
        """
        self.calls.append((b20_addresses, quote_token_address))
        return PoolDiscoveryBatch(
            source="fixture:block-123",
            observed_at=datetime(2026, 9, 6, 10, 30, tzinfo=UTC),
            candidates=self._candidates,
            enumerated_pool_count=22_674,
        )


def valid_candidate(**overrides: object) -> PoolCandidate:
    """Build a valid official B20/native-USDC Slipstream candidate.

    Args:
        **overrides: Candidate fields changed to exercise one trust-boundary behavior.

    Returns:
        A validated immutable pool candidate.
    """
    # Baseline values comply with the published Gauges V3 factory deployment.
    values: dict[str, object] = {
        "pool_address": POOL_ADDRESS,
        "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "token0_address": B20_ADDRESS,
        "token1_address": BASE_USDC_ADDRESS,
        "pool_kind": PoolKind.SLIPSTREAM,
        "tick_spacing": 10,
        "current_tick": -5,
        "sqrt_ratio": 1 << 96,
        "pool_fee_ppm": 500,
        "unstaked_fee_ppm": 100_000,
        "reserve0": 10**18,
        "reserve1": 5_000_000,
        "staked0": 10**17,
        "staked1": 1_000_000,
        "gauge_address": GAUGE_ADDRESS,
        "gauge_liquidity": 9_999,
        "gauge_alive": True,
        "emissions_per_second": 4_494_371_922_759_724,
        "emissions_token_address": AERO_TOKEN_ADDRESS,
    }
    values.update(overrides)
    return PoolCandidate.model_validate(values)


def test_only_aerodrome_is_a_reputable_enabled_venue() -> None:
    """The venue allowlist cannot silently broaden beyond Aerodrome."""
    assert frozenset({VenueId.AERODROME}) == REPUTABLE_VENUE_ALLOWLIST


def test_contract_evidence_contains_all_official_factory_generations() -> None:
    """Classic and all three published Slipstream factories remain discoverable."""
    # Primary-source evidence is rebuilt through the same path used by the adapter.
    contracts = aerodrome_contract_evidence()
    # Addresses form the hard set of officially known factory deployments.
    factory_addresses = {factory.address for factory in contracts.pool_factories}

    assert contracts.chain_id == 8453
    assert contracts.quote_token_address == BASE_USDC_ADDRESS.lower()
    assert contracts.reward_token_address == AERO_TOKEN_ADDRESS.lower()
    assert len(factory_addresses) == 4
    assert AERODROME_CLASSIC_FACTORY_ADDRESS.lower() in factory_addresses
    assert SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS.lower() in factory_addresses
    assert all(
        factory.source_url.startswith("https://github.com/aerodrome-finance/")
        for factory in contracts.pool_factories
    )


def test_missing_backend_returns_explicit_unavailable_diagnostic() -> None:
    """An unconfigured RPC path produces no pool claims and reports inspected evidence."""
    # The adapter intentionally starts without any live discovery capability.
    result = AerodromeVenueAdapter().discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.UNAVAILABLE
    assert result.pools == ()
    assert result.observed_at is None
    assert "No LP Sugar read-only Base RPC discovery backend is configured" in result.diagnostics[0]
    assert "3 official Slipstream factories" in result.diagnostics[0]


def test_adapter_accepts_validated_official_pair() -> None:
    """A valid candidate survives every adapter boundary with freshness evidence."""
    # The fixture backend supplies one candidate matching official contracts and gauge state.
    backend = FixtureBackend((valid_candidate(),))
    # The adapter validates the raw batch before returning any pool.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.VERIFIED
    assert result.pools == (valid_candidate(),)
    assert result.observed_at == datetime(2026, 9, 6, 10, 30, tzinfo=UTC)
    assert len(backend.calls) == 1
    assert backend.calls[0][0] == frozenset({B20_ADDRESS})
    assert backend.calls[0][1] == BASE_USDC_ADDRESS.lower()
    # The summary diagnostic reports both the pair-scoped and enumerated counts.
    assert "Accepted 1 of 1 pair-scoped Aerodrome pools" in result.diagnostics[0]
    assert "inspected 22674 pools" in result.diagnostics[0]


def test_backend_failure_returns_explicit_unavailable_diagnostic() -> None:
    """A read-only source failure produces no pools and retains operator evidence."""
    # The protocol-shaped mock raises the backend's explicit availability exception.
    backend = Mock()
    backend.discover.side_effect = PoolDiscoveryUnavailableError("RPC timed out at block 123")
    # The adapter converts external failure into a stable fail-closed result.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.UNAVAILABLE
    assert result.pools == ()
    assert result.observed_at is None
    assert "LP Sugar discovery backend was unavailable" in result.diagnostics[0]
    assert "RPC timed out at block 123" in result.diagnostics[0]


@pytest.mark.parametrize(
    ("overrides", "expected_diagnostic"),
    [
        (
            {"factory_address": "0x3333333333333333333333333333333333333333"},
            "non-allowlisted factory",
        ),
        (
            {
                "factory_address": AERODROME_CLASSIC_FACTORY_ADDRESS,
                "pool_kind": PoolKind.SLIPSTREAM,
            },
            "incompatible with factory",
        ),
        (
            {"token1_address": "0x4444444444444444444444444444444444444444"},
            "outside the official B20/native-USDC pair boundary",
        ),
    ],
)
def test_adapter_rejects_batch_evidence_violating_backend_contract(
    overrides: dict[str, object], expected_diagnostic: str
) -> None:
    """Source inconsistencies reject the complete batch without leaking partial pools.

    Args:
        overrides: Candidate fields that violate one backend-contract invariant.
        expected_diagnostic: Evidence fragment identifying the failed invariant.
    """
    # The backend returns one deliberately inconsistent observation.
    backend = FixtureBackend((valid_candidate(**overrides),))
    # The adapter must reject rather than filter and return a misleading partial set.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.REJECTED
    assert result.pools == ()
    assert any(expected_diagnostic in diagnostic for diagnostic in result.diagnostics)


def test_adapter_rejects_duplicate_pool_observations() -> None:
    """Duplicate source observations invalidate the entire discovery batch."""
    # The same contract appears twice to simulate an inconsistent pagination merge.
    backend = FixtureBackend((valid_candidate(), valid_candidate()))
    # The adapter detects the duplicate after validating both observations.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.REJECTED
    assert result.pools == ()
    assert "appeared more than once" in result.diagnostics[0]


@pytest.mark.parametrize(
    ("overrides", "expected_diagnostic"),
    [
        ({"gauge_alive": False}, "no live Aerodrome gauge"),
        ({"gauge_address": None}, "no live Aerodrome gauge"),
        ({"emissions_per_second": 0}, "not emitting official AERO"),
        (
            {"emissions_token_address": "0x5555555555555555555555555555555555555555"},
            "not emitting official AERO",
        ),
        (
            {
                "factory_address": AERODROME_CLASSIC_FACTORY_ADDRESS,
                "pool_kind": PoolKind.CLASSIC_VOLATILE,
                "tick_spacing": -1,
            },
            "outside the Slipstream-only policy",
        ),
    ],
)
def test_adapter_excludes_pools_failing_policy_conditions(
    overrides: dict[str, object], expected_diagnostic: str
) -> None:
    """Pools with dead or non-emitting gauges are excluded with explicit evidence.

    Args:
        overrides: Candidate fields that fail one acceptance condition.
        expected_diagnostic: Evidence fragment documenting the exclusion.
    """
    # The backend returns one in-scope candidate that fails one policy condition.
    backend = FixtureBackend((valid_candidate(**overrides),))
    # The adapter documents the exclusion instead of failing the entire batch.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.VERIFIED
    assert result.pools == ()
    assert len(result.diagnostics) == 2
    assert expected_diagnostic in result.diagnostics[1]


def test_adapter_separates_accepted_pools_from_excluded_pools() -> None:
    """A mixed batch returns only accepted pools with every exclusion documented."""
    # One healthy pool and one dead-gauge pool exercise both outcome paths.
    healthy = valid_candidate()
    dead_gauge = valid_candidate(
        pool_address="0x1212121212121212121212121212121212121212", gauge_alive=False
    )
    result = AerodromeVenueAdapter(FixtureBackend((healthy, dead_gauge))).discover_pools(
        frozenset({B20_ADDRESS})
    )

    assert result.status is PoolDiscoveryStatus.VERIFIED
    assert result.pools == (healthy,)
    assert "Accepted 1 of 2 pair-scoped Aerodrome pools" in result.diagnostics[0]
    assert "no live Aerodrome gauge" in result.diagnostics[1]
