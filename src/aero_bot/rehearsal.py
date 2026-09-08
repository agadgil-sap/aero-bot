"""Deterministic rehearsal replay of the locked policy over reconstructed history.

The rehearsal folds the pure policy engine over one pool's reconstructed
onchain histories - the swap-driven price path and the gauge emissions-APR
series - and emits a per-pool profit-and-loss ledger. Every economic input is
injected, so the replay itself performs no I/O and unit tests drive it
entirely from synthetic paths.

Because the underlying reference market cannot be reconstructed keylessly
minute-by-minute for a whole window, the reference path defaults to the
documented assumption that the AMM equaled the real market at every swap, and
a clearly labeled synthetic schedule may overlay bounded stale-high,
stale-low, and stale-feed episodes so the dislocation monitor's exits, holds,
and convergence-timeout sells appear in the ledger. Every accrual and cost is
a documented approximation, labeled on the ledger itself through its
assumption-label list.
"""

from bisect import bisect_right
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, NonNegativeDecimal
from aero_bot.history import (
    AERO_DECIMALS,
    EmissionsAprHistory,
    EmissionsAprPoint,
    PoolPricePath,
    PoolPricePoint,
)
from aero_bot.policy import (
    LOCKED_POLICY_PARAMETERS,
    STARTING_EQUITY_USDC,
    PolicyActionKind,
    PolicyEngine,
    PolicyObservation,
    PolicyPosition,
    PolicyReason,
    PolicyState,
    SwapPlan,
    load_event_calendar,
)
from aero_bot.ranging import (
    MICROSECONDS_PER_SECOND,
    SECONDS_PER_DAY,
    RangingEvidence,
    WidthSolveMode,
)

# High internal precision keeps replay accounting deterministic across platforms.
MATH_PRECISION = 60
# Pool depth is measured as the value of the active liquidity spanning a one
# percent band on each side of the current price, the standard executable
# depth-at-one-percent notion the position gate and impact model share.
DEPTH_BAND_HALF_WIDTH_FRACTION = Decimal("0.01")
# The conservative default AERO price assumption matches the history module's.
DEFAULT_AERO_PRICE_ASSUMPTION_USD = Decimal("0.50")
# Base's quiet base fee sits around one milli-gwei; the gas gate still defers
# entries at higher prices and those deferrals appear in the ledger.
DEFAULT_GAS_PRICE_ASSUMPTION_GWEI = Decimal("0.001")
# Fee tiers are expressed in parts per million of the swapped notional.
PPM_SCALE = Decimal(1_000_000)
# Basis points scale impact fractions for the per-swap ledger view.
BPS_SCALE = Decimal(10_000)
# Live ranging evidence spans the trailing day of reconstructed swaps, so the
# width solver sees the pool's recent fee flow and realized volatility rather
# than figures averaged over the whole replay window.
RANGING_EVIDENCE_WINDOW = timedelta(hours=24)


class WidthSelectionMode(StrEnum):
    """Select how every entry and recenter range width is chosen in a replay."""

    # Widths derive from the target net daily yield solver over live evidence.
    DERIVED_FROM_TARGET = "derived_from_target"
    # Widths sit at the locked ceiling, the v1 fixed-width policy baseline.
    FIXED_CEILING_BASELINE = "fixed_ceiling_baseline"


class ReferenceQuote(BaseModel):
    """Represent one reference-market quote in USDC per stock for the replay."""

    # Frozen strict fields keep each quote exactly as the replay consumed it.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The instant this quote was current.
    timestamp: datetime
    # The real-market price in USDC per one whole stock token.
    price_usdc: Annotated[Decimal, Field(gt=0)]

    @model_validator(mode="after")
    def require_aware_timestamp(self) -> Self:
        """Reject naive timestamps so reference ages stay absolute."""
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


class SyntheticEpisodeKind(StrEnum):
    """Identify the three synthetic reference-path stress scenarios."""

    # The reference is held below the AMM, the stale-high dislocation case.
    STALE_HIGH = "stale_high"
    # The reference is held above the AMM, the stale-low dislocation case.
    STALE_LOW = "stale_low"
    # Reference quotes are absent entirely, the stale-feed case.
    STALE_FEED = "stale_feed"


class SyntheticEpisode(BaseModel):
    """Describe one bounded synthetic distortion of the reference path."""

    # Frozen strict fields keep the schedule exactly as documented.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Kind selects which dislocation scenario the episode injects.
    kind: SyntheticEpisodeKind
    # Start fraction locates the episode start as a fraction of the replay
    # window measured from its first observation to its last.
    start_fraction: Annotated[Decimal, Field(ge=0, lt=1)]
    # Duration bounds how long the distortion lasts.
    duration: timedelta
    # Magnitude is the reference's fractional distance from the AMM price.
    magnitude_fraction: Annotated[Decimal, Field(gt=0, lt=1)] = Decimal("0.005")

    @model_validator(mode="after")
    def require_positive_duration(self) -> Self:
        """Reject non-positive durations so every episode is a real interval."""
        if self.duration <= timedelta(0):
            raise ValueError("duration must be positive")
        return self


class SyntheticDislocationSchedule(BaseModel):
    """Collect the synthetic episodes one replay overlays on the reference path."""

    # Frozen strict fields keep the whole schedule immutable and reviewable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Episodes may be empty, which selects the plain AMM-equals-reference mode.
    episodes: tuple[SyntheticEpisode, ...] = ()


