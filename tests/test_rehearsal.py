"""Behavior tests for the offline rehearsal replay and P&L ledger."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from aero_bot.history import EmissionsAprHistory, EmissionsAprPoint, PoolPricePath, PoolPricePoint
from aero_bot.policy import PolicyActionKind, PolicyReason
from aero_bot.rehearsal import (
    DEFAULT_SYNTHETIC_DISLOCATION_SCHEDULE,
    PoolRehearsalLedger,
    ReferenceQuote,
    RehearsalActionCounts,
    RehearsalActionRecord,
    RehearsalAssumptions,
    SyntheticDislocationSchedule,
    SyntheticEpisode,
    SyntheticEpisodeKind,
    band_depth_usd,
    build_reference_quotes,
    replay_pool,
    swap_usd_notional,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
OTHER_POOL_ADDRESS = "0x1313131313131313131313131313131313131313"
GAUGE_ADDRESS = "0x3111111111111111111111111111111111111111"
# The fixture session is a Saturday noon UTC, clear of every session event window.
BASE_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
# Six-and-six decimals make the raw price equal the human price in fixtures.
FIXTURE_STOCK_DECIMALS = 6
FIXTURE_QUOTE_DECIMALS = 6
# The default raw active liquidity prices a one-percent band near twenty
# thousand USDC at price one hundred, so the equity cap binds entries.
DEFAULT_POOL_LIQUIDITY = 80_000_000_000_000
# Every fixture swap moves a five-thousand USDC notional.
DEFAULT_SWAP_NOTIONAL_RAW = 5_000_000_000
# The fixture gauge shares the pool's liquidity so staking shares stay readable.
DEFAULT_GAUGE_LIQUIDITY = DEFAULT_POOL_LIQUIDITY
# The fixture reward rate is one whole AERO per second.
DEFAULT_EMISSIONS_PER_SECOND = 10**18
# Two fractional comparisons below need a shared high-precision context.
MATH_PRECISION = 60


def make_point(
    index: int,
    price: Decimal,
    liquidity: int = DEFAULT_POOL_LIQUIDITY,
    step_seconds: int = 60,
) -> PoolPricePoint:
    """Build one synthetic swap observation with a consistent raw witness.

    Args:
        index: Observation offset from the session base instant.
        price: Human USDC-per-stock price this swap settled at.
        liquidity: Raw active pool liquidity after the swap.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        An immutable price point with onchain-shaped evidence.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        # Equal six-and-six decimals make the raw price the human price.
        raw_price = price
        sqrt_ratio = int((raw_price * Decimal(1 << 192)).sqrt())
        tick = int((price.ln() / Decimal("1.0001").ln()).to_integral_value(rounding="ROUND_FLOOR"))
    return PoolPricePoint(
        timestamp=BASE_TIME + timedelta(seconds=step_seconds * index),
        block_number=index,
        log_index=0,
        amount0=0,
        amount1=DEFAULT_SWAP_NOTIONAL_RAW,
        sqrt_ratio=sqrt_ratio,
        liquidity=liquidity,
        tick=tick,
        price_usdc=price,
    )


def make_path(
    prices: list[Decimal],
    liquidity: int | Sequence[int] = DEFAULT_POOL_LIQUIDITY,
    step_seconds: int = 60,
) -> PoolPricePath:
    """Build one synthetic price path from a list of prices.

    Args:
        prices: Ordered human prices, one per reconstructed swap.
        liquidity: Raw active pool liquidity carried by every swap, either one
            value for all points or one value per point.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        An immutable price path over the synthetic session.
    """
    per_point_liquidity = [liquidity] * len(prices) if isinstance(liquidity, int) else liquidity
    points = tuple(
        make_point(index, price, level, step_seconds)
        for index, (price, level) in enumerate(zip(prices, per_point_liquidity, strict=True))
    )
    return PoolPricePath(
        pool_address=POOL_ADDRESS,
        token_address=B20_ADDRESS,
        token_is_token0=True,
        token_decimals=FIXTURE_STOCK_DECIMALS,
        quote_decimals=FIXTURE_QUOTE_DECIMALS,
        from_block=0,
        to_block=max(len(prices) - 1, 0),
        observed_at=BASE_TIME,
        points=points,
    )


