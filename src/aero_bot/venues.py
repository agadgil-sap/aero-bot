"""Strict venue contracts and fail-closed Aerodrome pool discovery boundary."""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress

# Aerodrome's official classic-contract repository publishes its Base deployments.
AERODROME_CLASSIC_SOURCE_URL = "https://github.com/aerodrome-finance/contracts"
# Aerodrome's official Slipstream repository publishes all concentrated-liquidity deployments.
AERODROME_SLIPSTREAM_SOURCE_URL = "https://github.com/aerodrome-finance/slipstream"
# Circle publishes the native Base USDC contract and distinguishes it from bridged USDbC.
CIRCLE_BASE_USDC_SOURCE_URL = "https://www.circle.com/blog/usdc-now-available-natively-on-base"
# The evidence date records when the official deployment tables were reviewed.
AERODROME_SOURCE_OBSERVED_AT = date(2026, 9, 6)
# Aerodrome's factory registry is the onchain authority for approved factory relationships.
AERODROME_FACTORY_REGISTRY_ADDRESS = "0x5C3F18F06CC09CA1910767A34a20F771039E37C0"
# Aerodrome's voter maps pools to gauges and exposes whether each gauge is alive.
AERODROME_VOTER_ADDRESS = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
# The AERO reward token is required for emissions valuation.
AERO_TOKEN_ADDRESS = "0x940181a94A35A4569E4529A3CDfB74e38FD98631"  # noqa: S105
# Circle's native Base USDC is the only quote asset supported by this release.
BASE_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# The classic factory creates stable and volatile Aerodrome v2 pools.
AERODROME_CLASSIC_FACTORY_ADDRESS = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
# The initial Slipstream deployment remains relevant to existing pools and gauges.
SLIPSTREAM_INITIAL_FACTORY_ADDRESS = "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
# The gauge-caps Slipstream deployment remains relevant to its existing pools and gauges.
SLIPSTREAM_GAUGE_CAPS_FACTORY_ADDRESS = "0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a"
# The Gauges V3 Slipstream deployment is the current factory for new concentrated pools.
SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS = "0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef"


class VenueId(StrEnum):
    """Identify venues that can be explicitly enabled by this application."""

    # Aerodrome is the only reputable venue approved for the first release.
    AERODROME = "aerodrome"


# Adding a future venue requires both a new identifier and explicit allowlist membership.
REPUTABLE_VENUE_ALLOWLIST = frozenset({VenueId.AERODROME})


class PoolKind(StrEnum):
    """Distinguish pool mathematics and discovery interfaces."""

    # Classic stable pools use Aerodrome's correlated-asset invariant.
    CLASSIC_STABLE = "classic_stable"
    # Classic volatile pools use a constant-product invariant.
    CLASSIC_VOLATILE = "classic_volatile"
    # Slipstream pools use concentrated-liquidity tick ranges.
    SLIPSTREAM = "slipstream"


class FactoryGeneration(StrEnum):
    """Identify why an official pool factory remains in the hard allowlist."""

    # Classic is the original stable and volatile pool system.
    CLASSIC = "classic"
    # Initial is the first Slipstream concentrated-liquidity deployment.
    SLIPSTREAM_INITIAL = "slipstream_initial"
    # Gauge caps introduced capped gauge emissions and remains active for existing pools.
    SLIPSTREAM_GAUGE_CAPS = "slipstream_gauge_caps"
    # Gauges V3 is the latest deployment used for newly created gauges.
    SLIPSTREAM_GAUGES_V3 = "slipstream_gauges_v3"


class PoolDiscoveryStatus(StrEnum):
    """Describe whether pool observations are safe to consume."""

    # Verified means every candidate passed venue, factory, pair, and uniqueness checks.
    VERIFIED = "verified"
    # Unavailable means no live source produced observations.
    UNAVAILABLE = "unavailable"
    # Rejected means a source returned at least one candidate outside the hard boundary.
    REJECTED = "rejected"


class PoolDiscoveryUnavailableError(RuntimeError):
    """Signal that a read-only backend could not complete trustworthy observations."""


