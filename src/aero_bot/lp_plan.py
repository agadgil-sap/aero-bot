"""Pure, deterministic planning for the Slipstream LP mint lifecycle.

This module is the policy layer between one block-pinned pool snapshot and the
calldata builders in ``lp_calldata``: it derives the tick-aligned position
range from a half width expressed in tick spacings, converts a USDC budget
into both sides' raw mint amounts at the snapshot's square-root price, plans
the balancing swap the Safe's inventory requires, and enforces every LP cap
before anything reaches the signing layer. Everything here is offline and
Decimal-exact: no network reads, no signing, no broadcast, no clock.

The amount mathematics are the canonical Uniswap v3 position formulas over
raw ``sqrtPriceX96`` integers. With ``Sa``, ``Sp``, ``Sb`` the raw
square-root prices at the lower bound, the current price, and the upper
bound, liquidity ``L`` holds

- ``amount0 = L * 2**96 * (Sb - Sp) / (Sp * Sb)`` raw token-zero units, and
- ``amount1 = L * (Sp - Sa) / 2**96`` raw token-one units,

which is exactly what the NonfungiblePositionManager's own ``liquidity``
computation inverts when it receives the desired amounts this plan produces.
"""

from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, NonNegativeDecimal
from aero_bot.history import price_usdc_per_stock
from aero_bot.lp_calldata import INT24_MAX, INT24_MIN
from aero_bot.ranging import TICK_PRICE_RATIO

# High internal precision keeps every square-root and power deterministic.
MATH_PRECISION = 60
# The locked safety ceiling half width mirrors the ranging solver's ceiling.
DEFAULT_MAX_RANGE_HALF_WIDTH_FRACTION = Decimal("0.003")
# The default mint min-amount tolerance mirrors the swap executor's slippage.
DEFAULT_MINT_SLIPPAGE_TOLERANCE = Decimal("0.001")
# The balancing swap buys the shortfall plus this fraction so the mint's
# stock pull never exceeds the realized swap output through small adverse
# moves; the executor re-derives the mint amounts from realized output.
DEFAULT_SWAP_BUFFER_FRACTION = Decimal("0.001")
# Native Base USDC carries six decimals on every path in this release.
QUOTE_TOKEN_DECIMALS = 6
# One tick spacing per side is the tightest range the planner accepts.
MIN_HALF_WIDTH_SPACINGS = 1
# The hard pilot cap on one pool's committed position value, in USDC.
MAX_POSITION_USDC_PER_POOL = Decimal("50")
# The hard pilot cap on the fleet's total committed value, in USDC.
MAX_TOTAL_PILOT_EXPOSURE_USDC = Decimal("100")
# A position may commit at most this fraction of the pool's in-range depth.
MAX_POSITION_FRACTION_OF_POOL_DEPTH = Decimal("0.01")
# The impact ceiling no balancing swap may reach, as a price-impact fraction.
SWAP_IMPACT_CEILING_FRACTION = Decimal("0.001")
# Modeled impact above this fraction splits the balancing swap into tranches.
SWAP_TRANCHE_THRESHOLD_FRACTION = Decimal("0.0005")
# The X96 fixed-point scale of raw square-root prices.
X96_SCALE = 1 << 96


class LpPlanRefusalError(RuntimeError):
    """Refuse one planning request with its catalog code and explanation."""

    def __init__(self, code: "LpPlanRefusalCode", message: str) -> None:
        """Store the refusal's catalog code alongside its explanation.

        Args:
            code: The stable refusal-code identifier documented in
                docs/lp_execution.md.
            message: The actionable refusal explanation.
        """
        super().__init__(message)
        self.code = code


class LpPlanRefusalCode(StrEnum):
    """Catalog every planning refusal this layer can raise."""

    # The requested budget exceeds the per-pool pilot cap.
    BUDGET_ABOVE_POOL_CAP = "budget_above_pool_cap"
    # The requested budget exceeds the fleet-wide pilot exposure cap.
    BUDGET_ABOVE_TOTAL_EXPOSURE_CAP = "budget_above_total_exposure_cap"
    # The position value exceeds the allowed share of pool in-range depth.
    POSITION_ABOVE_POOL_DEPTH_FRACTION = "position_above_pool_depth_fraction"
    # The snapshot price sits outside the derived range or on its boundary.
    PRICE_OUTSIDE_RANGE = "price_outside_range"
    # The budget is too small to fund both sides of the range after flooring.
    BUDGET_TOO_SMALL_FOR_BOTH_SIDES = "budget_too_small_for_both_sides"
    # A balancing swap would reach or exceed the impact ceiling.
    SWAP_IMPACT_ABOVE_CEILING = "swap_impact_above_ceiling"
    # The Safe's USDC cannot fund both the quote side and the balancing swap.
    INSUFFICIENT_USDC_FOR_ENTRY = "insufficient_usdc_for_entry"


class WidthSource(StrEnum):
    """Identify how a mint's half width was chosen."""

    # An explicit operator override in tick spacings per side.
    EXPLICIT_OVERRIDE = "explicit_override"
    # The ranging solver's derived width; its emissions-APR input is known
    # understated until the APR convention fix lands, so every derived width
    # carries the pre-fix label in its diagnostics.
    SOLVER_PRE_FIX_APR = "solver_pre_fix_apr"


