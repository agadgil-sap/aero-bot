"""Behavior tests for the cross-board B20 pool selector."""

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from aero_bot.domain import normalize_evm_address
from aero_bot.policy import (
    HeldInventory,
    PolicyActionKind,
    PolicyEngine,
    PolicyObservation,
    PolicyReason,
    PolicyState,
)
from aero_bot.ranging import RangingEvidence
from aero_bot.selector import (
    DEFAULT_SWITCH_MARGIN_FRACTION,
    PoolBoardOption,
    evaluate_pool_entries,
    evaluate_switch,
    select_best_entry,
    select_board,
)

# Fixture identities: three pools, one per board symbol, all distinct.
AAA_TOKEN = normalize_evm_address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
BBB_TOKEN = normalize_evm_address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
CCC_TOKEN = normalize_evm_address("0xcccccccccccccccccccccccccccccccccccccccc")
AAA_POOL = normalize_evm_address("0x1111111111111111111111111111111111111111")
BBB_POOL = normalize_evm_address("0x2222222222222222222222222222222222222222")
CCC_POOL = normalize_evm_address("0x3333333333333333333333333333333333333333")
# The fixture session is a Saturday noon UTC, clear of every session window.
BASE_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
# The fixture observation mirrors the policy tests' passing entry shape:
# emissions over the floor, deep pool, fresh reference, cheap gas.
RANGING = RangingEvidence.model_validate(
    {
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
)


def board_option(
    symbol: str, pool_address: str, token_address: str, **overrides: object
) -> PoolBoardOption:
    """Build one board option over a passing entry observation.

    Args:
        symbol: The registry-matched stock symbol.
        pool_address: The verified pool contract.
        token_address: The B20 stock token.
        **overrides: Observation fields changed for one behavior test.

    Returns:
        A validated immutable board option.
    """
    values: dict[str, object] = {
        "observed_at": BASE_TIME,
        "pool_address": pool_address,
        "token_address": token_address,
        "amm_price_usdc": Decimal("200"),
        "emissions_apr": Decimal("2.0"),
        "fee_apr": Decimal("0.5"),
        "pool_depth_usd": Decimal("50000"),
        "equity_usd": Decimal("200"),
        "reference_price_usdc": Decimal("200"),
        "reference_age_seconds": 10,
        "gas_price_gwei": Decimal("0.002"),
        "ranging": RANGING,
    }
    values.update(overrides)
    return PoolBoardOption(
        symbol=symbol,
        pool_address=pool_address,
        token_address=token_address,
        observation=PolicyObservation.model_validate(values),
    )


def qualified_board() -> tuple[PoolBoardOption, ...]:
    """Build the three-pool scripted universe for selection tests."""
    return (
        board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
        board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.0")),
        board_option(
            "CCCc",
            CCC_POOL,
            CCC_TOKEN,
            emissions_apr=Decimal("1.2"),
        ),
    )


def held_state(pool_address: str, token_address: str) -> PolicyState:
    """Build one state whose funded position has cleared the switch hold period."""
    engine = PolicyEngine()
    entry = engine.decide(
        PolicyState(),
        board_option("AAAc", pool_address, token_address).observation,
    )
    assert entry.decision.action is PolicyActionKind.ENTER
    position = entry.next_state.position
    assert position is not None
    return entry.next_state.model_copy(
        update={
            "position": position.model_copy(
                update={"entered_at": BASE_TIME - timedelta(hours=2)}
            )
        }
    )


class TestBoardQualification:
    """Entry-gate evaluation and best-pool selection across the board."""

    def test_selector_picks_the_best_qualifying_pool(self) -> None:
        """The highest qualifying emissions APR wins across the universe."""
        options = qualified_board()
        selection = select_board(PolicyEngine(), PolicyState(), options, {})
        assert selection.selected_symbol == "BBBc"
        assert selection.outcome.decision.action is PolicyActionKind.ENTER
        assert selection.outcome.decision.size_usd == Decimal("160")
        position = selection.outcome.next_state.position
        assert position is not None
        assert position.pool_address == BBB_POOL
        assert selection.switch is None

    def test_disqualifying_pools_are_skipped_with_reasons(self) -> None:
        """Every blocking gate names its stable reason in the evaluation."""
        options = (
            board_option(
                "AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("1.2")
            ),  # below the 150 percent floor
            board_option(
                "BBBc", BBB_POOL, BBB_TOKEN, reference_price_usdc=None
            ),  # no reference quote
            board_option("CCCc", CCC_POOL, CCC_TOKEN),  # qualified
        )
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, PolicyState(), options, {})
        reasons = {evaluation.symbol: evaluation.blocked_reason for evaluation in evaluations}
        assert reasons["AAAc"] is PolicyReason.EMISSIONS_BELOW_ENTRY_THRESHOLD
        assert reasons["BBBc"] is PolicyReason.REFERENCE_STALE
        assert reasons["CCCc"] is None
        selection = select_board(engine, PolicyState(), options, {})
        assert selection.selected_symbol == "CCCc"
        assert "AAAc: skipped (emissions_below_entry_threshold)" in selection.summary
        assert "BBBc: skipped (reference_stale)" in selection.summary

    def test_ties_break_deterministically_on_the_symbol(self) -> None:
        """Equal qualifying APRs select the lexicographically smallest symbol."""
        options = (
            board_option("BBBc", BBB_POOL, BBB_TOKEN),
            board_option("AAAc", AAA_POOL, AAA_TOKEN),
        )
        selection = select_board(PolicyEngine(), PolicyState(), options, {})
        assert selection.selected_symbol == "AAAc"
        # The ranking helper pins the same tie-break directly.
        evaluations = evaluate_pool_entries(PolicyEngine(), PolicyState(), options, {})
        best = select_best_entry(evaluations)
        assert best is not None
        assert best.symbol == "AAAc"

    def test_no_qualifying_pool_holds_with_every_reason(self) -> None:
        """A fully disqualified board holds naming each pool's reason."""
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("1.2")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, reference_price_usdc=None),
        )
        selection = select_board(PolicyEngine(), PolicyState(), options, {})
        assert selection.outcome.decision.action is PolicyActionKind.HOLD
        assert selection.outcome.decision.reason is PolicyReason.NO_QUALIFYING_POOL
        assert "AAAc emissions_below_entry_threshold" in selection.outcome.decision.diagnostics[0]
        assert "BBBc reference_stale" in selection.outcome.decision.diagnostics[0]
        assert selection.selected_symbol is None

    def test_per_pool_cooldown_blocks_only_its_own_pool(self) -> None:
        """A stop-out cooldown on one pool never blocks another pool's entry."""
        options = qualified_board()
        cooldowns = {"AAAc": BASE_TIME + timedelta(minutes=10)}
        evaluations = evaluate_pool_entries(PolicyEngine(), PolicyState(), options, cooldowns)
        blocked = {evaluation.symbol: evaluation.blocked_reason for evaluation in evaluations}
        assert blocked["AAAc"] is PolicyReason.ENTRY_COOLDOWN_ACTIVE
        assert blocked["BBBc"] is None
        assert blocked["CCCc"] is not PolicyReason.ENTRY_COOLDOWN_ACTIVE
        # The expired cooldown on another pool leaves selection unchanged.
        selection = select_board(PolicyEngine(), PolicyState(), options, cooldowns)
        assert selection.selected_symbol == "BBBc"

    def test_daily_halt_blocks_the_whole_board(self) -> None:
        """The session-scoped daily loss halt disqualifies every pool."""
        options = qualified_board()
        state = PolicyState(day=BASE_TIME.date(), halted_day=BASE_TIME.date())
        selection = select_board(PolicyEngine(), state, options, {})
        assert selection.outcome.decision.action is PolicyActionKind.HOLD
        assert selection.outcome.decision.reason is PolicyReason.NO_QUALIFYING_POOL
        assert "daily_loss_halt_active" in selection.outcome.decision.diagnostics[0]
        assert all(
            evaluation.blocked_reason is PolicyReason.DAILY_LOSS_HALT_ACTIVE
            for evaluation in selection.evaluations
        )