# The documented default stress schedule injects one bounded episode of each
# dislocation scenario across the replay window so the monitor's actions are
# exercised on live data; the ledger labels the overlay explicitly.
DEFAULT_SYNTHETIC_DISLOCATION_SCHEDULE = SyntheticDislocationSchedule(
    episodes=(
        # A two-minute stale-high spell: the reference market drops half a
        # percent below the AMM, so an open position burns and sells while the
        # AMM still prices the stock above the real market.
        SyntheticEpisode(
            kind=SyntheticEpisodeKind.STALE_HIGH,
            start_fraction=Decimal("0.30"),
            duration=timedelta(minutes=2),
        ),
        # A two-minute stale-low spell inside the five-minute convergence
        # timeout: an open position burns, holds the tokens, and sells on
        # convergence once the episode ends.
        SyntheticEpisode(
            kind=SyntheticEpisodeKind.STALE_LOW,
            start_fraction=Decimal("0.50"),
            duration=timedelta(minutes=2),
        ),
        # An eight-minute stale-low spell outlasting the convergence timeout:
        # an open position burns, holds the tokens, and sells at market.
        SyntheticEpisode(
            kind=SyntheticEpisodeKind.STALE_LOW,
            start_fraction=Decimal("0.70"),
            duration=timedelta(minutes=8),
        ),
        # A twenty-minute reference outage longer than the open-position
        # bound: an open position exits defensively and flat sessions defer.
        SyntheticEpisode(
            kind=SyntheticEpisodeKind.STALE_FEED,
            start_fraction=Decimal("0.85"),
            duration=timedelta(minutes=20),
        ),
    )
)


class RehearsalAssumptions(BaseModel):
    """Collect every documented configurable assumption behind one replay."""

    # Frozen strict fields keep one replay's assumptions stable and reviewable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The AERO price assumption values accrued emissions and must equal the
    # one the reconstructed APR series was built under.
    aero_price_assumption_usd: Annotated[Decimal, Field(gt=0)] = DEFAULT_AERO_PRICE_ASSUMPTION_USD
    # The constant Base gas price assumption drives every batch cost estimate.
    gas_price_assumption_gwei: Annotated[Decimal, Field(gt=0)] = DEFAULT_GAS_PRICE_ASSUMPTION_GWEI
    # Starting equity seeds the replay's books and the engine's day snapshot.
    starting_equity_usd: Annotated[Decimal, Field(gt=0)] = STARTING_EQUITY_USDC
    # The pool's staked fee tier in parts per million feeds fee accrual.
    pool_fee_ppm: Annotated[int, Field(ge=0)]


class RehearsalActionRecord(BaseModel):
    """Record one replayed policy action with its full economic evidence."""

    # Frozen strict fields preserve the action exactly as the ledger emits it.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The observation instant the action was decided at.
    timestamp: datetime
    # The action the engine chose.
    action: PolicyActionKind
    # The stable reason the engine reported.
    reason: PolicyReason
    # Diagnostics carry the engine's numeric evidence verbatim.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]
    # Committed size is present for enter actions.
    size_usd: NonNegativeDecimal | None = None
    # The aligned range prices are present for enter and recenter actions.
    range_lower_price: NonNegativeDecimal | None = None
    range_upper_price: NonNegativeDecimal | None = None
    # Swap evidence summarizes the modeled execution; a present total with an
    # absent impact means the depth was zero and the impact is unmodeled.
    swap_total_usd: NonNegativeDecimal | None = None
    swap_tranche_count: int | None = None
    swap_max_impact_bps: NonNegativeDecimal | None = None
    swap_impact_cost_usd: NonNegativeDecimal = Decimal(0)
    # Batch gas evidence mirrors the decision's estimates.
    estimated_gas_units: int | None = None
    estimated_gas_cost_usd: NonNegativeDecimal | None = None
    # Width-solve evidence for enter and recenter actions: how the half width
    # resolved and which tick-aligned width was committed.
    width_mode: WidthSolveMode | None = None
    half_width_ticks: Annotated[int, Field(ge=1)] | None = None
    half_width_fraction: Decimal | None = None
    # Replay books after applying the action, for auditing the fold.
    cash_after_usd: Decimal
    equity_after_usd: Decimal

    @model_validator(mode="after")
    def require_aware_timestamp(self) -> Self:
        """Reject naive timestamps so the ledger timeline stays absolute."""
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


class RehearsalActionCounts(BaseModel):
    """Summarize how often each policy action appeared in one replay."""

    # Frozen strict fields keep the counts inseparable from the ledger.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Entries minted and staked a new position.
    entries: Annotated[int, Field(ge=0)] = 0
    # Recenters rebuilt the range after the upside wait elapsed.
    recenters: Annotated[int, Field(ge=0)] = 0
    # Stop-outs exited at the downside stop level.
    stop_outs: Annotated[int, Field(ge=0)] = 0
    # Dilution exits closed below the emissions threshold while open.
    dilution_exits: Annotated[int, Field(ge=0)] = 0
    # Event exits closed for scheduled or condition-driven flat windows.
    event_exits: Annotated[int, Field(ge=0)] = 0
    # Dislocation exits sold into a stale-high AMM.
    dislocation_exits: Annotated[int, Field(ge=0)] = 0
    # Stale-low burns closed the position and held the stock tokens.
    stale_low_burns: Annotated[int, Field(ge=0)] = 0
    # Defensive exits closed on a reference stale beyond the open bound.
    defensive_exits: Annotated[int, Field(ge=0)] = 0
    # Held inventory sold on AMM convergence to the reference.
    sell_inventory_convergence: Annotated[int, Field(ge=0)] = 0
    # Held inventory sold at market when the convergence timeout hit.
    sell_inventory_timeout: Annotated[int, Field(ge=0)] = 0
    # Held inventory sold because a flat window required USDC.
    sell_inventory_flat_window: Annotated[int, Field(ge=0)] = 0
    # Non-urgent actions deferred by the gas sense-check gate.
    gas_deferrals: Annotated[int, Field(ge=0)] = 0


