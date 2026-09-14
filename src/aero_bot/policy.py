"""Pure decision code for the locked v1 emissions-farming LP policy.

The engine decides one pool observation at a time without any I/O: observations,
the event calendar, and the immutable parameters are injected by the caller, and
every decision returns both the typed action and the successor state so callers
such as the rehearsal harness can replay deterministic sessions.
"""

import tomllib
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from importlib.resources import files
from typing import Annotated, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, NonNegativeDecimal
from aero_bot.ranging import (
    RangingEvidence,
    RangingObservations,
    WidthSolution,
    ceiling_width_solution,
    solve_range_width,
)

# The bundled events file starts empty and is extended by the local operator.
POLICY_EVENTS_RESOURCE = "policy_events.toml"
# US equity market events are scheduled in the America/New_York session timezone.
NEW_YORK = ZoneInfo("America/New_York")
# The US equity regular session opens at 09:30 America/New_York.
MARKET_OPEN_TIME = time(9, 30)
# The US equity regular session closes at 16:00 America/New_York.
MARKET_CLOSE_TIME = time(16, 0)
# Event windows start sixty minutes before each scheduled event.
EVENT_PRE_WINDOW = timedelta(minutes=60)
# Event windows end thirty minutes after each scheduled event.
EVENT_POST_WINDOW = timedelta(minutes=30)
# High internal precision keeps tick logarithms deterministic across platforms.
MATH_PRECISION = 60
# Uniswap v3-style ticks advance the price by exactly this ratio per tick.
TICK_PRICE_RATIO = Decimal("1.0001")
# One week's weekend days begin at weekday index five (Saturday).
WEEKEND_WEEKDAY_START = 5
# The v1 starting equity for the rehearsal harness and default state.
STARTING_EQUITY_USDC = Decimal("200")
# Gas prices are observed in gwei and converted with the exact one-billion scale.
GWEI_PER_ETH = Decimal(1_000_000_000)
# A fixed 365-day year matches the risk engine's annualization convention.
DAYS_PER_YEAR = Decimal(365)
# USDC has exactly six decimal places, so tranche sizes quantize to this unit.
USDC_QUANTUM = Decimal("0.000001")
# A runaway guard bounds tranche lists exactly like the discovery pagination:
# a position that outgrew its pool this far cannot tranche its way out anyway.
MAX_SWAP_TRANCHES = 1_000


class EventKind(StrEnum):
    """Identify the operator-scheduled event categories from the bundled TOML."""

    # Earnings reports are scheduled volatility events for one stock.
    EARNINGS = "earnings"
    # Ex-dividend dates are scheduled reference-price adjustment events.
    EX_DIVIDEND = "ex_dividend"


class ScheduledEvent(BaseModel):
    """Represent one operator-scheduled event from the bundled calendar."""

    # Frozen strict fields preserve the exact reviewed schedule evidence.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Kind distinguishes earnings volatility from ex-dividend adjustment events.
    kind: EventKind
    # Scheduled time is the event instant; naive timestamps mean New York time.
    scheduled_at: datetime
    # Token scope applies the window to one B20 contract, or every pool when absent.
    token_address: EvmAddress | None = None

    @model_validator(mode="before")
    @classmethod
    def localize_naive_schedule(cls, data: object) -> object:
        """Interpret naive TOML or string timestamps as America/New_York instants.

        Args:
            data: Raw model input mapping or value.

        Returns:
            Input with a timezone-aware scheduled_at ready for interval checks.

        Raises:
            ValueError: If a string scheduled_at is not a valid ISO 8601 timestamp.
        """
        if isinstance(data, dict):
            scheduled_at = data.get("scheduled_at")
            if isinstance(scheduled_at, str):
                # ISO parsing keeps the TOML text form interchangeable with native dates.
                scheduled_at = datetime.fromisoformat(scheduled_at)
            if isinstance(scheduled_at, datetime) and scheduled_at.tzinfo is None:
                # The New York session timezone is the documented default for this file.
                data["scheduled_at"] = scheduled_at.replace(tzinfo=NEW_YORK)
        return data


class EventCalendar(BaseModel):
    """Collect every operator-scheduled event in one immutable reviewable snapshot."""

    # Frozen strict fields keep one calendar stable for a whole replay session.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Events may be empty because the bundled file starts empty by design.
    events: tuple[ScheduledEvent, ...] = ()


class EventWindowView(BaseModel):
    """Report whether one observation instant sits inside any flat event window."""

    # Frozen strict fields preserve one coherent window evaluation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Active means the policy must be flat in USDC at this instant.
    active: bool
    # Description names the window, or its absence, for decision diagnostics.
    description: str


def parse_event_calendar(document: str) -> EventCalendar:
    """Parse and validate one events TOML document.

    Args:
        document: UTF-8 TOML content using one `[[event]]` table per entry.

    Returns:
        A validated immutable calendar, empty when the document has no entries.

    Raises:
        TOMLDecodeError: If the document is not valid TOML.
        ValidationError: If any entry has an unknown kind or invalid fields.
    """
    # The standard-library parser avoids adding any calendar-specific dependency.
    parsed_document = tomllib.loads(document)
    # Unknown top-level keys are ignored so operators may keep notes in the file.
    events = tuple(
        ScheduledEvent.model_validate(entry) for entry in parsed_document.get("event", [])
    )
    return EventCalendar(events=events)


def load_event_calendar() -> EventCalendar:
    """Load the bundled events calendar resource.

    Returns:
        The validated immutable calendar bundled with the application.

    Raises:
        OSError: If the packaged resource cannot be read.
        TOMLDecodeError: If the bundled document is not valid TOML.
        ValidationError: If any bundled entry fails validation.
    """
    # The resource API works from both a source checkout and an installed wheel.
    document = files("aero_bot").joinpath(POLICY_EVENTS_RESOURCE).read_text(encoding="utf-8")
    return parse_event_calendar(document)


def evaluate_event_window(
    observed_at: datetime,
    token_address: EvmAddress,
    calendar: EventCalendar,
) -> EventWindowView:
    """Evaluate every scheduled and session-derived flat window for one token.

    Market open and close windows are derived for every weekday in the
    America/New_York session because the v1 calendar has no holiday source.
    Operator-scheduled earnings and ex-dividend events may be scoped to one
    token or apply to every pool when no token address is configured.

    Since the captain's 2026-09-09 ruling this view is informational only:
    the B20 pools are continuous DeFi markets, so nights and weekends are in
    scope and no scheduled or session-derived window gates entries or forces
    exits anymore. The decision surfaces still report the active window for
    operator awareness.

    Args:
        observed_at: Aware observation instant.
        token_address: B20 contract evaluated by the calling decision.
        calendar: Operator-scheduled earnings and ex-dividend events.

    Returns:
        An active flag plus a description naming the matching window.
    """
    # Scheduled operator events are checked first so explicit dates win ties.
    for event in calendar.events:
        # An event without a token scope applies to every pool.
        scoped_to_other_token = (
            event.token_address is not None and event.token_address != token_address
        )
        # The flat window spans sixty minutes before through thirty minutes after.
        inside_window = (
            event.scheduled_at - EVENT_PRE_WINDOW
            <= observed_at
            < event.scheduled_at + EVENT_POST_WINDOW
        )
        if inside_window and not scoped_to_other_token:
            return EventWindowView(
                active=True,
                description=(
                    f"Inside {event.kind.value} event window "
                    f"around {event.scheduled_at.isoformat()}."
                ),
            )
    # Session events apply only on weekday trading days.
    local_time = observed_at.astimezone(NEW_YORK)
    if local_time.weekday() < WEEKEND_WEEKDAY_START:
        for session_name, session_time in (
            ("market open", MARKET_OPEN_TIME),
            ("market close", MARKET_CLOSE_TIME),
        ):
            # Combining the local date with the session time yields an aware instant.
            session_at = datetime.combine(local_time.date(), session_time, tzinfo=NEW_YORK)
            inside_window = (
                session_at - EVENT_PRE_WINDOW <= observed_at < session_at + EVENT_POST_WINDOW
            )
            if inside_window:
                return EventWindowView(
                    active=True,
                    description=(
                        f"Inside {session_name} event window around {session_at.isoformat()}."
                    ),
                )
    return EventWindowView(
        active=False,
        description="No scheduled or session event window is active.",
    )


class AlignedPriceRange(BaseModel):
    """Represent one tick-grid-aligned price range in USDC per stock."""

    # Frozen strict fields preserve the exact minted boundaries for the ledger.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Lower tick is the greatest grid tick at or below the raw lower bound.
    lower_tick: int
    # Upper tick is the least grid tick at or above the raw upper bound.
    upper_tick: int
    # Lower price is the pool price at the lower tick in USDC per stock.
    lower_price: Decimal
    # Upper price is the pool price at the upper tick in USDC per stock.
    upper_price: Decimal

    @model_validator(mode="after")
    def require_ordered_nonempty_range(self) -> Self:
        """Reject a collapsed or inverted aligned range."""
        if self.lower_tick >= self.upper_tick:
            raise ValueError("lower_tick must be less than upper_tick")
        if self.lower_price >= self.upper_price:
            raise ValueError("lower_price must be less than upper_price")
        return self


