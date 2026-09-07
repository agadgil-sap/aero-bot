"""Pure target-yield-derived range-width solving for the emissions-farming policy.

The v1 engine's fixed plus-or-minus 0.3 percent half width captures only a
fraction of these pools' quoted APRs because the emissions come from
ultra-tight ranges. This module derives the half width from a target net
daily yield instead: given live observables - the reconstructed emissions APR,
gauge staked liquidity and staked value, active-liquidity depth and fee
observations, and realized volatility from the reconstructed swap path - it
scans every tick-aligned candidate from one tick spacing up to the locked
ceiling and selects the tightest whose modeled net yield per deployed dollar
meets the target. Gross yields follow from the position's liquidity per
deployed dollar at the candidate width against the pool's observed
concentrations, and net subtracts the expected recenter gas and impact at
the capped position size, the event-window flat time, and the stop-risk
basis of an exact composition loss at the stop level plus exit and re-entry
batches. Every frequency and uptime figure comes from one documented
two-branch renewal model over the realized volatility: from the range center
the price first exits the band after the Brownian first-passage time, the
upside branch waits out the recenter wait and re-mints, and the downside
branch rides the excursion for the gambler's-ruin expected time and stops
with the gambler's-ruin probability or recovers for free. The module is
pure, deterministic, and Decimal-exact; missing or inconsistent inputs fail
toward the locked ceiling with an explicit fallback label.
"""

from datetime import timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, NonNegativeDecimal
from aero_bot.history import MAX_TOKEN_DECIMALS, PoolPricePath

# High internal precision keeps width solving deterministic across platforms.
MATH_PRECISION = 60
# Uniswap v3-style ticks advance the price by exactly this ratio per tick.
TICK_PRICE_RATIO = Decimal("1.0001")
# A fixed 365-day year matches the policy engine's annualization convention.
DAYS_PER_YEAR = Decimal(365)
# One day holds exactly 86,400 seconds for rate-to-frequency conversions.
SECONDS_PER_DAY = 86_400
# Gas prices are observed in gwei and converted with the exact one-billion scale.
GWEI_PER_ETH = Decimal(1_000_000_000)
# Fee tiers are expressed in parts per million of the swapped notional.
PPM_SCALE = Decimal(1_000_000)
# The default target is one percent net per day on deployed capital.
DEFAULT_TARGET_NET_DAILY_YIELD = Decimal("0.01")
# The locked safety ceiling is the v1 fixed half width; the solver never
# widens past it and falls back to it when its inputs are unusable.
DEFAULT_MAX_RANGE_HALF_WIDTH_FRACTION = Decimal("0.003")
# The default stop buffer mirrors the engine's downside stop placement.
DEFAULT_STOP_BUFFER_FRACTION = Decimal("0.005")
# The default recenter wait mirrors the engine's upside wait before a
# re-mint, during which the position sits out of range.
DEFAULT_RECENTER_WAIT_SECONDS = 900
# The default re-entry cooldown mirrors the engine's stop-exit cooldown.
DEFAULT_REENTRY_COOLDOWN_SECONDS = 900
# Weekday session windows (market open and close) keep the policy flat for
# ninety minutes on each of the five trading days.
DEFAULT_FLAT_HOURS_PER_WEEKDAY = Decimal("1.5")
WEEKDAYS_PER_WEEK = 5
HOURS_PER_WEEK = 168
# Default gas units mirror the engine's locked batch estimates.
DEFAULT_ENTER_BATCH_GAS_UNITS = 650_000
DEFAULT_RECENTER_BATCH_GAS_UNITS = 550_000
DEFAULT_EXIT_BATCH_GAS_UNITS = 350_000
DEFAULT_SAFE_OVERHEAD_GAS_PER_BATCH = 100_000
# The documented ETH price assumption mirrors the engine's locked value.
DEFAULT_ETH_PRICE_ASSUMPTION_USD = Decimal("3000")
# The binary search for the implied average half width closes far past
# Decimal working precision within this many bisections.
IMPLIED_WIDTH_SEARCH_STEPS = 200
# The implied-average inversion stays inside the region where liquidity per
# deployed dollar is strictly decreasing in the half width.
IMPLIED_WIDTH_SEARCH_BOUND = Decimal("0.5")
# Microsecond resolution keeps elapsed-time arithmetic exact in Decimals.
MICROSECONDS_PER_SECOND = Decimal(1_000_000)