def _count_actions(actions: tuple[RehearsalActionRecord, ...]) -> RehearsalActionCounts:
    """Derive the action counts from one recorded action list.

    Args:
        actions: The recorded non-hold actions plus gas-gated holds.

    Returns:
        The counts the ledger carries, derived so they cannot drift.
    """
    return RehearsalActionCounts(
        entries=sum(1 for a in actions if a.action == PolicyActionKind.ENTER),
        recenters=sum(1 for a in actions if a.action == PolicyActionKind.RECENTER),
        stop_outs=sum(1 for a in actions if a.action == PolicyActionKind.STOP_OUT),
        dilution_exits=sum(1 for a in actions if a.action == PolicyActionKind.DILUTION_EXIT),
        event_exits=sum(1 for a in actions if a.action == PolicyActionKind.EVENT_EXIT),
        dislocation_exits=sum(1 for a in actions if a.action == PolicyActionKind.DISLOCATION_EXIT),
        stale_low_burns=sum(1 for a in actions if a.action == PolicyActionKind.STALE_LOW_BURN),
        defensive_exits=sum(1 for a in actions if a.action == PolicyActionKind.DEFENSIVE_EXIT),
        sell_inventory_convergence=sum(
            1
            for a in actions
            if a.action == PolicyActionKind.SELL_INVENTORY
            and a.reason == PolicyReason.INVENTORY_CONVERGENCE_REACHED
        ),
        sell_inventory_timeout=sum(
            1
            for a in actions
            if a.action == PolicyActionKind.SELL_INVENTORY
            and a.reason == PolicyReason.INVENTORY_CONVERGENCE_TIMEOUT
        ),
        sell_inventory_flat_window=sum(
            1
            for a in actions
            if a.action == PolicyActionKind.SELL_INVENTORY
            and a.reason == PolicyReason.INVENTORY_FLAT_WINDOW_SELL
        ),
        gas_deferrals=sum(
            1
            for a in actions
            if a.action == PolicyActionKind.HOLD and a.reason == PolicyReason.GAS_GATE_DEFERRED
        ),
    )


class PoolRehearsalLedger(BaseModel):
    """Emit one pool's deterministic rehearsal profit-and-loss ledger."""

    # Frozen strict fields keep the ledger immutable evidence of the replay.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The Slipstream pool the replay covered.
    pool_address: EvmAddress
    # The B20 stock token paired with native USDC in that pool.
    token_address: EvmAddress
    # The pool symbol as discovered, carried for readable output.
    symbol: str
    # The instant the replay was performed, for provenance only.
    replayed_at: datetime
    # The replay window's first and last observation instants.
    window_start: datetime | None = None
    window_end: datetime | None = None
    # The emissions series' reconstruction mode, labeled on the ledger.
    emissions_reconstruction_mode: Literal["event_fold", "constant_anchor_apr"]
    # The reference path mode the replay consumed.
    reference_mode: Literal["amm_equals_reference", "synthetic_stress"]
    # How every entry and recenter range width was chosen in this replay.
    width_selection: WidthSelectionMode = WidthSelectionMode.DERIVED_FROM_TARGET
    # The assumptions the replay ran under, verbatim.
    assumptions: RehearsalAssumptions
    # Human-readable labels for every documented approximation in force.
    assumption_labels: Annotated[tuple[str, ...], Field(min_length=1)]
    # How many reconstructed swap observations the fold consumed.
    observation_count: Annotated[int, Field(ge=0)]
    # Equity accounting across the whole window.
    starting_equity_usd: Annotated[Decimal, Field(gt=0)]
    final_equity_usd: Decimal
    final_cash_usd: Decimal
    final_open_position_committed_usd: NonNegativeDecimal | None = None
    final_held_stock_quantity: NonNegativeDecimal = Decimal(0)
    final_held_stock_value_usd: NonNegativeDecimal = Decimal(0)
    pnl_usd: Decimal
    return_fraction: Decimal
    # Exposure time in seconds while a position was open or in range.
    time_open_seconds: Annotated[int, Field(ge=0)]
    time_in_range_seconds: Annotated[int, Field(ge=0)]
    # Fee accrual under the pro-rata active-liquidity approximation.
    fees_accrued_usd: NonNegativeDecimal = Decimal(0)
    # AERO emissions accrued under the in-range staked-share approximation.
    aero_accrued_units: NonNegativeDecimal = Decimal(0)
    aero_accrued_usd: NonNegativeDecimal = Decimal(0)
    # Execution drag under the tranche impact model.
    total_swap_impact_cost_usd: NonNegativeDecimal = Decimal(0)
    max_swap_impact_bps: NonNegativeDecimal = Decimal(0)
    unmodeled_swap_impact: bool = False
    # Gas drag under the documented constant assumptions.
    total_gas_cost_usd: NonNegativeDecimal = Decimal(0)
    # Per-action-kind totals cross-checked against the action list.
    action_counts: RehearsalActionCounts
    # Every non-hold action in order, plus gas-gated holds, with evidence.
    actions: tuple[RehearsalActionRecord, ...]

    @model_validator(mode="after")
    def require_counts_match_actions(self) -> Self:
        """Reject ledgers whose action counts disagree with the action list."""
        if _count_actions(self.actions) != self.action_counts:
            raise ValueError("action_counts must match the actions list")
        return self