def make_apr_step(
    index: int,
    emissions_apr: Decimal,
    gauge_liquidity: int = DEFAULT_GAUGE_LIQUIDITY,
    step_seconds: int = 60,
) -> EmissionsAprPoint:
    """Build one synthetic emissions-APR step.

    Args:
        index: Step offset from the session base instant.
        emissions_apr: Raw emissions APR in Aerodrome's display convention.
        gauge_liquidity: Staked gauge liquidity from this step onward.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        An immutable APR step without onchain ordering evidence.
    """
    return EmissionsAprPoint(
        timestamp=BASE_TIME + timedelta(seconds=step_seconds * index),
        gauge_liquidity=gauge_liquidity,
        emissions_apr=emissions_apr,
    )


def make_apr_history(
    steps: list[EmissionsAprPoint],
    aero_price_assumption_usd: Decimal = Decimal("0.5"),
) -> EmissionsAprHistory:
    """Build one synthetic emissions-APR series over the fixture window.

    Args:
        steps: Ordered APR steps starting at the session base instant.
        aero_price_assumption_usd: AERO price the series assumes.

    Returns:
        An immutable series labeled event_fold over the synthetic session.
    """
    return EmissionsAprHistory(
        pool_address=POOL_ADDRESS,
        gauge_address=GAUGE_ADDRESS,
        from_block=0,
        to_block=10_000,
        anchor_block=10_000,
        observed_at=BASE_TIME,
        anchor_gauge_liquidity=DEFAULT_GAUGE_LIQUIDITY,
        anchor_staked_tvl_usd=Decimal("100000"),
        emissions_per_second=DEFAULT_EMISSIONS_PER_SECOND,
        aero_price_assumption_usd=aero_price_assumption_usd,
        reconstruction_mode="event_fold",
        starting_gauge_liquidity=DEFAULT_GAUGE_LIQUIDITY,
        steps=tuple(steps),
    )


def run_rehearsal(
    prices: list[Decimal],
    apr_steps: list[EmissionsAprPoint],
    *,
    liquidity: int | Sequence[int] = DEFAULT_POOL_LIQUIDITY,
    step_seconds: int = 60,
    schedule: SyntheticDislocationSchedule | None = None,
    assumptions: RehearsalAssumptions | None = None,
) -> PoolRehearsalLedger:
    """Replay one synthetic session through the default fixture assumptions.

    Args:
        prices: Ordered human prices, one per reconstructed swap.
        apr_steps: Ordered emissions-APR steps for the same session.
        liquidity: Raw active pool liquidity, one value or one per swap.
        step_seconds: Wall-clock seconds between observations.
        schedule: Optional synthetic dislocation overlay.
        assumptions: Optional assumption overrides.

    Returns:
        The deterministic ledger for the synthetic session.
    """
    return replay_pool(
        price_path=make_path(prices, liquidity, step_seconds),
        emissions_history=make_apr_history(apr_steps),
        symbol="AAPLc/USDC",
        assumptions=assumptions or RehearsalAssumptions(pool_fee_ppm=500),
        schedule=schedule,
    )


def high_apr_steps(count: int, step_seconds: int = 60) -> list[EmissionsAprPoint]:
    """Build APR steps holding a three-hundred-percent APR for one session.

    Args:
        count: Number of observations the session spans.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        A single opening step at the threshold-clearing APR.
    """
    return [make_apr_step(0, Decimal("3.0"), step_seconds=step_seconds)]