class RangingObservations(BaseModel):
    """Collect every live observable one target-yield width solve consumes.

    The observables mirror what the rehearsal harness reconstructs and what
    the policy engine observes: the emissions APR in Aerodrome's display
    convention, the gauge's staked liquidity and staked value at one instant,
    the active-liquidity depth and fee evidence, the realized daily
    volatility of the reconstructed swap path, and the capped position size
    the entry gates would commit.
    """

    # Frozen strict fields keep one solve on a single coherent observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The pool price in USDC per one whole stock token at the solving instant.
    pool_price_usdc: Annotated[Decimal, Field(gt=0)]
    # Raw emissions APR per staked liquidity in Aerodrome's display
    # convention, the same reading the entry gate consumes.
    emissions_apr: NonNegativeDecimal
    # Gauge staked liquidity in the pool's raw liquidity units at the same
    # instant the staked value below was observed.
    gauge_liquidity_raw: Annotated[int, Field(ge=0)]
    # Staked value in USDC at that same instant, under the history module's
    # linear staked-value convention.
    staked_tvl_usd: Annotated[Decimal, Field(ge=0)]
    # Active in-range pool liquidity in raw units, the fee-share denominator.
    active_liquidity_raw: Annotated[int, Field(ge=0)]
    # Executable US-dollar depth across the plus-or-minus one-percent band,
    # the same notion the engine's position gate and impact model share.
    pool_depth_usd: NonNegativeDecimal
    # How many seconds of reconstructed swaps the fee evidence covers.
    fee_window_seconds: Annotated[int, Field(ge=0)]
    # Total swapped notional in USDC over that fee evidence window.
    fee_window_notional_usd: NonNegativeDecimal
    # The pool's staked fee tier in parts per million.
    pool_fee_ppm: Annotated[int, Field(ge=0)]
    # Realized daily volatility of the pool price; None means the path was
    # too thin to estimate it, which fails the solve toward the ceiling.
    realized_daily_volatility: NonNegativeDecimal | None = None
    # Decimal counts of the stock and USDC tokens scaling raw liquidity.
    stock_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    quote_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    # The capped position size in USDC the entry gates would commit.
    position_size_usd: Annotated[Decimal, Field(gt=0)]
    # The current Base L2 gas price in gwei behind every batch cost; zero is
    # a valid injected reading that makes every batch cost exactly zero.
    gas_price_gwei: Annotated[Decimal, Field(ge=0)]
    # Target net daily yield per deployed dollar the width must achieve.
    target_net_daily_yield: Annotated[Decimal, Field(gt=0)] = DEFAULT_TARGET_NET_DAILY_YIELD
    # The safety ceiling on each side; the solver never solves wider.
    max_range_half_width_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = (
        DEFAULT_MAX_RANGE_HALF_WIDTH_FRACTION
    )
    # The pool tick grid spacing; one spacing per side is the tightest
    # candidate the solver may select.
    tick_spacing: Annotated[int, Field(ge=1)] = 10
    # The downside stop sits this fraction below the aligned lower edge.
    stop_buffer_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = DEFAULT_STOP_BUFFER_FRACTION
    # The engine's upside wait before a re-mint, in seconds.
    recenter_wait_seconds: Annotated[int, Field(ge=0)] = DEFAULT_RECENTER_WAIT_SECONDS
    # The engine's re-entry cooldown after a stop exit, in seconds.
    reentry_cooldown_seconds: Annotated[int, Field(ge=0)] = DEFAULT_REENTRY_COOLDOWN_SECONDS
    # Batch gas estimates mirroring the engine's locked values.
    enter_batch_gas_units: Annotated[int, Field(ge=1)] = DEFAULT_ENTER_BATCH_GAS_UNITS
    recenter_batch_gas_units: Annotated[int, Field(ge=1)] = DEFAULT_RECENTER_BATCH_GAS_UNITS
    exit_batch_gas_units: Annotated[int, Field(ge=1)] = DEFAULT_EXIT_BATCH_GAS_UNITS
    safe_overhead_gas_per_batch: Annotated[int, Field(ge=0)] = DEFAULT_SAFE_OVERHEAD_GAS_PER_BATCH
    # The documented ETH price assumption behind every gas cost.
    eth_price_assumption_usd: Annotated[Decimal, Field(gt=0)] = DEFAULT_ETH_PRICE_ASSUMPTION_USD
    # Flat hours per weekday from the market open and close event windows.
    flat_hours_per_weekday: Annotated[Decimal, Field(ge=0, le=24)] = DEFAULT_FLAT_HOURS_PER_WEEKDAY


class RangingEvidence(BaseModel):
    """Carry the live ranging observables the policy engine does not already see.

    The engine's observation already supplies the pool price, the raw
    emissions APR, the executable depth, the capped position size, and the
    gas price; this model carries exactly the remaining observables one
    width solve consumes, so the engine can assemble a complete
    ``RangingObservations`` from one observation plus its locked parameters.
    """

    # Frozen strict fields keep one solve's live inputs on a single snapshot.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Gauge staked liquidity in the pool's raw liquidity units at the same
    # instant the staked value below was observed.
    gauge_liquidity_raw: Annotated[int, Field(ge=0)]
    # Staked value in USDC at that same instant, under the history module's
    # linear staked-value convention.
    staked_tvl_usd: Annotated[Decimal, Field(ge=0)]
    # Active in-range pool liquidity in raw units, the fee-share denominator.
    active_liquidity_raw: Annotated[int, Field(ge=0)]
    # How many seconds of reconstructed swaps the fee evidence covers.
    fee_window_seconds: Annotated[int, Field(ge=0)]
    # Total swapped notional in USDC over that fee evidence window.
    fee_window_notional_usd: Annotated[Decimal, Field(ge=0)]
    # The pool's staked fee tier in parts per million.
    pool_fee_ppm: Annotated[int, Field(ge=0)]
    # Realized daily volatility of the pool price; None means the path was
    # too thin to estimate it, which fails the solve toward the ceiling.
    realized_daily_volatility: NonNegativeDecimal | None = None
    # Decimal counts of the stock and USDC tokens scaling raw liquidity.
    stock_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    quote_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]