class FactoryContract(BaseModel):
    """Describe one officially published Aerodrome pool factory."""

    # Frozen strict fields make the factory evidence stable during discovery.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Generation explains the deployment lineage and expected pool behavior.
    generation: FactoryGeneration
    # Address is the normalized official factory contract on Base.
    address: EvmAddress
    # Supported kinds prevent confusing classic and concentrated-liquidity pools.
    supported_pool_kinds: frozenset[PoolKind]
    # Source URL identifies the Aerodrome repository publishing this deployment.
    source_url: str
    # Current-for-new-pools distinguishes the latest deployment without excluding historical pools.
    current_for_new_pools: bool


class AerodromeContractEvidence(BaseModel):
    """Collect the hard contract boundary and its primary-source provenance."""

    # Frozen strict fields prevent runtime mutation of trusted contracts.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Venue is fixed to Aerodrome for this adapter evidence.
    venue: VenueId
    # Chain ID fixes every contract to Base mainnet.
    chain_id: Annotated[int, Field(ge=1)]
    # Factory registry is the official onchain approval registry.
    factory_registry_address: EvmAddress
    # Voter links pools with gauges and emission state.
    voter_address: EvmAddress
    # Reward token is the AERO asset used for emissions.
    reward_token_address: EvmAddress
    # Quote token is native Circle-issued USDC rather than bridged USDbC.
    quote_token_address: EvmAddress
    # Pool factories include all officially published classic and Slipstream generations.
    pool_factories: tuple[FactoryContract, ...]
    # Observed date makes the age of static contract evidence visible.
    source_observed_at: date
    # Quote source preserves Circle's authoritative USDC provenance.
    quote_token_source_url: str


class PoolCandidate(BaseModel):
    """Represent one pool returned by an injected read-only discovery backend."""

    # Frozen strict fields prevent source observations changing during validation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Pool address identifies the candidate contract on Base.
    pool_address: EvmAddress
    # Factory address must match one official hard-allowlisted deployment.
    factory_address: EvmAddress
    # Token zero is read directly from the candidate pool contract.
    token0_address: EvmAddress
    # Token one is read directly from the candidate pool contract.
    token1_address: EvmAddress
    # Pool kind selects the correct invariant and position mathematics.
    pool_kind: PoolKind
    # Fee basis points record the active swap fee observed by the backend.
    fee_bps: Annotated[int, Field(ge=0, le=10_000)]
    # Gauge address is absent when Aerodrome has no gauge for the pool.
    gauge_address: EvmAddress | None


class PoolDiscoveryBatch(BaseModel):
    """Carry raw backend observations with source and freshness evidence."""

    # Frozen strict fields preserve one coherent backend response.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Source describes the read-only RPC backend and block evidence used.
    source: str
    # Observed time records when the backend completed its reads.
    observed_at: datetime
    # Candidates remain untrusted until the Aerodrome adapter validates them.
    candidates: tuple[PoolCandidate, ...]


class PoolDiscoveryResult(BaseModel):
    """Expose accepted pools or explicit fail-closed diagnostics."""

    # Frozen strict fields preserve the complete discovery outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Venue identifies which strict adapter produced the result.
    venue: VenueId
    # Status determines whether any returned pools may reach risk evaluation.
    status: PoolDiscoveryStatus
    # Source identifies either the live backend or the missing configuration boundary.
    source: str
    # Observed time is absent when no onchain observation occurred.
    observed_at: datetime | None
    # Pools are empty unless the entire discovery batch passes validation.
    pools: tuple[PoolCandidate, ...]
    # Diagnostics provide ordered evidence for acceptance, rejection, or unavailability.
    diagnostics: tuple[str, ...]


