"""Bounded production reads assembling live ranging evidence.

The adaptive width solve consumes observables the decision path does not
already carry: realized volatility, the bounded trailing price path behind
measured band dwell, the trailing fee flow, and the gauge concentration
numbers. This module assembles exactly that evidence from one bounded,
read-only reconstruction of the pool's own Swap logs - a handful of
1,000-block pages over roughly the last 4.4 hours at Base's two-second
blocks - through the same ``EventHistoryRpcBackend`` the rehearsal uses,
with the same decode, estimator, and price conversion, so production and
replay consume matching inputs.

Fail-closed doctrine: any read failure raises ``HistoryUnavailableError``
and the caller attaches NO ranging evidence, which defers new entries and
voluntary recenters with an explicit reason under the adaptive policy -
never a substituted constant range. Safety exits never consume this
evidence and stay armed through every failure mode here.
"""

from datetime import timedelta
from decimal import Decimal

from aero_bot.emissions_apr import staked_value_usdc
from aero_bot.history import (
    RANGING_READ_LOOKBACK,
    EventHistoryRpcBackend,
    swap_usd_notional,
)
from aero_bot.ranging import RangingEvidence, realized_volatility_from_points
from aero_bot.venues import PoolCandidate

# Native USDC on Base, the quote side of every verified B20 pool.
NATIVE_USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# The log-window size for ranging reads: the public gateway's measured
# eth_getLogs block-span cap. The production unsigned preflight of
# 52d7846 proved the deployed public Tenderly gateway rejects any span
# above 1,000 blocks with RPC error -32602 (invalid params) regardless
# of filter shape, while spans at or below 1,000 succeed, so the window
# sits exactly at that cap and the bounded pagination covers the full
# lookback through more, smaller pages.
RANGING_READ_LOG_WINDOW_BLOCKS = 1_000


def build_ranging_evidence(
    backend: EventHistoryRpcBackend,
    pool: PoolCandidate,
    snapshot_price_usdc: Decimal,
    stock_decimals: int,
    quote_decimals: int = 6,
    lookback: timedelta = RANGING_READ_LOOKBACK,
) -> RangingEvidence:
    """Assemble one pool's live ranging evidence from bounded Swap reads.

    Args:
        backend: The bounded read-only history backend sharing the cycle's
            RPC endpoint.
        pool: The verified pool whose gauge snapshot anchors the
            concentration inputs and whose Swap logs are reconstructed.
        snapshot_price_usdc: The pool price in USDC per stock at the same
            snapshot the observation's APR and staked balances came from.
        stock_decimals: Decimal count of the B20 stock token.
        quote_decimals: Decimal count of the USDC quote token.
        lookback: The trailing window to reconstruct; the default is the
            bounded production read (a handful of log-window pages).

    Returns:
        The complete ranging evidence for one adaptive width solve.

    Raises:
        HistoryUnavailableError: If any bounded read cannot complete; the
            caller then attaches no evidence and the decision defers.
    """
    stock_is_token0 = pool.token0_address.lower() != NATIVE_USDC_ADDRESS.lower()
    stock_token = pool.token1_address if stock_is_token0 else pool.token0_address
    path = backend.fetch_price_path(
        pool_address=pool.pool_address,
        token_address=stock_token,
        token0_address=pool.token0_address,
        token1_address=pool.token1_address,
        token_decimals=stock_decimals,
        quote_decimals=quote_decimals,
        lookback=lookback,
    )
    points = tuple(path.points)
    volatility = realized_volatility_from_points(points)
    if len(points) >= 2:
        fee_window_seconds = int((points[-1].timestamp - points[0].timestamp).total_seconds())
        fee_notional = sum(
            (
                swap_usd_notional(point, path.token_is_token0, stock_decimals, quote_decimals)
                for point in points
            ),
            start=Decimal(0),
        )
    else:
        fee_window_seconds = 0
        fee_notional = Decimal(0)
    # The staked value follows the display APR's own current-cell convention
    # (the APR's denominator), from the same verified Sugar snapshot the
    # observation's APR and staked balances came from.
    staked_tvl = staked_value_usdc(
        pool.staked0,
        pool.staked1,
        stock_is_token0,
        stock_decimals,
        quote_decimals,
        snapshot_price_usdc,
    )
    return RangingEvidence(
        gauge_liquidity_raw=pool.gauge_liquidity,
        staked_tvl_usd=staked_tvl,
        active_liquidity_raw=pool.pool_active_liquidity,
        fee_window_seconds=fee_window_seconds,
        fee_window_notional_usd=fee_notional,
        pool_fee_ppm=pool.pool_fee_ppm,
        realized_daily_volatility=volatility,
        trailing_path=points,
        stock_decimals=stock_decimals,
        quote_decimals=quote_decimals,
        # The verbatim-geometry anchor: the solve's executable bounds derive
        # from the snapshot's own raw tick and token orientation, carried
        # exactly as the injected-evidence paths carry them.
        pool_tick_raw=pool.current_tick,
        stock_is_token0=stock_is_token0,
    )