class TestReferencePathBuilders:
    """Tests for the reference path and its synthetic stress overlay."""

    def test_default_reference_equals_the_amm_path(self) -> None:
        """Without episodes every quote mirrors its price point exactly."""
        path = make_path([Decimal("100"), Decimal("101")])
        quotes = build_reference_quotes(path)

        assert [q.price_usdc for q in quotes] == [Decimal("100"), Decimal("101")]
        assert [q.timestamp for q in quotes] == [p.timestamp for p in path.points]

    def test_stale_high_episode_holds_the_reference_below_the_amm(self) -> None:
        """A stale-high episode scales the covered quotes down by its magnitude."""
        path = make_path([Decimal("100")] * 11)
        schedule = SyntheticDislocationSchedule(
            episodes=(
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_HIGH,
                    start_fraction=Decimal("0.5"),
                    duration=timedelta(minutes=2),
                ),
            )
        )
        quotes = build_reference_quotes(path, schedule)

        # The episode covers the observations five and six minutes in.
        assert quotes[4].price_usdc == Decimal("100")
        assert quotes[5].price_usdc == Decimal("100") * Decimal("0.995")
        assert quotes[6].price_usdc == Decimal("100") * Decimal("0.995")
        assert quotes[7].price_usdc == Decimal("100")

    def test_stale_feed_episode_drops_covered_quotes(self) -> None:
        """A stale-feed episode removes the covered quotes entirely."""
        path = make_path([Decimal("100")] * 6, step_seconds=600)
        schedule = SyntheticDislocationSchedule(
            episodes=(
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_FEED,
                    start_fraction=Decimal("0.4"),
                    duration=timedelta(minutes=30),
                ),
            )
        )
        quotes = build_reference_quotes(path, schedule)

        # The outage spans 12:20 through 12:40 exclusive of 12:50.
        assert [q.timestamp for q in quotes] == [
            BASE_TIME,
            BASE_TIME + timedelta(minutes=10),
            BASE_TIME + timedelta(minutes=50),
        ]

    def test_default_schedule_episodes_do_not_overlap(self) -> None:
        """The bundled default schedule is internally non-overlapping."""
        path = make_path([Decimal("100")] * 400, step_seconds=600)

        quotes = build_reference_quotes(path, DEFAULT_SYNTHETIC_DISLOCATION_SCHEDULE)

        assert len(quotes) > 0

    def test_overlapping_episodes_are_rejected(self) -> None:
        """Two episodes sharing an instant fail closed instead of amBIGuating."""
        path = make_path([Decimal("100")] * 11)
        schedule = SyntheticDislocationSchedule(
            episodes=(
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_HIGH,
                    start_fraction=Decimal("0.5"),
                    duration=timedelta(minutes=5),
                ),
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_LOW,
                    start_fraction=Decimal("0.5"),
                    duration=timedelta(minutes=1),
                ),
            )
        )

        with pytest.raises(ValueError, match="overlap"):
            build_reference_quotes(path, schedule)

    def test_empty_path_with_a_schedule_is_rejected(self) -> None:
        """A stress overlay on an empty reconstruction has no meaning."""
        with pytest.raises(ValueError, match="non-empty"):
            build_reference_quotes(
                make_path([]),
                SyntheticDislocationSchedule(
                    episodes=(
                        SyntheticEpisode(
                            kind=SyntheticEpisodeKind.STALE_HIGH,
                            start_fraction=Decimal("0.5"),
                            duration=timedelta(minutes=1),
                        ),
                    )
                ),
            )

    def test_episode_and_quote_validators_reject_degenerate_inputs(self) -> None:
        """Zero durations and naive quote timestamps fail model validation."""
        with pytest.raises(ValidationError, match="positive"):
            SyntheticEpisode(
                kind=SyntheticEpisodeKind.STALE_HIGH,
                start_fraction=Decimal("0.5"),
                duration=timedelta(0),
            )
        with pytest.raises(ValidationError, match="timezone-aware"):
            ReferenceQuote(timestamp=datetime(2026, 8, 15, 12), price_usdc=Decimal("100"))


