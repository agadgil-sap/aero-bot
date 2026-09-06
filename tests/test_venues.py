"""Behavior tests for the strict Aerodrome venue boundary."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from aero_bot.venues import (
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
            candidates: Untrusted pool observations for adapter validation.
        """
        # Candidates remain immutable so repeated discovery calls are deterministic.
        self._candidates = candidates
        # Calls capture the adapter's query boundary for allowlist assertions.
        self.calls: list[tuple[frozenset[str], frozenset[str], str]] = []

    def discover(
        self,
        factory_addresses: frozenset[str],
        b20_addresses: frozenset[str],
        quote_token_address: str,
    ) -> PoolDiscoveryBatch:
        """Return the fixture batch and record every constrained query input.

        Args:
            factory_addresses: Factories the adapter permits this backend to query.
            b20_addresses: Issuer identities the adapter permits this backend to match.
            quote_token_address: Native USDC contract required by the adapter.

        Returns:
            A deterministic source-stamped candidate batch.
        """
        self.calls.append((factory_addresses, b20_addresses, quote_token_address))
        return PoolDiscoveryBatch(
            source="fixture:block-123",
            observed_at=datetime(2026, 9, 6, 10, 30, tzinfo=UTC),
            candidates=self._candidates,
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
        "fee_bps": 30,
        "gauge_address": GAUGE_ADDRESS,
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
    # Addresses form the hard set permitted for backend factory reads.
    factory_addresses = {factory.address for factory in contracts.pool_factories}

    assert contracts.chain_id == 8453
    assert contracts.quote_token_address == BASE_USDC_ADDRESS.lower()
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
    assert "No read-only Base RPC discovery backend is configured" in result.diagnostics[0]
    assert "4 official Aerodrome factories" in result.diagnostics[0]


def test_adapter_accepts_only_validated_official_pair() -> None:
    """A valid candidate survives every adapter boundary with freshness evidence."""
    # The fixture backend supplies one candidate matching official contracts and pool kind.
    backend = FixtureBackend((valid_candidate(),))
    # The adapter validates the raw batch before returning any pool.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.VERIFIED
    assert result.pools == (valid_candidate(),)
    assert result.observed_at == datetime(2026, 9, 6, 10, 30, tzinfo=UTC)
    assert len(backend.calls) == 1
    assert len(backend.calls[0][0]) == 4
    assert backend.calls[0][1] == frozenset({B20_ADDRESS})
    assert backend.calls[0][2] == BASE_USDC_ADDRESS.lower()


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
    assert "RPC timed out at block 123" in result.diagnostics[0]


@pytest.mark.parametrize(
    ("overrides", "expected_diagnostic"),
    [
        (
            {"factory_address": "0x3333333333333333333333333333333333333333"},
            "non-allowlisted factory",
        ),
        (
            {"pool_kind": PoolKind.CLASSIC_VOLATILE},
            "incompatible with factory",
        ),
        (
            {"token1_address": "0x4444444444444444444444444444444444444444"},
            "not an official B20/native-USDC pair",
        ),
    ],
)
def test_adapter_rejects_candidate_outside_hard_boundary(
    overrides: dict[str, object], expected_diagnostic: str
) -> None:
    """One invalid candidate rejects the complete batch without leaking partial pools.

    Args:
        overrides: Candidate fields that violate one adapter boundary.
        expected_diagnostic: Evidence fragment identifying the failed boundary.
    """
    # The backend returns one deliberately non-compliant candidate.
    backend = FixtureBackend((valid_candidate(**overrides),))
    # The adapter must reject rather than filter and return a misleading partial set.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.REJECTED
    assert result.pools == ()
    assert any(expected_diagnostic in diagnostic for diagnostic in result.diagnostics)


def test_adapter_rejects_duplicate_pool_observations() -> None:
    """Duplicate source observations invalidate the entire discovery batch."""
    # The same contract appears twice to simulate an inconsistent backend merge.
    backend = FixtureBackend((valid_candidate(), valid_candidate()))
    # The adapter detects the duplicate after validating both observations.
    result = AerodromeVenueAdapter(backend).discover_pools(frozenset({B20_ADDRESS}))

    assert result.status is PoolDiscoveryStatus.REJECTED
    assert result.pools == ()
    assert "appeared more than once" in result.diagnostics[0]


def test_classic_factory_accepts_classic_pool_kind() -> None:
    """The classic factory permits its volatile invariant without enabling other venues."""
    # The classic candidate changes only the official factory and compatible invariant kind.
    candidate = valid_candidate(
        factory_address=AERODROME_CLASSIC_FACTORY_ADDRESS,
        pool_kind=PoolKind.CLASSIC_VOLATILE,
    )
    # The adapter uses per-factory pool-kind constraints instead of a global kind allowlist.
    result = AerodromeVenueAdapter(FixtureBackend((candidate,))).discover_pools(
        frozenset({B20_ADDRESS})
    )

    assert result.status is PoolDiscoveryStatus.VERIFIED