class SwapDirection(StrEnum):
    """Identify the two execution directions a policy swap can take."""

    # Buy stock spends USDC into the pool to acquire stock inventory.
    BUY_STOCK = "buy_stock"
    # Sell stock converts stock inventory back into USDC.
    SELL_STOCK = "sell_stock"


class SwapTranche(BaseModel):
    """Represent one executable slice of a modeled policy swap."""

    # Frozen strict fields keep each tranche exactly as the audit will record it.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Tranche size is the US-dollar value executed in this slice.
    usd_size: Annotated[Decimal, Field(gt=0)]
    # Modeled impact is this tranche's price-impact fraction against the observed
    # depth; None means the depth was zero and the impact could not be bounded.
    # The linear model can exceed one whole price unit when a swap outgrows the
    # entire observed depth, which the ledger must surface rather than hide.
    modeled_impact_fraction: NonNegativeDecimal | None


class SwapPlan(BaseModel):
    """Model one policy swap against the pool's observed executable depth."""

    # Frozen strict fields preserve the execution plan the ledger will replay.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Direction identifies whether the swap buys or sells the stock token.
    direction: SwapDirection
    # Total size is the whole US-dollar value the swap must convert.
    total_usd: Annotated[Decimal, Field(gt=0)]
    # Route depth is the executable US-dollar depth the swap is routed through;
    # v1 has one venue per stock, so deepest-path routing is the pool itself.
    route_depth_usd: NonNegativeDecimal
    # Tranches are the ordered slices, one executed per observation interval.
    tranches: Annotated[tuple[SwapTranche, ...], Field(min_length=1)]
    # Worst tranche impact summarizes the plan against the locked ceiling; None
    # means zero depth left the impact unmodeled and labeled as an assumption.
    max_modeled_impact_fraction: NonNegativeDecimal | None

    @model_validator(mode="after")
    def require_tranche_sizes_cover_the_total(self) -> Self:
        """Reject tranche lists that do not add up to the plan's total size."""
        with localcontext() as decimal_context:
            # High precision keeps exactly-split high-digit tranche lists from
            # failing this check through default-context rounding.
            decimal_context.prec = MATH_PRECISION
            tranche_total = sum((tranche.usd_size for tranche in self.tranches), Decimal(0))
        if tranche_total != self.total_usd:
            raise ValueError("tranche sizes must sum to the plan total")
        return self


class PolicyParameters(BaseModel):
    """Lock the v1 emissions-farming parameters as one immutable decision input."""

    # Frozen strict fields make the locked parameter set reproducible in audits.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The target net daily yield per deployed dollar the range width is
    # derived against; the tightest width meeting it wins and the tightest
    # candidate enters anyway when the target proves unreachable.
    target_net_daily_yield: Annotated[Decimal, Field(gt=0)] = Decimal("0.01")
    # The maximum half width on each side of the reference price: the safety
    # ceiling the derived width never exceeds and the fallback when the width
    # solver's inputs are missing or inconsistent, never the entered value
    # itself. The minimum width is exactly one tick spacing per side.
    max_range_half_width_fraction: Decimal = Decimal("0.003")
    # Range boundaries are aligned to the pool tick grid of spacing ten.
    tick_spacing: Annotated[int, Field(ge=1)] = 10
    # Upside out-of-range recenters only after a fifteen-minute wait.
    recenter_wait: timedelta = timedelta(minutes=15)
    # The downside stop triggers 0.5 percent below the lower range edge.
    stop_buffer_fraction: Decimal = Decimal("0.005")
    # Re-entry is blocked for fifteen minutes after a stop or dilution exit.
    reentry_cooldown: timedelta = timedelta(minutes=15)
    # Entry requires raw AERO emissions APR of at least 150 percent per staked
    # liquidity in Aerodrome's APR convention (1.50 decimal, about 0.41 percent
    # per day simple); swap fees are credited on top and the haircut is applied
    # only inside the P&L forecast, never to this gate.
    min_entry_emissions_apr: Decimal = Decimal("1.5")
    # A position commits at most eighty percent of current equity. Raised
    # from twenty percent by the captain's calibration ruling (2026-09-09):
    # the ninety-dollar trial book sizes roughly seventy-two USDC per
    # position, and the fraction stays inside the pilot's unchanged hard
    # ceilings (100 USDC total exposure and 100 USDC per pool, enforced by
    # the LP executor's caps) rather than replacing them.
    max_position_equity_fraction: Decimal = Decimal("0.80")
    # A position is hard-gated to one percent of observed pool depth.
    max_position_depth_fraction: Decimal = Decimal("0.01")
    # A five percent same-day equity loss halts new entries until the next day.
    daily_loss_halt_fraction: Decimal = Decimal("0.05")
    # The underlying reference quote blocks entries when older than this bound.
    reference_max_age_seconds: Annotated[int, Field(ge=0)] = 300
    # While a position is open, a reference older than this bound triggers a
    # defensive exit; it is deliberately looser than the entry bound so a brief
    # quote outage never forces an immediate exit.
    reference_open_position_max_age_seconds: Annotated[int, Field(ge=0)] = 900
    # Dislocation actions fire when the AMM and reference prices differ by at
    # least 0.15 percent in either direction.
    dislocation_threshold_fraction: Decimal = Decimal("0.0015")
    # Stock tokens held after a stale-low burn are sold at market once this
    # convergence timeout elapses without the AMM converging to the reference.
    convergence_timeout: timedelta = timedelta(minutes=5)
    # Every swap must keep its modeled price impact at or below 0.1 percent.
    swap_impact_ceiling_fraction: Decimal = Decimal("0.001")
    # A swap whose single-shot impact would exceed 0.05 percent is split into
    # smaller tranches, one executed per observation interval.
    swap_impact_tranche_fraction: Decimal = Decimal("0.0005")
    # Non-urgent actions are deferred while the L2 gas price exceeds 0.5 gwei.
    gas_price_ceiling_gwei: Decimal = Decimal("0.5")
    # Non-urgent actions are deferred while the estimated batch cost exceeds
    # five percent of the position's expected daily gross yield.
    gas_cost_max_gross_yield_fraction: Decimal = Decimal("0.05")
    # Every batch pays roughly one hundred thousand gas of Safe proxy overhead.
    safe_overhead_gas_per_batch: Annotated[int, Field(ge=0)] = 100_000
    # An entry batch swaps, mints, and stakes (approvals included).
    enter_batch_gas_units: Annotated[int, Field(ge=1)] = 650_000
    # A recenter batch burns, swaps, re-mints, and re-stakes.
    recenter_batch_gas_units: Annotated[int, Field(ge=1)] = 550_000
    # An exit batch burns, unstakes, and swaps the inventory back to USDC.
    exit_batch_gas_units: Annotated[int, Field(ge=1)] = 350_000
    # Selling held inventory after a stale-low burn is a single swap batch.
    inventory_sell_gas_units: Annotated[int, Field(ge=1)] = 180_000
    # The ETH price is a documented configurable assumption, not a live quote.
    eth_price_assumption_usd: Decimal = Decimal("3000")

    @model_validator(mode="after")
    def require_unit_fractions(self) -> Self:
        """Reject fraction parameters outside their meaningful intervals."""
        if not Decimal(0) < self.max_range_half_width_fraction < Decimal(1):
            raise ValueError("max_range_half_width_fraction must be between zero and one")
        if not Decimal(0) < self.stop_buffer_fraction < Decimal(1):
            raise ValueError("stop_buffer_fraction must be between zero and one")
        if not Decimal(0) < self.max_position_equity_fraction <= Decimal(1):
            raise ValueError("max_position_equity_fraction must be in (0, 1]")
        if not Decimal(0) < self.max_position_depth_fraction <= Decimal(1):
            raise ValueError("max_position_depth_fraction must be in (0, 1]")
        if not Decimal(0) < self.daily_loss_halt_fraction < Decimal(1):
            raise ValueError("daily_loss_halt_fraction must be between zero and one")
        if not Decimal(0) < self.dislocation_threshold_fraction < Decimal(1):
            raise ValueError("dislocation_threshold_fraction must be between zero and one")
        if not Decimal(0) < self.swap_impact_tranche_fraction <= self.swap_impact_ceiling_fraction:
            raise ValueError(
                "swap_impact_tranche_fraction must be in (0, swap_impact_ceiling_fraction]"
            )
        if not Decimal(0) < self.swap_impact_ceiling_fraction < Decimal(1):
            raise ValueError("swap_impact_ceiling_fraction must be between zero and one")
        if self.gas_price_ceiling_gwei <= 0:
            raise ValueError("gas_price_ceiling_gwei must be positive")
        if not Decimal(0) < self.gas_cost_max_gross_yield_fraction < Decimal(1):
            raise ValueError("gas_cost_max_gross_yield_fraction must be between zero and one")
        if self.eth_price_assumption_usd <= 0:
            raise ValueError("eth_price_assumption_usd must be positive")
        if self.convergence_timeout <= timedelta(0):
            raise ValueError("convergence_timeout must be positive")
        if self.reference_open_position_max_age_seconds < self.reference_max_age_seconds:
            raise ValueError(
                "reference_open_position_max_age_seconds must not be tighter than "
                "reference_max_age_seconds"
            )
        return self