@runtime_checkable
class PoolDiscoveryBackend(Protocol):
    """Define the read-only backend required by the Aerodrome venue adapter."""

    def discover(
        self,
        factory_addresses: frozenset[str],
        b20_addresses: frozenset[str],
        quote_token_address: str,
    ) -> PoolDiscoveryBatch:
        """Read candidate pools without signing, sending, or mutating chain state.

        Args:
            factory_addresses: Official Aerodrome factories eligible for read calls.
            b20_addresses: Official Coinbase-issued B20 contracts eligible for pairing.
            quote_token_address: Official native Base USDC contract.

        Returns:
            Source-stamped raw candidates for adapter validation.
        """
        ...


@runtime_checkable
class VenueAdapter(Protocol):
    """Define the strict interface every future reputable venue must implement."""

    @property
    def venue_id(self) -> VenueId:
        """Return the explicitly allowlisted venue identifier."""
        ...

    def discover_pools(self, b20_addresses: frozenset[str]) -> PoolDiscoveryResult:
        """Discover only official B20 pairs through venue-approved contracts.

        Args:
            b20_addresses: Issuer-verified contracts permitted for pool matching.

        Returns:
            Validated pools or an evidence-backed fail-closed diagnostic.
        """
        ...


def aerodrome_contract_evidence() -> AerodromeContractEvidence:
    """Build immutable contract evidence from Aerodrome and Circle primary sources."""
    return AerodromeContractEvidence(
        venue=VenueId.AERODROME,
        chain_id=8453,
        factory_registry_address=AERODROME_FACTORY_REGISTRY_ADDRESS,
        voter_address=AERODROME_VOTER_ADDRESS,
        reward_token_address=AERO_TOKEN_ADDRESS,
        quote_token_address=BASE_USDC_ADDRESS,
        pool_factories=(
            FactoryContract(
                generation=FactoryGeneration.CLASSIC,
                address=AERODROME_CLASSIC_FACTORY_ADDRESS,
                supported_pool_kinds=frozenset(
                    {PoolKind.CLASSIC_STABLE, PoolKind.CLASSIC_VOLATILE}
                ),
                source_url=AERODROME_CLASSIC_SOURCE_URL,
                current_for_new_pools=True,
            ),
            FactoryContract(
                generation=FactoryGeneration.SLIPSTREAM_INITIAL,
                address=SLIPSTREAM_INITIAL_FACTORY_ADDRESS,
                supported_pool_kinds=frozenset({PoolKind.SLIPSTREAM}),
                source_url=AERODROME_SLIPSTREAM_SOURCE_URL,
                current_for_new_pools=False,
            ),
            FactoryContract(
                generation=FactoryGeneration.SLIPSTREAM_GAUGE_CAPS,
                address=SLIPSTREAM_GAUGE_CAPS_FACTORY_ADDRESS,
                supported_pool_kinds=frozenset({PoolKind.SLIPSTREAM}),
                source_url=AERODROME_SLIPSTREAM_SOURCE_URL,
                current_for_new_pools=False,
            ),
            FactoryContract(
                generation=FactoryGeneration.SLIPSTREAM_GAUGES_V3,
                address=SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
                supported_pool_kinds=frozenset({PoolKind.SLIPSTREAM}),
                source_url=AERODROME_SLIPSTREAM_SOURCE_URL,
                current_for_new_pools=True,
            ),
        ),
        source_observed_at=AERODROME_SOURCE_OBSERVED_AT,
        quote_token_source_url=CIRCLE_BASE_USDC_SOURCE_URL,
    )


