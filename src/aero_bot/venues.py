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

    # Verified means every returned pool passed all hard checks with exclusions documented.
    VERIFIED = "verified"
    # Unavailable means no live source produced observations.
    UNAVAILABLE = "unavailable"
    # Rejected means a source returned evidence inconsistent with its own contract.
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
    # Tick spacing is positive for Slipstream pools and 0 or -1 for classic pools.
    tick_spacing: int
    # The current pool tick is zero for classic pools.
    current_tick: int
    # The current square-root price is zero for classic pools.
    sqrt_ratio: Annotated[int, Field(ge=0)]
    # Pool fee is the Slipstream fee tier in parts per million.
    pool_fee_ppm: Annotated[int, Field(ge=0)]
    # Unstaked fee is the higher tier charged when liquidity is not staked.
    unstaked_fee_ppm: Annotated[int, Field(ge=0)]
    # Reserve zero is the raw token-unit pool balance.
    reserve0: Annotated[int, Field(ge=0)]
    # Reserve one is the raw token-unit pool balance.
    reserve1: Annotated[int, Field(ge=0)]
    # Staked zero is the raw token-unit balance held in gauge positions.
    staked0: Annotated[int, Field(ge=0)]
    # Staked one is the raw token-unit balance held in gauge positions.
    staked1: Annotated[int, Field(ge=0)]
    # Gauge address is absent when Aerodrome has no gauge for the pool.
    gauge_address: EvmAddress | None
    # Gauge liquidity measures the staked liquidity sharing gauge emissions.
    gauge_liquidity: Annotated[int, Field(ge=0)]
    # Gauge liveness is Aerodrome's own kill-switch state for emissions.
    gauge_alive: bool
    # Emissions are the raw per-second reward rate paid by the gauge.
    emissions_per_second: Annotated[int, Field(ge=0)]
    # The emissions token is absent when the gauge is not emitting.
    emissions_token_address: EvmAddress | None


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
    # Enumerated count records the complete inventory size before pair scoping.
    enumerated_pool_count: Annotated[int, Field(ge=0)]


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
        b20_addresses: frozenset[str],
        quote_token_address: str,
    ) -> PoolDiscoveryBatch:
        """Read candidate pools without signing, sending, or mutating chain state.

        Args:
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
        """Return official B20/native-USDC Slipstream pools with live AERO gauges.

        The authoritative source is the LP Sugar enumeration backend, which supplies
        every Aerodrome pool in one coherent block-pinned snapshot. Pools outside the
        B20/native-USDC pair scope never reach this adapter; this method independently
        revalidates that scope and then applies the factory, gauge, and emissions
        acceptance boundaries.

        Args:
            b20_addresses: Issuer-verified B20 contracts allowed for pool matching.

        Returns:
            Validated pools plus ordered acceptance, exclusion, or failure diagnostics.
        """
        # Official factory evidence identifies every known deployment lineage.
        factories_by_address = {
            factory.address: factory for factory in self._contracts.pool_factories
        }
        # The emissions-farming policy accepts only the three official Slipstream factories.
        slipstream_factory_addresses = frozenset(
            factory.address
            for factory in self._contracts.pool_factories
            if PoolKind.SLIPSTREAM in factory.supported_pool_kinds
        )
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
                    "No LP Sugar read-only Base RPC discovery backend is configured; no onchain "
                    f"pool claims were made after loading {len(slipstream_factory_addresses)} "
                    f"official Slipstream factories and {len(normalized_b20_addresses)} official "
                    "B20 identities.",
                ),
            )

        try:
            # The backend returns pair-scoped read-only candidates for validation.
            batch = self._backend.discover(
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
                diagnostics=(f"Aerodrome LP Sugar discovery backend was unavailable: {error}",),
            )
        # Integrity violations reject the entire batch because they contradict the
        # backend's own enumeration contract rather than describing market state.
        integrity_diagnostics: list[str] = []
        # Exclusion diagnostics document pools that were inspected but not accepted.
        exclusion_diagnostics: list[str] = []
        # Duplicate pool contracts imply inconsistent pagination or source state.
        seen_pool_addresses: set[str] = set()
        accepted_pools: list[PoolCandidate] = []

        for candidate in batch.candidates:
            # Pair membership must contain exactly native USDC and one issuer-verified B20.
            candidate_tokens = frozenset({candidate.token0_address, candidate.token1_address})
            matching_b20_addresses = candidate_tokens.intersection(normalized_b20_addresses)
            if (
                self._contracts.quote_token_address not in candidate_tokens
                or len(candidate_tokens) != 2
                or len(matching_b20_addresses) != 1
            ):
                integrity_diagnostics.append(
                    f"Pair-scoped backend returned pool {candidate.pool_address} outside the "
                    "official B20/native-USDC pair boundary."
                )
                continue
            # The candidate factory determines the supported invariant family.
            factory = factories_by_address.get(candidate.factory_address)
            if factory is None:
                integrity_diagnostics.append(
                    f"Pool {candidate.pool_address} came from non-allowlisted factory "
                    f"{candidate.factory_address}."
                )
                continue
            if candidate.pool_kind not in factory.supported_pool_kinds:
                integrity_diagnostics.append(
                    f"Pool {candidate.pool_address} reported kind {candidate.pool_kind} that is "
                    f"incompatible with factory {candidate.factory_address}."
                )
                continue
            if candidate.pool_address in seen_pool_addresses:
                integrity_diagnostics.append(
                    f"Pool {candidate.pool_address} appeared more than once in one discovery batch."
                )
                continue
            seen_pool_addresses.add(candidate.pool_address)
            # Only the three official Slipstream factories support this policy's
            # concentrated-liquidity mathematics; classic pairs are excluded by policy.
            if candidate.factory_address not in slipstream_factory_addresses:
                exclusion_diagnostics.append(
                    f"Pool {candidate.pool_address} is a B20/native-USDC pair from "
                    f"{factory.generation} factory {candidate.factory_address} that is outside "
                    "the Slipstream-only policy."
                )
                continue
            # A pool without a live gauge cannot support staked emissions farming.
            if candidate.gauge_address is None or not candidate.gauge_alive:
                exclusion_diagnostics.append(
                    f"Pool {candidate.pool_address} has no live Aerodrome gauge and was excluded."
                )
                continue
            # A live gauge that is not emitting AERO cannot clear the entry threshold.
            if (
                candidate.emissions_per_second == 0
                or candidate.emissions_token_address != self._contracts.reward_token_address
            ):
                exclusion_diagnostics.append(
                    f"Pool {candidate.pool_address} has a live gauge that is not emitting "
                    "official AERO rewards and was excluded."
                )
                continue
            accepted_pools.append(candidate)

        if integrity_diagnostics:
            return PoolDiscoveryResult(
                venue=self.venue_id,
                status=PoolDiscoveryStatus.REJECTED,
                source=batch.source,
                observed_at=batch.observed_at,
                pools=(),
                diagnostics=tuple(integrity_diagnostics),
            )

        summary = (
            f"Accepted {len(accepted_pools)} of {len(batch.candidates)} pair-scoped Aerodrome "
            f"pools after factory, pair, kind, uniqueness, gauge-liveness, and AERO-emission "
            f"validation; the LP Sugar enumeration inspected {batch.enumerated_pool_count} pools."
        )
        return PoolDiscoveryResult(
            venue=self.venue_id,
            status=PoolDiscoveryStatus.VERIFIED,
            source=batch.source,
            observed_at=batch.observed_at,
            pools=tuple(accepted_pools),
            diagnostics=(summary, *exclusion_diagnostics),
        )