class PositionTickRange(BaseModel):
    """Hold one derived tick-aligned range with its complete evidence."""

    # Frozen strict fields keep the built range bound to its derivation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The pool's tick grid spacing the range aligns to.
    tick_spacing: Annotated[int, Field(gt=0)]
    # The snapshot current tick the range was centered on.
    current_tick: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # The requested half width in tick spacings per side.
    requested_half_width_spacings: Annotated[int, Field(gt=0)]
    # The effective half width in whole ticks per side after clamping.
    half_width_ticks: Annotated[int, Field(gt=0)]
    # The exact fractional price half width, 1.0001**ticks - 1.
    half_width_fraction: Annotated[Decimal, Field(gt=0)]
    # The inclusive lower range boundary on the spacing grid.
    tick_lower: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # The exclusive upper range boundary on the spacing grid.
    tick_upper: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # How the half width was chosen.
    width_source: WidthSource
    # Whether the requested width was clamped down to the ceiling.
    clamped_to_ceiling: bool

    @model_validator(mode="after")
    def require_coherent_range(self) -> Self:
        """Reject off-grid, inverted, or non-containing ranges."""
        if self.tick_lower >= self.tick_upper:
            raise ValueError("tick_lower must be below tick_upper")
        if self.tick_lower % self.tick_spacing or self.tick_upper % self.tick_spacing:
            raise ValueError("both range boundaries must be multiples of the tick spacing")
        if self.half_width_ticks % self.tick_spacing:
            raise ValueError("half_width_ticks must be a whole number of spacings")
        if not self.tick_lower <= self.current_tick < self.tick_upper:
            raise ValueError("the range must contain the current tick")
        return self


class MintAmountPlan(BaseModel):
    """Hold both sides' raw mint amounts for one budget and range."""

    # Frozen strict fields keep the mint pull bound to the priced plan.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The liquidity the budget buys, in the pool's raw L units.
    liquidity: Annotated[Decimal, Field(gt=0)]
    # The desired token-zero input in raw token units.
    amount0_desired_units: Annotated[int, Field(gt=0)]
    # The desired token-one input in raw token units.
    amount1_desired_units: Annotated[int, Field(gt=0)]
    # The minimum accepted token-zero input after slippage.
    amount0_min_units: Annotated[int, Field(ge=0)]
    # The minimum accepted token-one input after slippage.
    amount1_min_units: Annotated[int, Field(ge=0)]
    # The token-zero side's committed value at the plan price, in USDC.
    token0_value_usdc: NonNegativeDecimal
    # The token-one side's committed value at the plan price, in USDC.
    token1_value_usdc: NonNegativeDecimal
    # The tolerance the minima sit below the desired amounts.
    slippage_tolerance_fraction: Annotated[Decimal, Field(gt=0, lt=1)]

    @model_validator(mode="after")
    def require_minima_below_desired(self) -> Self:
        """Reject minima that exceed their desired amounts."""
        if self.amount0_min_units > self.amount0_desired_units:
            raise ValueError("amount0_min_units exceeds amount0_desired_units")
        if self.amount1_min_units > self.amount1_desired_units:
            raise ValueError("amount1_min_units exceeds amount1_desired_units")
        return self


class BalancingSwapPlan(BaseModel):
    """Hold the balancing swap the Safe's inventory requires before a mint."""

    # Frozen strict fields keep the swap plan bound to one inventory snapshot.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Whether any balancing swap is required at all.
    required: bool
    # The stock shortfall the swap covers, in raw stock units.
    stock_shortfall_units: Annotated[int, Field(ge=0)]
    # The USDC the swap spends, in raw six-decimal units; zero when absent.
    usdc_in_units: Annotated[int, Field(ge=0)]
    # The spot-quoted stock output, in raw stock units; zero when absent.
    expected_stock_units: Annotated[int, Field(ge=0)]
    # The conservative reserve-based impact bound of the whole swap.
    modeled_impact_fraction: NonNegativeDecimal
    # How many tranches the swap splits into; one below the tranche rule.
    tranche_count: Annotated[int, Field(gt=0)]
    # The acquisition buffer fraction applied over the raw shortfall.
    buffer_fraction: NonNegativeDecimal

    @model_validator(mode="after")
    def require_coherent_swap(self) -> Self:
        """Reject a present swap without amounts or an absent one with them."""
        if self.required and (self.usdc_in_units <= 0 or self.expected_stock_units <= 0):
            raise ValueError("a required balancing swap carries positive amounts")
        if not self.required and (self.usdc_in_units > 0 or self.stock_shortfall_units > 0):
            raise ValueError("an absent balancing swap carries no shortfall or USDC")
        return self