class TestValuationHelpers:
    """Tests for the depth, notional, and liquidity helpers."""

    def test_band_depth_scales_linearly_with_liquidity(self) -> None:
        """Depth is proportional to active liquidity and positive for real bands."""
        small = band_depth_usd(
            10**12,
            Decimal("100"),
            Decimal("0.01"),
            FIXTURE_STOCK_DECIMALS,
            FIXTURE_QUOTE_DECIMALS,
        )
        large = band_depth_usd(
            10**14,
            Decimal("100"),
            Decimal("0.01"),
            FIXTURE_STOCK_DECIMALS,
            FIXTURE_QUOTE_DECIMALS,
        )

        assert small > 0
        # The ratio is exact even though each value carries high-precision tails.
        assert large / small == Decimal(100)

    def test_band_depth_rejects_degenerate_inputs(self) -> None:
        """Non-positive prices or widths fail closed."""
        with pytest.raises(ValueError, match="positive"):
            band_depth_usd(1, Decimal("0"), Decimal("0.01"), 6, 6)
        with pytest.raises(ValueError, match="positive"):
            band_depth_usd(1, Decimal("100"), Decimal("0"), 6, 6)

    def test_swap_notional_takes_the_larger_valued_side(self) -> None:
        """The notional proxy values both sides and keeps the larger."""
        point = make_point(0, Decimal("100"))
        stock_heavy = point.model_copy(update={"amount0": 2_000_000, "amount1": 50_000_000})

        assert swap_usd_notional(point, True, 6, 6) == Decimal("5000")
        # Two stock tokens at one hundred USDC outweigh fifty USDC.
        assert swap_usd_notional(stock_heavy, True, 6, 6) == Decimal("200")