class PolicyObservation(BaseModel):
    """Inject every external fact one policy decision depends on."""

    # Frozen strict fields keep one decision on a single coherent observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Observation time is the instant this evidence set became current.
    observed_at: datetime
    # The Aerodrome Slipstream pool this decision applies to.
    pool_address: EvmAddress
    # The B20 stock token paired with native USDC in that pool.
    token_address: EvmAddress
    # AMM price is the pool's token price in USDC per one stock token.
    amm_price_usdc: Annotated[Decimal, Field(gt=0)]
    # Raw emissions APR is the pool's AERO emissions APR per staked liquidity
    # before any haircut, in the same convention Aerodrome displays.
    emissions_apr: NonNegativeDecimal
    # Fee APR annualizes the pool's gross swap fees; it feeds the expected
    # daily gross yield the gas sense-check gate compares batch costs against.
    fee_apr: NonNegativeDecimal = Decimal("0")
    # Pool depth is the executable US-dollar depth used by the position gate.
    pool_depth_usd: NonNegativeDecimal
    # Equity is the caller's current marked total portfolio value in USDC.
    equity_usd: Annotated[Decimal, Field(gt=0)]
    # Reference price is the keyless real-market quote in USDC per stock.
    reference_price_usdc: Annotated[Decimal, Field(gt=0)] | None = None
    # Reference age is seconds since that quote; absence means no live quote.
    reference_age_seconds: Annotated[int, Field(ge=0)] | None = None
    # External references are advisory unless an explicit caller enables
    # reference enforcement. Scheduled production cycles deliberately disable
    # it: the resolved Aerodrome pool is authoritative for trading actions.
    reference_enforcement_enabled: bool = True
    # A stale oracle observation is a condition-driven flat event per policy.
    oracle_stale: bool = False
    # A paused B20 registry is a condition-driven flat event per policy.
    registry_paused: bool = False
    # The current Base L2 gas price in gwei; None means the reading is
    # unavailable and the gas gate defers every non-urgent action fail-closed.
    gas_price_gwei: NonNegativeDecimal | None = None
    # Live ranging observables behind the target-yield width derivation;
    # absence fails the derived width toward the locked ceiling with an
    # explicit fallback label on the entry or recenter decision.
    ranging: RangingEvidence | None = None

    @model_validator(mode="after")
    def require_aware_observation_time(self) -> Self:
        """Reject naive observation times so all wait and cooldown math is absolute."""
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


class PolicyPosition(BaseModel):
    """Track the full lifecycle state of one open concentrated position."""

    # Frozen strict fields keep each observed position snapshot immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The pool the position was minted in.
    pool_address: EvmAddress
    # The stock token committed to the position.
    token_address: EvmAddress
    # The tick-grid-aligned range the position spans.
    price_range: AlignedPriceRange
    # Committed capital is the USDC value entered at mint or recenter.
    committed_usd: Annotated[Decimal, Field(gt=0)]
    # Entry time anchors the wait and cooldown timeline of this position.
    entered_at: datetime
    # The upside out-of-range wait anchor is set once price exits above range.
    out_of_range_since: datetime | None = None

    @model_validator(mode="after")
    def require_aware_entry_time(self) -> Self:
        """Reject naive entry times for the same reason as observations."""
        if self.entered_at.tzinfo is None:
            raise ValueError("entered_at must be timezone-aware")
        if self.out_of_range_since is not None and self.out_of_range_since.tzinfo is None:
            raise ValueError("out_of_range_since must be timezone-aware")
        return self


class HeldInventory(BaseModel):
    """Track stock tokens held outside the pool after a stale-low burn."""

    # Frozen strict fields keep one coherent mid-unwind inventory snapshot.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The pool the burned position lived in and the tokens will be sold into.
    pool_address: EvmAddress
    # The stock token whose quantity is held unsold.
    token_address: EvmAddress
    # Stock quantity is the token-unit inventory carried out of the burn.
    stock_quantity: Annotated[Decimal, Field(gt=0)]
    # Held-since anchors the convergence timeout of the hold.
    held_since: datetime

    @model_validator(mode="after")
    def require_aware_held_since(self) -> Self:
        """Reject naive held-since instants for the same reason as observations."""
        if self.held_since.tzinfo is None:
            raise ValueError("held_since must be timezone-aware")
        return self


class PolicyState(BaseModel):
    """Carry every engine-owned fact between observations of one replay session."""

    # Frozen strict fields make the whole session a pure fold over observations.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The America/New_York day the day-start equity snapshot belongs to.
    day: date | None = None
    # Day-start equity anchors the five-percent daily loss halt.
    day_start_equity_usd: Annotated[Decimal, Field(gt=0)] = STARTING_EQUITY_USDC
    # The day a daily loss halt tripped; entries stay blocked until it changes.
    halted_day: date | None = None
    # The open position, or None while the policy is flat in USDC.
    position: PolicyPosition | None = None
    # Stock tokens held unsold after a stale-low burn, awaiting convergence.
    held_inventory: HeldInventory | None = None
    # Re-entry stays blocked until this instant after stop or dilution exits.
    reentry_blocked_until: datetime | None = None


class PolicyActionKind(StrEnum):
    """Identify every action the v1 policy can emit."""

    # Hold leaves the current posture unchanged at this observation.
    HOLD = "hold"
    # Enter mints and stakes a new position at the tick-aligned range.
    ENTER = "enter"
    # Recenter burns and re-mints the range around the current pool price.
    RECENTER = "recenter"
    # Stop out burns and swaps all inventory back to USDC at the downside stop.
    STOP_OUT = "stop_out"
    # Dilution exit burns and swaps all inventory back to USDC below the APR gate.
    DILUTION_EXIT = "dilution_exit"
    # Event exit burns and swaps all inventory back to USDC for an event window.
    EVENT_EXIT = "event_exit"
    # Dislocation exit burns and swaps all inventory back to USDC while the
    # AMM still prices the stock above the real market (stale-high).
    DISLOCATION_EXIT = "dislocation_exit"
    # Stale-low burn burns the position and holds the stock tokens unsold
    # because the pool prices them below the real market.
    STALE_LOW_BURN = "stale_low_burn"
    # Defensive exit burns and swaps all inventory back to USDC when the
    # reference quote went stale beyond the open-position bound.
    DEFENSIVE_EXIT = "defensive_exit"
    # Sell inventory swaps held stock tokens back to USDC on convergence,
    # timeout, or a forced flat window.
    SELL_INVENTORY = "sell_inventory"
    # Switch pools exits the held position and enters a better-qualifying
    # one in the same cycle. Composed only by the cross-board selector
    # (aero_bot.selector); the per-pool engine never emits it, because a
    # switch is a cross-pool judgment the single-pool fold cannot see.
    POOL_SWITCH = "pool_switch"


class PolicyReason(StrEnum):
    """Provide stable, machine-readable evidence for one policy decision."""

    # An open position is inside its range and no rule requires action.
    OPEN_IN_RANGE = "open_in_range"
    # Price exited above the range and the time-based recenter wait is running.
    OPEN_ABOVE_RANGE_WAITING = "open_above_range_waiting"
    # Price sits below the range edge but above the stop level, so the
    # position holds and may recover into the range.
    OPEN_BELOW_EDGE_HOLDING = "open_below_edge_holding"
    # Entry is eligible because the raw emissions APR threshold is met.
    ENTRY_THRESHOLD_MET = "entry_threshold_met"
    # The pool's raw emissions APR is below the entry threshold.
    EMISSIONS_BELOW_ENTRY_THRESHOLD = "emissions_below_entry_threshold"
    # A stop or dilution exit's re-entry cooldown is still running.
    ENTRY_COOLDOWN_ACTIVE = "entry_cooldown_active"
    # The five-percent daily loss halt blocks new entries for the day.
    DAILY_LOSS_HALT_ACTIVE = "daily_loss_halt_active"
    # A condition-driven flat event (a stale oracle observation or a paused
    # registry) requires being flat; scheduled windows no longer gate since
    # the captain's 2026-09-09 twenty-four-seven ruling.
    EVENT_WINDOW_FLAT = "event_window_flat"
    # The real-market reference quote is missing or stale, blocking entry.
    REFERENCE_STALE = "reference_stale"
    # The equity and depth caps leave no positive position size to enter.
    ENTRY_SIZE_EMPTY = "entry_size_empty"
    # The upside wait elapsed and the range recentered around the pool price.
    RECENTER_WAIT_ELAPSED = "recenter_wait_elapsed"
    # Price reached 0.5 percent below the lower range edge.
    DOWNSIDE_STOP_TRIGGERED = "downside_stop_triggered"
    # The pool's raw emissions APR fell below the threshold while open.
    DILUTION_EXIT_TRIGGERED = "dilution_exit_triggered"
    # A condition-driven flat event arrived while a position was open;
    # scheduled windows no longer force exits since the captain's
    # 2026-09-09 twenty-four-seven ruling.
    EVENT_EXIT_TRIGGERED = "event_exit_triggered"
    # The reference quote went missing or stale beyond the open-position bound.
    REFERENCE_STALE_DEFENSIVE_EXIT = "reference_stale_defensive_exit"
    # The AMM price rose beyond the dislocation threshold above the reference.
    DISLOCATION_STALE_HIGH_TRIGGERED = "dislocation_stale_high_triggered"
    # The AMM price fell beyond the dislocation threshold below the reference.
    DISLOCATION_STALE_LOW_TRIGGERED = "dislocation_stale_low_triggered"
    # Held stock tokens are sold because the AMM converged to the reference.
    INVENTORY_CONVERGENCE_REACHED = "inventory_convergence_reached"
    # Held stock tokens are sold at market because the convergence timeout hit.
    INVENTORY_CONVERGENCE_TIMEOUT = "inventory_convergence_timeout"
    # Held stock tokens are sold because a condition-driven flat event
    # requires being in USDC.
    INVENTORY_FLAT_WINDOW_SELL = "inventory_flat_window_sell"
    # Held stock tokens wait for convergence inside the timeout bound.
    HOLDING_INVENTORY_AWAITING_CONVERGENCE = "holding_inventory_awaiting_convergence"
    # New entries stay blocked while stock tokens from a stale-low burn are
    # still held unsold.
    INVENTORY_UNWIND_PENDING = "inventory_unwind_pending"
    # A non-urgent entry or recenter is deferred by the gas sense-check gate.
    GAS_GATE_DEFERRED = "gas_gate_deferred"
    # Another pool's qualifying emissions APR exceeded the held pool's by
    # the relative switch margin and the exit-plus-entry gas economics
    # passed. Composed only by the cross-board selector
    # (aero_bot.selector); the per-pool engine never emits it.
    POOL_SWITCH_TRIGGERED = "pool_switch_triggered"
    # No pool on the enumerated board qualified through the complete entry
    # gate chain. Composed only by the cross-board selector; the per-pool
    # engine never emits it.
    NO_QUALIFYING_POOL = "no_qualifying_pool"


