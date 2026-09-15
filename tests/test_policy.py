"""Behavior tests for the pure emissions-farming policy decision engine."""

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from aero_bot.policy import (
    NEW_YORK,
    TICK_PRICE_RATIO,
    AlignedPriceRange,
    EventCalendar,
    HeldInventory,
    PolicyActionKind,
    PolicyEngine,
    PolicyObservation,
    PolicyParameters,
    PolicyPosition,
    PolicyReason,
    PolicyState,
    ScheduledEvent,
    SwapDirection,
    SwapPlan,
    SwapTranche,
    evaluate_event_window,
    load_event_calendar,
    parse_event_calendar,
)
from aero_bot.ranging import RangingEvidence, WidthSolveMode

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
        "fee_apr": Decimal("0.5"),
        "pool_depth_usd": Decimal("50000"),
        "equity_usd": Decimal("200"),
        "reference_age_seconds": 10,
        "gas_price_gwei": Decimal("0.002"),
    }
    values.update(overrides)
    # The reference defaults to the AMM price so price-move tests observe a
    # coherent market; dislocation tests override the reference explicitly.
    values.setdefault("reference_price_usdc", values["amm_price_usdc"])
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


def ranging_evidence(**overrides: object) -> RangingEvidence:
    """Build one solvable ranging-evidence fixture for width-derivation tests.

    Args:
        **overrides: Evidence fields changed to exercise one solve behavior.

    Returns:
        A validated immutable evidence set whose default inputs solve the
        width at the tightest candidate given a passing high-APR observation.
    """
    values: dict[str, object] = {
        "gauge_liquidity_raw": 80_000_000_000_000,
        "staked_tvl_usd": Decimal("100000"),
        "active_liquidity_raw": 80_000_000_000_000,
        "fee_window_seconds": 86_400,
        "fee_window_notional_usd": Decimal("1000000"),
        "pool_fee_ppm": 500,
        "realized_daily_volatility": Decimal("0.005"),
        "stock_decimals": 6,
        "quote_decimals": 6,
    }
    values.update(overrides)
    return RangingEvidence.model_validate(values)