class WidthSolveMode(StrEnum):
    """Identify how one width solve resolved."""

    # A candidate half width met the target net daily yield.
    SOLVED = "solved"
    # Even the tightest candidate could not reach the target; the engine
    # still enters at the tightest width when the coarse gate passed.
    TARGET_UNREACHABLE = "target_unreachable"
    # The solver's inputs were missing or inconsistent, so the width fell
    # back to the locked ceiling as the anomaly-safe default.
    FALLBACK_CEILING = "fallback_ceiling"


class CandidateWidthEvaluation(BaseModel):
    """Record every modeled quantity behind one tick-aligned candidate width."""

    # Frozen strict fields keep each candidate's evidence inseparable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Half width in whole ticks on each side of the reference price.
    half_width_ticks: Annotated[int, Field(ge=1)]
    # Half width as the exact fractional price distance 1.0001**ticks - 1.
    half_width_fraction: Annotated[Decimal, Field(gt=0)]
    # The position's liquidity per deployed dollar at this width.
    position_liquidity_per_dollar: Annotated[Decimal, Field(gt=0)]
    # Gross emissions yield per deployed dollar per day at this width.
    gross_emissions_yield_per_day: NonNegativeDecimal
    # Gross swap-fee yield per deployed dollar per day at this width.
    fee_yield_per_day: NonNegativeDecimal
    # Fraction of each day the position is open and in range.
    uptime_fraction: Annotated[Decimal, Field(ge=0, le=1)]
    # Expected upside-exit recenters per day under the renewal model.
    recenter_rate_per_day: NonNegativeDecimal
    # Recenter gas and impact cost per deployed dollar per day.
    recenter_cost_per_day: NonNegativeDecimal
    # Expected stop-outs per day under the renewal model.
    stop_rate_per_day: NonNegativeDecimal
    # Stop-loss, exit, and re-entry cost per deployed dollar per day.
    stop_cost_per_day: NonNegativeDecimal
    # Net yield per deployed dollar per day after every modeled cost.
    net_yield_per_day: Decimal
    # Whether this candidate's net yield meets the target.
    meets_target: bool


class WidthSolution(BaseModel):
    """Emit one deterministic width solve with its complete evidence."""

    # Frozen strict fields keep the solve exactly as the audit will record it.
    model_config = IMMUTABLE_MODEL_CONFIG

    # How the solve resolved: solved, unreachable at the tightest width, or
    # a fail-toward-the-ceiling fallback.
    mode: WidthSolveMode
    # The selected half width in whole ticks on each side.
    half_width_ticks: Annotated[int, Field(ge=1)]
    # The selected half width as the exact fractional price distance.
    half_width_fraction: Annotated[Decimal, Field(gt=0)]
    # The pool tick grid spacing the candidate ticks align to.
    tick_spacing: Annotated[int, Field(ge=1)]
    # The target net daily yield the solve aimed for.
    target_net_daily_yield: Annotated[Decimal, Field(gt=0)]
    # The half width implied by the pool's average staked concentration,
    # carried as a diagnostic; None when the inputs cannot imply one.
    implied_average_half_width_fraction: Decimal | None = None
    # Every evaluated candidate in ascending width order.
    evaluations: Annotated[tuple[CandidateWidthEvaluation, ...], Field(min_length=1)]
    # Human-readable evidence lines covering inputs, intermediates, and the
    # chosen width, ready for decision diagnostics and audit events.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_fraction_matches_ticks(self) -> Self:
        """Reject a half-width fraction that disagrees with its tick count."""
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            expected = TICK_PRICE_RATIO**self.half_width_ticks - Decimal(1)
            if self.half_width_fraction != expected:
                raise ValueError("half_width_fraction must equal 1.0001**half_width_ticks - 1")
        return self


def realized_daily_volatility(path: PoolPricePath) -> Decimal | None:
    """Estimate realized daily volatility from one reconstructed price path.

    The estimator sums squared logarithmic returns over the window and
    normalizes by the window's exact length in days, the standard zero-mean
    realized-variance estimator appropriate for high-frequency observations.

    Args:
        path: The pool's reconstructed swap-driven price path.

    Returns:
        The realized daily volatility as a decimal fraction (0.02 means two
        percent per day), or None when the path holds fewer than two points
        or spans no positive time.
    """
    points = path.points
    if len(points) < 2:
        return None
    # Whole microseconds keep the span exact where float seconds drift.
    span_micros = (points[-1].timestamp - points[0].timestamp) // timedelta(microseconds=1)
    if span_micros <= 0:
        return None
    with localcontext() as decimal_context:
        # Local precision isolates deterministic variance math from settings.
        decimal_context.prec = MATH_PRECISION
        squared_returns = [
            (later.price_usdc / earlier.price_usdc).ln() ** Decimal(2)
            # The pairwise zip is intentionally one element shorter on the right.
            for earlier, later in zip(points, points[1:], strict=False)
        ]
        squared_return_sum = sum(squared_returns, start=Decimal(0))
        span_days = Decimal(span_micros) / MICROSECONDS_PER_SECOND / Decimal(SECONDS_PER_DAY)
        return +(squared_return_sum / span_days).sqrt()