class LpPoolObservation(BaseModel):
    """Carry one block-pinned Sugar pool snapshot the planner consumes."""

    # Frozen strict fields keep the plan on one coherent observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol, like AAPLc.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager from its Sugar record.
    nfpm_address: EvmAddress
    # The pool's live CLGauge.
    gauge_address: EvmAddress
    # The pool's lower-address token, USDC for every supported B20 pool here.
    token0_address: EvmAddress
    # The pool's higher-address token, the B20 stock for supported pools.
    token1_address: EvmAddress
    # True when the stock token sorts before USDC by address.
    stock_is_token0: bool
    # The stock token's decimal count.
    stock_decimals: Annotated[int, Field(gt=0)]
    # The USDC quote token's decimal count.
    quote_decimals: Annotated[int, Field(gt=0)]
    # The pool's positive Slipstream tick spacing.
    tick_spacing: Annotated[int, Field(gt=0)]
    # The pool's current tick from the same snapshot.
    current_tick: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # The pool's current raw sqrtPriceX96.
    sqrt_ratio: Annotated[int, Field(gt=0)]
    # The pool's current active in-range liquidity, raw L units.
    pool_active_liquidity: Annotated[int, Field(gt=0)]
    # The pool's USDC-side reserve in raw units, the swap impact base.
    usdc_reserve_units: Annotated[int, Field(gt=0)]
    # The block every field of this observation was pinned to.
    snapshot_block: Annotated[int, Field(ge=0)]
    # When the snapshot completed, timezone-aware.
    observed_at: datetime

    @model_validator(mode="after")
    def require_stock_side(self) -> Self:
        """Reject a degenerate pair or a naive observation timestamp."""
        if self.token0_address == self.token1_address:
            raise ValueError("the pool pair needs two distinct tokens")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self

    @property
    def price_usdc_per_stock(self) -> Decimal:
        """Return the snapshot's USDC price of one whole stock token."""
        return price_usdc_per_stock(
            self.sqrt_ratio,
            self.stock_is_token0,
            self.stock_decimals,
            self.quote_decimals,
        )


class MintDirective(BaseModel):
    """Carry one mint request's budget and width directive."""

    # Frozen strict fields keep one plan bound to its directive.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The total USDC value the position commits across both sides.
    budget_usdc: Annotated[Decimal, Field(gt=0)]
    # The half width in tick spacings per side.
    half_width_spacings: Annotated[int, Field(gt=0)]
    # How the half width was chosen.
    width_source: WidthSource
    # The locked ceiling half width; directives never widen past it.
    max_range_half_width_fraction: Annotated[Decimal, Field(gt=0)] = (
        DEFAULT_MAX_RANGE_HALF_WIDTH_FRACTION
    )
    # The tolerance the mint minima sit below the desired amounts.
    slippage_tolerance_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = (
        DEFAULT_MINT_SLIPPAGE_TOLERANCE
    )
    # The acquisition buffer over the stock shortfall the swap buys.
    swap_buffer_fraction: Annotated[Decimal, Field(ge=0, lt=1)] = DEFAULT_SWAP_BUFFER_FRACTION


class SafeInventory(BaseModel):
    """Carry the Safe's token inventory and pilot exposure at plan time."""

    # Frozen strict fields keep one plan bound to one inventory read.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The Safe's spendable USDC in raw six-decimal units.
    usdc_units: Annotated[int, Field(ge=0)]
    # The Safe's stock balance in raw stock units.
    stock_units: Annotated[int, Field(ge=0)]
    # The fleet's existing committed position value, in USDC.
    existing_position_value_usdc: NonNegativeDecimal = Decimal(0)


class LpMintPlan(BaseModel):
    """Hold one complete, cap-cleared mint plan ready for calldata building."""

    # Frozen strict fields keep the audit record identical to the plan.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The position manager the mint executes through, from the Sugar record.
    nfpm_address: EvmAddress
    # The pool's gauge the mint's stake step will target.
    gauge_address: EvmAddress
    # The derived tick-aligned range.
    position_range: PositionTickRange
    # Both sides' raw mint amounts with their minima.
    amounts: MintAmountPlan
    # The balancing swap the Safe's inventory requires, or its absence.
    balancing_swap: BalancingSwapPlan
    # The snapshot's USDC price of one whole stock token.
    price_usdc_per_stock: Decimal
    # The budget the position commits, in USDC.
    budget_usdc: Decimal
    # The estimated pool in-range depth the depth cap evaluated against.
    pool_depth_usdc: Decimal
    # Every cap checked in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Human-readable evidence lines covering the plan's derivation.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]
    # The block the underlying pool snapshot was pinned to.
    snapshot_block: Annotated[int, Field(ge=0)]
    # When the underlying pool snapshot completed.
    observed_at: datetime