def half_width_for_ticks(ticks: int) -> Decimal:
    """Return the exact fractional half width of one tick count.

    Args:
        ticks: Whole ticks on each side of the reference price.

    Returns:
        The exact 1.0001**ticks - 1 fraction at engine precision.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = 60
        return +(TICK_PRICE_RATIO ** Decimal(ticks) - Decimal(1))


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
        assert parameters.target_net_daily_yield == Decimal("0.01")
        assert parameters.max_range_half_width_fraction == Decimal("0.003")
        assert parameters.tick_spacing == 10
        assert parameters.recenter_wait.total_seconds() == 15 * 60
        assert parameters.stop_buffer_fraction == Decimal("0.005")
        assert parameters.reentry_cooldown.total_seconds() == 15 * 60
        assert parameters.min_entry_emissions_apr == Decimal("1.5")
        # Raised from twenty percent by the captain's calibration ruling
        # (2026-09-09); the provenance test below pins the ruling in source.
        assert parameters.max_position_equity_fraction == Decimal("0.80")
        assert parameters.max_position_depth_fraction == Decimal("0.01")
        assert parameters.daily_loss_halt_fraction == Decimal("0.05")
        assert parameters.reference_max_age_seconds == 300
        assert parameters.reference_open_position_max_age_seconds == 900
        assert parameters.dislocation_threshold_fraction == Decimal("0.0015")
        assert parameters.convergence_timeout.total_seconds() == 5 * 60
        assert parameters.swap_impact_ceiling_fraction == Decimal("0.001")
        assert parameters.swap_impact_tranche_fraction == Decimal("0.0005")
        assert parameters.gas_price_ceiling_gwei == Decimal("0.5")
        assert parameters.gas_cost_max_gross_yield_fraction == Decimal("0.05")
        assert parameters.safe_overhead_gas_per_batch == 100_000
        assert parameters.enter_batch_gas_units == 650_000
        assert parameters.recenter_batch_gas_units == 550_000
        assert parameters.exit_batch_gas_units == 350_000
        assert parameters.inventory_sell_gas_units == 180_000
        assert parameters.eth_price_assumption_usd == Decimal("3000")

    def test_out_of_range_fraction_parameters_are_rejected(self) -> None:
        """Fraction parameters outside their meaningful intervals fail closed."""
        with pytest.raises(ValidationError):
            PolicyParameters(max_range_half_width_fraction=Decimal("1.5"))
        with pytest.raises(ValidationError):
            PolicyParameters(max_range_half_width_fraction=Decimal("0"))
        with pytest.raises(ValidationError):
            PolicyParameters(target_net_daily_yield=Decimal("0"))
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
        with pytest.raises(ValidationError):
            PolicyParameters(dislocation_threshold_fraction=Decimal("1.5"))
        with pytest.raises(ValidationError):
            PolicyParameters(swap_impact_tranche_fraction=Decimal("0.002"))
        with pytest.raises(ValidationError):
            PolicyParameters(swap_impact_ceiling_fraction=Decimal("1"))
        with pytest.raises(ValidationError):
            PolicyParameters(gas_price_ceiling_gwei=Decimal("0"))
        with pytest.raises(ValidationError):
            PolicyParameters(gas_cost_max_gross_yield_fraction=Decimal("1"))
        with pytest.raises(ValidationError):
            PolicyParameters(eth_price_assumption_usd=Decimal("0"))
        with pytest.raises(ValidationError):
            PolicyParameters(convergence_timeout=timedelta(0))
        with pytest.raises(ValidationError):
            PolicyParameters(reference_open_position_max_age_seconds=60)

    def test_sizing_ruling_provenance_is_recorded_in_source(self) -> None:
        """The eighty-percent equity cap carries its ruling as provenance.

        The captain's calibration ruling (2026-09-09) raised the
        per-position equity-fraction cap from twenty to eighty percent of
        the book (roughly 72 USDC on the 90-dollar trial book) while the
        hard ceilings stayed 100 USDC total exposure and 100 USDC per pool;
        the source comment above the field records exactly that.
        """
        source = inspect.getsource(PolicyParameters)
        assert 'max_position_equity_fraction: Decimal = Decimal("0.80")' in source
        assert "captain's calibration ruling (2026-09-09)" in source
        assert "100 USDC total exposure" in source

    def test_session_and_scheduled_windows_no_longer_gate_entries(self) -> None:
        """Tuesday-night and market-open fixtures produce entry verdicts.

        The captain's 2026-09-09 twenty-four-seven ruling removed the
        market-session/event-window gate from entry decisions: the B20
        pools are continuous DeFi markets, so nights and weekends are in
        scope. These fixtures previously held as event_window_flat - the
        doctrine test inverted here with the ruling recorded as provenance.
        """
        # 2026-09-08 is a Tuesday; 09:40 America/New_York sits inside the
        # old market-open window.
        market_open = PolicyEngine().decide(
            PolicyState(),
            base_observation(observed_at=datetime(2026, 9, 8, 9, 40, tzinfo=NEW_YORK)),
        )
        assert market_open.decision.action is PolicyActionKind.ENTER
        assert market_open.decision.reason is PolicyReason.ENTRY_THRESHOLD_MET
        # The same Tuesday at 21:00 America/New_York, inside a scheduled
        # earnings window scoped to this token.
        calendar = parse_event_calendar(
            "[[event]]\n"
            'kind = "earnings"\n'
            'scheduled_at = "2026-09-08T21:30:00"\n'
            f'token_address = "{TOKEN_ADDRESS}"\n'
        )
        tuesday_night = PolicyEngine(calendar=calendar).decide(
            PolicyState(),
            base_observation(observed_at=datetime(2026, 9, 8, 21, 0, tzinfo=NEW_YORK)),
        )
        assert tuesday_night.decision.action is PolicyActionKind.ENTER
        assert tuesday_night.decision.reason is PolicyReason.ENTRY_THRESHOLD_MET
        # The window machinery still evaluates and reports, informationally.
        window = evaluate_event_window(
            datetime(2026, 9, 8, 21, 0, tzinfo=NEW_YORK), TOKEN_ADDRESS, calendar
        )
        assert window.active is True
        assert "earnings" in window.description


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
        # The captain's 2026-09-09 sizing ruling: eighty percent of the
        # 200-USDC book, 160 USDC, against the 500-USDC depth cap.
        assert outcome.decision.size_usd == Decimal("160")
        assert outcome.decision.price_range is not None
        position = outcome.next_state.position
        assert position is not None
        assert position.committed_usd == Decimal("160")
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

    def test_reference_bounds_while_open_ride_then_exit_defensively(self) -> None:
        """While open, a merely-old reference rides but a missing or beyond-bound one exits."""
        engine, state = entered_session()
        older_than_entry_bound = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                reference_age_seconds=400,
            ),
        )
        assert older_than_entry_bound.decision.reason is PolicyReason.OPEN_IN_RANGE
        at_open_bound = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                reference_age_seconds=900,
            ),
        )
        assert at_open_bound.decision.reason is PolicyReason.OPEN_IN_RANGE
        beyond_open_bound = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                reference_age_seconds=901,
            ),
        )
        assert beyond_open_bound.decision.action is PolicyActionKind.DEFENSIVE_EXIT
        assert beyond_open_bound.decision.reason is PolicyReason.REFERENCE_STALE_DEFENSIVE_EXIT
        assert beyond_open_bound.next_state.position is None
        assert beyond_open_bound.next_state.reentry_blocked_until is None
        missing = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 2, tzinfo=NEW_YORK),
                reference_price_usdc=None,
                reference_age_seconds=None,
            ),
        )
        assert missing.decision.action is PolicyActionKind.DEFENSIVE_EXIT

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

    def test_downside_recenter_waits_for_distance_then_uses_economics(self) -> None:
        """A sustained material downside breach recenters when churn pays back quickly."""
        engine, state = entered_session()
        lower = entered_position_for(state).price_range.lower_price
        below_price = lower * Decimal("0.998")
        first = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=below_price,
            ),
        )
        assert first.decision.action is PolicyActionKind.HOLD
        assert first.decision.reason is PolicyReason.OPEN_BELOW_EDGE_HOLDING
        assert first.next_state.position is not None
        assert first.next_state.position.out_of_range_side == "below"

        recentred = engine.decide(
            first.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 17, tzinfo=NEW_YORK),
                amm_price_usdc=below_price,
            ),
        )
        assert recentred.decision.action is PolicyActionKind.RECENTER
        assert recentred.decision.reason is PolicyReason.DOWNSIDE_RECENTER_ECONOMIC
        assert recentred.decision.swap_plan is not None
        assert recentred.decision.swap_plan.direction is SwapDirection.SELL_STOCK
        assert recentred.next_state.position is not None
        assert recentred.next_state.position.out_of_range_since is None
        assert recentred.next_state.position.out_of_range_side is None
        assert any("payback is" in line for line in recentred.decision.diagnostics)

    def test_downside_recenter_does_not_chase_a_tiny_edge_breach(self) -> None:
        """Even after the wait, a sub-threshold breach holds rather than churns."""
        engine, state = entered_session()
        lower = entered_position_for(state).price_range.lower_price
        below_price = lower * Decimal("0.9995")
        first = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=below_price,
            ),
        )
        held = engine.decide(
            first.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK),
                amm_price_usdc=below_price,
            ),
        )
        assert held.decision.action is PolicyActionKind.HOLD
        assert held.decision.reason is PolicyReason.OPEN_BELOW_EDGE_HOLDING
        assert any("minimum" in line for line in held.decision.diagnostics)

    def test_downside_recenter_holds_when_modeled_payback_is_too_slow(self) -> None:
        """The downside path exposes an explicit economic hold instead of blind recentering."""
        parameters = PolicyParameters(downside_recenter_max_payback_days=Decimal("0.001"))
        engine = PolicyEngine(parameters=parameters)
        entered = engine.decide(PolicyState(), base_observation())
        assert entered.decision.action is PolicyActionKind.ENTER
        position = entered_position_for(entered.next_state)
        below_price = position.price_range.lower_price * Decimal("0.998")
        first = engine.decide(
            entered.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=below_price,
            ),
        )
        held = engine.decide(
            first.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 17, tzinfo=NEW_YORK),
                amm_price_usdc=below_price,
            ),
        )
        assert held.decision.action is PolicyActionKind.HOLD
        assert held.decision.reason is PolicyReason.DOWNSIDE_RECENTER_UNECONOMIC
        assert any("payback is" in line for line in held.decision.diagnostics)

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

    def test_scheduled_windows_no_longer_gate_since_the_ruling(self) -> None:
        """Scheduled event windows neither exit the position nor block re-entry.

        The captain's 2026-09-09 twenty-four-seven ruling: the B20 pools are
        continuous DeFi markets - nights and weekends are in scope - so the
        flat-window doctrine around scheduled events and US equity session
        boundaries was removed. This test is the inversion of the doctrine
        test that previously expected an event exit and a blocked entry;
        the ruling is recorded here as its provenance.
        """
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
        during_window = engine.decide(
            entered.next_state,
            base_observation(observed_at=datetime(2026, 8, 19, 11, 30, tzinfo=NEW_YORK)),
        )
        assert during_window.decision.action is PolicyActionKind.HOLD
        assert during_window.decision.reason is PolicyReason.OPEN_IN_RANGE
        exited = engine.decide(
            during_window.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 13, 0, tzinfo=NEW_YORK),
                amm_price_usdc=stop_level_for(during_window.next_state),
            ),
        )
        assert exited.decision.action is PolicyActionKind.STOP_OUT
        assert exited.next_state.reentry_blocked_until is not None
        reentered = engine.decide(
            exited.next_state,
            base_observation(observed_at=datetime(2026, 8, 19, 13, 30, tzinfo=NEW_YORK)),
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
        assert next_day.decision.size_usd == Decimal("159.2")

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
        assert next_day.decision.size_usd == Decimal("120")


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


class TestGasSenseCheckGate:
    """Gas sense-check deferral behavior for non-urgent actions."""

    def test_entry_deferred_without_a_gas_price_reading(self) -> None:
        """An unavailable gas price defers the entry fail-closed."""
        outcome = PolicyEngine().decide(PolicyState(), base_observation(gas_price_gwei=None))
        assert outcome.decision.action is PolicyActionKind.HOLD
        assert outcome.decision.reason is PolicyReason.GAS_GATE_DEFERRED
        assert "unavailable" in outcome.decision.diagnostics[0]

    def test_entry_deferred_above_the_gas_price_ceiling(self) -> None:
        """A gas price above 0.5 gwei defers the entry."""
        outcome = PolicyEngine().decide(
            PolicyState(), base_observation(gas_price_gwei=Decimal("0.6"))
        )
        assert outcome.decision.action is PolicyActionKind.HOLD
        assert outcome.decision.reason is PolicyReason.GAS_GATE_DEFERRED
        assert "ceiling" in outcome.decision.diagnostics[0]

    def test_entry_deferred_when_batch_cost_exceeds_yield_share(self) -> None:
        """A cheap-but-costly batch defers the entry against the yield bound."""
        # At 0.2 gwei the 750k-unit entry batch costs 0.45 USDC, above five
        # percent of the 160-USDC position's 0.87671 expected daily yield.
        outcome = PolicyEngine().decide(
            PolicyState(), base_observation(gas_price_gwei=Decimal("0.2"))
        )
        assert outcome.decision.action is PolicyActionKind.HOLD
        assert outcome.decision.reason is PolicyReason.GAS_GATE_DEFERRED
        assert "gross yield" in outcome.decision.diagnostics[0]

    def test_entry_at_exactly_the_gas_ceiling_passes_with_real_yield(self) -> None:
        """A large position's yield absorbs the ceiling-bound gas cost."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(
                gas_price_gwei=Decimal("0.5"),
                equity_usd=Decimal("1000000"),
                pool_depth_usd=Decimal("100000000"),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.ENTER
        assert outcome.decision.size_usd == Decimal("800000")

    def test_recenter_deferred_by_gas_then_fires_on_a_cheap_observation(self) -> None:
        """A gas spike defers the elapsed recenter without resetting its wait."""
        engine, state = entered_session()
        upper = entered_position_for(state).price_range.upper_price
        above_price = upper * Decimal("1.01")
        waiting = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
            ),
        )
        assert waiting.decision.reason is PolicyReason.OPEN_ABOVE_RANGE_WAITING
        deferred = engine.decide(
            waiting.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 21, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
                gas_price_gwei=Decimal("0.5"),
            ),
        )
        assert deferred.decision.action is PolicyActionKind.HOLD
        assert deferred.decision.reason is PolicyReason.GAS_GATE_DEFERRED
        position = deferred.next_state.position
        assert position is not None
        assert position.out_of_range_since == datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK)
        recentred = engine.decide(
            deferred.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 22, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
            ),
        )
        assert recentred.decision.action is PolicyActionKind.RECENTER

    def test_safety_exits_are_never_deferred_by_gas(self) -> None:
        """A downside stop fires at an absurd gas price with its cost attached."""
        engine, state = entered_session()
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=stop_level_for(state),
                gas_price_gwei=Decimal("50"),
            ),
        )
        assert stopped.decision.action is PolicyActionKind.STOP_OUT
        assert stopped.decision.estimated_gas_units == 450_000
        assert stopped.decision.estimated_gas_cost_usd == Decimal("67.5")

    def test_safety_exit_without_a_gas_reading_reports_an_unknown_cost(self) -> None:
        """A stop-out with no gas reading still exits with units and no cost."""
        engine, state = entered_session()
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=stop_level_for(state),
                gas_price_gwei=None,
            ),
        )
        assert stopped.decision.action is PolicyActionKind.STOP_OUT
        assert stopped.decision.estimated_gas_units == 450_000
        assert stopped.decision.estimated_gas_cost_usd is None
        assert "unavailable" in stopped.decision.diagnostics[-1]

    def test_gas_estimate_uses_the_configurable_eth_assumption(self) -> None:
        """Doubling the ETH price assumption doubles the modeled batch cost."""
        engine = PolicyEngine(parameters=PolicyParameters(eth_price_assumption_usd=Decimal("6000")))
        outcome = engine.decide(PolicyState(), base_observation())
        assert outcome.decision.action is PolicyActionKind.ENTER
        assert outcome.decision.estimated_gas_units == 750_000
        assert outcome.decision.estimated_gas_cost_usd == Decimal("0.009")