def build_reference_quotes(
    path: PoolPricePath,
    schedule: SyntheticDislocationSchedule | None = None,
) -> tuple[ReferenceQuote, ...]:
    """Build the reference path one replay consumes from the AMM price path.

    Without episodes the reference equals the AMM price at every observation,
    the documented AMM-equals-reference assumption. Each scheduled episode
    distorts or drops the quotes inside its bounded interval, which is the
    clearly labeled synthetic approximation that exercises the dislocation
    monitor on live data.

    Args:
        path: The pool's reconstructed swap-driven price path.
        schedule: Synthetic episodes to overlay; None means no episodes.

    Returns:
        The ordered reference quotes, one per non-dropped price point.

    Raises:
        ValueError: If the path is empty with a non-empty schedule, or two
            episodes overlap inside the window.
    """
    episodes = schedule.episodes if schedule is not None else ()
    points = path.points
    if not points:
        if episodes:
            raise ValueError("a synthetic schedule requires a non-empty price path")
        return ()
    # The window span anchors every episode's absolute start instant; whole
    # microseconds keep fraction arithmetic exact where float seconds drift.
    window_start = points[0].timestamp
    window_span_micros = Decimal(
        (points[-1].timestamp - points[0].timestamp) // timedelta(microseconds=1)
    )
    spans = [
        (
            window_start + timedelta(microseconds=int(episode.start_fraction * window_span_micros)),
            episode,
        )
        for episode in episodes
    ]
    ordered_starts = sorted(spans, key=lambda item: item[0])
    # The pairwise zip is intentionally one element shorter on the right.
    for (earlier_start, earlier), (later_start, _) in zip(
        ordered_starts, ordered_starts[1:], strict=False
    ):
        if later_start < earlier_start + earlier.duration:
            raise ValueError(f"synthetic episodes must not overlap around {later_start}")
    quotes: list[ReferenceQuote] = []
    for point in points:
        active = next(
            (
                episode
                for start, episode in spans
                if start <= point.timestamp < start + episode.duration
            ),
            None,
        )
        if active is None:
            quotes.append(ReferenceQuote(timestamp=point.timestamp, price_usdc=point.price_usdc))
        elif active.kind == SyntheticEpisodeKind.STALE_FEED:
            # The feed outage drops the quote entirely; the replay then reads
            # the previous quote with its growing age instead.
            continue
        elif active.kind == SyntheticEpisodeKind.STALE_HIGH:
            quotes.append(
                ReferenceQuote(
                    timestamp=point.timestamp,
                    price_usdc=point.price_usdc * (Decimal(1) - active.magnitude_fraction),
                )
            )
        else:
            quotes.append(
                ReferenceQuote(
                    timestamp=point.timestamp,
                    price_usdc=point.price_usdc * (Decimal(1) + active.magnitude_fraction),
                )
            )
    return tuple(quotes)


def band_depth_usd(
    active_liquidity_raw: int,
    price_usdc: Decimal,
    half_width_fraction: Decimal,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Value the pool's active liquidity across a symmetric price band.

    The value of liquidity L across a band is L times the difference of the
    band's square-root prices, so scaling the raw liquidity into human terms
    and spanning the band around the current price yields the executable
    depth the position gate and impact model share.

    Args:
        active_liquidity_raw: Raw active pool liquidity from the swap event.
        price_usdc: Positive pool price in USDC per stock.
        half_width_fraction: Fractional band half width on each side.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The executable US-dollar depth across the band.

    Raises:
        ValueError: If the price or half width is not positive.
    """
    if price_usdc <= 0:
        raise ValueError("price_usdc must be positive")
    if half_width_fraction <= 0:
        raise ValueError("half_width_fraction must be positive")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic depth math from settings.
        decimal_context.prec = MATH_PRECISION
        # Human liquidity divides raw liquidity by ten to the half the
        # combined decimal count of the pair.
        human_scale = Decimal(10) ** (
            (Decimal(stock_decimals) + Decimal(quote_decimals)) / Decimal(2)
        )
        human_liquidity = Decimal(active_liquidity_raw) / human_scale
        sqrt_price = price_usdc.sqrt()
        sqrt_lower = (price_usdc * (Decimal(1) - half_width_fraction)).sqrt()
        sqrt_upper = (price_usdc * (Decimal(1) + half_width_fraction)).sqrt()
        # Value spans the quote side across the band.
        return +(human_liquidity * (Decimal(2) * sqrt_price - sqrt_lower - sqrt_upper))


def swap_usd_notional(
    point: PoolPricePoint,
    token_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Value one swap's size in US dollars at the post-swap price.

    Both emitted token deltas are valued at the post-swap price and the
    larger side wins, a documented proxy for the swapped notional that stays
    correct for either direction and either token ordering.

    Args:
        point: The reconstructed swap observation with its signed amounts.
        token_is_token0: True when the stock token sorts before the quote token.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The approximate US-dollar notional of the swap.
    """
    stock_value_raw = abs(point.amount0 if token_is_token0 else point.amount1)
    quote_value_raw = abs(point.amount1 if token_is_token0 else point.amount0)
    stock_value = Decimal(stock_value_raw) / Decimal(10) ** stock_decimals * point.price_usdc
    quote_value = Decimal(quote_value_raw) / Decimal(10) ** quote_decimals
    return max(stock_value, quote_value)


def trailing_ranging_evidence(
    points: Sequence[PoolPricePoint],
    squared_log_returns: Sequence[Decimal],
    notionals_usd: Sequence[Decimal],
    window_start: int,
    index: int,
    gauge_liquidity_raw: int,
    anchor_gauge_liquidity: int,
    anchor_staked_tvl_usd: Decimal,
    pool_fee_ppm: int,
    stock_decimals: int,
    quote_decimals: int,
) -> RangingEvidence:
    """Assemble the live ranging observables as of one replay observation.

    Fee evidence and realized volatility span the trailing evidence window
    ending at the observation, carried with the window's exact observed span.
    The realized volatility equals the ranging module's estimator applied to
    the retained sub-path bit for bit, because the squared returns are summed
    in observation order inside the same precision context. Staked value
    follows the history module's frozen per-liquidity-unit anchor convention,
    so the solve's concentration ratio matches the emissions series the replay
    already consumes.

    Args:
        points: The replay's whole ordered price path.
        squared_log_returns: Squared log return between consecutive points,
            one entry per consecutive pair in observation order.
        notionals_usd: Each point's swapped notional in USDC.
        window_start: First retained point index, the earliest observation
            inside the trailing evidence window.
        index: The observation the evidence is assembled for.
        gauge_liquidity_raw: Gauge staked liquidity in effect at this instant.
        anchor_gauge_liquidity: Staked liquidity at the anchor block.
        anchor_staked_tvl_usd: Staked value in USDC at the anchor block.
        pool_fee_ppm: The pool's staked fee tier in parts per million.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The ranging evidence the observation carries for the width solve.

    Raises:
        ValueError: If the window bounds or per-point arrays disagree with
            the path, or the anchor liquidity is not positive.
    """
    if len(squared_log_returns) != max(len(points) - 1, 0):
        raise ValueError("squared_log_returns must carry one entry per consecutive pair")
    if len(notionals_usd) != len(points):
        raise ValueError("notionals_usd must carry one entry per point")
    if not 0 <= window_start <= index < len(points):
        raise ValueError("window bounds must address an ordered observation prefix")
    if anchor_gauge_liquidity <= 0:
        raise ValueError("anchor_gauge_liquidity must be positive")
    current = points[index]
    window_seconds = int((current.timestamp - points[window_start].timestamp).total_seconds())
    realized_volatility: Decimal | None = None
    with localcontext() as decimal_context:
        # Local precision keeps the evidence assembly deterministic.
        decimal_context.prec = MATH_PRECISION
        if index > window_start:
            # The retained sub-path's consecutive pairs are exactly the
            # squared-return slice, summed in order like the ranging module's
            # whole-path estimator.
            squared_return_sum = sum(squared_log_returns[window_start:index], start=Decimal(0))
            span_micros = (current.timestamp - points[window_start].timestamp) // timedelta(
                microseconds=1
            )
            span_days = Decimal(span_micros) / MICROSECONDS_PER_SECOND / Decimal(SECONDS_PER_DAY)
            if span_days > 0:
                realized_volatility = +(squared_return_sum / span_days).sqrt()
            # A zero-second span (two observations inside one block) carries no
            # time basis, so the volatility stays None and the evidence is
            # honestly incomplete rather than divided by zero.
        # Staked value scales linearly with staked liquidity at the frozen
        # per-unit anchor value, the emissions series' documented convention.
        staked_tvl_usd = (
            anchor_staked_tvl_usd * Decimal(gauge_liquidity_raw) / Decimal(anchor_gauge_liquidity)
        )
        fee_notional = sum(notionals_usd[window_start : index + 1], start=Decimal(0))
    return RangingEvidence(
        gauge_liquidity_raw=gauge_liquidity_raw,
        staked_tvl_usd=staked_tvl_usd,
        active_liquidity_raw=current.liquidity,
        fee_window_seconds=window_seconds,
        fee_window_notional_usd=fee_notional,
        pool_fee_ppm=pool_fee_ppm,
        realized_daily_volatility=realized_volatility,
        stock_decimals=stock_decimals,
        quote_decimals=quote_decimals,
    )


