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


class PolicyParameters(BaseModel):
    """Lock the v1 emissions-farming parameters as one immutable decision input."""

    # Frozen strict fields make the locked parameter set reproducible in audits.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Half width is 0.3 percent on each side of the reference price.
    range_half_width_fraction: Decimal = Decimal("0.003")
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
    # A position commits at most twenty percent of current equity.
    max_position_equity_fraction: Decimal = Decimal("0.20")
    # A position is hard-gated to one percent of observed pool depth.
    max_position_depth_fraction: Decimal = Decimal("0.01")
    # A five percent same-day equity loss halts new entries until the next day.
    daily_loss_halt_fraction: Decimal = Decimal("0.05")
    # The underlying reference quote blocks entries when older than this bound.
    reference_max_age_seconds: Annotated[int, Field(ge=0)] = 300

    @model_validator(mode="after")
    def require_unit_fractions(self) -> Self:
        """Reject fraction parameters outside their meaningful intervals."""
        if not Decimal(0) < self.range_half_width_fraction < Decimal(1):
            raise ValueError("range_half_width_fraction must be between zero and one")
        if not Decimal(0) < self.stop_buffer_fraction < Decimal(1):
            raise ValueError("stop_buffer_fraction must be between zero and one")
        if not Decimal(0) < self.max_position_equity_fraction <= Decimal(1):
            raise ValueError("max_position_equity_fraction must be in (0, 1]")
        if not Decimal(0) < self.max_position_depth_fraction <= Decimal(1):
            raise ValueError("max_position_depth_fraction must be in (0, 1]")
        if not Decimal(0) < self.daily_loss_halt_fraction < Decimal(1):
            raise ValueError("daily_loss_halt_fraction must be between zero and one")
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
    # Pool depth is the executable US-dollar depth used by the position gate.
    pool_depth_usd: NonNegativeDecimal
    # Equity is the caller's current marked total portfolio value in USDC.
    equity_usd: Annotated[Decimal, Field(gt=0)]
    # Reference price is the keyless real-market quote in USDC per stock.
    reference_price_usdc: Annotated[Decimal, Field(gt=0)] | None = None
    # Reference age is seconds since that quote; absence means no live quote.
    reference_age_seconds: Annotated[int, Field(ge=0)] | None = None
    # A stale oracle observation is a condition-driven flat event per policy.
    oracle_stale: bool = False
    # A paused B20 registry is a condition-driven flat event per policy.
    registry_paused: bool = False

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
    # A scheduled or condition-driven event window requires being flat.
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
    # A scheduled or condition-driven event window arrived while a position
    # was open.
    EVENT_EXIT_TRIGGERED = "event_exit_triggered"


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