class TestReplayLifecycle:
    """Tests for full replays over synthetic sessions."""

    def test_entry_hold_and_downside_stop_out(self) -> None:
        """A session enters, holds below the edge, and stops out on the crash."""
        prices = [Decimal("100")] * 10 + [Decimal("99.5"), Decimal("98.5")]
        ledger = run_rehearsal(prices, high_apr_steps(len(prices)))

        assert ledger.observation_count == 12
        assert ledger.action_counts.entries == 1
        assert ledger.action_counts.stop_outs == 1
        assert ledger.actions[0].action == PolicyActionKind.ENTER
        assert ledger.actions[1].action == PolicyActionKind.STOP_OUT
        # The committed size is the twenty-percent equity cap.
        assert ledger.actions[0].size_usd == Decimal("40")
        # The stop-out swap converts the whole stock inventory back to USDC.
        assert ledger.actions[1].swap_total_usd is not None
        assert ledger.actions[1].swap_total_usd > Decimal("35")
        assert ledger.actions[1].swap_impact_cost_usd > 0
        # The stop batch burns, unstakes, and swaps at the documented assumptions.
        assert ledger.actions[1].estimated_gas_cost_usd == Decimal("0.00135")
        # Exposure spans twelve minutes minus the cooldown tail, in range until
        # the price left the band one observation before the stop.
        assert ledger.time_open_seconds == 660
        assert ledger.time_in_range_seconds == 600
        assert ledger.final_open_position_committed_usd is None
        assert ledger.pnl_usd < 0
        assert ledger.aero_accrued_units > 0
        assert ledger.fees_accrued_usd > 0

    def test_dilution_exit_when_the_apr_falls_below_threshold(self) -> None:
        """A reconstructed APR drop closes the position and arms the cooldown."""
        steps = [
            make_apr_step(0, Decimal("3.0")),
            make_apr_step(5, Decimal("1.2")),
        ]
        prices = [Decimal("100")] * 12
        ledger = run_rehearsal(prices, steps)

        assert ledger.action_counts.entries == 1
        assert ledger.action_counts.dilution_exits == 1
        assert ledger.actions[1].action == PolicyActionKind.DILUTION_EXIT
        assert ledger.actions[1].reason == PolicyReason.DILUTION_EXIT_TRIGGERED
        assert ledger.actions[1].timestamp == BASE_TIME + timedelta(minutes=5)
        # The cooldown runs fifteen minutes and the APR stays below threshold,
        # so no re-entry appears for the rest of the session.
        assert all(a.action != PolicyActionKind.ENTER for a in ledger.actions[2:])

    def test_upside_recenter_after_the_fifteen_minute_wait(self) -> None:
        """A sustained move above the range recenters once the wait elapses."""
        prices = [Decimal("100")] + [Decimal("101")] * 16
        ledger = run_rehearsal(prices, high_apr_steps(len(prices)))

        assert ledger.action_counts.entries == 1
        assert ledger.action_counts.recenters == 1
        recenter = ledger.actions[1]
        assert recenter.action == PolicyActionKind.RECENTER
        assert recenter.reason == PolicyReason.RECENTER_WAIT_ELAPSED
        assert recenter.timestamp == BASE_TIME + timedelta(minutes=16)
        # The new range spans at least the locked half width around 101.
        assert recenter.range_upper_price is not None
        assert Decimal("101") * Decimal("1.0029") < recenter.range_upper_price
        assert recenter.range_upper_price < Decimal("101") * Decimal("1.0045")

    def test_stale_high_episode_exits_on_the_amm(self) -> None:
        """A synthetic stale-high spell sells into the pool while it is high."""
        prices = [Decimal("100")] * 11
        schedule = SyntheticDislocationSchedule(
            episodes=(
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_HIGH,
                    start_fraction=Decimal("0.5"),
                    duration=timedelta(minutes=2),
                ),
            )
        )
        ledger = run_rehearsal(prices, high_apr_steps(len(prices)), schedule=schedule)

        assert ledger.reference_mode == "synthetic_stress"
        assert ledger.action_counts.dislocation_exits == 1
        exit_record = next(
            a for a in ledger.actions if a.action == PolicyActionKind.DISLOCATION_EXIT
        )
        assert exit_record.timestamp == BASE_TIME + timedelta(minutes=5)
        assert exit_record.reason == PolicyReason.DISLOCATION_STALE_HIGH_TRIGGERED

    def test_stale_low_burn_then_convergence_sell(self) -> None:
        """A short stale-low spell burns, holds, and sells on convergence."""
        prices = [Decimal("100")] * 8
        schedule = SyntheticDislocationSchedule(
            episodes=(
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_LOW,
                    start_fraction=Decimal("0.5"),
                    duration=timedelta(minutes=2),
                ),
            )
        )
        ledger = run_rehearsal(prices, high_apr_steps(len(prices)), schedule=schedule)

        assert ledger.action_counts.stale_low_burns == 1
        assert ledger.action_counts.sell_inventory_convergence == 1
        burn = next(a for a in ledger.actions if a.action == PolicyActionKind.STALE_LOW_BURN)
        sell = next(a for a in ledger.actions if a.action == PolicyActionKind.SELL_INVENTORY)
        # The episode covers 12:03:30 through 12:05:30, so the burn lands on
        # the 12:04 observation and convergence releases at 12:06.
        assert burn.timestamp == BASE_TIME + timedelta(minutes=4)
        assert sell.timestamp == BASE_TIME + timedelta(minutes=6)
        assert sell.reason == PolicyReason.INVENTORY_CONVERGENCE_REACHED
        # The stale-low burn performs no swap; the sell converts the tokens.
        assert burn.swap_total_usd is None
        assert sell.swap_total_usd is not None

    def test_stale_low_burn_then_timeout_sell(self) -> None:
        """A stale-low spell outlasting the timeout sells the tokens at market."""
        prices = [Decimal("100")] * 12
        schedule = SyntheticDislocationSchedule(
            episodes=(
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_LOW,
                    start_fraction=Decimal("0.5"),
                    duration=timedelta(minutes=8),
                ),
            )
        )
        ledger = run_rehearsal(prices, high_apr_steps(len(prices)), schedule=schedule)

        assert ledger.action_counts.stale_low_burns == 1
        assert ledger.action_counts.sell_inventory_timeout == 1
        burn = next(a for a in ledger.actions if a.action == PolicyActionKind.STALE_LOW_BURN)
        sell = next(a for a in ledger.actions if a.action == PolicyActionKind.SELL_INVENTORY)
        # The episode starts at 12:05:30, so the burn lands on the 12:06
        # observation and the five-minute timeout releases at 12:11.
        assert burn.timestamp == BASE_TIME + timedelta(minutes=6)
        assert sell.timestamp == BASE_TIME + timedelta(minutes=11)
        assert sell.reason == PolicyReason.INVENTORY_CONVERGENCE_TIMEOUT

    def test_stale_feed_exits_defensively_then_blocks_and_reenters(self) -> None:
        """A reference outage exits defensively, blocks entries, then clears."""
        prices = [Decimal("100")] * 6
        schedule = SyntheticDislocationSchedule(
            episodes=(
                SyntheticEpisode(
                    kind=SyntheticEpisodeKind.STALE_FEED,
                    start_fraction=Decimal("0.4"),
                    duration=timedelta(minutes=30),
                ),
            )
        )
        ledger = run_rehearsal(
            prices,
            high_apr_steps(len(prices), step_seconds=600),
            step_seconds=600,
            schedule=schedule,
        )

        assert ledger.action_counts.defensive_exits == 1
        assert ledger.action_counts.entries == 2
        defensive = next(a for a in ledger.actions if a.action == PolicyActionKind.DEFENSIVE_EXIT)
        # The outage starts at 12:20; the reference ages past the
        # fifteen-minute open-position bound at the 12:30 observation.
        assert defensive.timestamp == BASE_TIME + timedelta(minutes=30)
        assert defensive.reason == PolicyReason.REFERENCE_STALE_DEFENSIVE_EXIT
        # The 12:40 observation stays flat with a stale reference, and the
        # outage clears in time for the 12:50 re-entry.
        reentry = ledger.actions[-1]
        assert reentry.action == PolicyActionKind.ENTER
        assert reentry.timestamp == BASE_TIME + timedelta(minutes=50)

    def test_gas_gate_defers_entries_at_a_high_gas_price(self) -> None:
        """A gas assumption over the ceiling defers every entry."""
        assumptions = RehearsalAssumptions(
            pool_fee_ppm=500, gas_price_assumption_gwei=Decimal("1.0")
        )
        ledger = run_rehearsal(
            [Decimal("100")] * 3,
            high_apr_steps(3),
            assumptions=assumptions,
        )

        assert ledger.action_counts.gas_deferrals == 3
        assert ledger.action_counts.entries == 0
        assert all(a.reason == PolicyReason.GAS_GATE_DEFERRED for a in ledger.actions)
        # Nothing executed, so the books close exactly where they opened.
        assert ledger.final_cash_usd == Decimal("200")
        assert ledger.pnl_usd == 0
        assert ledger.return_fraction == 0

    def test_zero_observed_liquidity_blocks_entries_fail_closed(self) -> None:
        """A pool with no reconstructable depth leaves the policy flat."""
        ledger = run_rehearsal([Decimal("100")] * 3, high_apr_steps(3), liquidity=0)

        assert ledger.action_counts.entries == 0
        assert ledger.actions == ()
        assert ledger.observation_count == 3
        assert ledger.pnl_usd == 0

    def test_empty_price_path_emits_an_empty_ledger(self) -> None:
        """A quiet pool replays to a zero-observation ledger unchanged."""
        ledger = run_rehearsal([], high_apr_steps(0))

        assert ledger.observation_count == 0
        assert ledger.window_start is None
        assert ledger.window_end is None
        assert ledger.actions == ()
        assert ledger.final_equity_usd == Decimal("200")
        assert ledger.pnl_usd == 0
        assert ledger.time_open_seconds == 0