class PolicyDecision(BaseModel):
    """Return one typed immutable action with explicit reasons and evidence."""

    # Frozen strict fields preserve the decision exactly as it will be audited.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Action is the single execution instruction for this observation.
    action: PolicyActionKind
    # Reason is the primary stable cause for the chosen action.
    reason: PolicyReason
    # Diagnostics carry the numeric evidence behind the decision.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]
    # Price range is present for enter and recenter actions.
    price_range: AlignedPriceRange | None = None
    # Size is the committed USDC value present for enter actions.
    size_usd: NonNegativeDecimal | None = None
    # Swap plan models every swap the action performs against observed depth
    # with tranche splitting; absent when the action performs no swap.
    swap_plan: SwapPlan | None = None
    # Width solution carries the complete target-yield width derivation behind
    # an enter or recenter range: every input, every modeled candidate width,
    # and how the solve resolved (solved, unreachable at the tightest width,
    # or failed toward the ceiling).
    width_solution: WidthSolution | None = None
    # Estimated batch gas units include the Safe proxy overhead per batch.
    estimated_gas_units: Annotated[int, Field(ge=0)] | None = None
    # Estimated batch cost applies the documented ETH price assumption; None
    # when the gas price reading is unavailable, which never blocks a safety
    # exit because gap risk dominates an unknown gas cost.
    estimated_gas_cost_usd: NonNegativeDecimal | None = None


class PolicyOutcome(BaseModel):
    """Pair one decision with the successor state the caller must thread forward."""

    # Frozen strict fields keep the decision and state transition inseparable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Decision is the immutable action chosen for this observation.
    decision: PolicyDecision
    # Next state is the complete engine state after applying the decision.
    next_state: PolicyState


class PolicyDecisionRequest(BaseModel):
    """Carry one observation and the threaded state through the app boundary."""

    # Frozen strict fields keep the request exactly as the audit will record it.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Observation contains every external fact this decision depends on.
    observation: PolicyObservation
    # State is the engine state threaded from the previous decision's outcome;
    # a fresh request defaults to a new session at the starting equity.
    state: PolicyState = Field(default_factory=PolicyState)


# The locked v1 parameters exist once as an immutable module-level default.
LOCKED_POLICY_PARAMETERS = PolicyParameters()
# The empty calendar exists once as the default until the operator schedules events.
EMPTY_EVENT_CALENDAR = EventCalendar()