class LpExecutionPolicy(BaseModel):
    """Hold every hard LP cap enforced before anything is signed."""

    # Frozen strict fields keep one attempt's caps stable end to end.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The largest single-pool committed value this configuration permits.
    max_position_usdc_per_pool: Annotated[Decimal, Field(gt=0)] = MAX_POSITION_USDC_PER_POOL
    # The largest fleet-wide committed value this configuration permits.
    max_total_pilot_exposure_usdc: Annotated[Decimal, Field(gt=0)] = MAX_TOTAL_PILOT_EXPOSURE_USDC
    # The largest share of pool in-range depth one position may commit.
    max_position_fraction_of_pool_depth: Annotated[Decimal, Field(gt=0, lt=1)] = (
        MAX_POSITION_FRACTION_OF_POOL_DEPTH
    )
    # The impact ceiling no balancing swap may reach.
    swap_impact_ceiling_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = (
        SWAP_IMPACT_CEILING_FRACTION
    )
    # Modeled impact above this fraction splits the balancing swap.
    swap_tranche_threshold_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = (
        SWAP_TRANCHE_THRESHOLD_FRACTION
    )

    @model_validator(mode="after")
    def require_ceiling_compliance(self) -> Self:
        """Enforce the hard pilot ceilings no configuration may exceed."""
        if self.max_position_usdc_per_pool > MAX_POSITION_USDC_PER_POOL:
            raise ValueError(
                f"max_position_usdc_per_pool {self.max_position_usdc_per_pool} exceeds the "
                f"hard pilot ceiling of {MAX_POSITION_USDC_PER_POOL} USDC per pool"
            )
        if self.max_total_pilot_exposure_usdc > MAX_TOTAL_PILOT_EXPOSURE_USDC:
            raise ValueError(
                f"max_total_pilot_exposure_usdc {self.max_total_pilot_exposure_usdc} exceeds "
                f"the hard pilot ceiling of {MAX_TOTAL_PILOT_EXPOSURE_USDC} USDC total"
            )
        if self.max_position_fraction_of_pool_depth > MAX_POSITION_FRACTION_OF_POOL_DEPTH:
            raise ValueError(
                f"max_position_fraction_of_pool_depth {self.max_position_fraction_of_pool_depth}"
                f" exceeds the hard ceiling of {MAX_POSITION_FRACTION_OF_POOL_DEPTH}"
            )
        if self.swap_impact_ceiling_fraction > SWAP_IMPACT_CEILING_FRACTION:
            raise ValueError(
                f"swap_impact_ceiling_fraction {self.swap_impact_ceiling_fraction} exceeds the "
                f"hard ceiling of {SWAP_IMPACT_CEILING_FRACTION}"
            )
        return self


def _sqrt_price_at_tick(tick: int) -> Decimal:
    """Compute the raw sqrtPriceX96 at one tick as a high-precision Decimal.

    Args:
        tick: The tick whose exact price 1.0001**tick is square-rooted.

    Returns:
        sqrt(1.0001**tick) * 2**96 to full working precision.

    Raises:
        ValueError: If the tick is outside the int24 range.
    """
    if not INT24_MIN <= tick <= INT24_MAX:
        raise ValueError("tick is outside the int24 range")
    return (TICK_PRICE_RATIO**tick).sqrt() * X96_SCALE


def position_amounts_for_liquidity(
    sqrt_ratio: int,
    tick_lower: int,
    tick_upper: int,
    liquidity: Decimal,
) -> tuple[Decimal, Decimal]:
    """Compute both sides' raw amounts for one liquidity over one range.

    Args:
        sqrt_ratio: The pool's positive raw sqrtPriceX96.
        tick_lower: The range's inclusive lower tick.
        tick_upper: The range's exclusive upper tick.
        liquidity: The positive raw liquidity amount L.

    Returns:
        The raw token-zero and token-one amounts held at the current price.

    Raises:
        ValueError: If any argument is malformed or the current price does not
            sit strictly inside the range.
    """
    if sqrt_ratio <= 0:
        raise ValueError("sqrt_ratio must be positive")
    if liquidity <= 0:
        raise ValueError("liquidity must be positive")
    if tick_lower >= tick_upper:
        raise ValueError("tick_lower must be below tick_upper")
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        sqrt_lower = _sqrt_price_at_tick(tick_lower)
        sqrt_upper = _sqrt_price_at_tick(tick_upper)
        sqrt_current = Decimal(sqrt_ratio)
        if not sqrt_lower < sqrt_current < sqrt_upper:
            raise ValueError(
                "the current price must sit strictly inside the range for a two-sided position"
            )
        amount0 = liquidity * X96_SCALE * (sqrt_upper - sqrt_current) / (sqrt_current * sqrt_upper)
        amount1 = liquidity * (sqrt_current - sqrt_lower) / X96_SCALE
        return +amount0, +amount1