class TestReplayAccounting:
    """Tests for the accrual arithmetic and ledger integrity."""

    def test_fee_and_emissions_accrual_match_first_principles(self) -> None:
        """A flat in-range session accrues exactly the pro-rata streams."""
        prices = [Decimal("100")] * 5
        ledger = run_rehearsal(prices, high_apr_steps(len(prices)))

        entry = ledger.actions[0]
        assert entry.size_usd is not None
        assert entry.range_lower_price is not None
        assert entry.range_upper_price is not None
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_lower = entry.range_lower_price.sqrt()
            sqrt_upper = entry.range_upper_price.sqrt()
            sqrt_center = (sqrt_lower * sqrt_upper).sqrt()
            human_liquidity = entry.size_usd / (Decimal(2) * (sqrt_center - sqrt_lower))
            raw_liquidity = human_liquidity * Decimal(10) ** (
                (Decimal(FIXTURE_STOCK_DECIMALS) + Decimal(FIXTURE_QUOTE_DECIMALS)) / Decimal(2)
            )
            share = raw_liquidity / Decimal(DEFAULT_GAUGE_LIQUIDITY)
            expected_fees = (
                Decimal("5000") * Decimal("500") / Decimal(1_000_000) * share * Decimal(len(prices))
            )
            expected_aero = share * Decimal(1) * Decimal(4 * 60)

        # The replay accumulates each accrual in the default context while the
        # expectation runs at full precision, so tails below one attodollar
        # are compared by tolerance rather than exact Decimal equality.
        assert abs(ledger.fees_accrued_usd - expected_fees) < Decimal("1e-18")
        assert abs(ledger.aero_accrued_units - expected_aero) < Decimal("1e-18")
        assert abs(ledger.aero_accrued_usd - expected_aero * Decimal("0.5")) < Decimal("1e-18")
        assert ledger.time_in_range_seconds == 240
        assert ledger.time_open_seconds == 240

    def test_entry_costs_use_the_documented_gas_assumption(self) -> None:
        """The entry batch cost follows the constant gas and ETH assumptions."""
        ledger = run_rehearsal([Decimal("100")] * 2, high_apr_steps(2))

        entry = ledger.actions[0]
        # 650k action gas plus 100k Safe overhead at 0.001 gwei and 3000 USD/ETH.
        assert entry.estimated_gas_units == 750_000
        assert entry.estimated_gas_cost_usd == Decimal("0.00225")
        assert ledger.total_gas_cost_usd == Decimal("0.00225")

    def test_replay_is_deterministic(self) -> None:
        """Two replays over identical inputs emit identical economics."""
        prices = [Decimal("100")] * 10 + [Decimal("99.5"), Decimal("98.5")]
        first = run_rehearsal(prices, high_apr_steps(len(prices)))
        second = run_rehearsal(prices, high_apr_steps(len(prices)))

        assert first.actions == second.actions
        assert first.final_equity_usd == second.final_equity_usd
        assert first.fees_accrued_usd == second.fees_accrued_usd
        assert first.aero_accrued_units == second.aero_accrued_units
        assert first.pnl_usd == second.pnl_usd

    def test_dilution_exit_above_range_executes_no_swap(self) -> None:
        """A dilution exit while the position sits all in USDC records no swap."""
        steps = [
            make_apr_step(0, Decimal("3.0")),
            make_apr_step(1, Decimal("1.2")),
        ]
        ledger = run_rehearsal([Decimal("100"), Decimal("101.5")], steps)

        assert ledger.action_counts.dilution_exits == 1
        exit_record = ledger.actions[1]
        # Above the range the burned position is entirely USDC, so the exit
        # batch performs no swap and charges no impact.
        assert exit_record.swap_total_usd is None
        assert exit_record.swap_impact_cost_usd == 0

    def test_vanished_depth_marks_the_exit_swap_unmodeled(self) -> None:
        """A stop-out swap against vanished depth is labeled unmodeled."""
        ledger = run_rehearsal(
            [Decimal("100"), Decimal("97")],
            high_apr_steps(2),
            liquidity=[DEFAULT_POOL_LIQUIDITY, 0],
        )

        assert ledger.action_counts.stop_outs == 1
        stop = ledger.actions[1]
        # The whole position still sells at market, but the zero observed
        # depth leaves the impact unbounded and explicitly labeled.
        assert stop.swap_total_usd is not None
        assert stop.swap_max_impact_bps is None
        assert ledger.unmodeled_swap_impact is True
        assert any("unmodeled" in label for label in ledger.assumption_labels)

    def test_constant_anchor_series_is_labeled_on_the_ledger(self) -> None:
        """The constant-anchor fallback surfaces as an assumption label."""
        history = make_apr_history([make_apr_step(0, Decimal("3.0"))])
        history = history.model_copy(update={"reconstruction_mode": "constant_anchor_apr"})
        ledger = replay_pool(
            price_path=make_path([Decimal("100")] * 2),
            emissions_history=history,
            symbol="AAPLc/USDC",
            assumptions=RehearsalAssumptions(pool_fee_ppm=500),
        )

        assert ledger.emissions_reconstruction_mode == "constant_anchor_apr"
        assert any("constant-anchor fallback" in label for label in ledger.assumption_labels)

    def test_mismatched_histories_and_assumptions_fail_closed(self) -> None:
        """Wrong pools or AERO assumptions refuse to replay."""
        with pytest.raises(ValueError, match="one pool"):
            replay_pool(
                price_path=make_path([Decimal("100")]),
                emissions_history=make_apr_history([make_apr_step(0, Decimal("3.0"))]).model_copy(
                    update={"pool_address": OTHER_POOL_ADDRESS}
                ),
                symbol="AAPLc/USDC",
                assumptions=RehearsalAssumptions(pool_fee_ppm=500),
            )
        with pytest.raises(ValueError, match="aero_price_assumption_usd"):
            replay_pool(
                price_path=make_path([Decimal("100")]),
                emissions_history=make_apr_history([make_apr_step(0, Decimal("3.0"))]),
                symbol="AAPLc/USDC",
                assumptions=RehearsalAssumptions(
                    pool_fee_ppm=500, aero_price_assumption_usd=Decimal("0.6")
                ),
            )