class PolicyEngine:
    """Fold injected observations into typed policy decisions without I/O."""

    def __init__(
        self,
        parameters: PolicyParameters = LOCKED_POLICY_PARAMETERS,
        calendar: EventCalendar = EMPTY_EVENT_CALENDAR,
    ) -> None:
        """Create an engine around one immutable parameter and calendar set.

        Args:
            parameters: Locked v1 thresholds, widths, waits, caps, and cooldowns.
            calendar: Operator-scheduled earnings and ex-dividend events.
        """
        # The immutable inputs remain fixed for the engine's lifetime.
        self._parameters = parameters
        self._calendar = calendar

    @property
    def parameters(self) -> PolicyParameters:
        """Return the immutable parameter set required to reproduce decisions."""
        return self._parameters

    @property
    def calendar(self) -> EventCalendar:
        """Return the immutable event calendar injected into this engine."""
        return self._calendar

    def decide(self, state: PolicyState, observation: PolicyObservation) -> PolicyOutcome:
        """Decide one observation against the current engine state.

        Decision precedence is fixed: held stock inventory from a stale-low
        burn is unwound first, then safety exits (reference-stale defensive
        exit, dislocation monitor, downside stop, emissions dilution,
        condition-driven flat events), then position maintenance (upside
        recenter wait behind the gas gate), then the ordered entry gates
        behind the same gas gate.

        Args:
            state: Engine state threaded from the previous decision.
            observation: One coherent injected observation for one pool.

        Returns:
            The typed decision plus the successor state to thread forward.
        """
        # Day rollover and the daily loss halt are evaluated before any gate.
        working_state = self._observe_day(state, observation)
        # Captain's ruling (2026-09-09): the B20 pools are continuous DeFi
        # markets - nights and weekends are in scope - so the scheduled and
        # session-derived event windows no longer gate entries or force
        # exits. The calendar machinery stays loaded for reporting (the
        # decision surfaces still print the window view), and only the
        # condition-driven flats below (a stale oracle observation or a
        # paused registry) still require the policy to be flat in USDC.
        flat_description = self._flat_reason(observation)

        # Held inventory is a mid-unwind safety posture and resolves before any
        # new exposure; the engine never holds inventory and a position at once.
        if working_state.held_inventory is not None:
            return self._decide_holding_inventory(working_state, observation, flat_description)
        if working_state.position is not None:
            return self._decide_with_position(working_state, observation, flat_description)
        return self._decide_flat(working_state, observation, flat_description)

    def build_aligned_range(
        self,
        center_price: Decimal,
        half_width_fraction: Decimal | None = None,
    ) -> AlignedPriceRange:
        """Build one width's range around a price, aligned to the tick grid.

        Args:
            center_price: Positive pool price in USDC per stock.
            half_width_fraction: Fractional half width on each side of the
                center the range must span; None means the locked ceiling,
                which is the fallback width rather than a derived one.

        Returns:
            The grid-aligned range spanning at least the requested half width.

        Raises:
            ValueError: If the center price is not positive or the requested
                half width is not inside the unit interval.
        """
        if center_price <= 0:
            raise ValueError("center_price must be positive")
        width = (
            self._parameters.max_range_half_width_fraction
            if half_width_fraction is None
            else half_width_fraction
        )
        if not Decimal(0) < width < Decimal(1):
            raise ValueError("half_width_fraction must be between zero and one")
        with localcontext() as decimal_context:
            # Local precision isolates deterministic tick logarithms from settings.
            decimal_context.prec = MATH_PRECISION
            # The tick index of a price is its natural logarithm over log(1.0001).
            tick_log = TICK_PRICE_RATIO.ln()
            # Raw bounds apply the requested half width on each side of the center.
            raw_lower_tick = (center_price * (Decimal(1) - width)).ln() / tick_log
            raw_upper_tick = (center_price * (Decimal(1) + width)).ln() / tick_log
            # Grid alignment floors the lower and ceils the upper boundary so
            # the aligned range always contains the raw width.
            spacing = Decimal(self._parameters.tick_spacing)
            lower_tick = int(
                (raw_lower_tick / spacing).to_integral_value(rounding=ROUND_FLOOR) * spacing
            )
            upper_tick = int(
                (raw_upper_tick / spacing).to_integral_value(rounding=ROUND_CEILING) * spacing
            )
            # Boundary prices are recovered exactly from the aligned ticks.
            lower_price = TICK_PRICE_RATIO**lower_tick
            upper_price = TICK_PRICE_RATIO**upper_tick
            return AlignedPriceRange(
                lower_tick=lower_tick,
                upper_tick=upper_tick,
                lower_price=+lower_price,
                upper_price=+upper_price,
            )

    def _solve_range_width(
        self,
        observation: PolicyObservation,
        position_size_usd: Decimal,
    ) -> WidthSolution:
        """Derive the range half width from the target net daily yield.

        The solve consumes the observation's ranging evidence plus every
        parameter the locked set already carries. Missing evidence, or a gas
        reading the gate would have deferred on, fails toward the locked
        ceiling with the fallback label rather than guessing a width.

        Args:
            observation: The passing observation whose entry or recenter
                range is being built.
            position_size_usd: The capped USDC value the range would commit.

        Returns:
            The immutable width solution with its complete evidence.
        """
        evidence = observation.ranging
        gas_price = observation.gas_price_gwei
        if evidence is None or gas_price is None:
            # The gas gate defers before any solve, so an absent reading here
            # is unreachable in practice; both absences fail toward the
            # ceiling with the same explicit fallback label.
            missing = (
                "the observation carries no ranging evidence"
                if evidence is None
                else "the gas price reading is unavailable"
            )
            return ceiling_width_solution(
                pool_price_usdc=observation.amm_price_usdc,
                tick_spacing=self._parameters.tick_spacing,
                max_range_half_width_fraction=self._parameters.max_range_half_width_fraction,
                target_net_daily_yield=self._parameters.target_net_daily_yield,
                reason=missing,
            )
        return solve_range_width(
            RangingObservations(
                pool_price_usdc=observation.amm_price_usdc,
                emissions_apr=observation.emissions_apr,
                gauge_liquidity_raw=evidence.gauge_liquidity_raw,
                staked_tvl_usd=evidence.staked_tvl_usd,
                active_liquidity_raw=evidence.active_liquidity_raw,
                pool_depth_usd=observation.pool_depth_usd,
                fee_window_seconds=evidence.fee_window_seconds,
                fee_window_notional_usd=evidence.fee_window_notional_usd,
                pool_fee_ppm=evidence.pool_fee_ppm,
                realized_daily_volatility=evidence.realized_daily_volatility,
                stock_decimals=evidence.stock_decimals,
                quote_decimals=evidence.quote_decimals,
                position_size_usd=position_size_usd,
                gas_price_gwei=gas_price,
                target_net_daily_yield=self._parameters.target_net_daily_yield,
                max_range_half_width_fraction=self._parameters.max_range_half_width_fraction,
                tick_spacing=self._parameters.tick_spacing,
                stop_buffer_fraction=self._parameters.stop_buffer_fraction,
                recenter_wait_seconds=int(self._parameters.recenter_wait.total_seconds()),
                reentry_cooldown_seconds=int(self._parameters.reentry_cooldown.total_seconds()),
                enter_batch_gas_units=self._parameters.enter_batch_gas_units,
                recenter_batch_gas_units=self._parameters.recenter_batch_gas_units,
                exit_batch_gas_units=self._parameters.exit_batch_gas_units,
                safe_overhead_gas_per_batch=self._parameters.safe_overhead_gas_per_batch,
                eth_price_assumption_usd=self._parameters.eth_price_assumption_usd,
            )
        )

    def _observe_day(self, state: PolicyState, observation: PolicyObservation) -> PolicyState:
        """Apply day rollover and the daily loss halt to the threaded state.

        Args:
            state: Engine state from the previous decision.
            observation: The current injected observation.

        Returns:
            State with the day snapshot rolled over and any halt recorded.
        """
        # The trading day is anchored to the America/New_York session date.
        observation_day = observation.observed_at.astimezone(NEW_YORK).date()
        if state.day != observation_day:
            # A new day resets both the day-start equity and any prior halt.
            state = state.model_copy(
                update={
                    "day": observation_day,
                    "day_start_equity_usd": observation.equity_usd,
                    "halted_day": None,
                }
            )
        if state.halted_day != observation_day:
            # Marked drawdown from the day-start equity is the halt trigger.
            drawdown_fraction = (
                state.day_start_equity_usd - observation.equity_usd
            ) / state.day_start_equity_usd
            if drawdown_fraction >= self._parameters.daily_loss_halt_fraction:
                # The halt latches for the day even if equity later recovers.
                state = state.model_copy(update={"halted_day": observation_day})
        return state

    def _flat_reason(self, observation: PolicyObservation) -> str | None:
        """Describe any condition-driven flat event, or None when healthy.

        Args:
            observation: The current injected observation.

        Returns:
            A diagnostic description when a condition event requires being flat.
        """
        # Oracle-stale and registry-pause signals are flat events from the
        # existing oracle health gates rather than scheduled calendar entries.
        if observation.oracle_stale:
            return "Oracle-stale signal requires the policy to be flat in USDC."
        if observation.registry_paused:
            return "Registry-pause signal requires the policy to be flat in USDC."
        return None

    def _decide_with_position(
        self,
        state: PolicyState,
        observation: PolicyObservation,
        flat_description: str | None,
    ) -> PolicyOutcome:
        """Apply safety exits, maintenance, and holds to one open position.

        Args:
            state: Day-rolled state carrying the open position.
            observation: The current injected observation.
            flat_description: Description of the active window or flat condition.

        Returns:
            The position decision and successor state.
        """
        # The open position is the only fact the position branch mutates.
        position = state.position
        if position is None:  # pragma: no cover - guarded by the caller
            raise ValueError("position branch requires an open position")
        # A reference missing or stale beyond the open-position bound leaves the
        # dislocation monitor blind, so the position exits defensively.
        if self._reference_beyond_open_bound(observation):
            return self._safety_exit(
                state,
                observation,
                PolicyActionKind.DEFENSIVE_EXIT,
                PolicyReason.REFERENCE_STALE_DEFENSIVE_EXIT,
                (
                    "The underlying reference quote is missing or older than the "
                    f"{self._parameters.reference_open_position_max_age_seconds}-second "
                    "open-position bound; exiting defensively while blind to dislocation.",
                    "Exit path burns the position and swaps all inventory back to USDC.",
                ),
                with_cooldown=False,
            )
        # The dislocation monitor is evaluated before every AMM-anchored rule
        # because the reference market is treated as the true price.
        dislocation = self._dislocation_outcome(state, observation)
        if dislocation is not None:
            return dislocation
        # The stop level sits 0.5 percent below the aligned lower range edge.
        stop_level = position.price_range.lower_price * (
            Decimal(1) - self._parameters.stop_buffer_fraction
        )
        # Safety exits are evaluated in fixed order and are never deferred.
        if observation.amm_price_usdc <= stop_level:
            return self._safety_exit(
                state,
                observation,
                PolicyActionKind.STOP_OUT,
                PolicyReason.DOWNSIDE_STOP_TRIGGERED,
                (
                    f"Pool price {observation.amm_price_usdc} reached the stop level "
                    f"{stop_level} at 0.5 percent below the lower range edge "
                    f"{position.price_range.lower_price}.",
                    "Exit path burns the position and swaps all inventory back to USDC.",
                ),
                with_cooldown=True,
            )
        # Other LPs adding sticky staked liquidity persistently lowers the
        # emissions APR per unit of staked liquidity, so the gate is re-checked
        # at every observation while open.
        if observation.emissions_apr < self._parameters.min_entry_emissions_apr:
            return self._safety_exit(
                state,
                observation,
                PolicyActionKind.DILUTION_EXIT,
                PolicyReason.DILUTION_EXIT_TRIGGERED,
                (
                    f"Raw emissions APR {observation.emissions_apr} fell below the entry "
                    f"threshold {self._parameters.min_entry_emissions_apr} while open.",
                    "Exit path burns the position and swaps all inventory back to USDC.",
                ),
                with_cooldown=True,
            )
        # Condition-driven flat events require being flat in USDC.
        if flat_description is not None:
            return self._safety_exit(
                state,
                observation,
                PolicyActionKind.EVENT_EXIT,
                PolicyReason.EVENT_EXIT_TRIGGERED,
                (
                    flat_description,
                    "Exit path burns the position and swaps all inventory back to USDC.",
                ),
                with_cooldown=False,
            )
        # Upside out-of-range starts a time-based wait before any recenter.
        if observation.amm_price_usdc >= position.price_range.upper_price:
            wait_anchor = position.out_of_range_since or observation.observed_at
            waited = observation.observed_at - wait_anchor
            if waited >= self._parameters.recenter_wait:
                # The gas sense-check gate defers non-urgent recenters.
                deferred, defer_diagnostics = self._gas_gate_blocks(
                    observation,
                    self._parameters.recenter_batch_gas_units,
                    position.committed_usd,
                )
                if deferred:
                    # The anchor persists so the elapsed wait stays elapsed and
                    # the recenter retries on a cheaper observation.
                    waiting_position = position.model_copy(
                        update={"out_of_range_since": wait_anchor}
                    )
                    return self._hold(
                        state.model_copy(update={"position": waiting_position}),
                        PolicyReason.GAS_GATE_DEFERRED,
                        defer_diagnostics,
                    )
                # The recenter width is re-derived from the target net daily
                # yield at the current observables, then the range is rebuilt
                # around the current pool price.
                width_solution = self._solve_range_width(observation, position.committed_usd)
                new_range = self.build_aligned_range(
                    observation.amm_price_usdc, width_solution.half_width_fraction
                )
                gas_units, gas_cost_usd = self._batch_gas(
                    observation, self._parameters.recenter_batch_gas_units
                )
                # Above the range the burned position is all USDC, so the
                # re-mint buys roughly half of it back into stock.
                swap_plan = self._swap_plan(
                    SwapDirection.BUY_STOCK,
                    position.committed_usd / Decimal(2),
                    observation.pool_depth_usd,
                )
                diagnostics = (
                    (
                        f"Upside out-of-range wait of {waited} elapsed the locked "
                        f"recenter wait {self._parameters.recenter_wait}.",
                        f"New range {new_range.lower_price}..{new_range.upper_price} "
                        f"USDC per stock around pool price {observation.amm_price_usdc}.",
                        f"Range half width {width_solution.half_width_fraction} "
                        f"({width_solution.half_width_ticks} ticks per side) derived "
                        f"against the target net daily yield "
                        f"{self._parameters.target_net_daily_yield}; solve resolved as "
                        f"{width_solution.mode.value}.",
                    )
                    + width_solution.diagnostics
                    + self._gas_diagnostics(gas_units, gas_cost_usd)
                )
                next_position = position.model_copy(
                    update={
                        "price_range": new_range,
                        "entered_at": observation.observed_at,
                        "out_of_range_since": None,
                    }
                )
                next_state = state.model_copy(update={"position": next_position})
                return PolicyOutcome(
                    decision=PolicyDecision(
                        action=PolicyActionKind.RECENTER,
                        reason=PolicyReason.RECENTER_WAIT_ELAPSED,
                        diagnostics=diagnostics,
                        price_range=new_range,
                        size_usd=position.committed_usd,
                        swap_plan=swap_plan,
                        estimated_gas_units=gas_units,
                        estimated_gas_cost_usd=gas_cost_usd,
                        width_solution=width_solution,
                    ),
                    next_state=next_state,
                )
            # The wait continues and the anchor persists across observations.
            diagnostics = (
                f"Pool price {observation.amm_price_usdc} is above the upper range "
                f"edge {position.price_range.upper_price}; waited {waited} of the "
                f"locked recenter wait {self._parameters.recenter_wait}.",
            )
            next_position = position.model_copy(update={"out_of_range_since": wait_anchor})
            next_state = state.model_copy(update={"position": next_position})
            return PolicyOutcome(
                decision=PolicyDecision(
                    action=PolicyActionKind.HOLD,
                    reason=PolicyReason.OPEN_ABOVE_RANGE_WAITING,
                    diagnostics=diagnostics,
                ),
                next_state=next_state,
            )
        # Returning inside the range clears any prior upside wait anchor.
        updated_position = (
            position
            if position.out_of_range_since is None
            else position.model_copy(update={"out_of_range_since": None})
        )
        next_state = state.model_copy(update={"position": updated_position})
        if observation.amm_price_usdc < position.price_range.lower_price:
            # Below the edge but above the stop level, the position holds.
            diagnostics = (
                f"Pool price {observation.amm_price_usdc} is below the lower range "
                f"edge {position.price_range.lower_price} but above the stop level "
                f"{stop_level}; holding for recovery.",
            )
            reason = PolicyReason.OPEN_BELOW_EDGE_HOLDING
        else:
            diagnostics = (
                f"Pool price {observation.amm_price_usdc} is inside the range "
                f"{position.price_range.lower_price}..{position.price_range.upper_price}; "
                f"no rule requires action.",
            )
            reason = PolicyReason.OPEN_IN_RANGE
        return PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.HOLD,
                reason=reason,
                diagnostics=diagnostics,
            ),
            next_state=next_state,
        )

    def _decide_holding_inventory(
        self,
        state: PolicyState,
        observation: PolicyObservation,
        flat_description: str | None,
    ) -> PolicyOutcome:
        """Resolve stock tokens held unsold after a stale-low burn.

        Args:
            state: Day-rolled state carrying the held inventory.
            observation: The current injected observation.
            flat_description: Description of the active window or flat condition.

        Returns:
            The inventory decision and successor state.
        """
        # The held inventory is the only fact this branch resolves.
        inventory = state.held_inventory
        if inventory is None:  # pragma: no cover - guarded by the caller
            raise ValueError("inventory branch requires held stock tokens")
        # A fresh reference allows a convergence judgment; without one only the
        # timeout bound or a flat window can release the held tokens.
        reference = observation.reference_price_usdc
        converged = (
            reference is not None
            and not self._reference_stale(observation)
            and observation.amm_price_usdc
            >= reference * (Decimal(1) - self._parameters.dislocation_threshold_fraction)
        )
        timed_out = (
            observation.observed_at - inventory.held_since >= self._parameters.convergence_timeout
        )
        if flat_description is not None:
            reason = PolicyReason.INVENTORY_FLAT_WINDOW_SELL
            trigger: tuple[str, ...] = (
                flat_description,
                "Held stock tokens are sold at market because the window requires USDC.",
            )
        elif timed_out:
            reason = PolicyReason.INVENTORY_CONVERGENCE_TIMEOUT
            trigger = (
                f"Held since {inventory.held_since} without convergence past the "
                f"{self._parameters.convergence_timeout} timeout; selling at market "
                "as the safety bound.",
            )
        elif converged:
            reason = PolicyReason.INVENTORY_CONVERGENCE_REACHED
            trigger = (
                f"Pool price {observation.amm_price_usdc} converged to within "
                f"{self._parameters.dislocation_threshold_fraction} of the reference "
                f"{reference}.",
            )
        else:
            return self._hold(
                state,
                PolicyReason.HOLDING_INVENTORY_AWAITING_CONVERGENCE,
                (
                    f"Holding {inventory.stock_quantity} stock tokens unsold; pool "
                    f"price {observation.amm_price_usdc} remains below the convergence "
                    "band around the reference.",
                ),
            )
        # All three sell paths share one swap-into-USDC execution model.
        swap_plan = self._swap_plan(
            SwapDirection.SELL_STOCK,
            inventory.stock_quantity * observation.amm_price_usdc,
            observation.pool_depth_usd,
        )
        gas_units, gas_cost_usd = self._batch_gas(
            observation, self._parameters.inventory_sell_gas_units
        )
        next_state = state.model_copy(update={"held_inventory": None})
        return PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.SELL_INVENTORY,
                reason=reason,
                diagnostics=trigger + self._gas_diagnostics(gas_units, gas_cost_usd),
                swap_plan=swap_plan,
                estimated_gas_units=gas_units,
                estimated_gas_cost_usd=gas_cost_usd,
            ),
            next_state=next_state,
        )

    def _dislocation_outcome(
        self,
        state: PolicyState,
        observation: PolicyObservation,
    ) -> PolicyOutcome | None:
        """Evaluate the underlying dislocation monitor against one open position.

        Comparisons require a reference at least as fresh as the entry bound;
        between that bound and the open-position bound the position rides the
        ordinary lifecycle without dislocation actions.

        Args:
            state: Day-rolled state carrying the open position.
            observation: The current injected observation.

        Returns:
            A stale-high or stale-low outcome, or None when no dislocation fires.

        Raises:
            ValueError: If the state carries no open position.
        """
        if not observation.reference_enforcement_enabled:
            return None
        reference = observation.reference_price_usdc
        if reference is None or self._reference_stale(observation):
            return None
        position = state.position
        if position is None:  # pragma: no cover - guarded by the caller
            raise ValueError("dislocation monitor requires an open position")
        threshold = self._parameters.dislocation_threshold_fraction
        if observation.amm_price_usdc >= reference * (Decimal(1) + threshold):
            # Stale-high includes the crash-anticipation case where the real
            # market shows a large imminent loss Aerodrome has not reflected;
            # selling on the AMM captures the better-than-real price.
            return self._safety_exit(
                state,
                observation,
                PolicyActionKind.DISLOCATION_EXIT,
                PolicyReason.DISLOCATION_STALE_HIGH_TRIGGERED,
                (
                    f"Pool price {observation.amm_price_usdc} is at least "
                    f"{threshold} above the reference {reference}.",
                    "Selling on the AMM while it still prices the stock above the "
                    "real market; never deferred by the gas gate.",
                ),
                with_cooldown=False,
            )
        if observation.amm_price_usdc <= reference * (Decimal(1) - threshold):
            # Stale-low: selling into the pool realizes the wrong price, so the
            # position burns and the stock tokens wait outside the pool.
            stock_quantity = self._stock_quantity(position, observation.amm_price_usdc)
            gas_units, gas_cost_usd = self._batch_gas(
                observation, self._parameters.exit_batch_gas_units
            )
            if stock_quantity <= 0:
                # An above-range composition is already all USDC, so the burn
                # completes the exit without any inventory to hold.
                next_state = state.model_copy(update={"position": None})
                return PolicyOutcome(
                    decision=PolicyDecision(
                        action=PolicyActionKind.STALE_LOW_BURN,
                        reason=PolicyReason.DISLOCATION_STALE_LOW_TRIGGERED,
                        diagnostics=(
                            f"Pool price {observation.amm_price_usdc} is at least "
                            f"{threshold} below the reference {reference}.",
                            "The position is entirely USDC above its range, so the "
                            "burn completes the exit with no stock inventory to hold.",
                        )
                        + self._gas_diagnostics(gas_units, gas_cost_usd),
                        estimated_gas_units=gas_units,
                        estimated_gas_cost_usd=gas_cost_usd,
                    ),
                    next_state=next_state,
                )
            held_inventory = HeldInventory(
                pool_address=position.pool_address,
                token_address=position.token_address,
                stock_quantity=stock_quantity,
                held_since=observation.observed_at,
            )
            next_state = state.model_copy(
                update={"position": None, "held_inventory": held_inventory}
            )
            return PolicyOutcome(
                decision=PolicyDecision(
                    action=PolicyActionKind.STALE_LOW_BURN,
                    reason=PolicyReason.DISLOCATION_STALE_LOW_TRIGGERED,
                    diagnostics=(
                        f"Pool price {observation.amm_price_usdc} is at least "
                        f"{threshold} below the reference {reference}.",
                        "Burning and holding the stock tokens unsold because selling "
                        "into the stale-low pool realizes the wrong price; the "
                        "reference market is treated as the true price.",
                    )
                    + self._gas_diagnostics(gas_units, gas_cost_usd),
                    estimated_gas_units=gas_units,
                    estimated_gas_cost_usd=gas_cost_usd,
                ),
                next_state=next_state,
            )
        return None

    def _safety_exit(
        self,
        state: PolicyState,
        observation: PolicyObservation,
        action: PolicyActionKind,
        reason: PolicyReason,
        diagnostics: tuple[str, ...],
        with_cooldown: bool,
    ) -> PolicyOutcome:
        """Build one burn-and-swap-to-USDC safety exit with execution models.

        Args:
            state: Day-rolled state carrying the open position.
            observation: The current injected observation.
            action: Exit action kind to emit.
            reason: Stable trigger reason for the exit.
            diagnostics: Trigger evidence; gas evidence is appended.
            with_cooldown: Whether the exit arms the re-entry cooldown.

        Returns:
            The exit decision plus the successor flat state.

        Raises:
            ValueError: If the state carries no open position.
        """
        position = state.position
        if position is None:  # pragma: no cover - guarded by the caller
            raise ValueError("safety exit requires an open position")
        # The composition rule turns the position into its stock quantity.
        stock_quantity = self._stock_quantity(position, observation.amm_price_usdc)
        # Only a positive stock inventory needs a swap back to USDC.
        swap_plan = (
            None
            if stock_quantity <= 0
            else self._swap_plan(
                SwapDirection.SELL_STOCK,
                stock_quantity * observation.amm_price_usdc,
                observation.pool_depth_usd,
            )
        )
        gas_units, gas_cost_usd = self._batch_gas(
            observation, self._parameters.exit_batch_gas_units
        )
        next_state = state.model_copy(
            update={
                "position": None,
                "reentry_blocked_until": (
                    observation.observed_at + self._parameters.reentry_cooldown
                    if with_cooldown
                    else None
                ),
            }
        )
        return PolicyOutcome(
            decision=PolicyDecision(
                action=action,
                reason=reason,
                diagnostics=diagnostics + self._gas_diagnostics(gas_units, gas_cost_usd),
                swap_plan=swap_plan,
                estimated_gas_units=gas_units,
                estimated_gas_cost_usd=gas_cost_usd,
            ),
            next_state=next_state,
        )

    def position_composition(
        self,
        position: PolicyPosition,
        amm_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Calculate the position's stock and USDC quantities at one pool price.

        The v3-style composition is exact for a range entered at its geometric
        center, which is how the engine builds every range: liquidity follows
        from the committed value at the center, the stock side spans the
        evaluated-to-upper square-root-price band, and the USDC side spans the
        lower-to-evaluated band.

        Args:
            position: Open position with its aligned range and committed value.
            amm_price: Positive pool price in USDC per stock.

        Returns:
            The (stock quantity, USDC quantity) pair held at this price; the
            stock side is zero above the range and the USDC side is zero below.

        Raises:
            ValueError: If the pool price is not positive.
        """
        if amm_price <= 0:
            raise ValueError("amm_price must be positive")
        with localcontext() as decimal_context:
            # Local precision isolates deterministic composition math from settings.
            decimal_context.prec = MATH_PRECISION
            lower = position.price_range.lower_price
            upper = position.price_range.upper_price
            sqrt_lower = lower.sqrt()
            sqrt_upper = upper.sqrt()
            # The center root is where the value splits evenly between assets.
            sqrt_center = (sqrt_lower * sqrt_upper).sqrt()
            # Committed value at the center implies the position's liquidity.
            liquidity = position.committed_usd / (Decimal(2) * (sqrt_center - sqrt_lower))
            sqrt_price = amm_price.sqrt()
            if amm_price <= lower:
                # Below the range the position is entirely stock tokens.
                stock_quantity = liquidity * (Decimal(1) / sqrt_lower - Decimal(1) / sqrt_upper)
                usdc_quantity = Decimal(0)
            elif amm_price < upper:
                # In range the stock side covers the current-to-upper band and
                # the USDC side covers the lower-to-current band.
                stock_quantity = liquidity * (Decimal(1) / sqrt_price - Decimal(1) / sqrt_upper)
                usdc_quantity = liquidity * (sqrt_price - sqrt_lower)
            else:
                # Above the range the position is entirely USDC.
                stock_quantity = Decimal(0)
                usdc_quantity = liquidity * (sqrt_upper - sqrt_lower)
            return +stock_quantity, +usdc_quantity

    def _stock_quantity(self, position: PolicyPosition, amm_price: Decimal) -> Decimal:
        """Calculate the position's stock inventory at one pool price.

        Args:
            position: Open position with its aligned range and committed value.
            amm_price: Positive pool price in USDC per stock.

        Returns:
            The stock token quantity held at this price; zero above the range.
        """
        # The stock side of the shared composition rule feeds every exit path.
        return self.position_composition(position, amm_price)[0]

    def _swap_plan(
        self,
        direction: SwapDirection,
        total_usd: Decimal,
        route_depth_usd: Decimal,
    ) -> SwapPlan:
        """Model one swap against observed depth with tranche splitting.

        The impact model is deliberately simple and conservative: a swap of one
        percent of the executable depth is treated as one percent of price
        impact, so tranches split no later than a curve model would require.

        Args:
            direction: Side of the pool the swap executes on.
            total_usd: Positive whole US-dollar value to convert.
            route_depth_usd: Observed executable depth of the swap route.

        Returns:
            The tranche-split plan; one whole-size tranche when depth is zero
            or too small to split, with unmodeled or ceiling-breaking impact.
        """
        with localcontext() as decimal_context:
            # Local precision keeps tranche arithmetic deterministic.
            decimal_context.prec = MATH_PRECISION
            max_tranche_usd = route_depth_usd * self._parameters.swap_impact_tranche_fraction
            # The tranche count keeps every slice at or below the split bound,
            # so the whole plan sits well under the hard impact ceiling.
            tranche_count = (
                int((total_usd / max_tranche_usd).to_integral_value(rounding=ROUND_CEILING))
                if max_tranche_usd > 0
                else 0
            )
            # Tranche sizes floor to USDC's six decimals so slices stay exactly
            # representable; a nonpositive or runaway count means the observed
            # depth cannot support splitting, so the whole swap models as one
            # tranche whose impact speaks for itself against the ceiling.
            tranche_size = (
                (total_usd / Decimal(tranche_count)).quantize(USDC_QUANTUM, rounding=ROUND_FLOOR)
                if tranche_count > 0
                else Decimal(0)
            )
            if tranche_count > MAX_SWAP_TRANCHES or tranche_size <= 0:
                fallback_impact = None if route_depth_usd <= 0 else total_usd / route_depth_usd
                return SwapPlan(
                    direction=direction,
                    total_usd=total_usd,
                    route_depth_usd=route_depth_usd,
                    tranches=(
                        SwapTranche(usd_size=total_usd, modeled_impact_fraction=fallback_impact),
                    ),
                    max_modeled_impact_fraction=fallback_impact,
                )
            # The final tranche absorbs the flooring remainder exactly so the
            # tranche sizes sum to the plan total.
            tranche_sizes = [tranche_size] * (tranche_count - 1) + [
                total_usd - tranche_size * Decimal(tranche_count - 1)
            ]
            tranche_impacts = [size / route_depth_usd for size in tranche_sizes]
            return SwapPlan(
                direction=direction,
                total_usd=total_usd,
                route_depth_usd=route_depth_usd,
                tranches=tuple(
                    SwapTranche(usd_size=size, modeled_impact_fraction=impact)
                    for size, impact in zip(tranche_sizes, tranche_impacts, strict=True)
                ),
                max_modeled_impact_fraction=max(tranche_impacts),
            )

    def _batch_gas(
        self,
        observation: PolicyObservation,
        action_gas_units: int,
    ) -> tuple[int, Decimal | None]:
        """Estimate one batch's gas units and US-dollar cost.

        Args:
            observation: The current injected observation.
            action_gas_units: Protocol-side gas estimate for the batch body.

        Returns:
            Total units including Safe proxy overhead, and the USD cost under
            the documented ETH price assumption; the cost is None when the gas
            price reading is unavailable.
        """
        total_units = action_gas_units + self._parameters.safe_overhead_gas_per_batch
        if observation.gas_price_gwei is None:
            # An unavailable reading leaves the cost unknown; safety exits still
            # proceed because gap risk dominates an unknown gas cost.
            return total_units, None
        cost_usd = (
            Decimal(total_units)
            * observation.gas_price_gwei
            / GWEI_PER_ETH
            * self._parameters.eth_price_assumption_usd
        )
        return total_units, cost_usd

    def _gas_gate_blocks(
        self,
        observation: PolicyObservation,
        action_gas_units: int,
        position_value_usd: Decimal,
    ) -> tuple[bool, tuple[str, ...]]:
        """Evaluate the gas sense-check gate for one non-urgent action.

        Args:
            observation: The current injected observation.
            action_gas_units: Protocol-side gas estimate for the batch body.
            position_value_usd: Position value whose expected daily gross yield
                anchors the cost comparison.

        Returns:
            True with deferral evidence when the action must wait, else False
            with empty diagnostics.
        """
        gas_price = observation.gas_price_gwei
        if gas_price is None:
            # Fail closed: an unreadable gas price defers non-urgent actions.
            return True, (
                "Base gas price reading is unavailable; deferring the non-urgent "
                "action fail-closed.",
            )
        if gas_price > self._parameters.gas_price_ceiling_gwei:
            return True, (
                f"L2 gas price {gas_price} gwei exceeds the "
                f"{self._parameters.gas_price_ceiling_gwei} gwei ceiling.",
            )
        total_units = action_gas_units + self._parameters.safe_overhead_gas_per_batch
        cost_usd = (
            Decimal(total_units)
            * gas_price
            / GWEI_PER_ETH
            * self._parameters.eth_price_assumption_usd
        )
        # Expected daily gross yield credits raw emissions plus fees on top.
        expected_daily_gross_yield = (
            position_value_usd * (observation.emissions_apr + observation.fee_apr) / DAYS_PER_YEAR
        )
        if cost_usd > self._parameters.gas_cost_max_gross_yield_fraction * (
            expected_daily_gross_yield
        ):
            return True, (
                f"Estimated batch cost {cost_usd} USDC exceeds "
                f"{self._parameters.gas_cost_max_gross_yield_fraction} of the expected "
                f"daily gross yield {expected_daily_gross_yield} USDC.",
            )
        return False, ()

    def _gas_diagnostics(
        self,
        gas_units: int,
        gas_cost_usd: Decimal | None,
    ) -> tuple[str, ...]:
        """Describe one batch's gas evidence for a decision's diagnostics.

        Args:
            gas_units: Estimated total batch gas units.
            gas_cost_usd: Estimated batch cost, or None when unknown.

        Returns:
            A one-line gas evidence tuple.
        """
        if gas_cost_usd is None:
            return (f"Estimated batch gas {gas_units} units at an unavailable gas price.",)
        return (
            f"Estimated batch gas {gas_units} units at {gas_cost_usd} USDC under the "
            f"{self._parameters.eth_price_assumption_usd} USDC-per-ETH assumption.",
        )

    def _reference_beyond_open_bound(self, observation: PolicyObservation) -> bool:
        """Check the fail-closed defensive-exit staleness bound while open.

        Args:
            observation: The current injected observation.

        Returns:
            True when the reference is missing or older than the open-position
            bound.
        """
        if not observation.reference_enforcement_enabled:
            return False
        if observation.reference_price_usdc is None or observation.reference_age_seconds is None:
            return True
        return observation.reference_age_seconds > (
            self._parameters.reference_open_position_max_age_seconds
        )

    def _decide_flat(
        self,
        state: PolicyState,
        observation: PolicyObservation,
        flat_description: str | None,
    ) -> PolicyOutcome:
        """Apply the ordered entry gates while the policy holds only USDC.

        Args:
            state: Day-rolled state with no open position.
            observation: The current injected observation.
            flat_description: Description of the active window or flat condition.

        Returns:
            The entry decision or blocking hold and the successor state.
        """
        # A plain hold carries the day-rolled state forward unchanged.
        hold_state = state
        # The daily loss halt blocks only new entries; safety exits above stay armed.
        if state.halted_day == state.day:
            return self._hold(
                hold_state,
                PolicyReason.DAILY_LOSS_HALT_ACTIVE,
                (
                    f"Marked equity {observation.equity_usd} fell at least "
                    f"{self._parameters.daily_loss_halt_fraction} below the day-start "
                    f"equity {state.day_start_equity_usd}; no new entries until the "
                    f"next America/New_York day.",
                ),
            )
        # Stop and dilution exits block re-entry through their cooldown.
        if (
            state.reentry_blocked_until is not None
            and observation.observed_at < state.reentry_blocked_until
        ):
            return self._hold(
                hold_state,
                PolicyReason.ENTRY_COOLDOWN_ACTIVE,
                (f"Re-entry cooldown runs until {state.reentry_blocked_until}.",),
            )
        # Condition-driven flat events require being flat.
        if flat_description is not None:
            return self._hold(
                hold_state,
                PolicyReason.EVENT_WINDOW_FLAT,
                (flat_description,),
            )
        # A missing, or older-than-bound, reference quote blocks new entries.
        if self._reference_stale(observation):
            return self._hold(
                hold_state,
                PolicyReason.REFERENCE_STALE,
                (
                    "Underlying reference quote is missing or older than the "
                    f"{self._parameters.reference_max_age_seconds}-second bound.",
                ),
            )
        # The gate reads the raw emissions APR; fees are credited on top.
        if observation.emissions_apr < self._parameters.min_entry_emissions_apr:
            return self._hold(
                hold_state,
                PolicyReason.EMISSIONS_BELOW_ENTRY_THRESHOLD,
                (
                    f"Raw emissions APR {observation.emissions_apr} is below the "
                    f"entry threshold {self._parameters.min_entry_emissions_apr}.",
                ),
            )
        # Size is the smaller of the equity cap and the pool depth hard gate.
        equity_cap = observation.equity_usd * self._parameters.max_position_equity_fraction
        depth_cap = observation.pool_depth_usd * self._parameters.max_position_depth_fraction
        size_usd = min(equity_cap, depth_cap)
        if size_usd <= 0:
            return self._hold(
                hold_state,
                PolicyReason.ENTRY_SIZE_EMPTY,
                (
                    f"Equity cap {equity_cap} and depth cap {depth_cap} leave no "
                    "positive position size.",
                ),
            )
        # The gas sense-check gate defers non-urgent entries fail-closed.
        deferred, defer_diagnostics = self._gas_gate_blocks(
            observation, self._parameters.enter_batch_gas_units, size_usd
        )
        if deferred:
            return self._hold(hold_state, PolicyReason.GAS_GATE_DEFERRED, defer_diagnostics)
        # The entry width is derived from the target net daily yield behind the
        # coarse APR gate, then the range is built around the observed price.
        width_solution = self._solve_range_width(observation, size_usd)
        entry_range = self.build_aligned_range(
            observation.amm_price_usdc, width_solution.half_width_fraction
        )
        gas_units, gas_cost_usd = self._batch_gas(
            observation, self._parameters.enter_batch_gas_units
        )
        # Minting at the range center needs roughly half the committed value in
        # stock, so the entry swap buys that half with USDC.
        swap_plan = self._swap_plan(
            SwapDirection.BUY_STOCK, size_usd / Decimal(2), observation.pool_depth_usd
        )
        position = PolicyPosition(
            pool_address=observation.pool_address,
            token_address=observation.token_address,
            price_range=entry_range,
            committed_usd=size_usd,
            entered_at=observation.observed_at,
        )
        next_state = state.model_copy(update={"position": position})
        return PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.ENTER,
                reason=PolicyReason.ENTRY_THRESHOLD_MET,
                diagnostics=(
                    f"Raw emissions APR {observation.emissions_apr} meets the entry "
                    f"threshold {self._parameters.min_entry_emissions_apr}.",
                    f"Size {size_usd} USDC is min(equity cap {equity_cap}, depth cap {depth_cap}).",
                    f"Range {entry_range.lower_price}..{entry_range.upper_price} "
                    f"USDC per stock around pool price {observation.amm_price_usdc} "
                    f"aligned to tick spacing {self._parameters.tick_spacing}.",
                    f"Range half width {width_solution.half_width_fraction} "
                    f"({width_solution.half_width_ticks} ticks per side) derived "
                    f"against the target net daily yield "
                    f"{self._parameters.target_net_daily_yield}; solve resolved as "
                    f"{width_solution.mode.value}.",
                )
                + width_solution.diagnostics
                + self._gas_diagnostics(gas_units, gas_cost_usd),
                price_range=entry_range,
                size_usd=size_usd,
                swap_plan=swap_plan,
                estimated_gas_units=gas_units,
                estimated_gas_cost_usd=gas_cost_usd,
                width_solution=width_solution,
            ),
            next_state=next_state,
        )

    def _reference_stale(self, observation: PolicyObservation) -> bool:
        """Check the fail-closed reference quote staleness gate.

        Args:
            observation: The current injected observation.

        Returns:
            True when the reference quote is missing or older than the bound.
        """
        # Scheduled production cycles use the resolved Aerodrome pool as the
        # trading authority; an external quote is then diagnostic only.
        if not observation.reference_enforcement_enabled:
            return False
        # A missing quote or age is treated as unavailable, never as fresh.
        if observation.reference_price_usdc is None or observation.reference_age_seconds is None:
            return True
        return observation.reference_age_seconds > self._parameters.reference_max_age_seconds

    def _hold(
        self,
        state: PolicyState,
        reason: PolicyReason,
        diagnostics: tuple[str, ...],
    ) -> PolicyOutcome:
        """Build a plain hold outcome that threads the state forward unchanged.

        Args:
            state: Day-rolled state to carry forward.
            reason: Stable reason code for the hold.
            diagnostics: Numeric evidence explaining the blocking gate.

        Returns:
            A hold decision paired with the unchanged successor state.
        """
        return PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.HOLD,
                reason=reason,
                diagnostics=diagnostics,
            ),
            next_state=state,
        )