class TestSwapExecutionModeling:
    """Per-swap execution-quality modeling and tranche splitting."""

    def test_entry_swap_buys_half_the_position_split_at_the_tranche_bound(self) -> None:
        """The entry rebalance buys size over two and splits at 0.05 percent impact."""
        outcome = PolicyEngine().decide(PolicyState(), base_observation())
        assert outcome.decision.action is PolicyActionKind.ENTER
        plan = outcome.decision.swap_plan
        assert plan is not None
        assert plan.direction is SwapDirection.BUY_STOCK
        assert plan.total_usd == Decimal("80")
        # The 50k-depth pool allows a 25-USDC max tranche, so 80 USDC splits
        # in four 20-USDC tranches.
        assert len(plan.tranches) == 4
        assert all(tranche.usd_size == Decimal("20") for tranche in plan.tranches)
        assert plan.max_modeled_impact_fraction == Decimal("0.0004")

    def test_tranche_remainder_sums_exactly_to_the_plan_total(self) -> None:
        """A non-terminating split floors every leading tranche to six decimals."""
        # Depth 29000 gives a 14.5 max tranche, so the 80-USDC entry swap
        # needs six tranches and 80/6 floors to 13.333333 with the last absorbing.
        outcome = PolicyEngine().decide(
            PolicyState(), base_observation(pool_depth_usd=Decimal("29000"))
        )
        assert outcome.decision.action is PolicyActionKind.ENTER
        plan = outcome.decision.swap_plan
        assert plan is not None
        assert len(plan.tranches) == 6
        assert plan.tranches[0].usd_size == Decimal("13.333333")
        assert plan.tranches[-1].usd_size == Decimal("13.333335")
        assert sum((tranche.usd_size for tranche in plan.tranches), Decimal(0)) == Decimal("80")
        assert plan.max_modeled_impact_fraction is not None
        assert plan.max_modeled_impact_fraction <= Decimal("0.0005")

    def test_a_small_swap_against_deep_liquidity_is_one_tranche(self) -> None:
        """A swap under the split bound stays whole with a tiny modeled impact."""
        outcome = PolicyEngine().decide(
            PolicyState(), base_observation(pool_depth_usd=Decimal("5000000"))
        )
        plan = outcome.decision.swap_plan
        assert plan is not None
        assert len(plan.tranches) == 1
        assert plan.tranches[0].usd_size == Decimal("80")
        assert plan.max_modeled_impact_fraction == Decimal("0.000016")

    def test_recenter_swap_buys_half_the_committed_value_back(self) -> None:
        """The above-range all-USDC position buys half its value back into stock."""
        engine, state = entered_session()
        upper = entered_position_for(state).price_range.upper_price
        above_price = upper * Decimal("1.01")
        waiting = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
            ),
        )
        recentred = engine.decide(
            waiting.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 21, tzinfo=NEW_YORK),
                amm_price_usdc=above_price,
            ),
        )
        assert recentred.decision.action is PolicyActionKind.RECENTER
        plan = recentred.decision.swap_plan
        assert plan is not None
        assert plan.direction is SwapDirection.BUY_STOCK
        assert plan.total_usd == Decimal("80")

    def test_stop_out_sells_the_full_stock_inventory_below_range(self) -> None:
        """Below the range the composition is all stock, swapped near committed value."""
        engine, state = entered_session()
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=stop_level_for(state),
            ),
        )
        assert stopped.decision.action is PolicyActionKind.STOP_OUT
        plan = stopped.decision.swap_plan
        assert plan is not None
        assert plan.direction is SwapDirection.SELL_STOCK
        assert Decimal("156") < plan.total_usd < Decimal("160")

    def test_in_range_exit_sells_the_stock_half_of_the_position(self) -> None:
        """A dilution exit in range only swaps the stock half of the composition."""
        engine, state = entered_session()
        diluted = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                emissions_apr=Decimal("1.49"),
            ),
        )
        assert diluted.decision.action is PolicyActionKind.DILUTION_EXIT
        plan = diluted.decision.swap_plan
        assert plan is not None
        assert plan.direction is SwapDirection.SELL_STOCK
        assert Decimal("76") < plan.total_usd < Decimal("88")

    def test_zero_depth_exit_models_one_unmodeled_tranche(self) -> None:
        """A vanished depth still exits safely with the impact labeled unmodeled."""
        engine, state = entered_session()
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 5, tzinfo=NEW_YORK),
                amm_price_usdc=stop_level_for(state),
                pool_depth_usd=Decimal("0"),
            ),
        )
        assert stopped.decision.action is PolicyActionKind.STOP_OUT
        plan = stopped.decision.swap_plan
        assert plan is not None
        assert len(plan.tranches) == 1
        assert plan.tranches[0].modeled_impact_fraction is None
        assert plan.max_modeled_impact_fraction is None

    def test_swap_plan_rejects_mismatched_tranche_sums(self) -> None:
        """A tranche list that does not sum to the plan total fails validation."""
        with pytest.raises(ValidationError):
            SwapPlan(
                direction=SwapDirection.SELL_STOCK,
                total_usd=Decimal("10"),
                route_depth_usd=Decimal("10000"),
                tranches=(
                    SwapTranche(usd_size=Decimal("4"), modeled_impact_fraction=Decimal("0")),
                ),
                max_modeled_impact_fraction=Decimal("0"),
            )
        with pytest.raises(ValidationError):
            SwapTranche(usd_size=Decimal("0"), modeled_impact_fraction=Decimal("0"))