def band_shape(half_width_fraction: Decimal) -> Decimal:
    """Measure the geometric shape of one symmetric fractional price band.

    The value is the difference between the band's geometric-center root and
    its lower root in square-root-price units scaled by the square root of
    the reference price: a position valued at the center over twice this
    quantity is the position's liquidity.

    Args:
        half_width_fraction: Fractional band half width on each side.

    Returns:
        The band shape value g(w) = (1-w^2)^(1/4) - (1-w)^(1/2).

    Raises:
        ValueError: If the half width is not inside the unit interval.
    """
    if not Decimal(0) < half_width_fraction < Decimal(1):
        raise ValueError("half_width_fraction must be between zero and one")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic band math from settings.
        decimal_context.prec = MATH_PRECISION
        width = half_width_fraction
        return +((Decimal(1) - width ** Decimal(2)).sqrt().sqrt() - (Decimal(1) - width).sqrt())


def liquidity_per_deployed_dollar(
    pool_price_usdc: Decimal,
    half_width_fraction: Decimal,
) -> Decimal:
    """Calculate a centered position's liquidity per deployed dollar.

    A range entered at its geometric center holds liquidity equal to the
    committed value over twice the center-to-lower square-root distance, so
    the liquidity bought per deployed dollar is exactly the reciprocal of
    that distance scaled by the square root of the reference price.

    Args:
        pool_price_usdc: Positive pool price in USDC per stock.
        half_width_fraction: Fractional band half width on each side.

    Returns:
        Human-unit liquidity per deployed USDC at this width.

    Raises:
        ValueError: If the price is not positive or the width is invalid.
    """
    if pool_price_usdc <= 0:
        raise ValueError("pool_price_usdc must be positive")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic liquidity math from settings.
        decimal_context.prec = MATH_PRECISION
        shape = band_shape(half_width_fraction)
        return +(Decimal(1) / (Decimal(2) * pool_price_usdc.sqrt() * shape))


