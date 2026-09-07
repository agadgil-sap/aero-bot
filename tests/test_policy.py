"""Behavior tests for the pure emissions-farming policy decision engine."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aero_bot.policy import (
    NEW_YORK,
    AlignedPriceRange,
    EventCalendar,
    PolicyActionKind,
    PolicyEngine,
    PolicyObservation,
    PolicyParameters,
    PolicyPosition,
    PolicyReason,
    PolicyState,
    ScheduledEvent,
    evaluate_event_window,
    load_event_calendar,
    parse_event_calendar,
)

# A deterministic fixture address represents one B20 stock contract.
TOKEN_ADDRESS = "0x1111111111111111111111111111111111111111"
# A second fixture address represents a different B20 stock contract.
OTHER_TOKEN_ADDRESS = "0x3333333333333333333333333333333333333333"
# A deterministic fixture address represents one Aerodrome Slipstream pool.
POOL_ADDRESS = "0x2222222222222222222222222222222222222222"
# Wednesday 2026-08-19 at 11:00 New York sits outside every event window.
BASE_OBSERVED_AT = datetime(2026, 8, 19, 11, 0, tzinfo=NEW_YORK)


def base_observation(**overrides: object) -> PolicyObservation:
    """Build a passing observation fixture and apply explicit per-test overrides.

    Args:
        **overrides: Observation fields changed to exercise one policy gate.

    Returns:
        A validated immutable observation.
    """
    # Base values pass every entry gate at the locked parameters.
    values: dict[str, object] = {
        "observed_at": BASE_OBSERVED_AT,
        "pool_address": POOL_ADDRESS,
        "token_address": TOKEN_ADDRESS,
        "amm_price_usdc": Decimal("200"),
        "emissions_apr": Decimal("1.5"),
        "pool_depth_usd": Decimal("10000"),
        "equity_usd": Decimal("200"),
        "reference_price_usdc": Decimal("200"),
        "reference_age_seconds": 10,
    }
    values.update(overrides)
    return PolicyObservation.model_validate(values)


def entered_session(**overrides: object) -> tuple[PolicyEngine, PolicyState]:
    """Drive one observation through the engine to produce an open position.

    Args:
        **overrides: Observation overrides for the entering observation.

    Returns:
        The engine and the state carrying the freshly entered position.
    """
    engine = PolicyEngine()
    outcome = engine.decide(PolicyState(), base_observation(**overrides))
    assert outcome.decision.action is PolicyActionKind.ENTER
    return engine, outcome.next_state


def entered_position_for(state: PolicyState) -> PolicyPosition:
    """Return the open position carried by one entered state.

    Args:
        state: State produced by a successful entry decision.

    Returns:
        The asserted non-None open position.
    """
    position = state.position
    assert position is not None
    return position


def stop_level_for(state: PolicyState) -> Decimal:
    """Return the downside stop level implied by one entered state's range.

    Args:
        state: State carrying an open position.

    Returns:
        The price 0.5 percent below the aligned lower range edge.
    """
    return entered_position_for(state).price_range.lower_price * Decimal("0.995")


class TestEventCalendar:
    """Calendar parsing and window evaluation behavior."""

    def test_parse_event_calendar_loads_scheduled_events(self) -> None:
        """Naive TOML timestamps become New York instants and token scope parses."""
        document = (
            "[[event]]\n"
            'kind = "earnings"\n'
            'scheduled_at = "2026-08-19T12:00:00"\n'
            f'token_address = "{TOKEN_ADDRESS}"\n'
            "\n"
            "[[event]]\n"
            'kind = "ex_dividend"\n'
            'scheduled_at = "2026-08-20T09:30:00-04:00"\n'
        )
        calendar = parse_event_calendar(document)
        assert len(calendar.events) == 2
        assert calendar.events[0].scheduled_at == datetime(2026, 8, 19, 12, 0, tzinfo=NEW_YORK)
        assert calendar.events[0].token_address == TOKEN_ADDRESS
        assert calendar.events[1].token_address is None

    def test_parse_event_calendar_rejects_unknown_kind(self) -> None:
        """An unreviewed event kind fails closed at parse time."""
        document = '[[event]]\nkind = "party"\nscheduled_at = "2026-08-19T12:00:00"\n'
        with pytest.raises(ValidationError):
            parse_event_calendar(document)

    def test_bundled_event_calendar_starts_empty(self) -> None:
        """The packaged calendar resource parses to zero scheduled events."""
        assert load_event_calendar().events == ()

    def test_scheduled_event_accepts_native_aware_datetime(self) -> None:
        """A native aware datetime input passes through without relocalization."""
        event = ScheduledEvent.model_validate(
            {
                "kind": "earnings",
                "scheduled_at": datetime(2026, 8, 19, 16, 0, tzinfo=UTC),
            }
        )
        assert event.scheduled_at.utcoffset() == UTC.utcoffset(None)

    def test_market_open_and_close_windows(self) -> None:
        """Weekday session windows span 60 minutes before through 30 minutes after."""
        calendar = EventCalendar()
        before_open = evaluate_event_window(
            datetime(2026, 8, 19, 8, 29, tzinfo=NEW_YORK), TOKEN_ADDRESS, calendar
        )
        inside_open = evaluate_event_window(
            datetime(2026, 8, 19, 9, 0, tzinfo=NEW_YORK), TOKEN_ADDRESS, calendar
        )
        open_ended = evaluate_event_window(
            datetime(2026, 8, 19, 10, 0, tzinfo=NEW_YORK), TOKEN_ADDRESS, calendar
        )
        inside_close = evaluate_event_window(
            datetime(2026, 8, 19, 15, 30, tzinfo=NEW_YORK), TOKEN_ADDRESS, calendar
        )
        close_ended = evaluate_event_window(
            datetime(2026, 8, 19, 16, 30, tzinfo=NEW_YORK), TOKEN_ADDRESS, calendar
        )
        assert before_open.active is False
        assert inside_open.active is True
        assert "market open" in inside_open.description
        assert open_ended.active is False
        assert inside_close.active is True
        assert "market close" in inside_close.description
        assert close_ended.active is False

    def test_market_windows_inactive_on_weekend(self) -> None:
        """Session windows apply only on weekday trading days."""
        saturday_open_time = evaluate_event_window(
            datetime(2026, 8, 22, 9, 0, tzinfo=NEW_YORK), TOKEN_ADDRESS, EventCalendar()
        )
        assert saturday_open_time.active is False

    def test_scheduled_event_window_is_token_scoped(self) -> None:
        """A token-scoped event flats only its own pool while unscoped events flat all."""
        calendar = parse_event_calendar(
            "[[event]]\n"
            'kind = "earnings"\n'
            'scheduled_at = "2026-08-19T12:00:00"\n'
            f'token_address = "{TOKEN_ADDRESS}"\n'
        )
        scoped = evaluate_event_window(
            datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK), TOKEN_ADDRESS, calendar
        )
        other = evaluate_event_window(
            datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK), OTHER_TOKEN_ADDRESS, calendar
        )
        assert scoped.active is True
        assert "earnings" in scoped.description
        assert other.active is False


class TestLockedParameters:
    """Locked parameter defaults and validation boundaries."""

    def test_default_parameters_match_the_locked_policy(self) -> None:
        """The immutable defaults encode every locked v1 constant."""
        parameters = PolicyParameters()
        assert parameters.range_half_width_fraction == Decimal("0.003")
        assert parameters.tick_spacing == 10
        assert parameters.recenter_wait.total_seconds() == 15 * 60
        assert parameters.stop_buffer_fraction == Decimal("0.005")
        assert parameters.reentry_cooldown.total_seconds() == 15 * 60
        assert parameters.min_entry_emissions_apr == Decimal("1.5")
        assert parameters.max_position_equity_fraction == Decimal("0.20")
        assert parameters.max_position_depth_fraction == Decimal("0.01")
        assert parameters.daily_loss_halt_fraction == Decimal("0.05")
        assert parameters.reference_max_age_seconds == 300

    def test_out_of_range_fraction_parameters_are_rejected(self) -> None:
        """Fraction parameters outside their meaningful intervals fail closed."""
        with pytest.raises(ValidationError):
            PolicyParameters(range_half_width_fraction=Decimal("1.5"))
        with pytest.raises(ValidationError):
            PolicyParameters(range_half_width_fraction=Decimal("0"))
        with pytest.raises(ValidationError):
            PolicyParameters(stop_buffer_fraction=Decimal("0"))
        with pytest.raises(ValidationError):
            PolicyParameters(stop_buffer_fraction=Decimal("1"))
        with pytest.raises(ValidationError):
            PolicyParameters(max_position_equity_fraction=Decimal("1.5"))
        with pytest.raises(ValidationError):
            PolicyParameters(max_position_depth_fraction=Decimal("0"))
        with pytest.raises(ValidationError):
            PolicyParameters(daily_loss_halt_fraction=Decimal("0"))
        with pytest.raises(ValidationError):
            PolicyParameters(daily_loss_halt_fraction=Decimal("1"))


class TestRangeConstruction:
    """Tick-grid-aligned range construction behavior."""

    def test_range_aligns_to_tick_grid_and_spans_the_locked_width(self) -> None:
        """Aligned boundaries contain the raw bounds and land on spacing-ten ticks."""
        engine = PolicyEngine()
        center = Decimal("200")
        price_range = engine.build_aligned_range(center)
        assert price_range.lower_tick % 10 == 0
        assert price_range.upper_tick % 10 == 0
        assert price_range.lower_price <= center * Decimal("0.997")
        assert price_range.upper_price >= center * Decimal("1.003")
        assert price_range.lower_price < center < price_range.upper_price
        width_fraction = price_range.upper_price / price_range.lower_price - Decimal(1)
        assert width_fraction >= Decimal("0.006")

    def test_range_rejects_nonpositive_center(self) -> None:
        """A nonpositive center price cannot define a range."""
        with pytest.raises(ValueError, match="positive"):
            PolicyEngine().build_aligned_range(Decimal("0"))

    def test_collapsed_or_inverted_range_model_is_rejected(self) -> None:
        """A directly constructed collapsed or inverted range fails validation."""
        with pytest.raises(ValidationError):
            AlignedPriceRange(
                lower_tick=100,
                upper_tick=100,
                lower_price=Decimal("200"),
                upper_price=Decimal("201"),
            )
        with pytest.raises(ValidationError):
            AlignedPriceRange(
                lower_tick=100,
                upper_tick=110,
                lower_price=Decimal("201"),
                upper_price=Decimal("200"),
            )


class TestEntryGates:
    """Ordered entry-gate behavior while the policy is flat."""

    def test_engine_enters_when_emissions_threshold_is_met(self) -> None:
        """A raw emissions APR at exactly 150 percent enters at the capped size."""
        outcome = PolicyEngine().decide(PolicyState(), base_observation())
        assert outcome.decision.action is PolicyActionKind.ENTER
        assert outcome.decision.reason is PolicyReason.ENTRY_THRESHOLD_MET
        assert outcome.decision.size_usd == Decimal("40")
        assert outcome.decision.price_range is not None
        position = outcome.next_state.position
        assert position is not None
        assert position.committed_usd == Decimal("40")
        assert position.entered_at == BASE_OBSERVED_AT

    def test_entry_blocked_below_emissions_threshold(self) -> None:
        """A raw emissions APR just under the threshold holds in USDC."""
        outcome = PolicyEngine().decide(
            PolicyState(), base_observation(emissions_apr=Decimal("1.4999"))
        )
        assert outcome.decision.action is PolicyActionKind.HOLD
        assert outcome.decision.reason is PolicyReason.EMISSIONS_BELOW_ENTRY_THRESHOLD

    def test_entry_size_uses_the_smaller_equity_or_depth_cap(self) -> None:
        """A shallow pool sizes the position at one percent of observed depth."""
        depth_capped = PolicyEngine().decide(
            PolicyState(), base_observation(pool_depth_usd=Decimal("2000"))
        )
        assert depth_capped.decision.size_usd == Decimal("20")
        empty = PolicyEngine().decide(PolicyState(), base_observation(pool_depth_usd=Decimal("0")))
        assert empty.decision.action is PolicyActionKind.HOLD
        assert empty.decision.reason is PolicyReason.ENTRY_SIZE_EMPTY

    def test_entry_blocked_by_missing_or_stale_reference(self) -> None:
        """A missing reference quote, or one past the bound, blocks new entries."""
        missing = PolicyEngine().decide(PolicyState(), base_observation(reference_price_usdc=None))
        assert missing.decision.reason is PolicyReason.REFERENCE_STALE
        no_age = PolicyEngine().decide(PolicyState(), base_observation(reference_age_seconds=None))
        assert no_age.decision.reason is PolicyReason.REFERENCE_STALE
        stale = PolicyEngine().decide(PolicyState(), base_observation(reference_age_seconds=301))
        assert stale.decision.reason is PolicyReason.REFERENCE_STALE
        fresh_at_bound = PolicyEngine().decide(
            PolicyState(), base_observation(reference_age_seconds=300)
        )
        assert fresh_at_bound.decision.action is PolicyActionKind.ENTER

    def test_entry_blocked_by_condition_flat_signals(self) -> None:
        """Oracle-stale and registry-pause signals are flat events that block entry."""
        oracle_flat = PolicyEngine().decide(PolicyState(), base_observation(oracle_stale=True))
        assert oracle_flat.decision.action is PolicyActionKind.HOLD
        assert oracle_flat.decision.reason is PolicyReason.EVENT_WINDOW_FLAT
        assert "Oracle-stale" in oracle_flat.decision.diagnostics[0]
        registry_flat = PolicyEngine().decide(PolicyState(), base_observation(registry_paused=True))
        assert registry_flat.decision.reason is PolicyReason.EVENT_WINDOW_FLAT

    def test_naive_observation_time_is_rejected(self) -> None:
        """Naive observation timestamps fail closed so all waits are absolute."""
        with pytest.raises(ValidationError):
            base_observation(observed_at=datetime(2026, 8, 19, 11, 0))


class TestPositionLifecycle:
    """Open-position safety exits, maintenance, and holds."""

    def test_in_range_position_holds(self) -> None:
        """An in-range position with a clearing APR holds without action."""
        engine, state = entered_session()
        outcome = engine.decide(
            state, base_observation(observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK))
        )
        assert outcome.decision.action is PolicyActionKind.HOLD
        assert outcome.decision.reason is PolicyReason.OPEN_IN_RANGE
        assert outcome.next_state.position is not None

    def test_reference_gate_applies_only_to_entries(self) -> None:
        """While open, a missing reference does not by itself force an exit."""
        engine, state = entered_session()
        outcome = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                reference_price_usdc=None,
                reference_age_seconds=None,
            ),
        )
        assert outcome.decision.reason is PolicyReason.OPEN_IN_RANGE

    def test_below_edge_above_stop_holds(self) -> None:
        """A price below the range edge but above the stop level holds for recovery."""
        engine, state = entered_session()
        held_price = (
            entered_position_for(state).price_range.lower_price + stop_level_for(state)
        ) / 2
        outcome = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=held_price,
            ),
        )
        assert outcome.decision.action is PolicyActionKind.HOLD
        assert outcome.decision.reason is PolicyReason.OPEN_BELOW_EDGE_HOLDING
        assert outcome.next_state.position is not None

    def test_downside_stop_exits_and_sets_reentry_cooldown(self) -> None:
        """A price at the stop level burns and swaps back to USDC, then cools down."""
        engine, state = entered_session()
        stop_price = stop_level_for(state)
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=stop_price,
            ),
        )
        assert stopped.decision.action is PolicyActionKind.STOP_OUT
        assert stopped.decision.reason is PolicyReason.DOWNSIDE_STOP_TRIGGERED
        assert stopped.next_state.position is None
        assert stopped.next_state.reentry_blocked_until == datetime(
            2026, 8, 19, 11, 20, tzinfo=NEW_YORK
        )
        still_cooling = engine.decide(
            stopped.next_state,
            base_observation(observed_at=datetime(2026, 8, 19, 11, 19, tzinfo=NEW_YORK)),
        )
        assert still_cooling.decision.reason is PolicyReason.ENTRY_COOLDOWN_ACTIVE
        reentered = engine.decide(
            stopped.next_state,
            base_observation(observed_at=datetime(2026, 8, 19, 11, 20, tzinfo=NEW_YORK)),
        )
        assert reentered.decision.action is PolicyActionKind.ENTER

    def test_dilution_exit_triggers_below_threshold_while_open(self) -> None:
        """A raw emissions APR fall below the threshold exits through the stop path."""
        engine, state = entered_session()
        outcome = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                emissions_apr=Decimal("1.49"),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.DILUTION_EXIT
        assert outcome.decision.reason is PolicyReason.DILUTION_EXIT_TRIGGERED
        assert outcome.next_state.position is None
        assert outcome.next_state.reentry_blocked_until is not None

    def test_event_exit_while_open_has_no_cooldown(self) -> None:
        """An event-window exit releases immediately once the window ends."""
        calendar = parse_event_calendar(
            "[[event]]\n"
            'kind = "earnings"\n'
            'scheduled_at = "2026-08-19T12:00:00"\n'
            f'token_address = "{TOKEN_ADDRESS}"\n'
        )
        engine = PolicyEngine(calendar=calendar)
        entered = engine.decide(
            PolicyState(),
            base_observation(observed_at=datetime(2026, 8, 19, 10, 15, tzinfo=NEW_YORK)),
        )
        assert entered.decision.action is PolicyActionKind.ENTER
        exited = engine.decide(
            entered.next_state,
            base_observation(observed_at=datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK)),
        )
        assert exited.decision.action is PolicyActionKind.EVENT_EXIT
        assert exited.decision.reason is PolicyReason.EVENT_EXIT_TRIGGERED
        assert exited.next_state.reentry_blocked_until is None
        reentered = engine.decide(
            exited.next_state,
            base_observation(observed_at=datetime(2026, 8, 19, 13, 0, tzinfo=NEW_YORK)),
        )
        assert reentered.decision.action is PolicyActionKind.ENTER

    def test_oracle_stale_exits_open_position(self) -> None:
        """An oracle-stale condition event exits an open position to USDC."""
        engine, state = entered_session()
        outcome = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                oracle_stale=True,
            ),
        )
        assert outcome.decision.action is PolicyActionKind.EVENT_EXIT
        assert "Oracle-stale" in outcome.decision.diagnostics[0]

    def test_upside_recenter_waits_fifteen_minutes(self) -> None:
        """The upside wait elapses only after fifteen minutes out of range."""
        engine, state = entered_session()
        above_price = entered_position_for(state).price_range.upper_price * Decimal("1.01")
        just_above = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
            ),
        )
        assert just_above.decision.action is PolicyActionKind.HOLD
        assert just_above.decision.reason is PolicyReason.OPEN_ABOVE_RANGE_WAITING
        position = just_above.next_state.position
        assert position is not None
        assert position.out_of_range_since == datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK)
        still_waiting = engine.decide(
            just_above.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 12, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
            ),
        )
        assert still_waiting.decision.reason is PolicyReason.OPEN_ABOVE_RANGE_WAITING
        recentred = engine.decide(
            still_waiting.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 21, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
            ),
        )
        assert recentred.decision.action is PolicyActionKind.RECENTER
        assert recentred.decision.reason is PolicyReason.RECENTER_WAIT_ELAPSED
        new_position = recentred.next_state.position
        assert new_position is not None
        assert new_position.out_of_range_since is None
        assert new_position.entered_at == datetime(2026, 8, 19, 11, 21, tzinfo=NEW_YORK)
        assert new_position.price_range.lower_price < above_price
        assert new_position.price_range.upper_price > above_price

    def test_returning_in_range_resets_the_recenter_wait(self) -> None:
        """A price returning inside the range clears the wait anchor."""
        engine, state = entered_session()
        upper = entered_position_for(state).price_range.upper_price
        above = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=upper * Decimal("1.01"),
            ),
        )
        assert above.next_state.position is not None
        assert above.next_state.position.out_of_range_since is not None
        back_inside = engine.decide(
            above.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 6, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("200.2"),
            ),
        )
        assert back_inside.decision.reason is PolicyReason.OPEN_IN_RANGE
        assert back_inside.next_state.position is not None
        assert back_inside.next_state.position.out_of_range_since is None
        restarted = engine.decide(
            back_inside.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK),
                amm_price_usdc=upper * Decimal("1.01"),
            ),
        )
        position = restarted.next_state.position
        assert position is not None
        assert position.out_of_range_since == datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK)


class TestDailyLossHalt:
    """Same-day equity drawdown halt behavior."""

    def test_daily_loss_halt_latches_until_the_next_day(self) -> None:
        """A five percent same-day drawdown blocks entries for the rest of the day."""
        engine, state = entered_session()
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=stop_level_for(state),
                equity_usd=Decimal("189"),
            ),
        )
        assert stopped.decision.action is PolicyActionKind.STOP_OUT
        recovered_but_halted = engine.decide(
            stopped.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK),
                equity_usd=Decimal("199"),
            ),
        )
        assert recovered_but_halted.decision.action is PolicyActionKind.HOLD
        assert recovered_but_halted.decision.reason is PolicyReason.DAILY_LOSS_HALT_ACTIVE
        next_day = engine.decide(
            recovered_but_halted.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 20, 11, 0, tzinfo=NEW_YORK),
                equity_usd=Decimal("199"),
            ),
        )
        assert next_day.decision.action is PolicyActionKind.ENTER
        assert next_day.decision.size_usd == Decimal("39.8")

    def test_day_rollover_resets_the_day_start_equity(self) -> None:
        """A new day re-anchors the halt at the current marked equity."""
        engine, state = entered_session()
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=stop_level_for(state),
                equity_usd=Decimal("150"),
            ),
        )
        next_day = engine.decide(
            stopped.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 20, 11, 0, tzinfo=NEW_YORK),
                equity_usd=Decimal("150"),
            ),
        )
        assert next_day.decision.action is PolicyActionKind.ENTER
        assert next_day.decision.size_usd == Decimal("30")


class TestEnginePurity:
    """Immutability and injection behavior of the pure engine."""

    def test_decide_does_not_mutate_the_input_state(self) -> None:
        """The engine threads state through model copies, never in-place mutation."""
        engine = PolicyEngine()
        initial_state = PolicyState()
        engine.decide(initial_state, base_observation())
        assert initial_state == PolicyState()

    def test_engine_exposes_injected_parameters_and_calendar(self) -> None:
        """The engine reports the exact immutable inputs used for reproduction."""
        parameters = PolicyParameters(tick_spacing=10)
        calendar = EventCalendar()
        engine = PolicyEngine(parameters=parameters, calendar=calendar)
        assert engine.parameters is parameters
        assert engine.calendar is calendar

    def test_naive_position_times_are_rejected(self) -> None:
        """Naive entry or out-of-range times fail closed at model construction."""
        # A directly constructed position reuses an aligned range from the engine.
        price_range = PolicyEngine().build_aligned_range(Decimal("200"))
        with pytest.raises(ValidationError):
            PolicyPosition(
                pool_address=POOL_ADDRESS,
                token_address=TOKEN_ADDRESS,
                price_range=price_range,
                committed_usd=Decimal("40"),
                entered_at=datetime(2026, 8, 19, 11, 0),
            )
        with pytest.raises(ValidationError):
            PolicyPosition(
                pool_address=POOL_ADDRESS,
                token_address=TOKEN_ADDRESS,
                price_range=price_range,
                committed_usd=Decimal("40"),
                entered_at=datetime(2026, 8, 19, 11, 0, tzinfo=NEW_YORK),
                out_of_range_since=datetime(2026, 8, 19, 11, 5),
            )

    def test_recenter_is_allowed_during_a_daily_loss_halt(self) -> None:
        """A latched halt blocks new entries but not maintenance of the open position."""
        engine = PolicyEngine()
        entered = engine.decide(
            PolicyState(),
            base_observation(observed_at=datetime(2026, 8, 19, 10, 15, tzinfo=NEW_YORK)),
        )
        assert entered.decision.action is PolicyActionKind.ENTER
        entered_position = entered.next_state.position
        assert entered_position is not None
        upper = entered_position.price_range.upper_price
        halted_and_above = engine.decide(
            entered.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 0, tzinfo=NEW_YORK),
                amm_price_usdc=upper * Decimal("1.01"),
                equity_usd=Decimal("189.9"),
            ),
        )
        # The 5 percent drawdown latches the halt while the position waits above range.
        assert halted_and_above.decision.reason is PolicyReason.OPEN_ABOVE_RANGE_WAITING
        recentred = engine.decide(
            halted_and_above.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 16, tzinfo=NEW_YORK),
                amm_price_usdc=upper * Decimal("1.01"),
                equity_usd=Decimal("189.9"),
            ),
        )
        assert recentred.decision.action is PolicyActionKind.RECENTER