class TestDislocationMonitor:
    """Underlying dislocation monitor and held-inventory lifecycle behavior."""

    def test_stale_high_amm_exits_by_selling_on_the_pool(self) -> None:
        """An AMM at least 0.15 percent above the reference sells immediately."""
        engine, state = entered_session()
        outcome = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("200.5"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.DISLOCATION_EXIT
        assert outcome.decision.reason is PolicyReason.DISLOCATION_STALE_HIGH_TRIGGERED
        assert outcome.next_state.position is None
        assert outcome.next_state.held_inventory is None
        assert outcome.next_state.reentry_blocked_until is None
        plan = outcome.decision.swap_plan
        assert plan is not None
        assert plan.direction is SwapDirection.SELL_STOCK

    def test_dislocation_boundaries_trigger_at_exactly_the_threshold(self) -> None:
        """A deviation of exactly 0.15 percent in either direction fires."""
        engine, state = entered_session()
        at_upper_boundary = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("200.3"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        assert at_upper_boundary.decision.action is PolicyActionKind.DISLOCATION_EXIT
        at_lower_boundary = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.7"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        assert at_lower_boundary.decision.action is PolicyActionKind.STALE_LOW_BURN
        inside_band = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.8"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        assert inside_band.decision.reason is PolicyReason.OPEN_IN_RANGE

    def test_stale_low_burn_holds_tokens_with_no_swap_and_no_cooldown(self) -> None:
        """A stale-low AMM burns and carries the stock tokens out of the pool."""
        engine, state = entered_session()
        burned = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.6"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        assert burned.decision.action is PolicyActionKind.STALE_LOW_BURN
        assert burned.decision.reason is PolicyReason.DISLOCATION_STALE_LOW_TRIGGERED
        assert burned.decision.swap_plan is None
        assert burned.next_state.position is None
        assert burned.next_state.reentry_blocked_until is None
        inventory = burned.next_state.held_inventory
        assert inventory is not None
        assert inventory.stock_quantity > 0
        assert inventory.held_since == datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK)
        assert inventory.token_address == TOKEN_ADDRESS

    def test_stale_low_above_range_closes_flat_with_no_inventory(self) -> None:
        """An all-USDC position burning stale-low simply ends flat."""
        engine, state = entered_session()
        upper = entered_position_for(state).price_range.upper_price
        outcome = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=upper * Decimal("1.001"),
                reference_price_usdc=Decimal("202"),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.STALE_LOW_BURN
        assert outcome.next_state.position is None
        assert outcome.next_state.held_inventory is None

    def test_dislocation_precedence_beats_the_downside_stop(self) -> None:
        """A dislocated reference overrides the AMM-anchored stop in both directions."""
        engine, state = entered_session()
        stop_price = stop_level_for(state)
        stale_high = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=stop_price,
                reference_price_usdc=stop_price * Decimal("0.998"),
            ),
        )
        # The crash-anticipation case: the real market fell further than the AMM,
        # so the position sells on the AMM while it still prices above reality.
        assert stale_high.decision.action is PolicyActionKind.DISLOCATION_EXIT
        stale_low = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=stop_price,
                reference_price_usdc=stop_price * Decimal("1.002"),
            ),
        )
        # The pool fell below the real market, so the stop's market sell would
        # realize the wrong price; the burn holds the tokens instead.
        assert stale_low.decision.action is PolicyActionKind.STALE_LOW_BURN

    def test_convergence_sell_releases_held_tokens_without_cooldown(self) -> None:
        """Held tokens sell once the AMM converges, and re-entry follows freely."""
        engine, state = entered_session()
        burned = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.6"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        converged = engine.decide(
            burned.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 3, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.75"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        assert converged.decision.action is PolicyActionKind.SELL_INVENTORY
        assert converged.decision.reason is PolicyReason.INVENTORY_CONVERGENCE_REACHED
        assert converged.next_state.held_inventory is None
        assert converged.next_state.reentry_blocked_until is None
        plan = converged.decision.swap_plan
        assert plan is not None
        assert plan.direction is SwapDirection.SELL_STOCK
        assert plan.total_usd > 0
        reentered = engine.decide(
            converged.next_state,
            base_observation(observed_at=datetime(2026, 8, 19, 11, 4, tzinfo=NEW_YORK)),
        )
        assert reentered.decision.action is PolicyActionKind.ENTER

    def test_disabled_reference_enforcement_cannot_trigger_inventory_convergence(self) -> None:
        """Diagnostic-only references cannot authorize an inventory sale."""
        engine, state = entered_session()
        burned = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.6"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        held = engine.decide(
            burned.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 3, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("205"),
                reference_price_usdc=Decimal("200"),
                reference_enforcement_enabled=False,
            ),
        )
        assert held.decision.action is PolicyActionKind.HOLD
        assert held.decision.reason is PolicyReason.HOLDING_INVENTORY_AWAITING_CONVERGENCE

    def test_convergence_timeout_sells_at_market_as_the_safety_bound(self) -> None:
        """Tokens still held five minutes after the burn sell at market."""
        engine, state = entered_session()
        burned = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.6"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        at_timeout = engine.decide(
            burned.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 6, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.6"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        assert at_timeout.decision.action is PolicyActionKind.SELL_INVENTORY
        assert at_timeout.decision.reason is PolicyReason.INVENTORY_CONVERGENCE_TIMEOUT
        assert at_timeout.next_state.held_inventory is None

    def test_stale_reference_while_holding_waits_for_the_timeout(self) -> None:
        """Without a fresh reference no convergence judgment is possible."""
        engine, state = entered_session()
        burned = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.6"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        still_holding = engine.decide(
            burned.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 3, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.75"),
                reference_price_usdc=Decimal("200"),
                reference_age_seconds=400,
            ),
        )
        assert still_holding.decision.action is PolicyActionKind.HOLD
        assert still_holding.decision.reason is PolicyReason.HOLDING_INVENTORY_AWAITING_CONVERGENCE
        timed_out = engine.decide(
            still_holding.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 7, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.75"),
                reference_price_usdc=Decimal("200"),
                reference_age_seconds=400,
            ),
        )
        assert timed_out.decision.action is PolicyActionKind.SELL_INVENTORY
        assert timed_out.decision.reason is PolicyReason.INVENTORY_CONVERGENCE_TIMEOUT

    def test_flat_window_while_holding_sells_the_inventory(self) -> None:
        """A condition-driven flat event forces held tokens back into USDC."""
        engine, state = entered_session()
        burned = engine.decide(
            state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 1, tzinfo=NEW_YORK),
                amm_price_usdc=Decimal("199.6"),
                reference_price_usdc=Decimal("200"),
            ),
        )
        forced = engine.decide(
            burned.next_state,
            base_observation(
                observed_at=datetime(2026, 8, 19, 11, 2, tzinfo=NEW_YORK),
                oracle_stale=True,
            ),
        )
        assert forced.decision.action is PolicyActionKind.SELL_INVENTORY
        assert forced.decision.reason is PolicyReason.INVENTORY_FLAT_WINDOW_SELL
        assert forced.next_state.held_inventory is None

    def test_naive_held_since_is_rejected(self) -> None:
        """Held inventory with a naive timestamp fails closed at construction."""
        with pytest.raises(ValidationError):
            HeldInventory(
                pool_address=POOL_ADDRESS,
                token_address=TOKEN_ADDRESS,
                stock_quantity=Decimal("0.15"),
                held_since=datetime(2026, 8, 19, 11, 1),
            )