def _position_liquidity_raw(
    position: PolicyPosition,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Express one position's liquidity in the pool's raw liquidity units.

    The engine mints every range at its geometric center, so liquidity is the
    committed value over twice the center-to-lower square-root distance, and
    the human result scales back into the raw units gauge and swap liquidity
    share so emissions and fee shares divide raw by raw.

    Args:
        position: Open position with its aligned range and committed value.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The position's liquidity in the same units as onchain liquidity.
    """
    with localcontext() as decimal_context:
        # Local precision isolates deterministic composition math from settings.
        decimal_context.prec = MATH_PRECISION
        sqrt_lower = position.price_range.lower_price.sqrt()
        sqrt_upper = position.price_range.upper_price.sqrt()
        sqrt_center = (sqrt_lower * sqrt_upper).sqrt()
        human_liquidity = position.committed_usd / (Decimal(2) * (sqrt_center - sqrt_lower))
        human_scale = Decimal(10) ** (
            (Decimal(stock_decimals) + Decimal(quote_decimals)) / Decimal(2)
        )
        return +(human_liquidity * human_scale)


def _swap_plan_impact(plan: SwapPlan | None) -> tuple[Decimal, Decimal | None, bool]:
    """Summarize one modeled swap plan's cost and worst tranche impact.

    The linear impact model degrades the average execution price by half the
    tranche's end impact, which is the documented cost charged to the replay's
    books; a tranche without a modeled impact marks the plan unmodeled.

    Args:
        plan: The decision's swap plan, or None when no swap executes.

    Returns:
        The impact cost in USDC, the worst tranche impact in basis points, and
        whether any tranche executed against unmodeled (zero) depth.
    """
    if plan is None:
        return Decimal(0), None, False
    cost = Decimal(0)
    worst_fraction: Decimal | None = None
    unmodeled = False
    for tranche in plan.tranches:
        if tranche.modeled_impact_fraction is None:
            unmodeled = True
            continue
        with localcontext() as decimal_context:
            # Local precision keeps repeated tranche additions exact.
            decimal_context.prec = MATH_PRECISION
            cost += tranche.usd_size * tranche.modeled_impact_fraction / Decimal(2)
        if worst_fraction is None or tranche.modeled_impact_fraction > worst_fraction:
            worst_fraction = tranche.modeled_impact_fraction
    worst_bps = None if worst_fraction is None else worst_fraction * BPS_SCALE
    return +cost, worst_bps, unmodeled


def _step_as_of(steps: tuple[EmissionsAprPoint, ...], at: datetime) -> EmissionsAprPoint:
    """Return the emissions step in effect at one instant.

    Steps are stepwise constants, so the latest step at or before the instant
    applies; before the first reconstructed step the first step's level is
    held, a clamped approximation documented on the ledger.

    Args:
        steps: The ordered reconstructed emissions steps.
        at: The observation instant being resolved.

    Returns:
        The step whose liquidity and APR govern the instant.
    """
    timestamps = [step.timestamp for step in steps]
    return steps[max(bisect_right(timestamps, at) - 1, 0)]


def _position_value(
    engine: PolicyEngine,
    position: PolicyPosition,
    price_usdc: Decimal,
) -> Decimal:
    """Mark one open position at the current price through engine math.

    Args:
        engine: The replay's engine, whose composition rule is authoritative.
        position: The open position being marked.
        price_usdc: The current pool price in USDC per stock.

    Returns:
        The position's marked value at the current price.
    """
    stock_quantity, usdc_quantity = engine.position_composition(position, price_usdc)
    return stock_quantity * price_usdc + usdc_quantity


def replay_pool(
    price_path: PoolPricePath,
    emissions_history: EmissionsAprHistory,
    symbol: str,
    assumptions: RehearsalAssumptions,
    schedule: SyntheticDislocationSchedule | None = None,
    width_selection: WidthSelectionMode = WidthSelectionMode.DERIVED_FROM_TARGET,
    engine: PolicyEngine | None = None,
) -> PoolRehearsalLedger:
    """Replay the locked policy over one pool's reconstructed histories.

    The fold consumes every reconstructed swap as one observation, marks the
    books at each price, and applies each decision exactly as the engine
    emitted it. Between observations, AERO emissions accrue to the open
    in-range position pro rata to gauge staked liquidity, and each observed
    swap credits fees pro rata to active pool liquidity while the position is
    open and in range at the post-swap price.

    Derived-width replays attach live ranging evidence to every observation,
    so each entry and recenter solves its tick-aligned half width against the
    target net daily yield; the observation opening the window carries no
    volatility evidence yet and fails toward the labeled ceiling. Baseline
    replays attach no evidence, reproducing the v1 fixed ceiling-width policy.

    Args:
        price_path: The pool's reconstructed swap-driven price path.
        emissions_history: The pool's reconstructed emissions-APR series.
        symbol: The pool symbol as discovered, carried for readable output.
        assumptions: The documented configurable assumptions for this replay.
        schedule: Optional synthetic dislocation overlay for the reference.
        width_selection: Whether widths derive from the target-yield solver
            or sit at the locked ceiling as the v1 baseline.
        engine: Optional engine override; defaults to the locked parameters
            with the bundled operator event calendar.

    Returns:
        The deterministic per-pool profit-and-loss ledger.

    Raises:
        ValueError: If the two histories describe different pools, the AERO
            price assumption disagrees with the APR series' assumption, or a
            synthetic schedule meets an empty price path.
    """
    if price_path.pool_address != emissions_history.pool_address:
        raise ValueError("price path and emissions history must describe one pool")
    if assumptions.aero_price_assumption_usd != emissions_history.aero_price_assumption_usd:
        raise ValueError("aero_price_assumption_usd must equal the emissions series' assumption")
    # The default engine pins the locked v1 parameters and the bundled calendar.
    active_engine = (
        engine
        if engine is not None
        else PolicyEngine(parameters=LOCKED_POLICY_PARAMETERS, calendar=load_event_calendar())
    )
    reference_quotes = build_reference_quotes(price_path, schedule)
    quote_timestamps = [quote.timestamp for quote in reference_quotes]
    points = price_path.points
    # Per-point ranging evidence inputs are computed once: each squared log
    # return mirrors the ranging module's estimator exactly, and each swap's
    # notional feeds the trailing fee window.
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        squared_log_returns = tuple(
            (later.price_usdc / earlier.price_usdc).ln() ** Decimal(2)
            # The pairwise zip is intentionally one element shorter on the right.
            for earlier, later in zip(points, points[1:], strict=False)
        )
    swap_notionals_usd = tuple(
        swap_usd_notional(
            point,
            price_path.token_is_token0,
            price_path.token_decimals,
            price_path.quote_decimals,
        )
        for point in points
    )
    # The trailing evidence window's first retained point index only advances.
    evidence_window_start = 0
    # Replay books: USDC cash, accrued aggregates, and the engine's own state.
    cash = assumptions.starting_equity_usd
    state = PolicyState(day_start_equity_usd=assumptions.starting_equity_usd)
    fees_accrued = aero_accrued_units = impact_cost_total = gas_cost_total = Decimal(0)
    max_impact_bps = Decimal(0)
    unmodeled_impact_seen = False
    time_open_seconds = 0
    time_in_range_seconds = 0
    actions: list[RehearsalActionRecord] = []
    quote_pointer = 0
    previous_point: PoolPricePoint | None = None
    previous_step: EmissionsAprPoint | None = None

    for observation_index, point in enumerate(points):
        observed_at = point.timestamp
        price = point.price_usdc
        if previous_point is not None and previous_step is not None:
            # Accruals over (previous, now] use the state held during the
            # interval: the position open after the previous decision, the
            # interval's price level, and the gauge step then in effect.
            elapsed = int((observed_at - previous_point.timestamp).total_seconds())
            position = state.position
            if position is not None:
                time_open_seconds += elapsed
                in_range = (
                    position.price_range.lower_price
                    <= previous_point.price_usdc
                    < position.price_range.upper_price
                )
                if in_range:
                    time_in_range_seconds += elapsed
                    with localcontext() as decimal_context:
                        # Local precision keeps share arithmetic deterministic.
                        decimal_context.prec = MATH_PRECISION
                        share = _position_liquidity_raw(
                            position,
                            price_path.token_decimals,
                            price_path.quote_decimals,
                        ) / Decimal(previous_step.gauge_liquidity)
                        accrued_units = (
                            share
                            * Decimal(emissions_history.emissions_per_second)
                            * Decimal(elapsed)
                            / Decimal(10) ** AERO_DECIMALS
                        )
                    aero_accrued_units += accrued_units
                    # Emissions are credited to cash at the documented price.
                    cash += accrued_units * assumptions.aero_price_assumption_usd
        # The gauge step and reference quote in effect at this observation.
        step = _step_as_of(emissions_history.steps, observed_at)
        ranging_evidence: RangingEvidence | None = None
        if width_selection is WidthSelectionMode.DERIVED_FROM_TARGET:
            # The trailing evidence window opens at the first observation
            # inside the last day, always retaining the current one.
            while (
                evidence_window_start < len(points) - 1
                and points[evidence_window_start].timestamp <= observed_at - RANGING_EVIDENCE_WINDOW
            ):
                evidence_window_start += 1
            ranging_evidence = trailing_ranging_evidence(
                points=points,
                squared_log_returns=squared_log_returns,
                notionals_usd=swap_notionals_usd,
                window_start=evidence_window_start,
                index=observation_index,
                gauge_liquidity_raw=step.gauge_liquidity,
                anchor_gauge_liquidity=emissions_history.anchor_gauge_liquidity,
                anchor_staked_tvl_usd=emissions_history.anchor_staked_tvl_usd,
                pool_fee_ppm=assumptions.pool_fee_ppm,
                stock_decimals=price_path.token_decimals,
                quote_decimals=price_path.quote_decimals,
            )
        while (
            quote_pointer + 1 < len(reference_quotes)
            and quote_timestamps[quote_pointer + 1] <= observed_at
        ):
            quote_pointer += 1
        quote = (
            reference_quotes[quote_pointer]
            if reference_quotes and quote_timestamps[quote_pointer] <= observed_at
            else None
        )
        position_before = state.position
        held_before = state.held_inventory
        position_value = (
            _position_value(active_engine, position_before, price)
            if position_before is not None
            else Decimal(0)
        )
        held_value = held_before.stock_quantity * price if held_before is not None else Decimal(0)
        depth = band_depth_usd(
            point.liquidity,
            price,
            DEPTH_BAND_HALF_WIDTH_FRACTION,
            price_path.token_decimals,
            price_path.quote_decimals,
        )
        observation = PolicyObservation(
            observed_at=observed_at,
            pool_address=price_path.pool_address,
            token_address=price_path.token_address,
            amm_price_usdc=price,
            emissions_apr=step.emissions_apr,
            pool_depth_usd=depth,
            equity_usd=cash + position_value + held_value,
            reference_price_usdc=quote.price_usdc if quote is not None else None,
            reference_age_seconds=(
                int((observed_at - quote.timestamp).total_seconds()) if quote is not None else None
            ),
            gas_price_gwei=assumptions.gas_price_assumption_gwei,
            ranging=ranging_evidence,
        )
        outcome = active_engine.decide(state, observation)
        decision = outcome.decision
        state = outcome.next_state
        impact_cost, worst_bps, unmodeled = _swap_plan_impact(decision.swap_plan)
        unmodeled_impact_seen = unmodeled_impact_seen or unmodeled
        if worst_bps is not None and worst_bps > max_impact_bps:
            max_impact_bps = worst_bps
        impact_cost_total += impact_cost
        if decision.estimated_gas_cost_usd is not None:
            gas_cost_total += decision.estimated_gas_cost_usd

        if decision.action == PolicyActionKind.ENTER:
            if decision.size_usd is None:
                raise ValueError("an enter decision must carry its committed size")
            # The entry commits the sized value and pays the rebalance swap.
            cash -= decision.size_usd + impact_cost
        elif decision.action == PolicyActionKind.RECENTER:
            if position_before is None:
                raise ValueError("a recenter decision requires an open position")
            # Burn the old position at the current price, re-mint the same
            # committed value into the new range, and pay the rebalance swap.
            stock_quantity, usdc_quantity = active_engine.position_composition(
                position_before, price
            )
            cash += stock_quantity * price + usdc_quantity
            cash -= position_before.committed_usd + impact_cost
        elif decision.action in (
            PolicyActionKind.STOP_OUT,
            PolicyActionKind.DILUTION_EXIT,
            PolicyActionKind.EVENT_EXIT,
            PolicyActionKind.DISLOCATION_EXIT,
            PolicyActionKind.DEFENSIVE_EXIT,
        ):
            if position_before is None:
                raise ValueError("an exit decision requires an open position")
            # Every safety exit burns and converts the whole position to USDC.
            stock_quantity, usdc_quantity = active_engine.position_composition(
                position_before, price
            )
            cash += stock_quantity * price + usdc_quantity - impact_cost
        elif decision.action == PolicyActionKind.STALE_LOW_BURN:
            if position_before is None:
                raise ValueError("a stale-low burn requires an open position")
            # The burn holds the stock tokens unsold; only USDC returns.
            _, usdc_quantity = active_engine.position_composition(position_before, price)
            cash += usdc_quantity
        elif decision.action == PolicyActionKind.SELL_INVENTORY:
            if held_before is None:
                raise ValueError("an inventory sell requires held stock tokens")
            # The held inventory converts back to USDC at the current price.
            cash += held_before.stock_quantity * price - impact_cost
        # Plain holds apply no book changes; gas-gated deferrals still surface
        # in the ledger so execution friction stays visible.

        # Books after the action feed both the audit trail and the next mark.
        new_position_value = (
            _position_value(active_engine, state.position, price)
            if state.position is not None
            else Decimal(0)
        )
        new_held_value = (
            state.held_inventory.stock_quantity * price
            if state.held_inventory is not None
            else Decimal(0)
        )
        if decision.action != PolicyActionKind.HOLD or (
            decision.reason == PolicyReason.GAS_GATE_DEFERRED
        ):
            actions.append(
                RehearsalActionRecord(
                    timestamp=observed_at,
                    action=decision.action,
                    reason=decision.reason,
                    diagnostics=decision.diagnostics,
                    size_usd=decision.size_usd,
                    range_lower_price=(
                        decision.price_range.lower_price
                        if decision.price_range is not None
                        else None
                    ),
                    range_upper_price=(
                        decision.price_range.upper_price
                        if decision.price_range is not None
                        else None
                    ),
                    swap_total_usd=(
                        decision.swap_plan.total_usd if decision.swap_plan is not None else None
                    ),
                    swap_tranche_count=(
                        len(decision.swap_plan.tranches) if decision.swap_plan is not None else None
                    ),
                    swap_max_impact_bps=worst_bps,
                    swap_impact_cost_usd=impact_cost,
                    estimated_gas_units=decision.estimated_gas_units,
                    estimated_gas_cost_usd=decision.estimated_gas_cost_usd,
                    width_mode=(
                        decision.width_solution.mode
                        if decision.width_solution is not None
                        else None
                    ),
                    half_width_ticks=(
                        decision.width_solution.half_width_ticks
                        if decision.width_solution is not None
                        else None
                    ),
                    half_width_fraction=(
                        decision.width_solution.half_width_fraction
                        if decision.width_solution is not None
                        else None
                    ),
                    cash_after_usd=cash,
                    equity_after_usd=cash + new_position_value + new_held_value,
                )
            )
        # The swap observed at this instant credits fees to the position open
        # and in range after the decision, pro rata to active pool liquidity.
        position_after = state.position
        if (
            position_after is not None
            and point.liquidity > 0
            and position_after.price_range.lower_price
            <= price
            < position_after.price_range.upper_price
        ):
            with localcontext() as decimal_context:
                # Local precision keeps the fee share deterministic.
                decimal_context.prec = MATH_PRECISION
                liquidity_share = _position_liquidity_raw(
                    position_after,
                    price_path.token_decimals,
                    price_path.quote_decimals,
                ) / Decimal(point.liquidity)
                fee = (
                    swap_usd_notional(
                        point,
                        price_path.token_is_token0,
                        price_path.token_decimals,
                        price_path.quote_decimals,
                    )
                    * Decimal(assumptions.pool_fee_ppm)
                    / PPM_SCALE
                    * liquidity_share
                )
            fees_accrued += fee
            cash += fee
        previous_point = point
        previous_step = step

    # Final marks close the books at the last observed price.
    last_price = price_path.points[-1].price_usdc if price_path.points else Decimal(0)
    final_position_value = (
        _position_value(active_engine, state.position, last_price)
        if state.position is not None
        else Decimal(0)
    )
    final_held_quantity = (
        state.held_inventory.stock_quantity if state.held_inventory is not None else Decimal(0)
    )
    final_held_value = final_held_quantity * last_price
    final_equity = cash + final_position_value + final_held_value
    pnl = final_equity - assumptions.starting_equity_usd
    labels = [
        "the reference path equals the AMM price at every observation unless "
        "a labeled synthetic episode overlays it",
        "fees accrue pro rata to active pool liquidity for each swap while "
        "the position is open and in range at the post-swap price",
        "AERO emissions accrue to the in-range staked liquidity share, held "
        "piecewise constant between observations",
        "pool depth is the active-liquidity value across a plus-or-minus "
        f"{DEPTH_BAND_HALF_WIDTH_FRACTION} price band",
        "swap tranches execute immediately with impact charged at half the modeled end impact",
        f"gas is held constant at {assumptions.gas_price_assumption_gwei} gwei "
        "with ETH at the engine's documented assumption",
        "the historical fee APR is unknown, so the gas gate sees a conservative zero fee APR",
        "the APR before the first reconstructed step is clamped to that step",
        "oracle and registry health are assumed healthy for the whole window",
    ]
    if emissions_history.reconstruction_mode == "constant_anchor_apr":
        labels.append(
            "emissions APR is held at the anchor level for the whole window "
            "(constant-anchor fallback)"
        )
    if width_selection is WidthSelectionMode.DERIVED_FROM_TARGET:
        labels.append(
            "range widths derive from the target net daily yield solver over the "
            "trailing day of fee and volatility evidence, entering at the tightest "
            "tick-aligned width whose modeled net meets the target and at one tick "
            "spacing when the target is unreachable"
        )
        labels.append(
            "ranging evidence spans the trailing day of reconstructed swaps and "
            "values staked liquidity at the frozen per-liquidity-unit anchor value, "
            "matching the emissions series' convention"
        )
    else:
        labels.append(
            "baseline replay: every entry and recenter uses the locked ceiling "
            "width, the v1 fixed-width policy, instead of the target-derived solve"
        )
    if unmodeled_impact_seen:
        labels.append(
            "one or more swaps executed against zero observed depth, so their impact is unmodeled"
        )
    return PoolRehearsalLedger(
        pool_address=price_path.pool_address,
        token_address=price_path.token_address,
        symbol=symbol,
        replayed_at=datetime.now(UTC),
        window_start=price_path.points[0].timestamp if price_path.points else None,
        window_end=price_path.points[-1].timestamp if price_path.points else None,
        emissions_reconstruction_mode=emissions_history.reconstruction_mode,
        reference_mode=(
            "synthetic_stress"
            if schedule is not None and schedule.episodes
            else "amm_equals_reference"
        ),
        width_selection=width_selection,
        assumptions=assumptions,
        assumption_labels=tuple(labels),
        observation_count=len(price_path.points),
        starting_equity_usd=assumptions.starting_equity_usd,
        final_equity_usd=final_equity,
        final_cash_usd=cash,
        final_open_position_committed_usd=(
            state.position.committed_usd if state.position is not None else None
        ),
        final_held_stock_quantity=final_held_quantity,
        final_held_stock_value_usd=final_held_value,
        pnl_usd=pnl,
        return_fraction=pnl / assumptions.starting_equity_usd,
        time_open_seconds=time_open_seconds,
        time_in_range_seconds=time_in_range_seconds,
        fees_accrued_usd=fees_accrued,
        aero_accrued_units=aero_accrued_units,
        aero_accrued_usd=aero_accrued_units * assumptions.aero_price_assumption_usd,
        total_swap_impact_cost_usd=impact_cost_total,
        max_swap_impact_bps=max_impact_bps,
        unmodeled_swap_impact=unmodeled_impact_seen,
        total_gas_cost_usd=gas_cost_total,
        action_counts=_count_actions(tuple(actions)),
        actions=tuple(actions),
    )
