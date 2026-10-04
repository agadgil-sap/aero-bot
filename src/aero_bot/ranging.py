"""Adaptive executable-range width solving for the emissions-farming policy.

The captain's restart direction (2026-10-03) supersedes both the v1 fixed
plus-or-minus 0.3 percent half width and the v2 tightest-meeting-target
satisficing solve: live ranges are selected sensibly inside a 0.1 to 0.3
percent per-side band, from evidence, with nothing fixed in advance. This
module implements that solve on the EXECUTABLE tick grid itself.

Given live observables - the conservative emissions income basis in
Aerodrome's display convention, gauge staked liquidity and the current-cell
staked value behind the displayed APR, active-liquidity depth and fee
observations, realized volatility and the bounded trailing price path from
the pool's own Swap logs, the capped position size, and the live gas price -
the solver enumerates the actual grid-aligned lower and upper bounds around
the current price whose REAL per-side price distances fall inside the band,
scores that executable geometry exactly as it would be minted, and selects
the candidate with the strongest conservative expected net emissions
return. Symmetric centered ideals are never solved and rounded outward:
the scored object IS the minted object, the per-side distances are
asymmetric whenever the price sits off-grid (which is always), and any
aligned bound whose realized side distance leaves the band is excluded as
infeasible rather than accommodated by silently widening the ceiling.

The economics: gross emissions follow from the position's added liquidity
against the gauge denominator AFTER our own stake - a candidate more
concentrated than the existing staked book dilutes itself more than a
dollar-value fraction predicts - gross fees follow from the position's
share of active liquidity after our stake, and the costs subtract the
expected recenter gas and impact at the capped position size and the
stop-risk basis of an exact composition loss at the stop level plus exit
and re-entry batches. Churn frequencies come from the documented
two-branch renewal model over the realized volatility generalized to
asymmetric bands, while the uptime input is the MEASURED trailing dwell
inside the candidate's own price band - the renewal model's vol-independent
uptime floor was measured misestimating dwell in both directions by pool
character, so modeled uptime survives only as a labeled fallback when no
path is carried. The one-percent-per-day target stays a REPORTED target:
selection is argmax of modeled net, a negative candidate is never picked,
and when every feasible candidate nets nonpositive the solve says so and
the engine holds cash.

Fail-closed doctrine (the spec's rule, superseding the v2 ceiling
fallback): when the ranging evidence is missing, stale, thin, or its read
budget is exhausted, the solve resolves DEFERRED with an explicit reason
and the engine defers new entries and voluntary recenters - never a
substituted constant range. Existing positions keep their stops, loss
halts, custody protections, and required safety exits, which consume no
ranging evidence. The module is pure, deterministic, and Decimal-exact.
"""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, NonNegativeDecimal
from aero_bot.history import MAX_TOKEN_DECIMALS, PoolPricePath, PoolPricePoint

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
# The reported target net yield is one percent per day on deployed capital;
# it is reported evidence, never a satisficing rule or a reason to pick a
# negative candidate (the captain's 2026-10-03 adaptive-width direction).
DEFAULT_TARGET_NET_DAILY_YIELD = Decimal("0.01")
# The captain's adaptive band: per-side REAL price distances from the
# current price must fall at or above this floor (0.1 percent per side).
DEFAULT_MIN_RANGE_HALF_WIDTH_FRACTION = Decimal("0.001")
# ...and at or below this ceiling (0.3 percent per side, the v1 locked
# half width, now the band's upper edge rather than a constant entry).
DEFAULT_MAX_RANGE_HALF_WIDTH_FRACTION = Decimal("0.003")
# The band's grid search never needs more aligned bounds per side than the
# widest possible ceiling over the smallest grid step plus one for phase.
MAX_ALIGNED_BOUNDS_PER_SIDE = 8
# The default stop buffer mirrors the engine's downside stop placement.
DEFAULT_STOP_BUFFER_FRACTION = Decimal("0.005")
# The default recenter wait and grace mirror the engine's acting window:
# the solve charges the production acting window min(grace, wait), never
# the retired longer wait alone (the model-drift correction).
DEFAULT_RECENTER_WAIT_SECONDS = 900
DEFAULT_OUT_OF_RANGE_GRACE_SECONDS = 600
# The default re-entry cooldown mirrors the engine's stop-exit cooldown.
DEFAULT_REENTRY_COOLDOWN_SECONDS = 900
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
# Basis points scale per-side distances for human-readable evidence lines.
BPS_SCALE = Decimal(10_000)
# The measured-dwell basis needs at least two path points spanning positive
# time to compute anything at all; anything thinner is insufficient
# evidence and defers.
MIN_DWELL_POINTS = 2
# The raw-tick conversion base: one raw tick multiplies the raw pool price
# (token-one per token-zero) by exactly this ratio, and the human USDC-per-
# stock price is the raw price times 10**(stock_decimals - quote_decimals)
# when the stock is token0 and its reciprocal otherwise.
RAW_TICK_ORIENTATION = Decimal(1)


