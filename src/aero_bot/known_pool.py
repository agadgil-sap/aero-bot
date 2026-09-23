"""The known-pool fast path for decision surfaces.

The decision surfaces - ``aero-bot-decide`` and every ``aero-bot-cycle``
decide phase, dry runs included - need one symbol's verified pool with a
fresh block-pinned snapshot. Resolving it through the full LP Sugar
enumeration walks every pool Aerodrome hosts (tens of thousands in one
block-pinned sweep), which public RPC endpoints rate-limit into a
multi-minute silent stall on every single run.

This module resolves one already-pinned pool the same way the LP
executor's own fast path does - identity re-verified live against the pool
contract's immutable views, live state read at one freshly pinned block -
and then derives the remaining Sugar-record fields from views verified
live against the Sugar's own values at the same block (2026-09-09,
the AAPLc pool):

- ``pool_fee`` / ``unstaked_fee`` come from the factory's per-pool
  ``getSwapFee`` / ``getUnstakedFee`` (both matched the Sugar record
  exactly: 500 and 100000 ppm).
- ``gauge_alive`` comes from the factory's Voter ``isAlive(gauge)``
  (matched the record's ``gauge_alive`` exactly).
- ``staked0`` / ``staked1`` are derived with the exact concentrated math
  the Sugar itself applies - one grid cell of the gauge's staked
  liquidity - reproducing the record's sides integer-exactly
  (117114415165 and 0 raw units at tick -11557, spacing 10).

The grid cell convention was derived from live contract behavior, not
assumed: the Sugar truncates the current tick toward zero onto the
spacing grid (EVM signed-remainder semantics), so a negative unaligned
tick selects the cell above the price and a positive one the cell below.

Any mismatch, unreadable view, or liveness failure raises ``ValueError``:
the caller falls back to the full enumeration, which re-verifies
everything the slow way and refreshes the pin. The store is a cache,
never a trust root, so the fast path can only ever cost speed.
"""

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from aero_bot.domain import EvmAddress
from aero_bot.lp_calldata import (
    build_erc20_balance_of_read_calldata,
    build_factory_get_swap_fee_read_calldata,
    build_factory_get_unstaked_fee_read_calldata,
    build_factory_is_pool_read_calldata,
    build_factory_voter_read_calldata,
    build_gauge_factory_nft_read_calldata,
    build_gauge_gauge_factory_read_calldata,
    build_gauge_reward_rate_read_calldata,
    build_gauge_reward_token_read_calldata,
    build_pool_factory_read_calldata,
    build_pool_gauge_read_calldata,
    build_pool_liquidity_read_calldata,
    build_pool_slot0_read_calldata,
    build_pool_staked_liquidity_read_calldata,
    build_pool_tick_spacing_read_calldata,
    build_pool_token0_read_calldata,
    build_pool_token1_read_calldata,
    build_voter_is_alive_read_calldata,
    decode_address_view_result,
    decode_boolean_view_result,
    decode_pool_slot0_view,
    decode_uint_view_result,
)
from aero_bot.lp_pins import LpPoolPin, LpPoolPinStore, build_pool_pin_from_discovery
from aero_bot.lp_plan import position_amounts_at_sqrt_ratio
from aero_bot.registry import B20AssetListing
from aero_bot.sugar import _pool_kind_for_tick_spacing
from aero_bot.venues import (
    AerodromeContractEvidence,
    PoolCandidate,
    PoolKind,
)

# The source label decision surfaces report for fast-path resolutions.
KNOWN_POOL_SOURCE = "known-pool-fast-path"


class ExecutorRpcBackendReader(Protocol):
    """Define the bounded read-only RPC surface the fast path consumes."""

    def fetch_block_number(self) -> int:
        """Read the endpoint's latest block number."""
        ...

    def eth_call_at(self, to_address: str, calldata: str, block_tag: str) -> str:
        """Perform one read-only eth_call pinned to an explicit block tag."""
        ...