class TestDerivedRangeWidth:
    """Target-yield-derived range-width behavior on entry and recenter."""

    def test_entry_derives_the_tightest_solved_width(self) -> None:
        """A passing solve enters at one tick spacing, not the ceiling."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.ENTER
        solution = outcome.decision.width_solution
        assert solution is not None
        assert solution.mode is WidthSolveMode.SOLVED
        assert solution.half_width_ticks == 10
        assert solution.half_width_fraction == half_width_for_ticks(10)
        price_range = entered_position_for(outcome.next_state).price_range
        center = Decimal("200")
        assert price_range.lower_price <= center * (Decimal(1) - solution.half_width_fraction)
        assert price_range.upper_price >= center * (Decimal(1) + solution.half_width_fraction)
        assert price_range.upper_price < center * Decimal("1.003")

    def test_unreachable_target_still_enters_at_one_tick_spacing(self) -> None:
        """A gate-passing pool enters at the tightest width without widening."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(ranging=ranging_evidence()),
        )
        assert outcome.decision.action is PolicyActionKind.ENTER
        solution = outcome.decision.width_solution
        assert solution is not None
        assert solution.mode is WidthSolveMode.TARGET_UNREACHABLE
        assert solution.half_width_ticks == 10
        joined = "\n".join(outcome.decision.diagnostics)
        assert "cannot reach the target" in joined
        assert "entering at the tightest width" in joined
        price_range = entered_position_for(outcome.next_state).price_range
        assert price_range.upper_price < Decimal("200") * Decimal("1.003")

    def test_missing_evidence_fails_toward_the_ceiling(self) -> None:
        """An observation without ranging evidence enters at the locked ceiling."""
        outcome = PolicyEngine().decide(PolicyState(), base_observation())
        assert outcome.decision.action is PolicyActionKind.ENTER
        solution = outcome.decision.width_solution
        assert solution is not None
        assert solution.mode is WidthSolveMode.FALLBACK_CEILING
        assert solution.half_width_ticks == 29
        assert solution.half_width_fraction == half_width_for_ticks(29)
        assert any("no ranging evidence" in line for line in outcome.decision.diagnostics)

    def test_inconsistent_evidence_fails_toward_the_ceiling(self) -> None:
        """Evidence the solver cannot use falls back through the solver itself."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(realized_daily_volatility=None),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.ENTER
        solution = outcome.decision.width_solution
        assert solution is not None
        assert solution.mode is WidthSolveMode.FALLBACK_CEILING
        assert solution.half_width_ticks == 29
        joined = "\n".join(outcome.decision.diagnostics)
        assert "unusable" in joined
        assert "realized volatility" in joined

    def test_interior_solved_width_builds_the_matching_range(self) -> None:
        """A solve landing past the tightest candidate widens the range to it."""
        engine = PolicyEngine(
            parameters=PolicyParameters(
                tick_spacing=1,
                target_net_daily_yield=Decimal("0.22"),
            )
        )
        outcome = engine.decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.ENTER
        solution = outcome.decision.width_solution
        assert solution is not None
        assert solution.mode is WidthSolveMode.SOLVED
        assert solution.half_width_ticks == 2
        width = solution.half_width_fraction
        price_range = entered_position_for(outcome.next_state).price_range
        center = Decimal("200")
        assert price_range.lower_price <= center * (Decimal(1) - width)
        assert price_range.upper_price >= center * (Decimal(1) + width)

    def test_recenter_re_derives_the_width_at_the_current_price(self) -> None:
        """The recenter solves again from current evidence around the new price."""
        engine, state = entered_session(
            emissions_apr=Decimal("4000"),
            ranging=ranging_evidence(),
        )
        entered_solution = engine.decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(),
            ),
        ).decision.width_solution
        assert entered_solution is not None
        assert entered_solution.mode is WidthSolveMode.SOLVED
        upper = entered_position_for(state).price_range.upper_price
        new_price = Decimal("200.4")
        assert new_price > upper
        # The first above-range observation only starts the wait anchor.
        waiting = engine.decide(
            state,
            base_observation(
                observed_at=BASE_OBSERVED_AT + timedelta(minutes=1),
                amm_price_usdc=new_price,
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(),
            ),
        )
        assert waiting.decision.reason is PolicyReason.OPEN_ABOVE_RANGE_WAITING
        outcome = engine.decide(
            waiting.next_state,
            base_observation(
                observed_at=BASE_OBSERVED_AT + timedelta(minutes=17),
                amm_price_usdc=new_price,
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.RECENTER
        solution = outcome.decision.width_solution
        assert solution is not None
        assert solution.mode is WidthSolveMode.SOLVED
        assert solution.half_width_ticks == 10
        price_range = outcome.decision.price_range
        assert price_range is not None
        assert price_range.lower_price <= new_price * (Decimal(1) - solution.half_width_fraction)
        assert price_range.upper_price >= new_price * (Decimal(1) + solution.half_width_fraction)
        assert outcome.next_state.position is not None
        assert outcome.next_state.position.price_range == price_range

    def test_derived_tight_width_keeps_the_downside_stop_math(self) -> None:
        """The stop stays 0.5 percent below the aligned lower edge of a tight range."""
        engine, state = entered_session(
            emissions_apr=Decimal("4000"),
            ranging=ranging_evidence(),
        )
        lower = entered_position_for(state).price_range.lower_price
        stop_level = lower * Decimal("0.995")
        holding = engine.decide(
            state,
            base_observation(
                observed_at=BASE_OBSERVED_AT + timedelta(minutes=1),
                amm_price_usdc=(lower + stop_level) / Decimal(2),
            ),
        )
        assert holding.decision.reason is PolicyReason.OPEN_BELOW_EDGE_HOLDING
        stopped = engine.decide(
            state,
            base_observation(
                observed_at=BASE_OBSERVED_AT + timedelta(minutes=2),
                amm_price_usdc=stop_level * Decimal("0.999"),
            ),
        )
        assert stopped.decision.action is PolicyActionKind.STOP_OUT
        assert stopped.decision.reason is PolicyReason.DOWNSIDE_STOP_TRIGGERED

    def test_composition_splits_value_evenly_at_a_derived_tight_width(self) -> None:
        """The exact v3 composition still marks the center as an even split."""
        engine, state = entered_session(
            emissions_apr=Decimal("4000"),
            ranging=ranging_evidence(),
        )
        position = entered_position_for(state)
        with localcontext() as decimal_context:
            decimal_context.prec = 60
            center = (position.price_range.lower_price * position.price_range.upper_price).sqrt()
            stock_quantity, usdc_quantity = engine.position_composition(position, center)
            stock_value = stock_quantity * center
        assert abs(stock_value - usdc_quantity) < Decimal("1e-30")

    def test_zero_gas_price_observation_still_solves(self) -> None:
        """A zero gas reading passes the gate and solves with zero batch costs."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("4000"),
                gas_price_gwei=Decimal("0"),
                ranging=ranging_evidence(),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.ENTER
        solution = outcome.decision.width_solution
        assert solution is not None
        assert solution.mode is WidthSolveMode.SOLVED

    def test_entry_diagnostics_carry_the_full_width_derivation(self) -> None:
        """Diagnostics echo the derivation summary and every solver input."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(),
            ),
        )
        joined = "\n".join(outcome.decision.diagnostics)
        assert "Range half width" in joined
        assert "solve resolved as solved" in joined
        assert "Solved half width" in joined
        assert "Realized daily volatility 0.005" in joined
        assert "Target net daily yield 0.01" in joined
        assert "Gauge staked liquidity 80000000000000" in joined
        solution = outcome.decision.width_solution
        assert solution is not None
        assert [row.half_width_ticks for row in solution.evaluations] == list(range(10, 30))

    def test_plain_holds_carry_no_width_solution(self) -> None:
        """Decisions that build no range leave the width solution absent."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("0.5"),
                ranging=ranging_evidence(),
            ),
        )
        assert outcome.decision.action is PolicyActionKind.HOLD
        assert outcome.decision.width_solution is None

    def test_solved_width_never_exceeds_the_locked_ceiling(self) -> None:
        """Every solved candidate fraction stays at or below the ceiling."""
        outcome = PolicyEngine().decide(
            PolicyState(),
            base_observation(
                emissions_apr=Decimal("4000"),
                ranging=ranging_evidence(),
            ),
        )
        solution = outcome.decision.width_solution
        assert solution is not None
        assert all(row.half_width_fraction <= Decimal("0.003") for row in solution.evaluations)

    def test_explicit_width_outside_the_unit_interval_is_rejected(self) -> None:
        """A requested half width at or beyond one whole price unit fails closed."""
        with pytest.raises(ValueError, match="between zero and one"):
            PolicyEngine().build_aligned_range(Decimal("200"), Decimal("1.5"))
        with pytest.raises(ValueError, match="between zero and one"):
            PolicyEngine().build_aligned_range(Decimal("200"), Decimal("0"))