def derive_position_range(
    current_tick: int,
    tick_spacing: int,
    half_width_spacings: int,
    width_source: WidthSource,
    max_range_half_width_fraction: Decimal = DEFAULT_MAX_RANGE_HALF_WIDTH_FRACTION,
) -> PositionTickRange:
    """Derive one grid-aligned symmetric range around the current tick.

    The anchor is the current tick rounded down onto the spacing grid, and the
    range extends exactly the requested number of spacings to each side, so
    the current tick is always contained. The half width is clamped to at
    least one spacing and at most the greatest whole spacing count whose
    fractional width stays at or below the locked ceiling.

    Args:
        current_tick: The snapshot's current pool tick.
        tick_spacing: The pool's positive tick grid spacing.
        half_width_spacings: The requested half width in spacings per side.
        width_source: How the half width was chosen.
        max_range_half_width_fraction: The locked ceiling half width.

    Returns:
        The derived range with its complete derivation evidence.

    Raises:
        ValueError: If the spacing or width is not positive, or the derived
            boundaries leave the int24 range.
    """
    if tick_spacing <= 0:
        raise ValueError("tick_spacing must be positive")
    if half_width_spacings <= 0:
        raise ValueError("half_width_spacings must be positive")
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        # The greatest whole-tick half width that stays at or below the
        # ceiling, mirroring the ranging solver's candidate bound.
        max_ticks = int(
            (
                (Decimal(1) + max_range_half_width_fraction).ln() / TICK_PRICE_RATIO.ln()
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
    max_spacings = max(MIN_HALF_WIDTH_SPACINGS, max_ticks // tick_spacing)
    effective_spacings = min(half_width_spacings, max_spacings)
    clamped = effective_spacings < half_width_spacings
    # The anchor rounds down onto the grid, keeping the current tick inside.
    anchor = (current_tick // tick_spacing) * tick_spacing
    half_width_ticks = effective_spacings * tick_spacing
    tick_lower = anchor - half_width_ticks
    tick_upper = anchor + half_width_ticks
    if not tick_lower >= INT24_MIN or not tick_upper <= INT24_MAX:
        raise ValueError("the derived range leaves the int24 tick range")
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        half_width_fraction = +(TICK_PRICE_RATIO**half_width_ticks - Decimal(1))
    return PositionTickRange(
        tick_spacing=tick_spacing,
        current_tick=current_tick,
        requested_half_width_spacings=half_width_spacings,
        half_width_ticks=half_width_ticks,
        half_width_fraction=half_width_fraction,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        width_source=width_source,
        clamped_to_ceiling=clamped,
    )


def estimate_in_range_depth_usdc(
    sqrt_ratio: int,
    tick_spacing: int,
    current_tick: int,
    pool_active_liquidity: int,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Estimate the pool's in-range depth as a USDC value from its snapshot.

    The estimator values the pool's active liquidity over the narrowest
    position the grid supports - one spacing per side of the current tick -
    so it deliberately understates wider-band depth and keeps the one-percent
    position cap conservative.

    Args:
        sqrt_ratio: The pool's positive raw sqrtPriceX96.
        tick_spacing: The pool's positive tick grid spacing.
        current_tick: The snapshot's current pool tick.
        pool_active_liquidity: The pool's positive active in-range liquidity.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: The stock token's decimal count.
        quote_decimals: The USDC token's decimal count.

    Returns:
        The estimated in-range depth in USDC.

    Raises:
        ValueError: If any argument is malformed.
    """
    band = derive_position_range(
        current_tick,
        tick_spacing,
        MIN_HALF_WIDTH_SPACINGS,
        WidthSource.EXPLICIT_OVERRIDE,
    )
    amount0, amount1 = position_amounts_for_liquidity(
        sqrt_ratio, band.tick_lower, band.tick_upper, Decimal(pool_active_liquidity)
    )
    price = price_usdc_per_stock(sqrt_ratio, stock_is_token0, stock_decimals, quote_decimals)
    token0_scale = (
        Decimal(10) ** -stock_decimals if stock_is_token0 else Decimal(10) ** (-quote_decimals)
    )
    token1_scale = (
        Decimal(10) ** -quote_decimals if stock_is_token0 else Decimal(10) ** (-stock_decimals)
    )
    token0_price = price if stock_is_token0 else Decimal(1)
    token1_price = Decimal(1) if stock_is_token0 else price
    return +(amount0 * token0_scale * token0_price) + +(amount1 * token1_scale * token1_price)


def plan_mint_composition(
    sqrt_ratio: int,
    position_range: PositionTickRange,
    budget_usdc: Decimal,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
    slippage_tolerance_fraction: Decimal,
) -> MintAmountPlan:
    """Convert one USDC budget into both sides' raw mint amounts.

    The unit liquidity's two sides are valued at the snapshot price, the
    budget buys liquidity at that unit value, and both desired amounts floor
    to raw integers so the plan never overstates what the budget funds.

    Args:
        sqrt_ratio: The pool's positive raw sqrtPriceX96.
        position_range: The derived range the position spans.
        budget_usdc: The positive total USDC value both sides commit.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: The stock token's decimal count.
        quote_decimals: The USDC token's decimal count.
        slippage_tolerance_fraction: The fraction the minima sit below desired.

    Returns:
        The mint amount plan with desired amounts, minima, and side values.

    Raises:
        LpPlanRefusalError: If the current price sits outside the range or the
            budget is too small to fund both sides after flooring.
        ValueError: If any argument is malformed.
    """
    if budget_usdc <= 0:
        raise ValueError("budget_usdc must be positive")
    price = price_usdc_per_stock(sqrt_ratio, stock_is_token0, stock_decimals, quote_decimals)
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        try:
            unit0, unit1 = position_amounts_for_liquidity(
                sqrt_ratio,
                position_range.tick_lower,
                position_range.tick_upper,
                Decimal(1),
            )
        except ValueError as error:
            raise LpPlanRefusalError(
                LpPlanRefusalCode.PRICE_OUTSIDE_RANGE,
                f"cannot compose a two-sided position: {error}",
            ) from error
        # Each side's human scale and USDC price follow from which side is stock.
        token0_scale = (
            Decimal(10) ** -stock_decimals if stock_is_token0 else Decimal(10) ** (-quote_decimals)
        )
        token1_scale = (
            Decimal(10) ** -quote_decimals if stock_is_token0 else Decimal(10) ** (-stock_decimals)
        )
        token0_price = price if stock_is_token0 else Decimal(1)
        token1_price = Decimal(1) if stock_is_token0 else price
        unit0_value = +(unit0 * token0_scale * token0_price)
        unit1_value = +(unit1 * token1_scale * token1_price)
        unit_value = unit0_value + unit1_value
        if unit_value <= 0:
            raise ValueError("the unit position value must be positive")
        liquidity = +(budget_usdc / unit_value)
        amount0 = (liquidity * unit0).to_integral_value(rounding=ROUND_FLOOR)
        amount1 = (liquidity * unit1).to_integral_value(rounding=ROUND_FLOOR)
        if amount0 <= 0 or amount1 <= 0:
            raise LpPlanRefusalError(
                LpPlanRefusalCode.BUDGET_TOO_SMALL_FOR_BOTH_SIDES,
                f"a {budget_usdc} USDC budget floors one side of this range to zero at the "
                f"price {price} USDC per stock; raise the budget or tighten the range",
            )
        tolerance = slippage_tolerance_fraction
        amount0_min = int(
            (amount0 * (Decimal(1) - tolerance)).to_integral_value(rounding=ROUND_FLOOR)
        )
        amount1_min = int(
            (amount1 * (Decimal(1) - tolerance)).to_integral_value(rounding=ROUND_FLOOR)
        )
        return MintAmountPlan(
            liquidity=liquidity,
            amount0_desired_units=int(amount0),
            amount1_desired_units=int(amount1),
            amount0_min_units=amount0_min,
            amount1_min_units=amount1_min,
            token0_value_usdc=+(amount0 * token0_scale * token0_price),
            token1_value_usdc=+(amount1 * token1_scale * token1_price),
            slippage_tolerance_fraction=tolerance,
        )


def plan_balancing_swap(
    stock_shortfall_units: int,
    price_usdc_per_stock_value: Decimal,
    stock_decimals: int,
    usdc_reserve_units: int,
    buffer_fraction: Decimal,
    impact_ceiling_fraction: Decimal,
    tranche_threshold_fraction: Decimal,
) -> BalancingSwapPlan:
    """Plan the USDC-to-stock swap covering one mint's stock shortfall.

    The swap buys the shortfall plus the buffer fraction, floored on the
    quoted stock output; its modeled impact is the conservative whole-reserve
    bound, and an impact above the tranche threshold splits the swap into
    enough tranches to bring each below the threshold.

    Args:
        stock_shortfall_units: The positive raw stock shortfall to cover.
        price_usdc_per_stock_value: The snapshot price of one whole stock.
        stock_decimals: The stock token's decimal count.
        usdc_reserve_units: The pool's USDC-side reserve in raw units.
        buffer_fraction: The acquisition buffer over the shortfall.
        impact_ceiling_fraction: The ceiling the whole swap's impact refuses at.
        tranche_threshold_fraction: The threshold that splits the swap.

    Returns:
        The balancing swap plan with its impact and tranche evidence.

    Raises:
        LpPlanRefusalError: If the whole swap's modeled impact reaches the
            impact ceiling.
        ValueError: If any argument is malformed.
    """
    if stock_shortfall_units <= 0:
        raise ValueError("stock_shortfall_units must be positive")
    if price_usdc_per_stock_value <= 0:
        raise ValueError("price must be positive")
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        stock_shortfall = Decimal(stock_shortfall_units).scaleb(-stock_decimals)
        # Ceiling the USDC input keeps the swap from underbuying the shortfall.
        usdc_in_units = int(
            (
                stock_shortfall
                * (Decimal(1) + buffer_fraction)
                * price_usdc_per_stock_value
                * Decimal(10) ** QUOTE_TOKEN_DECIMALS
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        usdc_in = Decimal(usdc_in_units).scaleb(-QUOTE_TOKEN_DECIMALS)
        expected_stock_units = int(
            (
                usdc_in / price_usdc_per_stock_value * Decimal(10) ** stock_decimals
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        impact = Decimal(usdc_in_units) / Decimal(usdc_reserve_units + usdc_in_units)
        if impact >= impact_ceiling_fraction:
            raise LpPlanRefusalError(
                LpPlanRefusalCode.SWAP_IMPACT_ABOVE_CEILING,
                f"the balancing swap's conservative impact bound {impact:.6f} reaches the "
                f"{impact_ceiling_fraction} ceiling; lower the budget below the pool's "
                "executable depth",
            )
        tranche_count = 1
        if impact > tranche_threshold_fraction:
            tranche_count = int(
                (impact / tranche_threshold_fraction).to_integral_value(rounding=ROUND_CEILING)
            )
        return BalancingSwapPlan(
            required=True,
            stock_shortfall_units=stock_shortfall_units,
            usdc_in_units=usdc_in_units,
            expected_stock_units=expected_stock_units,
            modeled_impact_fraction=+impact,
            tranche_count=tranche_count,
            buffer_fraction=buffer_fraction,
        )


def _absent_balancing_swap(buffer_fraction: Decimal) -> BalancingSwapPlan:
    """Build the no-swap plan for inventory that already covers the stock side.

    Args:
        buffer_fraction: The directive's buffer, carried for completeness.

    Returns:
        The absent balancing swap plan.
    """
    return BalancingSwapPlan(
        required=False,
        stock_shortfall_units=0,
        usdc_in_units=0,
        expected_stock_units=0,
        modeled_impact_fraction=Decimal(0),
        tranche_count=1,
        buffer_fraction=buffer_fraction,
    )


def plan_mint_entry(
    policy: LpExecutionPolicy,
    observation: LpPoolObservation,
    directive: MintDirective,
    inventory: SafeInventory,
) -> LpMintPlan:
    """Plan one complete capped mint with its balancing swap and caps.

    Every cap is enforced in a fixed order before the plan is returned: the
    per-pool and total-exposure budget caps, the width ceiling clamp, range
    containment, the pool-depth share, the two-sided budget floor, the
    balancing swap's impact ceiling and tranche rule, and the Safe's USDC
    sufficiency for both the quote side and the swap.

    Args:
        policy: The hard caps enforced before anything is signed.
        observation: The block-pinned Sugar pool snapshot being planned.
        directive: The budget and width directive being planned.
        inventory: The Safe's token inventory and pilot exposure.

    Returns:
        The complete mint plan with every cap label and diagnostic.

    Raises:
        LpPlanRefusalError: If any cap or coherence gate refuses the request.
    """
    caps: list[str] = []
    budget = directive.budget_usdc
    if budget > policy.max_position_usdc_per_pool:
        raise LpPlanRefusalError(
            LpPlanRefusalCode.BUDGET_ABOVE_POOL_CAP,
            f"the {budget} USDC budget exceeds the per-pool cap of "
            f"{policy.max_position_usdc_per_pool} USDC; lower the budget",
        )
    caps.append(f"budget at or below the {policy.max_position_usdc_per_pool} USDC per-pool cap")
    remaining_exposure = (
        policy.max_total_pilot_exposure_usdc - inventory.existing_position_value_usdc
    )
    if budget > remaining_exposure:
        raise LpPlanRefusalError(
            LpPlanRefusalCode.BUDGET_ABOVE_TOTAL_EXPOSURE_CAP,
            f"the {budget} USDC budget exceeds the remaining pilot exposure of "
            f"{remaining_exposure} USDC under the {policy.max_total_pilot_exposure_usdc} USDC "
            "total cap",
        )
    caps.append(
        f"budget at or below the remaining {remaining_exposure} USDC of the "
        f"{policy.max_total_pilot_exposure_usdc} USDC total pilot cap"
    )
    position_range = derive_position_range(
        observation.current_tick,
        observation.tick_spacing,
        directive.half_width_spacings,
        directive.width_source,
        directive.max_range_half_width_fraction,
    )
    width_label = (
        "explicit override"
        if position_range.width_source is WidthSource.EXPLICIT_OVERRIDE
        else "solver-derived (pre-fix APR input)"
    )
    if position_range.clamped_to_ceiling:
        caps.append(
            f"requested width clamped to {position_range.half_width_ticks} ticks at or below "
            f"the {directive.max_range_half_width_fraction} ceiling"
        )
    else:
        caps.append(
            f"half width {position_range.half_width_ticks} ticks at or below the "
            f"{directive.max_range_half_width_fraction} ceiling ({width_label})"
        )
    price = observation.price_usdc_per_stock
    amounts = plan_mint_composition(
        observation.sqrt_ratio,
        position_range,
        budget,
        observation.stock_is_token0,
        observation.stock_decimals,
        observation.quote_decimals,
        directive.slippage_tolerance_fraction,
    )
    caps.append("snapshot price strictly inside the derived range")
    depth = estimate_in_range_depth_usdc(
        observation.sqrt_ratio,
        observation.tick_spacing,
        observation.current_tick,
        observation.pool_active_liquidity,
        observation.stock_is_token0,
        observation.stock_decimals,
        observation.quote_decimals,
    )
    if budget > depth * policy.max_position_fraction_of_pool_depth:
        raise LpPlanRefusalError(
            LpPlanRefusalCode.POSITION_ABOVE_POOL_DEPTH_FRACTION,
            f"the {budget} USDC budget exceeds the "
            f"{policy.max_position_fraction_of_pool_depth} share of the pool's estimated "
            f"in-range depth {depth} USDC; lower the budget",
        )
    caps.append(
        f"budget at or below {policy.max_position_fraction_of_pool_depth} of the estimated "
        f"{depth} USDC in-range depth"
    )
    # The stock side of the mint is whichever desired amount is the stock.
    stock_desired = (
        amounts.amount0_desired_units
        if observation.stock_is_token0
        else amounts.amount1_desired_units
    )
    usdc_desired = (
        amounts.amount1_desired_units
        if observation.stock_is_token0
        else amounts.amount0_desired_units
    )
    stock_shortfall = stock_desired - inventory.stock_units
    if stock_shortfall > 0:
        swap = plan_balancing_swap(
            stock_shortfall,
            price,
            observation.stock_decimals,
            observation.usdc_reserve_units,
            directive.swap_buffer_fraction,
            policy.swap_impact_ceiling_fraction,
            policy.swap_tranche_threshold_fraction,
        )
        caps.append(
            f"balancing swap impact {swap.modeled_impact_fraction:.6f} below the "
            f"{policy.swap_impact_ceiling_fraction} ceiling in {swap.tranche_count} tranche(s)"
        )
    else:
        swap = _absent_balancing_swap(directive.swap_buffer_fraction)
        caps.append("held stock covers the stock side; no balancing swap required")
    usdc_needed_units = usdc_desired + swap.usdc_in_units
    if usdc_needed_units > inventory.usdc_units:
        held = Decimal(inventory.usdc_units).scaleb(-QUOTE_TOKEN_DECIMALS)
        needed = Decimal(usdc_needed_units).scaleb(-QUOTE_TOKEN_DECIMALS)
        raise LpPlanRefusalError(
            LpPlanRefusalCode.INSUFFICIENT_USDC_FOR_ENTRY,
            f"entering needs {needed} USDC (quote side plus balancing swap) but the Safe "
            f"holds {held} USDC; fund the Safe or lower the budget",
        )
    caps.append("Safe USDC covers the quote side plus the balancing swap")
    diagnostics = _mint_diagnostics(
        observation,
        position_range,
        amounts,
        swap,
        price,
        budget,
        depth,
        width_label,
    )
    return LpMintPlan(
        symbol=observation.symbol,
        pool_address=observation.pool_address,
        nfpm_address=observation.nfpm_address,
        gauge_address=observation.gauge_address,
        position_range=position_range,
        amounts=amounts,
        balancing_swap=swap,
        price_usdc_per_stock=price,
        budget_usdc=budget,
        pool_depth_usdc=depth,
        caps_enforced=tuple(caps),
        diagnostics=tuple(diagnostics),
        snapshot_block=observation.snapshot_block,
        observed_at=observation.observed_at,
    )


def _mint_diagnostics(
    observation: LpPoolObservation,
    position_range: PositionTickRange,
    amounts: MintAmountPlan,
    swap: BalancingSwapPlan,
    price: Decimal,
    budget: Decimal,
    depth: Decimal,
    width_label: str,
) -> tuple[str, ...]:
    """Assemble the plan's human-readable evidence lines.

    Args:
        observation: The pool snapshot the plan derived from.
        position_range: The derived tick range.
        amounts: The mint amount plan.
        swap: The balancing swap plan.
        price: The snapshot USDC price of one whole stock token.
        budget: The committed budget.
        depth: The estimated in-range depth.
        width_label: The width-source label for the evidence line.

    Returns:
        The ordered diagnostic lines.
    """
    stock_units = (
        amounts.amount0_desired_units
        if observation.stock_is_token0
        else amounts.amount1_desired_units
    )
    usdc_units = (
        amounts.amount1_desired_units
        if observation.stock_is_token0
        else amounts.amount0_desired_units
    )
    swap_line = (
        f"balancing swap: {swap.usdc_in_units} raw USDC -> ~{swap.expected_stock_units} raw "
        f"stock ({swap.tranche_count} tranche(s), impact "
        f"{swap.modeled_impact_fraction:.6f}) covering the {swap.stock_shortfall_units}-unit "
        "shortfall"
        if swap.required
        else "balancing swap: none required; held stock covers the stock side"
    )
    return (
        f"{observation.symbol} pool {observation.pool_address} at snapshot block "
        f"{observation.snapshot_block}, price {price} USDC per {observation.symbol}",
        f"range [{position_range.tick_lower}, {position_range.tick_upper}) ticks, half width "
        f"{position_range.half_width_ticks} ticks ({width_label}), fraction "
        f"{position_range.half_width_fraction}",
        f"composition: {usdc_units} raw USDC + {stock_units} raw stock, liquidity "
        f"{amounts.liquidity}, sides valued {amounts.token0_value_usdc} + "
        f"{amounts.token1_value_usdc} USDC of the {budget} USDC budget",
        f"minima {amounts.amount0_min_units}/{amounts.amount1_min_units} raw at tolerance "
        f"{amounts.slippage_tolerance_fraction}",
        f"pool in-range depth estimate {depth} USDC; budget is "
        f"{_percentage_of(budget, depth)} of it",
        swap_line,
    )


def _percentage_of(part: Decimal, whole: Decimal) -> str:
    """Format one part's share of a whole as a percentage string.

    Args:
        part: The non-negative portion being expressed.
        whole: The positive whole it is measured against.

    Returns:
        The percentage with four decimal places and a percent sign.
    """
    if whole <= 0:
        raise ValueError("the whole must be positive")
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        return f"{(part / whole * Decimal(100)).quantize(Decimal('0.0001'))}%"