def grid_cell_lower_tick(current_tick: int, tick_spacing: int) -> int:
    """Return the grid-cell lower tick exactly as the LP Sugar computes it.

    The Sugar derives one cell of staked liquidity with the current tick
    truncated toward zero onto the spacing grid - EVM signed-remainder
    semantics - so a negative unaligned tick selects the cell above the
    price and a positive one the cell below it. Verified integer-exact
    against the live AAPLc record: tick -11557, spacing 10, cell
    ``[-11550, -11540)``.

    Args:
        current_tick: The pool's live signed tick.
        tick_spacing: The pool's positive tick spacing.

    Returns:
        The cell's inclusive lower tick boundary.

    Raises:
        ValueError: If the spacing is not positive.
    """
    if tick_spacing <= 0:
        raise ValueError("tick_spacing must be positive")
    if current_tick < 0:
        return -((-current_tick) // tick_spacing) * tick_spacing
    return (current_tick // tick_spacing) * tick_spacing


def staked_sides_for_gauge_liquidity(
    sqrt_ratio: int, current_tick: int, tick_spacing: int, gauge_liquidity: int
) -> tuple[int, int]:
    """Derive the staked token sides the Sugar would report, one grid cell.

    Args:
        sqrt_ratio: The pool's positive raw sqrtPriceX96.
        current_tick: The pool's live signed tick.
        tick_spacing: The pool's positive tick spacing.
        gauge_liquidity: The raw staked liquidity sharing gauge emissions.

    Returns:
        The raw token-zero and token-one staked sides.

    Raises:
        ValueError: If any input is malformed.
    """
    if gauge_liquidity < 0:
        raise ValueError("gauge_liquidity must not be negative")
    lower = grid_cell_lower_tick(current_tick, tick_spacing)
    amount0, amount1 = position_amounts_at_sqrt_ratio(
        sqrt_ratio, lower, lower + tick_spacing, Decimal(gauge_liquidity)
    )
    return int(amount0), int(amount1)


def resolve_known_pool_candidate(
    rpc: ExecutorRpcBackendReader,
    pin: LpPoolPin,
    listing: B20AssetListing,
    contracts: AerodromeContractEvidence,
    *,
    block_number: int | None = None,
) -> tuple[PoolCandidate, int]:
    """Resolve one pinned pool into a decision candidate from live reads.

    Every field a decision consumes is read live at one freshly pinned
    block; the pinned identity is re-verified against the pool contract's
    own immutable views, the factory is re-checked against the official
    allowlist, the gauge's kill switch is read through the factory's Voter,
    and the emission token must be the official AERO the policy prices.

    Args:
        rpc: The bounded read-only RPC backend.
        pin: The persisted Sugar-verified identity for the pool.
        listing: The registry listing the symbol resolved to.
        contracts: The official Aerodrome contract evidence catalog.
        block_number: Optional shared snapshot block. When absent, the fast
            path fetches one fresh block itself. Board selection passes one
            block to every pinned pool so all candidates are comparable at
            the same chain height.

    Returns:
        The verified candidate and the block pinning every read.

    Raises:
        ValueError: If any pinned identity fact no longer matches the live
            pool, any acceptance boundary fails, or any view is unreadable.
    """
    snapshot_block = rpc.fetch_block_number() if block_number is None else block_number
    if snapshot_block < 0:
        raise ValueError("block_number must not be negative")
    block_tag = hex(snapshot_block)

    def read(contract_address: str, calldata: str) -> str:
        return rpc.eth_call_at(contract_address, calldata, block_tag)

    pool_address = pin.pool_address
    token0 = decode_address_view_result(read(pool_address, build_pool_token0_read_calldata()))
    token1 = decode_address_view_result(read(pool_address, build_pool_token1_read_calldata()))
    tick_spacing = decode_uint_view_result(
        read(pool_address, build_pool_tick_spacing_read_calldata())
    )
    gauge = decode_address_view_result(read(pool_address, build_pool_gauge_read_calldata()))
    factory = decode_address_view_result(read(pool_address, build_pool_factory_read_calldata()))
    gauge_factory = decode_address_view_result(
        read(pin.gauge_address, build_gauge_gauge_factory_read_calldata())
    )
    nfpm = decode_address_view_result(read(gauge_factory, build_gauge_factory_nft_read_calldata()))

    listing_stock = listing.address.lower()
    normalized_usdc = contracts.quote_token_address.lower()
    pinned_identity = (
        pin.token0_address.lower(),
        pin.token1_address.lower(),
        pin.tick_spacing,
        pin.gauge_address.lower(),
        pin.factory_address.lower(),
        pin.nfpm_address.lower(),
    )
    live_identity = (token0, token1, tick_spacing, gauge, factory, nfpm)
    if live_identity != pinned_identity or listing_stock not in (token0, token1):
        raise ValueError(
            f"the pinned identity for {listing.symbol} no longer matches the live "
            f"pool {pool_address}: pinned {pinned_identity} versus live {live_identity} "
            f"with registry stock {listing_stock}"
        )
    if {token0, token1} != {listing_stock, normalized_usdc}:
        raise ValueError(
            f"the live pool {pool_address} for {listing.symbol} is not the "
            "official B20/native-USDC pair"
        )

    # The factory allowlist boundary mirrors the venue adapter's acceptance,
    # with the factory's own membership view as the authoritative proof: a
    # pool's self-reported factory() is not trustworthy on its own.
    factory_contract = next(
        (entry for entry in contracts.pool_factories if entry.address.lower() == factory),
        None,
    )
    pool_kind = _pool_kind_for_tick_spacing(tick_spacing)
    if (
        factory_contract is None
        or PoolKind.SLIPSTREAM not in factory_contract.supported_pool_kinds
        or pool_kind not in factory_contract.supported_pool_kinds
    ):
        raise ValueError(
            f"the live pool {pool_address} reports factory {factory} outside the "
            "official Slipstream allowlist"
        )
    if not decode_boolean_view_result(
        read(factory, build_factory_is_pool_read_calldata(pool_address))
    ):
        raise ValueError(f"the factory {factory} does not claim the pool {pool_address} as its own")

    # The gauge kill switch is read through the factory's own Voter.
    voter = decode_address_view_result(read(factory, build_factory_voter_read_calldata()))
    gauge_alive = decode_boolean_view_result(
        read(voter, build_voter_is_alive_read_calldata(pin.gauge_address))
    )
    if not gauge_alive:
        raise ValueError(f"the gauge {pin.gauge_address} for {listing.symbol} is not alive")

    emissions_token = decode_address_view_result(
        read(pin.gauge_address, build_gauge_reward_token_read_calldata())
    )
    emissions_per_second = decode_uint_view_result(
        read(pin.gauge_address, build_gauge_reward_rate_read_calldata())
    )
    if emissions_per_second <= 0 or emissions_token != contracts.reward_token_address.lower():
        raise ValueError(
            f"the gauge {pin.gauge_address} for {listing.symbol} is not emitting "
            "official AERO rewards"
        )

    sqrt_ratio, current_tick = decode_pool_slot0_view(
        read(pool_address, build_pool_slot0_read_calldata())
    )
    pool_active_liquidity = decode_uint_view_result(
        read(pool_address, build_pool_liquidity_read_calldata())
    )
    gauge_liquidity = decode_uint_view_result(
        read(pool_address, build_pool_staked_liquidity_read_calldata())
    )
    staked0, staked1 = staked_sides_for_gauge_liquidity(
        sqrt_ratio, current_tick, tick_spacing, gauge_liquidity
    )

    pool_fee_ppm = decode_uint_view_result(
        read(factory, build_factory_get_swap_fee_read_calldata(pool_address))
    )
    unstaked_fee_ppm = decode_uint_view_result(
        read(factory, build_factory_get_unstaked_fee_read_calldata(pool_address))
    )

    reserve0 = decode_uint_view_result(
        read(token0, build_erc20_balance_of_read_calldata(pool_address))
    )
    reserve1 = decode_uint_view_result(
        read(token1, build_erc20_balance_of_read_calldata(pool_address))
    )

    candidate = PoolCandidate(
        pool_address=EvmAddress(pool_address),
        factory_address=EvmAddress(factory),
        token0_address=EvmAddress(token0),
        token1_address=EvmAddress(token1),
        pool_kind=pool_kind,
        tick_spacing=tick_spacing,
        current_tick=current_tick,
        sqrt_ratio=sqrt_ratio,
        pool_fee_ppm=pool_fee_ppm,
        unstaked_fee_ppm=unstaked_fee_ppm,
        reserve0=reserve0,
        reserve1=reserve1,
        staked0=staked0,
        staked1=staked1,
        gauge_address=EvmAddress(pin.gauge_address),
        gauge_liquidity=gauge_liquidity,
        gauge_alive=gauge_alive,
        emissions_per_second=emissions_per_second,
        emissions_token_address=EvmAddress(emissions_token),
        pool_active_liquidity=pool_active_liquidity,
        nfpm_address=EvmAddress(nfpm),
    )
    return candidate, snapshot_block


def persist_decision_pool_pin(
    store: LpPoolPinStore,
    listing: B20AssetListing,
    candidate: PoolCandidate,
    stock_decimals: int,
    snapshot_block: int,
    observed_at: datetime,
    discovery_source: str,
) -> None:
    """Persist one sweep-verified pool pin from a decision resolution.

    The pin store is a cache of verified facts, so persistence failures are
    ignored: the decision already stands on the verified sweep, and the next
    run simply re-runs the sweep when the pin could not be written.

    Args:
        store: The local pool-pin store to refresh.
        listing: The registry listing the symbol resolved to.
        candidate: The sweep-verified candidate carrying the identity.
        stock_decimals: The stock token's decimal count.
        snapshot_block: The sweep's pinned snapshot block.
        observed_at: The sweep's observation time.
        discovery_source: The sweep's source provenance string.
    """
    if candidate.gauge_address is None or candidate.nfpm_address is None:
        return
    try:
        store.save_pin(
            build_pool_pin_from_discovery(
                symbol=listing.symbol,
                pool_address=candidate.pool_address,
                factory_address=candidate.factory_address,
                token0_address=candidate.token0_address,
                token1_address=candidate.token1_address,
                tick_spacing=candidate.tick_spacing,
                gauge_address=candidate.gauge_address,
                nfpm_address=candidate.nfpm_address,
                stock_decimals=stock_decimals,
                snapshot_block=snapshot_block,
                observed_at=observed_at,
                discovery_source=discovery_source,
            )
        )
    except (OSError, ValueError):
        # The cache never gates the decision; a failed write only costs
        # the next run its fast path.
        return