class TestLedgerModels:
    """Tests for the ledger's typed models and validators."""

    def _fixture_ledger(
        self, action_counts: RehearsalActionCounts | None = None
    ) -> PoolRehearsalLedger:
        """Build one minimal valid ledger for validator tests.

        Args:
            action_counts: Override for the derived counts, defaults to the
                counts matching the fixture's single entry action.

        Returns:
            A ledger holding exactly one entry action and matching counts.
        """
        entry = RehearsalActionRecord(
            timestamp=BASE_TIME,
            action=PolicyActionKind.ENTER,
            reason=PolicyReason.ENTRY_THRESHOLD_MET,
            diagnostics=("entry",),
            size_usd=Decimal("40"),
            cash_after_usd=Decimal("160"),
            equity_after_usd=Decimal("200"),
        )
        return PoolRehearsalLedger(
            pool_address=POOL_ADDRESS,
            token_address=B20_ADDRESS,
            symbol="AAPLc/USDC",
            replayed_at=BASE_TIME,
            emissions_reconstruction_mode="event_fold",
            reference_mode="amm_equals_reference",
            assumptions=RehearsalAssumptions(pool_fee_ppm=500),
            assumption_labels=("labeled assumption",),
            observation_count=1,
            starting_equity_usd=Decimal("200"),
            final_equity_usd=Decimal("200"),
            final_cash_usd=Decimal("200"),
            pnl_usd=Decimal("0"),
            return_fraction=Decimal("0"),
            time_open_seconds=0,
            time_in_range_seconds=0,
            action_counts=action_counts or RehearsalActionCounts(entries=1),
            actions=(entry,),
        )

    def test_ledger_rejects_counts_that_drift_from_actions(self) -> None:
        """A ledger whose counts disagree with its actions fails validation."""
        with pytest.raises(ValidationError, match="must match"):
            self._fixture_ledger(action_counts=RehearsalActionCounts(entries=2))

    def test_ledger_accepts_consistent_models(self) -> None:
        """A consistent ledger validates and stays immutable."""
        ledger = self._fixture_ledger()

        assert ledger.action_counts.entries == 1
        with pytest.raises(ValidationError, match="frozen"):
            ledger.symbol = "other"