class PolicyOutcome(BaseModel):
    """Pair one decision with the successor state the caller must thread forward."""

    # Frozen strict fields keep the decision and state transition inseparable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Decision is the immutable action chosen for this observation.
    decision: PolicyDecision
    # Next state is the complete engine state after applying the decision.
    next_state: PolicyState


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

        Decision precedence is fixed: safety exits first (downside stop, then
        emissions dilution, then event windows), then position maintenance
        (upside recenter wait), then the ordered entry gates.

        Args:
            state: Engine state threaded from the previous decision.
            observation: One coherent injected observation for one pool.

        Returns:
            The typed decision plus the successor state to thread forward.
        """
        # Day rollover and the daily loss halt are evaluated before any gate.
        working_state = self._observe_day(state, observation)
        # Condition-driven flat events combine with scheduled event windows.
        flat_reason = self._flat_reason(observation)
        window_view = evaluate_event_window(
            observation.observed_at, observation.token_address, self._calendar
        )
        # A description exists only when some window or condition is active.
        if flat_reason is not None:
            flat_description: str | None = flat_reason
        elif window_view.active:
            flat_description = window_view.description
        else:
            flat_description = None

        if working_state.position is not None:
            return self._decide_with_position(working_state, observation, flat_description)
        return self._decide_flat(working_state, observation, flat_description)

    def build_aligned_range(self, center_price: Decimal) -> AlignedPriceRange:
        """Build the locked width range around one price, aligned to the tick grid.

        Args:
            center_price: Positive pool price in USDC per stock.

        Returns:
            The grid-aligned range spanning at least the locked half width.

        Raises:
            ValueError: If the center price is not positive.
        """
        if center_price <= 0:
            raise ValueError("center_price must be positive")
        with localcontext() as decimal_context:
            # Local precision isolates deterministic tick logarithms from settings.
            decimal_context.prec = MATH_PRECISION
            # The tick index of a price is its natural logarithm over log(1.0001).
            tick_log = TICK_PRICE_RATIO.ln()
            # Raw bounds apply the locked half width on each side of the center.
            raw_lower_tick = (
                center_price * (Decimal(1) - self._parameters.range_half_width_fraction)
            ).ln() / tick_log
            raw_upper_tick = (
                center_price * (Decimal(1) + self._parameters.range_half_width_fraction)
            ).ln() / tick_log
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
        # The stop level sits 0.5 percent below the aligned lower range edge.
        stop_level = position.price_range.lower_price * (
            Decimal(1) - self._parameters.stop_buffer_fraction
        )
        # Safety exits are evaluated in fixed order and are never deferred.
        if observation.amm_price_usdc <= stop_level:
            diagnostics: tuple[str, ...] = (
                f"Pool price {observation.amm_price_usdc} reached the stop level "
                f"{stop_level} at 0.5 percent below the lower range edge "
                f"{position.price_range.lower_price}.",
                "Exit path burns the position and swaps all inventory back to USDC.",
            )
            next_state = state.model_copy(
                update={
                    "position": None,
                    "reentry_blocked_until": (
                        observation.observed_at + self._parameters.reentry_cooldown
                    ),
                }
            )
            return PolicyOutcome(
                decision=PolicyDecision(
                    action=PolicyActionKind.STOP_OUT,
                    reason=PolicyReason.DOWNSIDE_STOP_TRIGGERED,
                    diagnostics=diagnostics,
                ),
                next_state=next_state,
            )
        # Other LPs adding sticky staked liquidity persistently lowers the
        # emissions APR per unit of staked liquidity, so the gate is re-checked
        # at every observation while open.
        if observation.emissions_apr < self._parameters.min_entry_emissions_apr:
            diagnostics = (
                f"Raw emissions APR {observation.emissions_apr} fell below the entry "
                f"threshold {self._parameters.min_entry_emissions_apr} while open.",
                "Exit path burns the position and swaps all inventory back to USDC.",
            )
            next_state = state.model_copy(
                update={
                    "position": None,
                    "reentry_blocked_until": (
                        observation.observed_at + self._parameters.reentry_cooldown
                    ),
                }
            )
            return PolicyOutcome(
                decision=PolicyDecision(
                    action=PolicyActionKind.DILUTION_EXIT,
                    reason=PolicyReason.DILUTION_EXIT_TRIGGERED,
                    diagnostics=diagnostics,
                ),
                next_state=next_state,
            )
        # Event windows require being flat in USDC while they are active.
        if flat_description is not None:
            diagnostics = (
                flat_description,
                "Exit path burns the position and swaps all inventory back to USDC.",
            )
            next_state = state.model_copy(update={"position": None})
            return PolicyOutcome(
                decision=PolicyDecision(
                    action=PolicyActionKind.EVENT_EXIT,
                    reason=PolicyReason.EVENT_EXIT_TRIGGERED,
                    diagnostics=diagnostics,
                ),
                next_state=next_state,
            )
        # Upside out-of-range starts a time-based wait before any recenter.
        if observation.amm_price_usdc >= position.price_range.upper_price:
            wait_anchor = position.out_of_range_since or observation.observed_at
            waited = observation.observed_at - wait_anchor
            if waited >= self._parameters.recenter_wait:
                # The recenter range is rebuilt around the current pool price.
                new_range = self.build_aligned_range(observation.amm_price_usdc)
                diagnostics = (
                    f"Upside out-of-range wait of {waited} elapsed the locked "
                    f"recenter wait {self._parameters.recenter_wait}.",
                    f"New range {new_range.lower_price}..{new_range.upper_price} "
                    f"USDC per stock around pool price {observation.amm_price_usdc}.",
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
        # Scheduled and condition-driven event windows require being flat.
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
        # The entry range is built around the observed pool price.
        entry_range = self.build_aligned_range(observation.amm_price_usdc)
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
                ),
                price_range=entry_range,
                size_usd=size_usd,
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