class AerodromeVenueAdapter:
    """Validate read-only Aerodrome candidates against hard trust boundaries."""

    def __init__(self, backend: PoolDiscoveryBackend | None = None) -> None:
        """Create an adapter with an optional read-only discovery backend.

        Args:
            backend: Read-only RPC implementation, or None for explicit unavailable diagnostics.
        """
        # Static evidence supplies every trusted contract and its source provenance.
        self._contracts = aerodrome_contract_evidence()
        # An absent backend is a supported fail-closed operating state.
        self._backend = backend

    @property
    def venue_id(self) -> VenueId:
        """Return Aerodrome's explicitly allowlisted venue identifier."""
        return VenueId.AERODROME

    def discover_pools(self, b20_addresses: frozenset[str]) -> PoolDiscoveryResult:
        """Return only official B20 and native-USDC pools from approved factories.

        Args:
            b20_addresses: Issuer-verified B20 contracts allowed for pool matching.

        Returns:
            An all-or-nothing pool set with freshness and validation diagnostics.
        """
        # Normalized official factories are the sole contracts a backend may query.
        factories_by_address = {
            factory.address: factory for factory in self._contracts.pool_factories
        }
        # The immutable set is passed to the backend without arbitrary factory expansion.
        factory_addresses = frozenset(factories_by_address)
        # Registry addresses are normalized before pair validation and backend use.
        normalized_b20_addresses = frozenset(address.lower() for address in b20_addresses)

        if self._backend is None:
            return PoolDiscoveryResult(
                venue=self.venue_id,
                status=PoolDiscoveryStatus.UNAVAILABLE,
                source="not_configured",
                observed_at=None,
                pools=(),
                diagnostics=(
                    "No read-only Base RPC discovery backend is configured; no onchain pool "
                    f"claims were made after loading {len(factory_addresses)} official Aerodrome "
                    f"factories and {len(normalized_b20_addresses)} official B20 identities.",
                ),
            )

        try:
            # The backend returns read-only candidates for independent validation.
            batch = self._backend.discover(
                factory_addresses,
                normalized_b20_addresses,
                self._contracts.quote_token_address,
            )
        except PoolDiscoveryUnavailableError as error:
            return PoolDiscoveryResult(
                venue=self.venue_id,
                status=PoolDiscoveryStatus.UNAVAILABLE,
                source="read_only_backend",
                observed_at=None,
                pools=(),
                diagnostics=(f"Aerodrome pool discovery backend was unavailable: {error}",),
            )
        # Rejection evidence accumulates in candidate order for deterministic diagnostics.
        diagnostics: list[str] = []
        # Duplicate pool contracts are rejected because they imply inconsistent source output.
        seen_pool_addresses: set[str] = set()

        for candidate in batch.candidates:
            # The candidate factory determines the supported invariant family.
            factory = factories_by_address.get(candidate.factory_address)
            if factory is None:
                diagnostics.append(
                    f"Pool {candidate.pool_address} came from non-allowlisted factory "
                    f"{candidate.factory_address}."
                )
                continue
            if candidate.pool_kind not in factory.supported_pool_kinds:
                diagnostics.append(
                    f"Pool {candidate.pool_address} reported kind {candidate.pool_kind} that is "
                    f"incompatible with factory {candidate.factory_address}."
                )
            # Pair membership must contain exactly native USDC and one issuer-verified B20.
            candidate_tokens = frozenset({candidate.token0_address, candidate.token1_address})
            # Matching assets identify whether exactly one official B20 is present.
            matching_b20_addresses = candidate_tokens.intersection(normalized_b20_addresses)
            if (
                self._contracts.quote_token_address not in candidate_tokens
                or len(candidate_tokens) != 2
                or len(matching_b20_addresses) != 1
            ):
                diagnostics.append(
                    f"Pool {candidate.pool_address} is not an official B20/native-USDC pair."
                )
            if candidate.pool_address in seen_pool_addresses:
                diagnostics.append(
                    f"Pool {candidate.pool_address} appeared more than once in one discovery batch."
                )
            seen_pool_addresses.add(candidate.pool_address)

        if diagnostics:
            return PoolDiscoveryResult(
                venue=self.venue_id,
                status=PoolDiscoveryStatus.REJECTED,
                source=batch.source,
                observed_at=batch.observed_at,
                pools=(),
                diagnostics=tuple(diagnostics),
            )

        return PoolDiscoveryResult(
            venue=self.venue_id,
            status=PoolDiscoveryStatus.VERIFIED,
            source=batch.source,
            observed_at=batch.observed_at,
            pools=batch.candidates,
            diagnostics=(
                f"Accepted {len(batch.candidates)} Aerodrome pools after factory, pair, kind, "
                "and uniqueness validation.",
            ),
        )
