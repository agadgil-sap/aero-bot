# Concentrated-liquidity analysis

## Scope and sources

Aerodrome documents that Slipstream concentrated pools allocate liquidity between explicit tick-defined price boundaries and require active management.
It also states that concentrated staking APR depends on active liquidity around the current tick rather than total pool liquidity.
The position inventory calculations follow the canonical Uniswap v3 position implementation because Aerodrome Slipstream adapts that design.

The current module works with normalized token units and token1-per-token0 prices.
A future onchain adapter must convert raw token decimals and `sqrtPriceX96` observations before constructing the validated snapshot.
No float arithmetic is used.

## Position amounts

Below the lower boundary, the position is entirely token0.
At or above the exclusive upper boundary, the position is entirely token1.
Inside the range, token0 represents the square-root-price distance to the upper boundary and token1 represents the distance from the lower boundary.
The lower boundary is active and the upper boundary is inactive, matching v3 tick semantics.

Square-root range progress reports zero below the range, one above it, and the normalized square-root-price location while active.
The current token0 value fraction exposes how range movement concentrates the position into one asset.

## Conservative impermanent-loss cost

Entry token amounts form the hold benchmark.
The current LP inventory and unchanged entry inventory are both valued with the same current independent token prices.
Impermanent loss is the non-negative shortfall of the LP inventory relative to holding the entry inventory.
Any calculated LP outperformance is clamped to zero loss rather than treated as negative risk cost.

Uncollected swap fees and AERO emissions are deliberately excluded from position value.
They remain separate return components for the risk engine and cannot hide inventory divergence.
The observed loss fraction is linearly annualized over the validated positive holding period and may exceed 100 percent for a severe short-duration observation.

The analysis also reports absolute basis-point deviation between the pool pair price and the pair price implied by independent current USD valuations.
That evidence feeds the separate oracle-deviation policy gate rather than silently changing the position formula.