def implied_average_half_width_fraction(
    pool_price_usdc: Decimal,
    gauge_liquidity_raw: int,
    staked_tvl_usd: Decimal,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal | None:
    """Invert the pool's staked concentration into an average half width.

    Gauge staked liquidity per staked dollar equals a centered position's
    liquidity per deployed dollar, so inverting that ratio at the pool price
    yields the half width the whole staked book would hold if every staked
    position shared one width. Because Sugar's gauge liquidity counts only
    active in-range staked liquidity while staked value counts everything
    staked, the implied width is a labeled concentration diagnostic, tighter
    than any individual LP's true range.

    Args:
        pool_price_usdc: Positive pool price in USDC per stock.
        gauge_liquidity_raw: Gauge staked liquidity in raw units.
        staked_tvl_usd: Staked value in USDC observed with that liquidity.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The implied average half width as a fractional price distance, or
        None when the ratio cannot be inverted inside the monotone region.
    """
    if gauge_liquidity_raw <= 0 or staked_tvl_usd <= 0:
        return None
    with localcontext() as decimal_context:
        # Local precision isolates deterministic inversion math from settings.
        decimal_context.prec = MATH_PRECISION
        # Human liquidity divides raw liquidity by ten to half the combined
        # decimal count of the pair.
        human_scale = Decimal(10) ** (
            (Decimal(stock_decimals) + Decimal(quote_decimals)) / Decimal(2)
        )
        observed_ratio = Decimal(gauge_liquidity_raw) / human_scale / staked_tvl_usd
        # Liquidity per deployed dollar decreases in the half width across
        # the search region, so a ratio below the region's minimum cannot
        # belong to any single width and degrades to None.
        region_minimum = liquidity_per_deployed_dollar(pool_price_usdc, IMPLIED_WIDTH_SEARCH_BOUND)
        if observed_ratio < region_minimum:
            return None
        # One bisection locates the crossing exactly inside the region.
        low = Decimal(0)
        high = IMPLIED_WIDTH_SEARCH_BOUND
        for _ in range(IMPLIED_WIDTH_SEARCH_STEPS):
            middle = (low + high) / Decimal(2)
            if liquidity_per_deployed_dollar(pool_price_usdc, middle) > observed_ratio:
                low = middle
            else:
                high = middle
        return +((low + high) / Decimal(2))


def _batch_cost_usd(
    gas_price_gwei: Decimal,
    action_gas_units: int,
    safe_overhead_gas_units: int,
    eth_price_assumption_usd: Decimal,
) -> Decimal:
    """Estimate one batch's gas cost in USDC under the locked assumptions.

    Args:
        gas_price_gwei: Current Base gas price in gwei.
        action_gas_units: Protocol-side gas estimate for the batch body.
        safe_overhead_gas_units: Safe proxy execution overhead per batch.
        eth_price_assumption_usd: Documented USDC-per-ETH price assumption.

    Returns:
        The estimated batch cost in USDC.
    """
    return (
        Decimal(action_gas_units + safe_overhead_gas_units)
        * gas_price_gwei
        / GWEI_PER_ETH
        * eth_price_assumption_usd
    )


def _swap_impact_cost_usd(swap_usd: Decimal, pool_depth_usd: Decimal) -> Decimal:
    """Charge one modeled swap half its end impact, the ledger's convention.

    Args:
        swap_usd: US-dollar value the swap converts.
        pool_depth_usd: Observed executable depth of the swap route.

    Returns:
        The modeled impact cost in USDC; zero when the depth is zero because
        the caller gates on positive depth before any solve.
    """
    if pool_depth_usd <= 0:
        return Decimal(0)
    return swap_usd * (swap_usd / pool_depth_usd) / Decimal(2)


def _position_value_fraction_at_price(
    half_width_fraction: Decimal,
    price: Decimal,
) -> Decimal:
    """Mark a centered position at one price as a fraction of committed value.

    The composition rule is the engine's exact v3-style math: liquidity
    follows from the committed value at the geometric center, the stock side
    spans the evaluated-to-upper square-root band, and the USDC side spans
    the lower-to-evaluated band.

    Args:
        half_width_fraction: Fractional band half width on each side.
        price: Positive pool price in USDC per stock to mark at.

    Returns:
        The position's value at the price divided by its committed value.

    Raises:
        ValueError: If the price is not positive.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic composition math from settings.
        decimal_context.prec = MATH_PRECISION
        width = half_width_fraction
        sqrt_lower = (Decimal(1) - width).sqrt()
        sqrt_upper = (Decimal(1) + width).sqrt()
        sqrt_center = (sqrt_lower * sqrt_upper).sqrt()
        # Committed value V at the center implies liquidity L = V / (2(c-l)),
        # so with V normalized to one the liquidity is this scale-free form.
        liquidity = Decimal(1) / (Decimal(2) * (sqrt_center - sqrt_lower))
        sqrt_price = price.sqrt()
        if price <= Decimal(1) - width:
            # Below the band the position is entirely stock tokens.
            stock = liquidity * (Decimal(1) / sqrt_lower - Decimal(1) / sqrt_upper)
            usdc = Decimal(0)
        elif price < Decimal(1) + width:
            stock = liquidity * (Decimal(1) / sqrt_price - Decimal(1) / sqrt_upper)
            usdc = liquidity * (sqrt_price - sqrt_lower)
        else:
            # Above the band the position is entirely USDC.
            stock = Decimal(0)
            usdc = liquidity * (sqrt_upper - sqrt_lower)
        return +(stock * price + usdc)


def _candidate_tick_bounds(observations: RangingObservations) -> tuple[int, int]:
    """Bound the candidate half widths in whole ticks on each side.

    Args:
        observations: The validated solve inputs.

    Returns:
        The inclusive (minimum, maximum) tick counts; the minimum is exactly
        one tick spacing per side and the maximum is the greatest tick count
        whose fractional width stays at or below the ceiling.
    """
    with localcontext() as decimal_context:
        # Local precision isolates the deterministic logarithm from settings.
        decimal_context.prec = MATH_PRECISION
        max_ticks = int(
            (
                (Decimal(1) + observations.max_range_half_width_fraction).ln()
                / TICK_PRICE_RATIO.ln()
            ).to_integral_value(rounding="ROUND_FLOOR")
        )
    return observations.tick_spacing, max(observations.tick_spacing, max_ticks)


def ceiling_width_solution(
    pool_price_usdc: Decimal,
    tick_spacing: int,
    max_range_half_width_fraction: Decimal,
    target_net_daily_yield: Decimal,
    reason: str,
    prefix_diagnostics: tuple[str, ...] = (),
) -> WidthSolution:
    """Build the fail-toward-the-ceiling solution for unusable solve inputs.

    Args:
        pool_price_usdc: Positive pool price in USDC per stock.
        tick_spacing: The pool tick grid spacing; one spacing per side is the
            tightest candidate the solver may select.
        max_range_half_width_fraction: The locked ceiling half width.
        target_net_daily_yield: The target the solve would have aimed for.
        reason: The fail-closed reason naming the unusable input.
        prefix_diagnostics: Optional evidence lines echoed before the fallback
            line, such as the raw inputs of a solve that failed.

    Returns:
        The ceiling-width solution with the fallback label and evidence.

    Raises:
        ValueError: If the pool price is not positive.
    """
    if pool_price_usdc <= 0:
        raise ValueError("pool_price_usdc must be positive")
    with localcontext() as decimal_context:
        # Local precision isolates the deterministic logarithm from settings.
        decimal_context.prec = MATH_PRECISION
        max_ticks = int(
            (
                (Decimal(1) + max_range_half_width_fraction).ln() / TICK_PRICE_RATIO.ln()
            ).to_integral_value(rounding="ROUND_FLOOR")
        )
        max_ticks = max(tick_spacing, max_ticks)
        ceiling_fraction = TICK_PRICE_RATIO**max_ticks - Decimal(1)
        ceiling_liquidity_per_dollar = liquidity_per_deployed_dollar(
            pool_price_usdc, ceiling_fraction
        )
        return WidthSolution(
            mode=WidthSolveMode.FALLBACK_CEILING,
            half_width_ticks=max_ticks,
            half_width_fraction=+ceiling_fraction,
            tick_spacing=tick_spacing,
            target_net_daily_yield=target_net_daily_yield,
            evaluations=(
                CandidateWidthEvaluation(
                    half_width_ticks=max_ticks,
                    half_width_fraction=+ceiling_fraction,
                    position_liquidity_per_dollar=+ceiling_liquidity_per_dollar,
                    gross_emissions_yield_per_day=Decimal(0),
                    fee_yield_per_day=Decimal(0),
                    uptime_fraction=Decimal(0),
                    recenter_rate_per_day=Decimal(0),
                    recenter_cost_per_day=Decimal(0),
                    stop_rate_per_day=Decimal(0),
                    stop_cost_per_day=Decimal(0),
                    net_yield_per_day=Decimal(0),
                    meets_target=False,
                ),
            ),
            diagnostics=prefix_diagnostics
            + (
                f"Width solver inputs are unusable - {reason}; failing toward the locked "
                f"ceiling half width {+ceiling_fraction} ({max_ticks} ticks per side).",
            ),
        )


def _fallback_solution(
    observations: RangingObservations,
    reason: str,
) -> WidthSolution:
    """Build the fail-toward-the-ceiling solution for unusable inputs.

    Args:
        observations: The solve inputs whose usability failed.
        reason: The fail-closed reason naming the unusable input.

    Returns:
        The ceiling-width solution with the fallback label and evidence.
    """
    return ceiling_width_solution(
        pool_price_usdc=observations.pool_price_usdc,
        tick_spacing=observations.tick_spacing,
        max_range_half_width_fraction=observations.max_range_half_width_fraction,
        target_net_daily_yield=observations.target_net_daily_yield,
        reason=reason,
        prefix_diagnostics=_input_diagnostics(observations),
    )


def _format_optional_decimal(value: Decimal | None) -> str:
    """Format one optional decimal for a diagnostics line.

    Args:
        value: The decimal to format, or None.

    Returns:
        The decimal's string form, or the unavailable label.
    """
    return "unavailable" if value is None else str(value)


def _input_diagnostics(observations: RangingObservations) -> tuple[str, ...]:
    """Echo every solve input as one evidence line per fact.

    Args:
        observations: The validated solve inputs.

    Returns:
        The diagnostic lines enumerating the inputs.
    """
    return (
        f"Pool price {observations.pool_price_usdc} USDC per stock.",
        f"Raw emissions APR {observations.emissions_apr} in Aerodrome's display convention.",
        f"Gauge staked liquidity {observations.gauge_liquidity_raw} raw units over "
        f"staked value {observations.staked_tvl_usd} USDC.",
        f"Active in-range liquidity {observations.active_liquidity_raw} raw units; "
        f"executable depth {observations.pool_depth_usd} USDC across the "
        "plus-or-minus one-percent band.",
        f"Fee evidence: notional {observations.fee_window_notional_usd} USDC over "
        f"{observations.fee_window_seconds} seconds at {observations.pool_fee_ppm} ppm.",
        f"Realized daily volatility "
        f"{_format_optional_decimal(observations.realized_daily_volatility)}.",
        f"Capped position size {observations.position_size_usd} USDC at gas "
        f"{observations.gas_price_gwei} gwei and ETH "
        f"{observations.eth_price_assumption_usd} USDC.",
        f"Target net daily yield {observations.target_net_daily_yield} per deployed "
        f"dollar; half-width bounds one tick spacing ({observations.tick_spacing} "
        f"ticks) to {observations.max_range_half_width_fraction}.",
    )


def solve_range_width(observations: RangingObservations) -> WidthSolution:
    """Solve for the tightest tick-aligned half width meeting the target yield.

    Every tick count from one spacing per side up to the ceiling is modeled.
    Gross emissions and fee yields follow from the position's liquidity per
    deployed dollar at that width and the pool's observed concentrations.
    The costs subtract the expected recenter gas and impact at the capped
    position size, the event-window flat time, and the stop-risk basis of an
    exact composition loss at the stop level plus exit and re-entry batches,
    with every frequency and uptime figure from the documented two-branch
    renewal model over the realized volatility. The tightest candidate whose
    net yield meets the target wins; when none does, the solve reports the
    target unreachable at the tightest width, and when the inputs are
    missing or inconsistent it fails toward the ceiling.

    Args:
        observations: The validated live observables for one solve.

    Returns:
        The immutable width solution with every candidate's evidence.
    """
    # Unusable inputs fail toward the ceiling before any modeling.
    if observations.realized_daily_volatility is None:
        return _fallback_solution(observations, "realized volatility is unavailable")
    if observations.gauge_liquidity_raw <= 0 or observations.staked_tvl_usd <= 0:
        return _fallback_solution(observations, "gauge staked liquidity or staked value is missing")
    if observations.active_liquidity_raw <= 0 or observations.pool_depth_usd <= 0:
        return _fallback_solution(observations, "active liquidity or executable depth is missing")
    if observations.fee_window_notional_usd > 0 and observations.fee_window_seconds <= 0:
        return _fallback_solution(
            observations, "fee evidence carries notional over an empty window"
        )

    volatility = observations.realized_daily_volatility
    position_size = observations.position_size_usd
    with localcontext() as decimal_context:
        # Local precision keeps every modeled quantity deterministic.
        decimal_context.prec = MATH_PRECISION
        # The staked book's concentration and its implied average half width.
        human_scale = Decimal(10) ** (
            (Decimal(observations.stock_decimals) + Decimal(observations.quote_decimals))
            / Decimal(2)
        )
        staked_liquidity_per_dollar = (
            Decimal(observations.gauge_liquidity_raw) / human_scale / observations.staked_tvl_usd
        )
        implied_width = implied_average_half_width_fraction(
            observations.pool_price_usdc,
            observations.gauge_liquidity_raw,
            observations.staked_tvl_usd,
            observations.stock_decimals,
            observations.quote_decimals,
        )
        # Fee yield normalizes the observed notional into a daily stream.
        daily_notional = (
            observations.fee_window_notional_usd
            * Decimal(SECONDS_PER_DAY)
            / Decimal(observations.fee_window_seconds)
            if observations.fee_window_seconds > 0
            else Decimal(0)
        )
        fee_stream_per_day = daily_notional * Decimal(observations.pool_fee_ppm) / PPM_SCALE
        # Batch costs under the documented gas and ETH assumptions.
        recenter_gas = _batch_cost_usd(
            observations.gas_price_gwei,
            observations.recenter_batch_gas_units,
            observations.safe_overhead_gas_per_batch,
            observations.eth_price_assumption_usd,
        )
        exit_gas = _batch_cost_usd(
            observations.gas_price_gwei,
            observations.exit_batch_gas_units,
            observations.safe_overhead_gas_per_batch,
            observations.eth_price_assumption_usd,
        )
        enter_gas = _batch_cost_usd(
            observations.gas_price_gwei,
            observations.enter_batch_gas_units,
            observations.safe_overhead_gas_per_batch,
            observations.eth_price_assumption_usd,
        )
        # Per-event impact at the capped position size: the recenter
        # rebalance swaps half the committed value, the stop exit swaps the
        # whole all-stock position.
        recenter_impact = _swap_impact_cost_usd(
            position_size / Decimal(2), observations.pool_depth_usd
        )
        stop_impact = _swap_impact_cost_usd(position_size, observations.pool_depth_usd)
        # Event windows keep the policy flat for the configured hours on each
        # of the five trading days, averaged over the whole week.
        flat_fraction = (
            Decimal(WEEKDAYS_PER_WEEK)
            * observations.flat_hours_per_weekday
            / Decimal(HOURS_PER_WEEK)
        )

        min_ticks, max_ticks = _candidate_tick_bounds(observations)
        evaluations: list[CandidateWidthEvaluation] = []
        for ticks in range(min_ticks, max_ticks + 1):
            width = TICK_PRICE_RATIO ** Decimal(ticks) - Decimal(1)
            liquidity_per_dollar = liquidity_per_deployed_dollar(
                observations.pool_price_usdc, width
            )
            # Gross emissions: the position's share of the gauge reward
            # stream valued per deployed dollar per day.
            gross_emissions = (
                observations.emissions_apr
                / DAYS_PER_YEAR
                * liquidity_per_dollar
                / staked_liquidity_per_dollar
            )
            # Gross fees: the position's active-liquidity share of the
            # observed daily fee stream, per deployed dollar. The share's
            # position size is already inside the raw liquidity per dollar,
            # matching the ledger's raw-share fee convention.
            fee_yield = (
                fee_stream_per_day
                * liquidity_per_dollar
                * human_scale
                / Decimal(observations.active_liquidity_raw)
                if fee_stream_per_day > 0
                else Decimal(0)
            )
            # The two-branch renewal cycle behind every frequency and uptime
            # figure: from the range center the price reaches either band
            # edge after the first-passage time w^2/sigma^2, each side with
            # probability one half. The upside branch waits out the recenter
            # wait and re-mints. The downside branch rides the excursion for
            # the gambler's-ruin expected time w*gap/sigma^2 and then stops
            # with probability w/(w+gap), paying the cooldown, or recovers
            # back into the range for free. A flat realized volatility means
            # the price never leaves the band at all.
            stop_distance = Decimal(1) - (Decimal(1) - width) * (
                Decimal(1) - observations.stop_buffer_fraction
            )
            stop_gap = stop_distance - width
            if volatility > 0:
                exit_days = width ** Decimal(2) / volatility ** Decimal(2)
                excursion_days = width * stop_gap / volatility ** Decimal(2)
                stop_probability = width / (width + stop_gap)
                wait_days = Decimal(observations.recenter_wait_seconds) / Decimal(SECONDS_PER_DAY)
                cooldown_days = Decimal(observations.reentry_cooldown_seconds) / Decimal(
                    SECONDS_PER_DAY
                )
                upside_days = wait_days
                downside_days = excursion_days + stop_probability * cooldown_days
                cycle_days = exit_days + (upside_days + downside_days) / Decimal(2)
                downtime_fraction = (upside_days + downside_days) / (Decimal(2) * cycle_days)
                recenter_rate = Decimal(1) / Decimal(2) / cycle_days
                stop_rate = stop_probability / Decimal(2) / cycle_days
                uptime = (Decimal(1) - flat_fraction) * (Decimal(1) - downtime_fraction)
            else:
                recenter_rate = Decimal(0)
                stop_rate = Decimal(0)
                uptime = Decimal(1) - flat_fraction
            # Recenter cost: gas plus rebalance impact per upside exit.
            recenter_cost = recenter_rate * (recenter_gas + recenter_impact) / position_size
            # Stop cost: the exact composition loss at the stop level plus
            # the exit and re-entry batches and the exit-swap impact.
            stop_loss_fraction = Decimal(1) - _position_value_fraction_at_price(
                width, Decimal(1) - stop_distance
            )
            stop_cost = stop_rate * (
                stop_loss_fraction + (exit_gas + enter_gas + stop_impact) / position_size
            )
            net_yield = (gross_emissions + fee_yield) * uptime - recenter_cost - stop_cost
            evaluations.append(
                CandidateWidthEvaluation(
                    half_width_ticks=ticks,
                    half_width_fraction=+width,
                    position_liquidity_per_dollar=+liquidity_per_dollar,
                    gross_emissions_yield_per_day=+gross_emissions,
                    fee_yield_per_day=+fee_yield,
                    uptime_fraction=+uptime,
                    recenter_rate_per_day=+recenter_rate,
                    recenter_cost_per_day=+recenter_cost,
                    stop_rate_per_day=+stop_rate,
                    stop_cost_per_day=+stop_cost,
                    net_yield_per_day=+net_yield,
                    meets_target=net_yield >= observations.target_net_daily_yield,
                )
            )

        # The tightest candidate meeting the target wins; never widen past
        # the solver's answer to hedge, and never solve wider than the
        # ceiling.
        chosen = next(
            (row for row in evaluations if row.meets_target),
            None,
        )
        input_lines = _input_diagnostics(observations)
        if chosen is None:
            tightest = evaluations[0]
            diagnostics = input_lines + (
                f"Implied average staked half width {_format_optional_decimal(implied_width)}.",
                f"Even the tightest candidate ({tightest.half_width_ticks} ticks, net "
                f"{tightest.net_yield_per_day} per deployed dollar per day) cannot "
                f"reach the target {observations.target_net_daily_yield}; entering at "
                "the tightest width anyway because the coarse gate passed.",
            )
            return WidthSolution(
                mode=WidthSolveMode.TARGET_UNREACHABLE,
                half_width_ticks=tightest.half_width_ticks,
                half_width_fraction=tightest.half_width_fraction,
                tick_spacing=observations.tick_spacing,
                target_net_daily_yield=observations.target_net_daily_yield,
                implied_average_half_width_fraction=implied_width,
                evaluations=tuple(evaluations),
                diagnostics=diagnostics,
            )
        diagnostics = input_lines + (
            f"Implied average staked half width {_format_optional_decimal(implied_width)}.",
            f"Solved half width {chosen.half_width_fraction} "
            f"({chosen.half_width_ticks} ticks per side) as the tightest candidate "
            "meeting the target: "
            f"gross emissions {chosen.gross_emissions_yield_per_day} plus fees "
            f"{chosen.fee_yield_per_day} per deployed dollar per day at uptime "
            f"{chosen.uptime_fraction}, minus recenter cost "
            f"{chosen.recenter_cost_per_day} at {chosen.recenter_rate_per_day} recenters "
            f"per day and stop cost {chosen.stop_cost_per_day} at "
            f"{chosen.stop_rate_per_day} stops per day, nets "
            f"{chosen.net_yield_per_day} against the target "
            f"{observations.target_net_daily_yield}.",
        )
        return WidthSolution(
            mode=WidthSolveMode.SOLVED,
            half_width_ticks=chosen.half_width_ticks,
            half_width_fraction=chosen.half_width_fraction,
            tick_spacing=observations.tick_spacing,
            target_net_daily_yield=observations.target_net_daily_yield,
            implied_average_half_width_fraction=implied_width,
            evaluations=tuple(evaluations),
            diagnostics=diagnostics,
        )