class TestSwitchDiscipline:
    """The anti-churn margin, gas economics, and precedence rules."""

    def held_board(self) -> tuple[PolicyEngine, PolicyState, tuple[PoolBoardOption, ...]]:
        """Build one in-range AAAc position with BBBc and CCCc alongside."""
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.0")),
            board_option("CCCc", CCC_POOL, CCC_TOKEN, emissions_apr=Decimal("1.2")),
        )
        return PolicyEngine(), held_state(AAA_POOL, AAA_TOKEN), options

    def test_fresh_position_observes_the_one_hour_switch_hold(self) -> None:
        """A new position cannot churn solely for a better APR during its first hour."""
        engine = PolicyEngine()
        entry = engine.decide(
            PolicyState(),
            board_option("AAAc", AAA_POOL, AAA_TOKEN).observation,
        )
        assert entry.next_state.position is not None
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.1")),
        )

        selection = select_board(engine, entry.next_state, options, {})

        assert selection.outcome.decision.action is PolicyActionKind.HOLD
        assert selection.switch is None
        assert "voluntary switch hold period active" in selection.summary

    def test_no_switch_below_the_margin(self) -> None:
        """A candidate inside the thirty percent margin never churns."""
        engine, state, _ = self.held_board()
        # BBBc at 2.5 sits inside the 2.6 threshold (2.0 held plus the margin).
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("2.5")),
        )
        selection = select_board(engine, state, options, {})
        assert selection.outcome.decision.action is PolicyActionKind.HOLD
        assert selection.outcome.decision.reason is PolicyReason.OPEN_IN_RANGE
        assert selection.switch is None
        assert "switch margin not met" in selection.summary

    def test_switch_above_the_margin_when_gas_economics_pass(self) -> None:
        """A candidate past the margin switches with combined gas evidence."""
        engine, state, _ = self.held_board()
        # BBBc at 3.1 clears the 2.6 threshold (2.0 held plus the margin).
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.1")),
        )
        selection = select_board(engine, state, options, {})
        assert selection.outcome.decision.action is PolicyActionKind.POOL_SWITCH
        assert selection.outcome.decision.reason is PolicyReason.POOL_SWITCH_TRIGGERED
        assert selection.switch is not None
        assert selection.switch.from_symbol == "AAAc"
        assert selection.switch.to_symbol == "BBBc"
        assert selection.selected_symbol == "BBBc"
        # The composed decision carries the winning entry's full economics.
        assert selection.outcome.decision.size_usd == Decimal("160")
        assert selection.outcome.decision.price_range is not None
        assert selection.outcome.decision.estimated_gas_units == 1_200_000
        # Exactly one position survives: the new pool's.
        position = selection.outcome.next_state.position
        assert position is not None
        assert position.pool_address == BBB_POOL

    def test_switch_refused_when_gas_economics_fail(self) -> None:
        """An entry that alone passes can still fail the combined cost bound.

        The BBBc entry is depth-capped near 111 USDC and priced at 0.02
        gwei: its own 750k-unit batch costs 0.045 USDC against the 0.0548
        USDC cost-share bound, so the entry gate passes - but the combined
        exit-plus-entry 1.2M units cost 0.072 USDC exceeds that bound, so
        the switch refuses.
        """
        engine, state, _ = self.held_board()
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option(
                "BBBc",
                BBB_POOL,
                BBB_TOKEN,
                emissions_apr=Decimal("3.1"),
                pool_depth_usd=Decimal("11111"),
                gas_price_gwei=Decimal("0.02"),
            ),
        )
        evaluations = evaluate_pool_entries(engine, PolicyState(), options, {})
        assert {evaluation.symbol: evaluation.qualifies for evaluation in evaluations}[
            "BBBc"
        ] is True
        selection = select_board(engine, state, options, {})
        assert selection.outcome.decision.action is PolicyActionKind.HOLD
        assert selection.switch is None
        assert "switch gas economics refused" in selection.summary

    def test_switch_fails_closed_without_a_gas_reading(self) -> None:
        """A pool the entry gate itself deferred never feeds a switch."""
        engine, state, options = self.held_board()
        no_gas = (
            options[0],
            board_option(
                "BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.1"), gas_price_gwei=None
            ),
            options[2],
        )
        selection = select_board(engine, state, no_gas, {})
        assert selection.outcome.decision.action is PolicyActionKind.HOLD
        assert selection.switch is None

    def test_the_margin_is_configurable(self) -> None:
        """A wider configured margin absorbs candidates the default churns on."""
        engine, state, options = self.held_board()
        directive_default, _ = evaluate_switch(
            engine,
            "AAAc",
            Decimal("2.0"),
            evaluate_pool_entries(engine, PolicyState(), options, {}),
            DEFAULT_SWITCH_MARGIN_FRACTION,
        )
        assert directive_default is not None
        directive_wide, _ = evaluate_switch(
            engine,
            "AAAc",
            Decimal("2.0"),
            evaluate_pool_entries(engine, PolicyState(), options, {}),
            Decimal("0.6"),
        )
        assert directive_wide is None

    def test_safety_exit_takes_precedence_over_any_switch(self) -> None:
        """The held pool's own stop-out wins over a better qualifying pool."""
        engine, state, options = self.held_board()
        position = state.position
        assert position is not None
        stop_price = position.price_range.lower_price * Decimal("0.994")
        crashed = (
            board_option(
                "AAAc",
                AAA_POOL,
                AAA_TOKEN,
                amm_price_usdc=stop_price,
                reference_price_usdc=stop_price,
            ),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.1")),
        )
        selection = select_board(engine, state, crashed, {})
        assert selection.outcome.decision.action is PolicyActionKind.STOP_OUT
        assert selection.switch is None
        assert selection.selected_symbol == "AAAc"
        # The stop arms the exited pool's cooldown in the successor state.
        assert selection.outcome.next_state.reentry_blocked_until is not None

    def test_held_inventory_resolves_before_any_board_entry(self) -> None:
        """Unsold stock from a stale-low burn blocks every new entry."""
        engine = PolicyEngine()
        entry = engine.decide(PolicyState(), board_option("AAAc", AAA_POOL, AAA_TOKEN).observation)
        assert entry.decision.action is PolicyActionKind.ENTER
        state = entry.next_state.model_copy(
            update={
                "position": None,
                "held_inventory": HeldInventory(
                    pool_address=AAA_POOL,
                    token_address=AAA_TOKEN,
                    stock_quantity=Decimal("0.4"),
                    held_since=BASE_TIME,
                ),
            }
        )
        options = (
            # The reference above the pool price keeps the held tokens
            # unconverged, so the inventory branch must hold them.
            board_option("AAAc", AAA_POOL, AAA_TOKEN, reference_price_usdc=Decimal("201")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.1")),
        )
        selection = select_board(engine, state, options, {})
        assert selection.outcome.decision.action is PolicyActionKind.HOLD
        assert (
            selection.outcome.decision.reason is PolicyReason.HOLDING_INVENTORY_AWAITING_CONVERGENCE
        )
        assert selection.selected_symbol == "AAAc"


class TestSinglePositionInvariant:
    """At most one funded position exists under every selection path."""

    def test_flat_entry_funds_exactly_one_position(self) -> None:
        """A flat selection enters one pool and leaves every other out."""
        selection = select_board(PolicyEngine(), PolicyState(), qualified_board(), {})
        position = selection.outcome.next_state.position
        assert position is not None
        assert selection.outcome.next_state.held_inventory is None

    def test_held_board_never_adds_a_second_position(self) -> None:
        """A below-margin board holds the one funded position unchanged."""
        engine, state = PolicyEngine(), held_state(AAA_POOL, AAA_TOKEN)
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("2.5")),
        )
        selection = select_board(engine, state, options, {})
        position = selection.outcome.next_state.position
        assert position is not None
        assert position.pool_address == AAA_POOL
        assert selection.outcome.next_state.held_inventory is None

    def test_switch_replaces_rather_than_stacks_the_position(self) -> None:
        """A qualified switch ends with exactly the new pool's position."""
        engine, state, _ = (
            PolicyEngine(),
            held_state(AAA_POOL, AAA_TOKEN),
            qualified_board(),
        )
        options = (
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.1")),
        )
        selection = select_board(engine, state, options, {})
        assert selection.outcome.decision.action is PolicyActionKind.POOL_SWITCH
        next_state = selection.outcome.next_state
        position = next_state.position
        assert position is not None
        assert position.pool_address == BBB_POOL
        assert next_state.held_inventory is None
        # The switch arms no cooldown: the exit is voluntary, not a stop.
        assert next_state.reentry_blocked_until is None

    def test_a_held_pool_missing_from_the_board_refuses(self) -> None:
        """A tracked position whose pool left the board fails loudly."""
        engine, state = PolicyEngine(), held_state(AAA_POOL, AAA_TOKEN)
        options = (board_option("BBBc", BBB_POOL, BBB_TOKEN),)
        try:
            select_board(engine, state, options, {})
        except ValueError as error:
            assert "not on the enumerated board" in str(error)
        else:
            raise AssertionError("a delisted held pool did not refuse")


class TestProvenance:
    """The selector's ruling provenance pins in source."""

    def test_default_margin_carries_the_ruling(self) -> None:
        """The thirty-percent default records the 2026-09-09 ruling."""
        from aero_bot import selector

        assert Decimal("0.30") == DEFAULT_SWITCH_MARGIN_FRACTION
        source = inspect.getsource(selector)
        assert "captain's 2026-09-09 trial ruling" in source
        assert 'Decimal("0.30")' in source