def human_price_from_raw_tick(
    raw_tick: int,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Convert one raw pool tick into the human USDC-per-stock price.

    The raw pool price is token-one raw units per token-zero raw unit and
    equals 1.0001**tick; the human price applies the decimal scales and the
    orientation flip exactly as ``history.price_usdc_per_stock`` does, so a
    raw tick and a raw sqrt ratio always agree on the same human price.

    Args:
        raw_tick: The pool's raw tick index.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The exact USDC price of one whole stock token at the raw tick.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        scale = Decimal(10) ** (Decimal(stock_decimals) - Decimal(quote_decimals))
        raw_price = TICK_PRICE_RATIO**raw_tick
        return +(raw_price * scale if stock_is_token0 else scale / raw_price)


def raw_tick_for_human_price(
    human_price: Decimal,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
) -> int:
    """Convert one human USDC-per-stock price into its raw pool tick.

    Args:
        human_price: Positive USDC price of one whole stock token.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The raw tick whose pool price matches the human price, floored.
    """
    if human_price <= 0:
        raise ValueError("human_price must be positive")
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        scale = Decimal(10) ** (Decimal(stock_decimals) - Decimal(quote_decimals))
        raw_price = human_price / scale if stock_is_token0 else scale / human_price
        log_tick = raw_price.ln() / TICK_PRICE_RATIO.ln()
        return int(log_tick.to_integral_value(rounding="ROUND_FLOOR"))


# The newest measured point may sit at most this many seconds before the
# observation instant: a window's span alone is no proof of recency, so a
# trailing path whose newest evidence is older - a delayed read, a stale
# cache, a replay threaded against the wrong instant - defers new entries
# and voluntary recenters instead of solving on it (the independent
# reviewer's stale-evidence probe, 2026-10-03).
MAX_EVIDENCE_AGE_SECONDS = 1_800
# The shared live/replay SUFFICIENCY contract: a solve may only score a
# width on measured evidence covering at least this many observed points
# over at least this much wall-clock time. Two points can compute a number
# but cannot measure a regime - one jump would masquerade as the pool's
# volatility and one interval as its dwell - so both bounds gate the solve
# identically in production reads and replay folds (the reviewer's evidence-
# sufficiency ruling, 2026-10-03).
MIN_EVIDENCE_POINTS = 10
MIN_EVIDENCE_WINDOW_SECONDS = 1_800


class RangingObservations(BaseModel):
    """Collect every live observable one adaptive width solve consumes.

    The observables mirror what the bounded production reader and the
    rehearsal harness reconstruct and what the policy engine observes: the
    conservative emissions income basis in Aerodrome's display convention,
    the gauge's staked liquidity and the current-cell staked value behind
    the displayed APR at one instant, the active-liquidity depth and fee
    evidence, realized volatility plus the bounded trailing price path from
    the pool's own Swap logs, the capped position size the entry gates
    would commit, and the live gas price behind every batch cost.
    """

    # Frozen strict fields keep one solve on a single coherent observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The pool price in USDC per one whole stock token at the solving instant.
    pool_price_usdc: Annotated[Decimal, Field(gt=0)]
    # Emissions income basis in Aerodrome's display convention; the caller
    # passes the conservative income expectation when one is carried, so a
    # transient spike never narrows the range the position must live in.
    emissions_apr: NonNegativeDecimal
    # Gauge staked liquidity in the pool's raw liquidity units at the same
    # instant the staked value below was observed.
    gauge_liquidity_raw: Annotated[int, Field(ge=0)]
    # Staked value in USDC at that same instant, under the display APR's
    # current-cell convention (the APR's own denominator).
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
    # too thin to estimate it, which defers the solve.
    realized_daily_volatility: NonNegativeDecimal | None = None
    # The bounded trailing price path from the pool's own Swap logs, the
    # measured-dwell basis for candidate uptime; empty means no measured
    # dwell is available and the solve defers (the renewal-model uptime is
    # a labeled modeled fallback only when a caller explicitly allows it).
    trailing_path: tuple[PoolPricePoint, ...] = ()
    # The observation instant the evidence is judged against: the newest
    # measured point's age is measured from here, so stale, delayed, or
    # time-skewed evidence defers rather than solves.
    observed_at: datetime
    # Decimal counts of the stock and USDC tokens scaling raw liquidity.
    stock_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    quote_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    # The snapshot's raw pool tick anchoring the executable grid: the
    # executor mints raw ticks, so the scored bounds must sit on this exact
    # grid; None defers (no verbatim geometry can be derived).
    pool_tick_raw: int | None = None
    # The pool's token orientation flipping the raw price against the human
    # USDC-per-stock price; None defers with the raw tick.
    stock_is_token0: bool | None = None
    # The capped position size in USDC the entry gates would commit.
    position_size_usd: Annotated[Decimal, Field(gt=0)]
    # The current Base L2 gas price in gwei behind every batch cost; zero is
    # a valid injected reading that makes every batch cost exactly zero.
    gas_price_gwei: Annotated[Decimal, Field(ge=0)]
    # Target net daily yield per deployed dollar, REPORTED ONLY: selection
    # is argmax of modeled net, never satisficing against this target.
    target_net_daily_yield: Annotated[Decimal, Field(gt=0)] = DEFAULT_TARGET_NET_DAILY_YIELD
    # The adaptive band's per-side floor and ceiling: every aligned bound's
    # REAL price distance from the current price must fall inside the band.
    min_range_half_width_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = (
        DEFAULT_MIN_RANGE_HALF_WIDTH_FRACTION
    )
    max_range_half_width_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = (
        DEFAULT_MAX_RANGE_HALF_WIDTH_FRACTION
    )
    # The pool tick grid spacing; aligned bounds are its multiples.
    tick_spacing: Annotated[int, Field(ge=1)] = 10
    # The downside stop sits this fraction below the aligned lower edge.
    stop_buffer_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = DEFAULT_STOP_BUFFER_FRACTION
    # The engine's upside wait before a re-mint, in seconds.
    recenter_wait_seconds: Annotated[int, Field(ge=0)] = DEFAULT_RECENTER_WAIT_SECONDS
    # The out-of-range grace window in seconds; the modeled upside downtime
    # charges the production acting window min(grace, wait), correcting the
    # retired model drift that charged the full wait alone.
    out_of_range_grace_seconds: Annotated[int, Field(ge=0)] = DEFAULT_OUT_OF_RANGE_GRACE_SECONDS
    # The engine's re-entry cooldown after a stop exit, in seconds.
    reentry_cooldown_seconds: Annotated[int, Field(ge=0)] = DEFAULT_REENTRY_COOLDOWN_SECONDS
    # The shared live/replay evidence-sufficiency floor: a solve consumes a
    # measured window only when it covers at least this many points.
    min_evidence_points: Annotated[int, Field(ge=2)] = MIN_EVIDENCE_POINTS
    # ...and at least this much wall-clock span, in seconds.
    min_evidence_window_seconds: Annotated[int, Field(ge=1)] = MIN_EVIDENCE_WINDOW_SECONDS
    # ...and whose newest point is at most this many seconds old at the
    # observation instant; a span alone is no proof of recency.
    max_evidence_age_seconds: Annotated[int, Field(ge=1)] = MAX_EVIDENCE_AGE_SECONDS
    # Batch gas estimates mirroring the engine's locked values.
    enter_batch_gas_units: Annotated[int, Field(ge=1)] = DEFAULT_ENTER_BATCH_GAS_UNITS
    recenter_batch_gas_units: Annotated[int, Field(ge=1)] = DEFAULT_RECENTER_BATCH_GAS_UNITS
    exit_batch_gas_units: Annotated[int, Field(ge=1)] = DEFAULT_EXIT_BATCH_GAS_UNITS
    safe_overhead_gas_per_batch: Annotated[int, Field(ge=0)] = DEFAULT_SAFE_OVERHEAD_GAS_PER_BATCH
    # The documented ETH price assumption behind every gas cost.
    eth_price_assumption_usd: Annotated[Decimal, Field(gt=0)] = DEFAULT_ETH_PRICE_ASSUMPTION_USD

    @model_validator(mode="after")
    def require_coherent_band(self) -> Self:
        """Reject an inverted or degenerate per-side band."""
        if self.min_range_half_width_fraction >= self.max_range_half_width_fraction:
            raise ValueError(
                "min_range_half_width_fraction must be below max_range_half_width_fraction"
            )
        return self


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
    # Staked value in USDC at that same instant, under the display APR's
    # current-cell convention (the APR's own denominator).
    staked_tvl_usd: Annotated[Decimal, Field(ge=0)]
    # Active in-range pool liquidity in raw units, the fee-share denominator.
    active_liquidity_raw: Annotated[int, Field(ge=0)]
    # How many seconds of reconstructed swaps the fee evidence covers.
    fee_window_seconds: Annotated[int, Field(ge=0)]
    # Total swapped notional in USDC over that fee evidence window.
    fee_window_notional_usd: NonNegativeDecimal
    # The pool's staked fee tier in parts per million.
    pool_fee_ppm: Annotated[int, Field(ge=0)]
    # Realized daily volatility of the pool price; None means the path was
    # too thin to estimate it, which defers the solve.
    realized_daily_volatility: NonNegativeDecimal | None = None
    # The bounded trailing price path from the pool's own Swap logs, the
    # measured-dwell basis for candidate uptime and churn calibration.
    trailing_path: tuple[PoolPricePoint, ...] = ()
    # Decimal counts of the stock and USDC tokens scaling raw liquidity.
    stock_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    quote_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    # The snapshot's raw pool tick anchoring the executable grid (see
    # RangingObservations.pool_tick_raw).
    pool_tick_raw: int | None = None
    # The pool's token orientation (see RangingObservations.stock_is_token0).
    stock_is_token0: bool | None = None


class WidthSolveMode(StrEnum):
    """Identify how one width solve resolved."""

    # The argmax candidate was picked with strictly positive modeled net.
    SOLVED = "solved"
    # Every feasible executable candidate nets at or below zero; the engine
    # holds cash and reports the evidence rather than entering on a raw
    # APR that clears the floor.
    CASH_HOLD = "cash_hold"
    # The ranging evidence is missing, stale, or too thin to score; the
    # engine defers new entries and voluntary recenters with the reason.
    DEFERRED = "deferred"
    # The v1 fixed-ceiling baseline posture (rehearsal comparison runs and
    # any engine explicitly locked to it); never the adaptive default.
    FALLBACK_CEILING = "fallback_ceiling"


class AlignedBound(BaseModel):
    """Carry one grid-aligned bound with its REAL price distance from spot."""

    # Frozen strict fields keep the bound exactly as it would be minted.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The bound's tick index, a multiple of the pool's tick spacing.
    tick: int
    # The bound's exact pool price in USDC per stock, 1.0001**tick.
    price: Decimal
    # The bound's REAL fractional price distance from the solving price:
    # 1 - price/spot below spot, price/spot - 1 above it.
    distance_fraction: Annotated[Decimal, Field(gt=0)]


class ExecutableRangeEvaluation(BaseModel):
    """Record every modeled quantity behind one executable aligned range."""

    # Frozen strict fields keep each candidate's evidence inseparable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The aligned lower and upper bounds exactly as they would be minted.
    lower_bound: AlignedBound
    upper_bound: AlignedBound
    # The total span's fractional price distance across the whole range.
    total_span_fraction: Annotated[Decimal, Field(gt=0)]
    # The position's human liquidity per deployed dollar at the entry price.
    position_liquidity_per_dollar: Annotated[Decimal, Field(gt=0)]
    # The position's added raw liquidity entering the gauge denominator -
    # the own-stake dilution input the dollar-value fraction replaced.
    position_liquidity_raw_added: Annotated[int, Field(gt=0)]
    # Gross emissions yield per deployed dollar per day after our own
    # dilution of the gauge liquidity denominator.
    gross_emissions_yield_per_day: NonNegativeDecimal
    # Gross swap-fee yield per deployed dollar per day after our own
    # dilution of the active-liquidity denominator.
    fee_yield_per_day: NonNegativeDecimal
    # Fraction of each day the position is open and in range.
    uptime_fraction: Annotated[Decimal, Field(ge=0, le=1)]
    # Whether uptime came from the measured trailing dwell or the renewal
    # model's construction, so every surface can label its evidence class.
    uptime_measured: bool
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


class WidthSolution(BaseModel):
    """Emit one deterministic adaptive width solve with its complete evidence."""

    # Frozen strict fields keep the solve exactly as the audit will record it.
    model_config = IMMUTABLE_MODEL_CONFIG

    # How the solve resolved: solved, cash hold, deferred, or the
    # fixed-ceiling baseline.
    mode: WidthSolveMode
    # The chosen aligned bounds; None unless the solve picked a range.
    lower_bound: AlignedBound | None = None
    upper_bound: AlignedBound | None = None
    # The pool tick grid spacing the bounds align to.
    tick_spacing: Annotated[int, Field(ge=1)]
    # The reported (never satisficing) target net daily yield.
    target_net_daily_yield: Annotated[Decimal, Field(gt=0)]
    # The half width implied by the pool's average staked concentration,
    # carried as a diagnostic; None when the inputs cannot imply one.
    implied_average_half_width_fraction: Decimal | None = None
    # Every evaluated executable candidate, in ascending span order.
    evaluations: tuple[ExecutableRangeEvaluation, ...] = ()
    # Human-readable evidence lines covering inputs, intermediates, the
    # grid geometry, and the chosen range, ready for decision diagnostics.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_bounds_come_in_pairs(self) -> Self:
        """Reject a chosen range carrying only one of its two bounds."""
        if (self.lower_bound is None) != (self.upper_bound is None):
            raise ValueError("chosen bounds must appear together or not at all")
        if self.mode is WidthSolveMode.SOLVED and self.lower_bound is None:
            raise ValueError("a solved range must carry its aligned bounds")
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
    return realized_volatility_from_points(tuple(path.points))


def realized_volatility_from_points(
    points: tuple[PoolPricePoint, ...],
) -> Decimal | None:
    """Estimate realized daily volatility from one trailing point window.

    Args:
        points: The bounded trailing price points, oldest first.

    Returns:
        The realized daily volatility as a decimal fraction, or None when
        the window holds fewer than two points or spans no positive time.
    """
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


def position_liquidity_at_price(
    entry_price: Decimal,
    lower_price: Decimal,
    upper_price: Decimal,
) -> Decimal:
    """Calculate liquidity per deployed dollar entering a range at a price.

    The exact v3-style composition rule: at the entry price the position
    holds stock worth L*(1/sqrt(P) - 1/sqrt(P_u))*P and USDC worth
    L*(sqrt(P) - sqrt(P_l)), so the liquidity one deployed dollar buys is
    the reciprocal of that value per unit liquidity. Entering off-center
    buys a different liquidity than the centered ideal, which is exactly the
    executable-geometry quantity the adaptive solve scores.

    Args:
        entry_price: Positive pool price in USDC per stock at entry.
        lower_price: The aligned lower bound price, strictly below entry.
        upper_price: The aligned upper bound price, strictly above entry.

    Returns:
        Human-unit liquidity per deployed US dollar at this geometry.

    Raises:
        ValueError: If any price is not positive or the entry price does not
            sit strictly inside the range.
    """
    if entry_price <= 0 or lower_price <= 0 or upper_price <= 0:
        raise ValueError("prices must be positive")
    if not lower_price < entry_price < upper_price:
        raise ValueError("entry_price must sit strictly inside the range")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic liquidity math from settings.
        decimal_context.prec = MATH_PRECISION
        sqrt_price = entry_price.sqrt()
        value_per_liquidity = (
            Decimal(1) / sqrt_price - Decimal(1) / upper_price.sqrt()
        ) * entry_price + (sqrt_price - lower_price.sqrt())
        return +(Decimal(1) / value_per_liquidity)


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


def _range_value_fraction_at_price(
    entry_price: Decimal,
    lower_price: Decimal,
    upper_price: Decimal,
    evaluated_price: Decimal,
) -> Decimal:
    """Mark one executable geometry at a price as a fraction of entry value.

    The composition rule generalizes the centered form to an asymmetric
    range entered at an arbitrary inside price: liquidity follows from the
    committed value at the entry price, and the evaluated price marks the
    stock and USDC legs exactly.

    Args:
        entry_price: Positive pool price the position was entered at.
        lower_price: The range's aligned lower bound price.
        upper_price: The range's aligned upper bound price.
        evaluated_price: Positive pool price to mark at.

    Returns:
        The position's value at the evaluated price divided by its value at
        entry, both under the exact composition rule.

    Raises:
        ValueError: If any price is not positive or the entry price does not
            sit strictly inside the range.
    """
    if evaluated_price <= 0:
        raise ValueError("evaluated_price must be positive")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic composition math from settings.
        decimal_context.prec = MATH_PRECISION
        entry_sqrt = entry_price.sqrt()
        lower_sqrt = lower_price.sqrt()
        upper_sqrt = upper_price.sqrt()

        def value_at(sqrt_p: Decimal) -> Decimal:
            return (Decimal(1) / sqrt_p - Decimal(1) / upper_sqrt) * sqrt_p ** Decimal(2) + (
                sqrt_p - lower_sqrt
            )

        return +(value_at(evaluated_price.sqrt()) / value_at(entry_sqrt))


def enumerate_aligned_bounds(
    pool_price_usdc: Decimal,
    tick_spacing: int,
    min_distance_fraction: Decimal,
    max_distance_fraction: Decimal,
    pool_tick_raw: int,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
) -> tuple[tuple[AlignedBound, ...], tuple[AlignedBound, ...]]:
    """Enumerate the raw-grid bounds whose real human distances sit in the band.

    The executable grid is the set of spacing-multiple RAW pool ticks - the
    exact ticks the executor mints, anchored at the raw current tick's
    floored cell. For each raw side this walks outward and keeps exactly the
    bounds whose REAL human-price distance from the solving price falls
    inside the per-side band - never a centered ideal rounded outward, and
    never a bound whose realized distance leaves the band. Because the raw
    price inverts against the human price when the stock is token1, a raw
    "lower" bound can carry the higher human price; each bound reports its
    own real human-side distance, and the two sides' feasible sets are
    generally asymmetric by grid phase. The scored bounds are the minted
    bounds, verbatim, on the raw grid.

    Args:
        pool_price_usdc: Positive pool price in USDC per stock at the same
            snapshot as the raw tick.
        tick_spacing: The pool's tick grid spacing.
        min_distance_fraction: The band's per-side floor.
        max_distance_fraction: The band's per-side ceiling.
        pool_tick_raw: The snapshot's raw current pool tick.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The (raw-lower, raw-upper) feasible bound tuples, each ordered by
        raw distance from the anchor cell (nearest first).

    Raises:
        ValueError: If the price is not positive, the spacing is not
            positive, or the band is inverted.
    """
    if pool_price_usdc <= 0:
        raise ValueError("pool_price_usdc must be positive")
    if tick_spacing <= 0:
        raise ValueError("tick_spacing must be positive")
    if min_distance_fraction <= 0 or max_distance_fraction <= min_distance_fraction:
        raise ValueError("the per-side distance band must be positive and non-inverted")
    with localcontext() as decimal_context:
        # Local precision isolates the deterministic logarithm from settings.
        decimal_context.prec = MATH_PRECISION
        anchor = (pool_tick_raw // tick_spacing) * tick_spacing
        spacing_step = TICK_PRICE_RATIO**tick_spacing - Decimal(1)
        # Walk outward over a generous multiple of the widest band: the band
        # ceiling over one spacing's price step bounds the walk, plus two
        # for grid phase and the orientation flip.
        max_steps = (
            int((max_distance_fraction / spacing_step).to_integral_value(rounding="ROUND_CEILING"))
            + 2
        )
        raw_lower: list[AlignedBound] = []
        raw_upper: list[AlignedBound] = []
        for step in range(1, min(max_steps, MAX_ALIGNED_BOUNDS_PER_SIDE) + 1):
            for side, bounds_list in (
                (-1, raw_lower),
                (1, raw_upper),
            ):
                raw_tick = int(anchor + side * step * tick_spacing)
                human_price = human_price_from_raw_tick(
                    raw_tick, stock_is_token0, stock_decimals, quote_decimals
                )
                if human_price <= pool_price_usdc:
                    distance = Decimal(1) - human_price / pool_price_usdc
                else:
                    distance = human_price / pool_price_usdc - Decimal(1)
                if min_distance_fraction <= distance <= max_distance_fraction:
                    bounds_list.append(
                        AlignedBound(
                            tick=raw_tick,
                            price=+human_price,
                            distance_fraction=+distance,
                        )
                    )
        return tuple(raw_lower), tuple(raw_upper)


def band_dwell_fraction(
    points: tuple[PoolPricePoint, ...],
    lower_distance_fraction: Decimal,
    upper_distance_fraction: Decimal,
) -> Decimal | None:
    """Measure the trailing persistence of one band geometry.

    The band is anchored at the trailing window's FIRST price - the
    measured convention the review's bounded reader established: of the
    last window, how much time did the price spend inside a band sitting
    these fractional distances below and above where it started. That is
    the empirical answer to "how long does a range of this geometry stay
    in range" for the current regime, and it stays measurable regardless
    of where the price sits now, so a fresh move never zeroes every
    candidate's uptime the way a now-anchored band would. The dwell is
    time-weighted over consecutive observed points with the left-point
    convention: each interval counts as inside when its opening price sits
    inside the band.

    Args:
        points: The bounded trailing price points, oldest first.
        lower_distance_fraction: The band's fractional distance below the
            anchor price.
        upper_distance_fraction: The band's fractional distance above the
            anchor price.

    Returns:
        The fraction of observed time spent inside the anchored band, or
        None when the window holds fewer than two points or spans no
        positive time.
    """
    if len(points) < MIN_DWELL_POINTS:
        return None
    span_micros = (points[-1].timestamp - points[0].timestamp) // timedelta(microseconds=1)
    if span_micros <= 0:
        return None
    with localcontext() as decimal_context:
        # Local precision isolates deterministic dwell math from settings.
        decimal_context.prec = MATH_PRECISION
        anchor = points[0].price_usdc
        lower_price = anchor * (Decimal(1) - lower_distance_fraction)
        upper_price = anchor * (Decimal(1) + upper_distance_fraction)
        inside_micros = 0
        for earlier, later in zip(points, points[1:], strict=False):
            interval_micros = (later.timestamp - earlier.timestamp) // timedelta(microseconds=1)
            if lower_price <= earlier.price_usdc <= upper_price:
                inside_micros += interval_micros
        return +(Decimal(inside_micros) / Decimal(span_micros))


def _deferred_solution(
    observations: RangingObservations,
    reason: str,
) -> WidthSolution:
    """Build the deferral solution for missing or unusable evidence.

    Args:
        observations: The solve inputs whose usability failed.
        reason: The fail-closed reason naming the unusable input.

    Returns:
        The deferred solution carrying no range and the explicit reason.
    """
    return WidthSolution(
        mode=WidthSolveMode.DEFERRED,
        tick_spacing=observations.tick_spacing,
        target_net_daily_yield=observations.target_net_daily_yield,
        evaluations=(),
        diagnostics=_input_diagnostics(observations)
        + (
            f"Range evidence is unusable - {reason}; deferring the entry or voluntary "
            "recenter with no substituted constant range (safety exits stay armed).",
        ),
    )


def ceiling_width_solution(
    pool_price_usdc: Decimal,
    tick_spacing: int,
    max_range_half_width_fraction: Decimal,
    target_net_daily_yield: Decimal,
    reason: str,
    prefix_diagnostics: tuple[str, ...] = (),
) -> WidthSolution:
    """Build the fixed-ceiling baseline solution for the v1 comparison posture.

    This is the locked comparison baseline (the rehearsal's
    ``--fixed-ceiling-width`` runs and engines explicitly pinned to it),
    never the adaptive default: the adaptive solve defers on missing
    evidence instead of substituting the ceiling.

    Args:
        pool_price_usdc: Positive pool price in USDC per stock.
        tick_spacing: The pool tick grid spacing.
        max_range_half_width_fraction: The locked ceiling half width.
        target_net_daily_yield: The target the solve would have aimed for.
        reason: The label naming why the ceiling was selected.
        prefix_diagnostics: Optional evidence lines echoed before the
            baseline line.

    Returns:
        The ceiling-width solution with the baseline label and evidence.

    Raises:
        ValueError: If the pool price is not positive.
    """
    if pool_price_usdc <= 0:
        raise ValueError("pool_price_usdc must be positive")
    with localcontext() as decimal_context:
        # Local precision isolates the deterministic logarithm from settings.
        decimal_context.prec = MATH_PRECISION
        tick_log = TICK_PRICE_RATIO.ln()
        raw_lower_tick = (
            pool_price_usdc * (Decimal(1) - max_range_half_width_fraction)
        ).ln() / tick_log
        raw_upper_tick = (
            pool_price_usdc * (Decimal(1) + max_range_half_width_fraction)
        ).ln() / tick_log
        # The v1 baseline's outward alignment: floor the lower and ceil the
        # upper bound onto the grid, the historical executor behavior this
        # posture reproduces for comparison runs.
        spacing = Decimal(tick_spacing)
        lower_tick = int(
            (raw_lower_tick / spacing).to_integral_value(rounding="ROUND_FLOOR") * spacing
        )
        upper_tick = int(
            (raw_upper_tick / spacing).to_integral_value(rounding="ROUND_CEILING") * spacing
        )
        lower_price = TICK_PRICE_RATIO**lower_tick
        upper_price = TICK_PRICE_RATIO**upper_tick
        lower_distance = Decimal(1) - lower_price / pool_price_usdc
        upper_distance = upper_price / pool_price_usdc - Decimal(1)
        total_span = upper_price / lower_price - Decimal(1)
        return WidthSolution(
            mode=WidthSolveMode.FALLBACK_CEILING,
            lower_bound=AlignedBound(
                tick=lower_tick,
                price=+lower_price,
                distance_fraction=+lower_distance,
            ),
            upper_bound=AlignedBound(
                tick=upper_tick,
                price=+upper_price,
                distance_fraction=+upper_distance,
            ),
            tick_spacing=tick_spacing,
            target_net_daily_yield=target_net_daily_yield,
            evaluations=(),
            diagnostics=prefix_diagnostics
            + (
                f"Fixed-ceiling baseline range [{lower_tick},{upper_tick}] "
                f"({lower_price}..{upper_price} USDC, real per-side distances "
                f"{_format_bps(lower_distance)}bp and {_format_bps(upper_distance)}bp, "
                f"total span {_format_bps(total_span)}bp); {reason}.",
            ),
        )


def _format_optional_decimal(value: Decimal | None) -> str:
    """Format one optional decimal for a diagnostics line.

    Args:
        value: The decimal to format, or None.

    Returns:
        The decimal's string form, or the unavailable label.
    """
    return "unavailable" if value is None else str(value)


def _format_bps(fraction: Decimal) -> str:
    """Format one fractional distance in readable basis points.

    Args:
        fraction: The fractional price distance.

    Returns:
        The distance in basis points, quantized to one decimal place.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        return str(+(fraction * BPS_SCALE).quantize(Decimal("0.1")))


def _input_diagnostics(observations: RangingObservations) -> tuple[str, ...]:
    """Echo every solve input as one evidence line per fact.

    Args:
        observations: The validated solve inputs.

    Returns:
        The diagnostic lines enumerating the inputs with their class
        labels (measured versus assumed or configured).
    """
    points = observations.trailing_path
    span_text = "empty"
    if len(points) >= 2:
        newest_age = (observations.observed_at - points[-1].timestamp).total_seconds()
        span_text = (
            f"{len(points)} points over "
            f"{(points[-1].timestamp - points[0].timestamp).total_seconds():.0f} seconds "
            f"whose newest point is {newest_age:.0f} seconds old at the observation "
            f"instant, against the bounds of at least "
            f"{observations.min_evidence_points} points over at least "
            f"{observations.min_evidence_window_seconds} seconds and at most "
            f"{observations.max_evidence_age_seconds} seconds of age"
        )
    return (
        f"Pool price {observations.pool_price_usdc} USDC per stock.",
        f"Emissions income basis {observations.emissions_apr} in Aerodrome's display "
        "convention (the conservative trailing median when one is carried).",
        f"Gauge staked liquidity {observations.gauge_liquidity_raw} raw units over "
        f"current-cell staked value {observations.staked_tvl_usd} USDC (measured).",
        f"Active in-range liquidity {observations.active_liquidity_raw} raw units; "
        f"executable depth {observations.pool_depth_usd} USDC across the "
        "plus-or-minus one-percent band (measured).",
        f"Fee evidence: notional {observations.fee_window_notional_usd} USDC over "
        f"{observations.fee_window_seconds} seconds at {observations.pool_fee_ppm} ppm "
        "(measured).",
        f"Realized daily volatility "
        f"{_format_optional_decimal(observations.realized_daily_volatility)} (measured "
        "over the trailing window); measured trailing path " + span_text + ".",
        f"Capped position size {observations.position_size_usd} USDC at gas "
        f"{observations.gas_price_gwei} gwei and the documented ETH "
        f"{observations.eth_price_assumption_usd} USDC assumption.",
        f"Adaptive per-side band {observations.min_range_half_width_fraction} to "
        f"{observations.max_range_half_width_fraction} on REAL aligned-bound distances "
        f"(the captain's 0.1-to-0.3-percent direction); grid spacing "
        f"{observations.tick_spacing} ticks; reported target "
        f"{observations.target_net_daily_yield} per deployed dollar per day - a "
        "reported target, never a satisficing rule or a reason to pick a negative "
        "candidate.",
    )


def solve_range_width(observations: RangingObservations) -> WidthSolution:
    """Solve the adaptive executable range with the strongest conservative net.

    The solve enumerates the actual grid-aligned bounds whose real per-side
    distances fall inside the band, scores that executable geometry exactly
    as it would be minted - own-stake dilution on the post-stake liquidity
    denominators, measured trailing dwell as the uptime input, the renewal
    model over the realized volatility for churn and stop frequencies - and
    selects the argmax of modeled net yield per deployed dollar per day.
    A nonpositive best candidate resolves CASH_HOLD with the evidence, and
    missing, stale, or thin evidence resolves DEFERRED with the reason.

    Args:
        observations: The validated live observables for one solve.

    Returns:
        The immutable width solution with every candidate's evidence.
    """
    # Unusable inputs defer before any modeling; each failure names itself.
    if observations.realized_daily_volatility is None:
        return _deferred_solution(observations, "realized volatility is unavailable")
    if len(observations.trailing_path) < MIN_DWELL_POINTS:
        return _deferred_solution(observations, "the trailing measured path is empty")
    if (
        observations.trailing_path[-1].timestamp - observations.trailing_path[0].timestamp
    ) <= timedelta(0):
        return _deferred_solution(observations, "the trailing measured path spans no time")
    # The shared sufficiency contract: thin windows compute numbers but do
    # not measure a regime, so both the point count and the time span gate
    # the solve identically in production reads and replay folds.
    evidence_span_seconds = int(
        (
            observations.trailing_path[-1].timestamp - observations.trailing_path[0].timestamp
        ).total_seconds()
    )
    if (
        len(observations.trailing_path) < observations.min_evidence_points
        or evidence_span_seconds < observations.min_evidence_window_seconds
    ):
        return _deferred_solution(
            observations,
            f"measured coverage is insufficient - {len(observations.trailing_path)} points "
            f"over {evidence_span_seconds} seconds against the bound of at least "
            f"{observations.min_evidence_points} points over at least "
            f"{observations.min_evidence_window_seconds} seconds",
        )
    # Recency: the span alone proves nothing about freshness, so the newest
    # measured point's age is judged against the observation instant. A
    # newest point in the future is time-skewed or corrupt evidence, and a
    # newest point older than the bound is stale; both defer rather than
    # solve (the independent reviewer's stale-evidence probe).
    newest_age_seconds = (
        observations.observed_at - observations.trailing_path[-1].timestamp
    ).total_seconds()
    if newest_age_seconds < 0:
        return _deferred_solution(
            observations,
            f"the newest measured point sits {-newest_age_seconds:.0f} seconds AFTER the "
            "observation instant - the evidence is time-skewed or corrupt",
        )
    if newest_age_seconds > observations.max_evidence_age_seconds:
        return _deferred_solution(
            observations,
            f"the newest measured point is {newest_age_seconds:.0f} seconds old against "
            f"the {observations.max_evidence_age_seconds}-second recency bound - the "
            "evidence is stale",
        )
    if observations.gauge_liquidity_raw <= 0 or observations.staked_tvl_usd <= 0:
        return _deferred_solution(observations, "gauge staked liquidity or staked value is missing")
    if observations.active_liquidity_raw <= 0 or observations.pool_depth_usd <= 0:
        return _deferred_solution(observations, "active liquidity or executable depth is missing")
    if observations.fee_window_notional_usd > 0 and observations.fee_window_seconds <= 0:
        return _deferred_solution(
            observations, "fee evidence carries notional over an empty window"
        )
    # The verbatim-geometry anchor: without the snapshot's raw tick and the
    # pool's token orientation no scored bound can be guaranteed to mint
    # unchanged, and an incoherent raw tick (one whose human price disagrees
    # with the observed price by more than a raw tick) is corrupt evidence.
    if observations.pool_tick_raw is None or observations.stock_is_token0 is None:
        return _deferred_solution(
            observations,
            "the snapshot's raw pool tick or token orientation is missing - no "
            "verbatim executable geometry can be derived",
        )
    assert observations.pool_tick_raw is not None  # noqa: S101 - narrowed above
    assert observations.stock_is_token0 is not None  # noqa: S101 - narrowed above
    tick_human_price = human_price_from_raw_tick(
        observations.pool_tick_raw,
        observations.stock_is_token0,
        observations.stock_decimals,
        observations.quote_decimals,
    )
    if abs(tick_human_price / observations.pool_price_usdc - Decimal(1)) > TICK_PRICE_RATIO - (
        Decimal(1)
    ):
        return _deferred_solution(
            observations,
            "the raw pool tick's human price disagrees with the observed pool price "
            "beyond one tick - the snapshot evidence is incoherent",
        )

    volatility = observations.realized_daily_volatility
    position_size = observations.position_size_usd
    with localcontext() as decimal_context:
        # Local precision keeps every modeled quantity deterministic.
        decimal_context.prec = MATH_PRECISION
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
        # Raw-liquidity scale converting human liquidity into gauge units.
        human_scale = Decimal(10) ** (
            (Decimal(observations.stock_decimals) + Decimal(observations.quote_decimals))
            / Decimal(2)
        )
        # The production acting window behind the modeled upside downtime:
        # min(grace, recenter wait), never the retired full wait alone.
        acting_window_seconds = min(
            observations.recenter_wait_seconds, observations.out_of_range_grace_seconds
        )

        lower_bounds, upper_bounds = enumerate_aligned_bounds(
            observations.pool_price_usdc,
            observations.tick_spacing,
            observations.min_range_half_width_fraction,
            observations.max_range_half_width_fraction,
            observations.pool_tick_raw,
            observations.stock_is_token0,
            observations.stock_decimals,
            observations.quote_decimals,
        )
        if not lower_bounds or not upper_bounds:
            return _deferred_solution(
                observations,
                "no grid-aligned bound's real distance falls inside the per-side band "
                "at this grid phase (the band cannot be minted on this spacing)",
            )

        def bounds_text(bounds: tuple[AlignedBound, ...]) -> str:
            return ", ".join(
                f"{bound.tick} at {_format_bps(bound.distance_fraction)}bp" for bound in bounds
            )

        grid_lines = (
            f"Executable grid: {len(lower_bounds)} feasible lower bound(s) "
            f"({bounds_text(lower_bounds)}) "
            f"and {len(upper_bounds)} upper bound(s) "
            f"({bounds_text(upper_bounds)}) "
            "inside the band; every feasible aligned pairing is scored - the sides' "
            "distances differ by the price's grid phase and the argmax keeps its "
            "choice among all of them - and the scored object is the minted object: "
            "no centered ideal is rounded outward, and any bound whose realized "
            "distance leaves the band is excluded as infeasible.",
        )

        evaluations: list[ExecutableRangeEvaluation] = []
        for lower in lower_bounds:
            for upper in upper_bounds:
                dwell = band_dwell_fraction(
                    observations.trailing_path,
                    lower.distance_fraction,
                    upper.distance_fraction,
                )
                # Dwell is structurally measurable here: the path checks
                # above guarantee at least two points over positive time.
                if dwell is None:  # pragma: no cover - guarded by the checks above
                    continue
                # The human-ordered band edges: under token1 orientation the
                # raw-lower bound carries the HIGHER human price, so every
                # price-space consumer (liquidity, dwell, composition, the
                # stop) reads the ordered pair while the minted bounds stay
                # raw-ordered.
                band_low = min(lower.price, upper.price)
                band_high = max(lower.price, upper.price)
                total_span = band_high / band_low - Decimal(1)
                liquidity_per_dollar = position_liquidity_at_price(
                    observations.pool_price_usdc, band_low, band_high
                )
                position_liquidity_raw = int(
                    (liquidity_per_dollar * position_size * human_scale).to_integral_value(
                        rounding="ROUND_FLOOR"
                    )
                )
                if position_liquidity_raw <= 0:
                    continue
                # Gross emissions: the position's share of the gauge reward
                # stream valued per deployed dollar per day, over the
                # post-own-stake liquidity denominator (a candidate more
                # concentrated than the book dilutes itself more than a
                # dollar-value fraction predicts).
                emissions_share = Decimal(position_liquidity_raw) / Decimal(
                    observations.gauge_liquidity_raw + position_liquidity_raw
                )
                gross_emissions = (
                    observations.emissions_apr
                    / DAYS_PER_YEAR
                    * observations.staked_tvl_usd
                    * emissions_share
                    / position_size
                )
                # Gross fees: the position's share of the observed daily fee
                # stream over the active-liquidity denominator after our
                # stake, per deployed dollar.
                fee_share = Decimal(position_liquidity_raw) / Decimal(
                    observations.active_liquidity_raw + position_liquidity_raw
                )
                fee_yield = (
                    fee_stream_per_day * fee_share / position_size
                    if fee_stream_per_day > 0
                    else Decimal(0)
                )
                # The two-branch renewal cycle behind every frequency
                # figure, generalized to the asymmetric band: from the entry
                # price at distance a below and b above, the price first
                # exits after a*b/sigma^2, each side with probability
                # proportional to the opposite distance. The upside branch
                # waits out the production acting window and re-mints. The
                # downside branch rides the excursion for the gambler's-ruin
                # expected time a*gap/sigma^2 and then stops with
                # probability a/(a+gap), paying the cooldown, or recovers
                # back into the range for free. A flat realized volatility
                # means the price never leaves the band at all.
                a = Decimal(1) - band_low / observations.pool_price_usdc
                b = band_high / observations.pool_price_usdc - Decimal(1)
                stop_gap = observations.stop_buffer_fraction
                if volatility > 0:
                    exit_days = a * b / volatility ** Decimal(2)
                    upside_probability = a / (a + b)
                    excursion_days = a * stop_gap / volatility ** Decimal(2)
                    stop_probability = a / (a + stop_gap)
                    wait_days = Decimal(acting_window_seconds) / Decimal(SECONDS_PER_DAY)
                    cooldown_days = Decimal(observations.reentry_cooldown_seconds) / Decimal(
                        SECONDS_PER_DAY
                    )
                    upside_days = wait_days
                    downside_days = excursion_days + stop_probability * cooldown_days
                    cycle_days = exit_days + (
                        upside_probability * upside_days
                        + (Decimal(1) - upside_probability) * downside_days
                    )
                    recenter_rate = upside_probability / cycle_days
                    stop_rate = (Decimal(1) - upside_probability) * stop_probability / cycle_days
                else:
                    recenter_rate = Decimal(0)
                    stop_rate = Decimal(0)
                # Recenter cost: gas plus rebalance impact per upside exit.
                recenter_cost = recenter_rate * (recenter_gas + recenter_impact) / position_size
                # Stop cost: the exact composition loss at the stop level -
                # the stop sits the buffer fraction below the ALIGNED lower
                # edge, exactly where the engine places it - plus the exit
                # and re-entry batches and the exit-swap impact.
                stop_level = band_low * (Decimal(1) - stop_gap)
                stop_loss_fraction = Decimal(1) - _range_value_fraction_at_price(
                    observations.pool_price_usdc,
                    band_low,
                    band_high,
                    stop_level,
                )
                stop_cost = stop_rate * (
                    stop_loss_fraction + (exit_gas + enter_gas + stop_impact) / position_size
                )
                net_yield = (gross_emissions + fee_yield) * dwell - recenter_cost - stop_cost
                evaluations.append(
                    ExecutableRangeEvaluation(
                        lower_bound=lower,
                        upper_bound=upper,
                        total_span_fraction=+total_span,
                        position_liquidity_per_dollar=+liquidity_per_dollar,
                        position_liquidity_raw_added=position_liquidity_raw,
                        gross_emissions_yield_per_day=+gross_emissions,
                        fee_yield_per_day=+fee_yield,
                        uptime_fraction=+dwell,
                        uptime_measured=True,
                        recenter_rate_per_day=+recenter_rate,
                        recenter_cost_per_day=+recenter_cost,
                        stop_rate_per_day=+stop_rate,
                        stop_cost_per_day=+stop_cost,
                        net_yield_per_day=+net_yield,
                    )
                )
        if not evaluations:
            return _deferred_solution(
                observations,
                "every feasible geometry's added liquidity floors to zero at the "
                "capped position size",
            )
        evaluations.sort(key=lambda row: (row.total_span_fraction, row.lower_bound.tick))

        input_lines = _input_diagnostics(observations) + grid_lines
        # Selection is argmax of modeled net; the first maximum wins in
        # ascending-span order, so ties prefer the tighter executable range.
        best = max(evaluations, key=lambda row: row.net_yield_per_day)
        if best.net_yield_per_day <= 0:
            nets = ", ".join(
                f"[{row.lower_bound.tick},{row.upper_bound.tick}] net {row.net_yield_per_day}"
                for row in evaluations
            )
            return WidthSolution(
                mode=WidthSolveMode.CASH_HOLD,
                tick_spacing=observations.tick_spacing,
                target_net_daily_yield=observations.target_net_daily_yield,
                implied_average_half_width_fraction=implied_width,
                evaluations=tuple(evaluations),
                diagnostics=input_lines
                + (
                    f"Every executable candidate nets nonpositive ({nets}); holding "
                    "cash - a raw APR above the floor never picks a negative candidate.",
                ),
            )
        chosen_lines = (
            f"Selected [{best.lower_bound.tick},{best.upper_bound.tick}] as the argmax "
            "of modeled net: lower "
            f"{best.lower_bound.price} at "
            f"{_format_bps(best.lower_bound.distance_fraction)}bp and upper "
            f"{best.upper_bound.price} at {_format_bps(best.upper_bound.distance_fraction)}bp "
            f"real per-side distances (total span {_format_bps(best.total_span_fraction)}bp, "
            f"{best.upper_bound.tick - best.lower_bound.tick} ticks): gross emissions "
            f"{best.gross_emissions_yield_per_day} plus fees {best.fee_yield_per_day} per "
            f"deployed dollar per day at measured dwell {best.uptime_fraction} (measured), "
            f"minus recenter cost {best.recenter_cost_per_day} at "
            f"{best.recenter_rate_per_day} recenters per day and stop cost "
            f"{best.stop_cost_per_day} at {best.stop_rate_per_day} stops per day, nets "
            f"{best.net_yield_per_day} per deployed dollar per day against the reported "
            f"target {observations.target_net_daily_yield}.",
        )
        return WidthSolution(
            mode=WidthSolveMode.SOLVED,
            lower_bound=best.lower_bound,
            upper_bound=best.upper_bound,
            tick_spacing=observations.tick_spacing,
            target_net_daily_yield=observations.target_net_daily_yield,
            implied_average_half_width_fraction=implied_width,
            evaluations=tuple(evaluations),
            diagnostics=input_lines + chosen_lines,
        )
