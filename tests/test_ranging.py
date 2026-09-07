"""Behavior tests for the pure target-yield range-width solver."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from aero_bot.history import PoolPricePath, PoolPricePoint
from aero_bot.ranging import (
    MATH_PRECISION,
    TICK_PRICE_RATIO,
    CandidateWidthEvaluation,
    RangingObservations,
    WidthSolution,
    WidthSolveMode,
    band_shape,
    implied_average_half_width_fraction,
    liquidity_per_deployed_dollar,
    realized_daily_volatility,
    solve_range_width,
)
from aero_bot.ranging import (
    _position_value_fraction_at_price as value_fraction_at_price,
)
from aero_bot.ranging import (
    _swap_impact_cost_usd as swap_impact_cost_usd,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
# The fixture session is a Saturday noon UTC, clear of every session event window.
BASE_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
# Six-and-six decimals make the raw price equal the human price in fixtures.
FIXTURE_STOCK_DECIMALS = 6
FIXTURE_QUOTE_DECIMALS = 6
# The default raw active liquidity prices a one-percent band near twenty
# thousand USDC at price one hundred, matching the rehearsal fixtures.
DEFAULT_POOL_LIQUIDITY = 80_000_000_000_000
# The fixture staked book matches the pool's active liquidity, so the staked
# liquidity per staked dollar is exactly one liquidity unit per eight hundred.
DEFAULT_STAKED_TVL_USD = Decimal("100000")
# One million USDC of notional per day at the five-hundred-ppm tier feeds a
# small but nonzero fee stream into every solve.
DEFAULT_FEE_WINDOW_NOTIONAL_USD = Decimal("1000000")
# The APR that makes the default fixture solve at the tightest candidate; a
# lower APR exercises the unreachable path instead.
DEFAULT_EMISSIONS_APR = Decimal("200")
# First-principles comparisons run in the same high-precision context as the
# solver so independent recomputation meets exact Decimal equality.
TOLERANCE = Decimal("1e-30")


def assert_close(actual: Decimal, expected: Decimal) -> None:
    """Compare two Decimals with a tolerance far below any rounding noise.

    Args:
        actual: The solver-produced value under test.
        expected: The independently recomputed value.
    """
    assert abs(actual - expected) < TOLERANCE


def make_observations(**overrides: object) -> RangingObservations:
    """Build a solvable observation fixture and apply explicit per-test overrides.

    Args:
        **overrides: Observation fields changed to exercise one solver behavior.

    Returns:
        A validated immutable observation whose default inputs solve at the
        tightest candidate width.
    """
    values: dict[str, object] = {
        "pool_price_usdc": Decimal("100"),
        "emissions_apr": DEFAULT_EMISSIONS_APR,
        "gauge_liquidity_raw": DEFAULT_POOL_LIQUIDITY,
        "staked_tvl_usd": DEFAULT_STAKED_TVL_USD,
        "active_liquidity_raw": DEFAULT_POOL_LIQUIDITY,
        "pool_depth_usd": Decimal("20000"),
        "fee_window_seconds": 86_400,
        "fee_window_notional_usd": DEFAULT_FEE_WINDOW_NOTIONAL_USD,
        "pool_fee_ppm": 500,
        "realized_daily_volatility": Decimal("0.005"),
        "stock_decimals": FIXTURE_STOCK_DECIMALS,
        "quote_decimals": FIXTURE_QUOTE_DECIMALS,
        "position_size_usd": Decimal("40"),
        "gas_price_gwei": Decimal("0.006"),
    }
    values.update(overrides)
    return RangingObservations.model_validate(values)


def make_path(prices: list[Decimal], step_seconds: int = 86_400) -> PoolPricePath:
    """Build one synthetic price path with a constant raw liquidity level.

    Args:
        prices: Ordered human prices, one per reconstructed swap.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        An immutable price path over the synthetic session.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        sqrt_ratio = int((Decimal(100) * Decimal(1 << 192)).sqrt())
        points = tuple(
            PoolPricePoint(
                timestamp=BASE_TIME + timedelta(seconds=step_seconds * index),
                block_number=index,
                log_index=0,
                amount0=0,
                amount1=5_000_000_000,
                sqrt_ratio=sqrt_ratio,
                liquidity=DEFAULT_POOL_LIQUIDITY,
                tick=9_210,
                price_usdc=price,
            )
            for index, price in enumerate(prices)
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


def solved_solution(**overrides: object) -> tuple[RangingObservations, WidthSolution]:
    """Solve the default fixture and return its inputs beside the solution.

    Args:
        **overrides: Observation fields changed for one solver behavior test.

    Returns:
        The validated observations and their deterministic width solution.
    """
    observations = make_observations(**overrides)
    return observations, solve_range_width(observations)


class TestBandShape:
    """Tests for the geometric band shape function."""

    def test_matches_closed_form(self) -> None:
        """The shape equals the closed-form fourth-and-square-root difference."""
        width = Decimal("0.001")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            expected = (Decimal(1) - width ** Decimal(2)).sqrt().sqrt() - (
                Decimal(1) - width
            ).sqrt()
        assert_close(band_shape(width), expected)

    def test_narrow_bands_approach_half_the_width(self) -> None:
        """Narrow bands satisfy the small-width approximation g(w) about w/2."""
        width = Decimal("1e-6")
        shape = band_shape(width)
        assert abs(shape - width / Decimal(2)) < width ** Decimal(2)

    def test_rejects_widths_outside_the_unit_interval(self) -> None:
        """Widths at or beyond the unit endpoints have no geometric meaning."""
        for width in (Decimal(0), Decimal(1), Decimal("-0.1"), Decimal("1.5")):
            with pytest.raises(ValueError, match="between zero and one"):
                band_shape(width)


class TestLiquidityPerDeployedDollar:
    """Tests for the centered-position liquidity bought per deployed dollar."""

    def test_matches_first_principles(self) -> None:
        """Liquidity per dollar is the reciprocal of twice root price times shape."""
        price = Decimal("100")
        width = Decimal("0.001")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            shape = (Decimal(1) - width ** Decimal(2)).sqrt().sqrt() - (Decimal(1) - width).sqrt()
            expected = Decimal(1) / (Decimal(2) * price.sqrt() * shape)
        assert_close(liquidity_per_deployed_dollar(price, width), expected)

    def test_halving_the_width_nearly_doubles_liquidity(self) -> None:
        """Liquidity scales like one over the width in the narrow-band limit."""
        wide = liquidity_per_deployed_dollar(Decimal("100"), Decimal("0.001"))
        tight = liquidity_per_deployed_dollar(Decimal("100"), Decimal("0.0005"))
        ratio = tight / wide
        assert Decimal("1.99") < ratio < Decimal(2)

    def test_rejects_non_positive_price(self) -> None:
        """A non-positive price cannot scale square-root liquidity."""
        with pytest.raises(ValueError, match="positive"):
            liquidity_per_deployed_dollar(Decimal(0), Decimal("0.001"))


class TestPositionValueFraction:
    """Tests for the exact composition marking of a centered position."""

    def test_value_at_the_geometric_center_is_the_committed_value(self) -> None:
        """The position redeems exactly its committed value at the band's center.

        The band spans one minus and one plus the width around one, whose
        geometric center price lies at the square root of one minus the width
        squared, slightly below one.
        """
        width = Decimal("0.001")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_center = ((Decimal(1) - width).sqrt() * (Decimal(1) + width).sqrt()).sqrt()
            center_price = sqrt_center * sqrt_center
            at_center = value_fraction_at_price(width, center_price)
            at_one = value_fraction_at_price(width, Decimal(1))
        assert_close(at_center, Decimal(1))
        # Marking at price one sits above the geometric center by the second
        # order in the width, the model's documented entry-valuation offset.
        assert abs(at_one - Decimal(1) - width ** Decimal(2) / Decimal(4)) < width ** Decimal(3)

    def test_below_range_value_is_all_stock(self) -> None:
        """Below the band the position is entirely stock marked at the price."""
        width = Decimal("0.001")
        price = Decimal("0.5")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_lower = (Decimal(1) - width).sqrt()
            sqrt_upper = (Decimal(1) + width).sqrt()
            liquidity = Decimal(1) / (Decimal(2) * ((sqrt_lower * sqrt_upper).sqrt() - sqrt_lower))
            expected = liquidity * (Decimal(1) / sqrt_lower - Decimal(1) / sqrt_upper) * price
        assert_close(value_fraction_at_price(width, price), expected)

    def test_above_range_value_is_all_quote(self) -> None:
        """Above the band the position is entirely its USDC side."""
        width = Decimal("0.003")
        price = Decimal("2")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_lower = (Decimal(1) - width).sqrt()
            sqrt_upper = (Decimal(1) + width).sqrt()
            liquidity = Decimal(1) / (Decimal(2) * ((sqrt_lower * sqrt_upper).sqrt() - sqrt_lower))
            expected = liquidity * (sqrt_upper - sqrt_lower)
        assert_close(value_fraction_at_price(width, price), expected)

    def test_band_edges_are_continuous(self) -> None:
        """The in-range formulas meet the out-of-range formulas at both edges."""
        width = Decimal("0.002")
        lower_edge = Decimal(1) - width
        upper_edge = Decimal(1) + width
        # Below the band the value moves one for one with the all-stock price,
        # so a one-nanoprice probe bounds the continuity gap by its own step.
        assert abs(
            value_fraction_at_price(width, lower_edge)
            - value_fraction_at_price(width, lower_edge - Decimal("1e-9"))
        ) < Decimal("2e-9")
        assert abs(
            value_fraction_at_price(width, upper_edge)
            - value_fraction_at_price(width, upper_edge + Decimal("1e-9"))
        ) < Decimal("2e-9")

    def test_rejects_non_positive_price(self) -> None:
        """A non-positive price cannot mark a position."""
        with pytest.raises(ValueError, match="positive"):
            value_fraction_at_price(Decimal("0.001"), Decimal(0))


class TestRealizedDailyVolatility:
    """Tests for the zero-mean realized-variance estimator."""

    def test_constant_price_is_zero(self) -> None:
        """A flat path carries no realized variance."""
        assert realized_daily_volatility(make_path([Decimal("100")] * 5)) == Decimal(0)

    def test_single_step_matches_the_log_return(self) -> None:
        """One move over exactly one day has volatility equal to its log return."""
        path = make_path([Decimal("100"), Decimal("101")], step_seconds=86_400)
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            expected = abs((Decimal("101") / Decimal("100")).ln())
        volatility = realized_daily_volatility(path)
        assert volatility is not None
        assert_close(volatility, expected)

    def test_alternating_returns_sum_squares(self) -> None:
        """Three equal-magnitude returns over one day triple the summed squares."""
        path = make_path(
            [Decimal("100"), Decimal("101"), Decimal("100"), Decimal("101")],
            step_seconds=28_800,
        )
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            log_return = (Decimal("101") / Decimal("100")).ln()
            expected = (Decimal(3) * log_return ** Decimal(2)).sqrt()
        volatility = realized_daily_volatility(path)
        assert volatility is not None
        assert_close(volatility, expected)

    def test_longer_windows_scale_down_variance(self) -> None:
        """The same moves over four days halve the daily volatility."""
        one_day = realized_daily_volatility(
            make_path([Decimal("100"), Decimal("101")], step_seconds=86_400)
        )
        four_days = realized_daily_volatility(
            make_path([Decimal("100"), Decimal("101")], step_seconds=4 * 86_400)
        )
        assert one_day is not None
        assert four_days is not None
        assert_close(four_days * Decimal(2), one_day)

    def test_too_few_points_returns_none(self) -> None:
        """Paths without a pair of points cannot estimate variance."""
        assert realized_daily_volatility(make_path([])) is None
        assert realized_daily_volatility(make_path([Decimal("100")])) is None

    def test_zero_span_returns_none(self) -> None:
        """Coincident observations span no time to normalize by."""
        assert realized_daily_volatility(make_path([Decimal("100"), Decimal("101")], 0)) is None


class TestImpliedAverageHalfWidth:
    """Tests for inverting the staked book's concentration into a width."""

    def test_recovers_a_constructed_width(self) -> None:
        """A book staked at exactly one width inverts back to that width."""
        width = Decimal("0.001")
        staked_tvl = Decimal("100000")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            shape = (Decimal(1) - width ** Decimal(2)).sqrt().sqrt() - (Decimal(1) - width).sqrt()
            liquidity_per_dollar = Decimal(1) / (Decimal(2) * Decimal(100).sqrt() * shape)
            human_scale = Decimal(10) ** Decimal(FIXTURE_STOCK_DECIMALS)
            gauge_liquidity = int(
                (liquidity_per_dollar * staked_tvl * human_scale).to_integral_value(
                    rounding="ROUND_CEILING"
                )
            )
        implied = implied_average_half_width_fraction(
            Decimal("100"),
            gauge_liquidity,
            staked_tvl,
            FIXTURE_STOCK_DECIMALS,
            FIXTURE_QUOTE_DECIMALS,
        )
        assert implied is not None
        assert abs(implied - width) < Decimal("1e-15")

    def test_missing_stake_inputs_return_none(self) -> None:
        """Zero staked liquidity or zero staked value implies no width."""
        assert implied_average_half_width_fraction(Decimal("100"), 0, Decimal("1"), 6, 6) is None
        assert implied_average_half_width_fraction(Decimal("100"), 1_000, Decimal(0), 6, 6) is None

    def test_dilute_book_returns_none(self) -> None:
        """A ratio below the search region's minimum belongs to no single width."""
        assert (
            implied_average_half_width_fraction(Decimal("100"), 1, Decimal("1000000000000"), 6, 6)
            is None
        )


class TestWidthSolutionValidation:
    """Tests for the immutable solution's tick-alignment validator."""

    def test_fraction_must_match_the_tick_count(self) -> None:
        """A fraction that disagrees with its tick count is rejected."""
        _, solution = solved_solution()
        with pytest.raises(ValidationError, match="half_width_fraction"):
            WidthSolution(
                mode=solution.mode,
                half_width_ticks=solution.half_width_ticks,
                half_width_fraction=Decimal("0.002"),
                tick_spacing=solution.tick_spacing,
                target_net_daily_yield=solution.target_net_daily_yield,
                evaluations=solution.evaluations,
                diagnostics=solution.diagnostics,
            )

    def test_observation_field_bounds_are_enforced(self) -> None:
        """Target, ceiling, and spacing stay inside their documented bounds."""
        with pytest.raises(ValidationError):
            make_observations(target_net_daily_yield=Decimal(0))
        with pytest.raises(ValidationError):
            make_observations(max_range_half_width_fraction=Decimal("1.5"))
        with pytest.raises(ValidationError):
            make_observations(tick_spacing=0)


class TestCandidateGridAlignment:
    """Tests for the tick-aligned candidate grid the solver scans."""

    def test_candidates_span_one_spacing_to_the_ceiling(self) -> None:
        """Candidates are every tick count from one spacing up to twenty-nine."""
        _, solution = solved_solution()
        assert [row.half_width_ticks for row in solution.evaluations] == list(range(10, 30))
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            for row in solution.evaluations:
                assert_close(
                    row.half_width_fraction, TICK_PRICE_RATIO**row.half_width_ticks - Decimal(1)
                )
                assert row.half_width_fraction <= Decimal("0.003")

    def test_wider_spacing_keeps_a_nonempty_grid(self) -> None:
        """A spacing above half the ceiling still scans through the ceiling."""
        _, solution = solved_solution(tick_spacing=20)
        assert [row.half_width_ticks for row in solution.evaluations] == list(range(20, 30))

    def test_spacing_above_the_ceiling_still_offers_one_spacing(self) -> None:
        """One spacing per side outranks the ceiling as the tightest bound."""
        _, solution = solved_solution(tick_spacing=40)
        assert [row.half_width_ticks for row in solution.evaluations] == [40]

    def test_the_solved_width_is_tick_aligned(self) -> None:
        """The chosen fraction is exactly the tick ratio's width."""
        _, solution = solved_solution()
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            expected = TICK_PRICE_RATIO**solution.half_width_ticks - Decimal(1)
        assert solution.half_width_fraction == expected


class TestSolveModes:
    """Tests for the three ways one width solve can resolve."""

    def test_solves_at_the_tightest_meeting_candidate(self) -> None:
        """A reachable target selects the tightest candidate that meets it."""
        observations, solution = solved_solution()
        assert solution.mode == WidthSolveMode.SOLVED
        assert solution.half_width_ticks == observations.tick_spacing
        assert solution.evaluations[0].meets_target
        assert solution.evaluations[0].net_yield_per_day >= observations.target_net_daily_yield

    def test_interior_solve_picks_the_first_meeting_candidate(self) -> None:
        """A non-monotone net curve still selects the tightest, not the peak."""
        observations, solution = solved_solution(tick_spacing=1)
        assert solution.mode == WidthSolveMode.SOLVED
        assert solution.half_width_ticks == 4
        rows = solution.evaluations
        assert [row.meets_target for row in rows[:3]] == [False, False, False]
        assert rows[3].meets_target
        assert solution.half_width_fraction == rows[3].half_width_fraction
        # The net curve peaks later than the chosen width, proving the scan
        # stops at the tightest meeting candidate rather than the best one.
        assert rows[8].net_yield_per_day > rows[3].net_yield_per_day
        assert observations.tick_spacing == 1

    def test_unreachable_target_enters_at_the_tightest_width(self) -> None:
        """An unreachable target still reports the tightest width to enter at."""
        observations, solution = solved_solution(emissions_apr=Decimal("25"))
        assert solution.mode == WidthSolveMode.TARGET_UNREACHABLE
        assert solution.half_width_ticks == observations.tick_spacing
        assert not any(row.meets_target for row in solution.evaluations)
        assert solution.half_width_fraction == solution.evaluations[0].half_width_fraction
        assert any("cannot reach the target" in line for line in solution.diagnostics)
        assert any("entering at the tightest width" in line for line in solution.diagnostics)

    def test_unreachable_never_widens_to_hedge(self) -> None:
        """The unreachable solve keeps the tightest width even when wider nets more."""
        _, solution = solved_solution(emissions_apr=Decimal("25"))
        tightest = solution.evaluations[0]
        widest = solution.evaluations[-1]
        assert widest.net_yield_per_day > tightest.net_yield_per_day
        assert solution.half_width_ticks == tightest.half_width_ticks

    @pytest.mark.parametrize(
        "overrides",
        [
            {"realized_daily_volatility": None},
            {"gauge_liquidity_raw": 0},
            {"staked_tvl_usd": Decimal(0)},
            {"active_liquidity_raw": 0},
            {"pool_depth_usd": Decimal(0)},
            {"fee_window_notional_usd": Decimal("1000"), "fee_window_seconds": 0},
        ],
    )
    def test_unusable_inputs_fail_toward_the_ceiling(self, overrides: dict[str, object]) -> None:
        """Missing or inconsistent inputs fall back to the locked ceiling width."""
        observations, solution = solved_solution(**overrides)
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            ceiling = TICK_PRICE_RATIO ** Decimal(29) - Decimal(1)
        assert solution.mode == WidthSolveMode.FALLBACK_CEILING
        assert solution.half_width_ticks == 29
        assert solution.half_width_fraction == ceiling
        assert len(solution.evaluations) == 1
        assert any("unusable" in line for line in solution.diagnostics)

    def test_fallback_evaluation_carries_no_yield_evidence(self) -> None:
        """The fallback's single evaluation is zeroed and never meets a target."""
        _, solution = solved_solution(realized_daily_volatility=None)
        row = solution.evaluations[0]
        assert row.gross_emissions_yield_per_day == Decimal(0)
        assert row.fee_yield_per_day == Decimal(0)
        assert row.uptime_fraction == Decimal(0)
        assert row.recenter_rate_per_day == Decimal(0)
        assert row.stop_rate_per_day == Decimal(0)
        assert row.net_yield_per_day == Decimal(0)
        assert not row.meets_target

    def test_zero_volatility_keeps_the_position_permanently_in_range(self) -> None:
        """A flat path never exits the band, so uptime is the event-window factor."""
        observations, solution = solved_solution(realized_daily_volatility=Decimal(0))
        row = solution.evaluations[0]
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            flat_fraction = Decimal(5) * observations.flat_hours_per_weekday / Decimal(168)
            expected_uptime = Decimal(1) - flat_fraction
        assert_close(row.uptime_fraction, expected_uptime)
        assert row.recenter_rate_per_day == Decimal(0)
        assert row.stop_rate_per_day == Decimal(0)
        assert solution.mode == WidthSolveMode.SOLVED
        assert observations.target_net_daily_yield == Decimal("0.01")


class TestSolveEconomicsFromFirstPrinciples:
    """Tests recomputing every modeled quantity from the raw formulas."""

    def row_for(self, ticks: int) -> tuple[RangingObservations, CandidateWidthEvaluation]:
        """Solve the default fixture and pick one candidate row to recompute.

        Args:
            ticks: The candidate's half-width tick count.

        Returns:
            The observations and the matching evaluation row.
        """
        observations, solution = solved_solution()
        row = next(item for item in solution.evaluations if item.half_width_ticks == ticks)
        return observations, row

    def test_tightest_candidate_matches_the_renewal_model(self) -> None:
        """Every field of the tightest row matches an independent recomputation."""
        observations, row = self.row_for(10)
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            width = TICK_PRICE_RATIO ** Decimal(10) - Decimal(1)
            shape = (Decimal(1) - width ** Decimal(2)).sqrt().sqrt() - (Decimal(1) - width).sqrt()
            liquidity_per_dollar = Decimal(1) / (
                Decimal(2) * observations.pool_price_usdc.sqrt() * shape
            )
            human_scale = Decimal(10) ** Decimal(FIXTURE_STOCK_DECIMALS)
            staked_per_dollar = (
                Decimal(observations.gauge_liquidity_raw)
                / human_scale
                / observations.staked_tvl_usd
            )
            gross = (
                observations.emissions_apr / Decimal(365) * liquidity_per_dollar / staked_per_dollar
            )
            fee_stream = (
                observations.fee_window_notional_usd
                * Decimal(86_400)
                / Decimal(observations.fee_window_seconds)
                * Decimal(observations.pool_fee_ppm)
                / Decimal(1_000_000)
            )
            fee = (
                fee_stream
                * liquidity_per_dollar
                * human_scale
                / Decimal(observations.active_liquidity_raw)
                / observations.position_size_usd
            )
            stop_distance = Decimal(1) - (Decimal(1) - width) * (
                Decimal(1) - observations.stop_buffer_fraction
            )
            stop_gap = stop_distance - width
            volatility = observations.realized_daily_volatility
            assert volatility is not None
            exit_days = width ** Decimal(2) / volatility ** Decimal(2)
            excursion_days = width * stop_gap / volatility ** Decimal(2)
            stop_probability = width / (width + stop_gap)
            wait_days = Decimal(observations.recenter_wait_seconds) / Decimal(86_400)
            cooldown_days = Decimal(observations.reentry_cooldown_seconds) / Decimal(86_400)
            downside_days = excursion_days + stop_probability * cooldown_days
            cycle_days = exit_days + (wait_days + downside_days) / Decimal(2)
            downtime = (wait_days + downside_days) / (Decimal(2) * cycle_days)
            flat_fraction = Decimal(5) * observations.flat_hours_per_weekday / Decimal(168)
            uptime = (Decimal(1) - flat_fraction) * (Decimal(1) - downtime)
            recenter_rate = Decimal(1) / Decimal(2) / cycle_days
            stop_rate = stop_probability / Decimal(2) / cycle_days
            gas_scale = Decimal(1_000_000_000)
            recenter_gas = (
                Decimal(observations.recenter_batch_gas_units + 100_000)
                * observations.gas_price_gwei
                / gas_scale
                * observations.eth_price_assumption_usd
            )
            exit_gas = (
                Decimal(observations.exit_batch_gas_units + 100_000)
                * observations.gas_price_gwei
                / gas_scale
                * observations.eth_price_assumption_usd
            )
            enter_gas = (
                Decimal(observations.enter_batch_gas_units + 100_000)
                * observations.gas_price_gwei
                / gas_scale
                * observations.eth_price_assumption_usd
            )
            size = observations.position_size_usd
            depth = observations.pool_depth_usd
            recenter_impact = size / Decimal(2) * (size / Decimal(2) / depth) / Decimal(2)
            stop_impact = size * (size / depth) / Decimal(2)
            recenter_cost = recenter_rate * (recenter_gas + recenter_impact) / size
            sqrt_lower = (Decimal(1) - width).sqrt()
            sqrt_upper = (Decimal(1) + width).sqrt()
            liquidity = Decimal(1) / (Decimal(2) * ((sqrt_lower * sqrt_upper).sqrt() - sqrt_lower))
            stop_price = Decimal(1) - stop_distance
            stop_value = (
                liquidity * (Decimal(1) / sqrt_lower - Decimal(1) / sqrt_upper) * stop_price
            )
            stop_loss = Decimal(1) - stop_value
            stop_cost = stop_rate * (stop_loss + (exit_gas + enter_gas + stop_impact) / size)
            net = (gross + fee) * uptime - recenter_cost - stop_cost
        assert_close(row.half_width_fraction, width)
        assert_close(row.position_liquidity_per_dollar, liquidity_per_dollar)
        assert_close(row.gross_emissions_yield_per_day, gross)
        assert_close(row.fee_yield_per_day, fee)
        assert_close(row.uptime_fraction, uptime)
        assert_close(row.recenter_rate_per_day, recenter_rate)
        assert_close(row.recenter_cost_per_day, recenter_cost)
        assert_close(row.stop_rate_per_day, stop_rate)
        assert_close(row.stop_cost_per_day, stop_cost)
        assert_close(row.net_yield_per_day, net)
        assert row.meets_target == (net >= observations.target_net_daily_yield)

    def test_widest_candidate_also_matches_the_renewal_model(self) -> None:
        """The ceiling-width row nets less than the tightest row here."""
        _, wide_row = self.row_for(29)
        _, tight_row = self.row_for(10)
        assert wide_row.net_yield_per_day < tight_row.net_yield_per_day
        assert wide_row.position_liquidity_per_dollar < tight_row.position_liquidity_per_dollar

    def test_stop_loss_grows_with_the_stop_distance(self) -> None:
        """Marking the position exactly at the stop level prices the loss basis."""
        width = Decimal("0.002")
        stop_distance = Decimal(1) - (Decimal(1) - width) * Decimal("0.995")
        stop_price = Decimal(1) - stop_distance
        below_band = Decimal(1) - width
        assert stop_price < below_band
        value = value_fraction_at_price(width, stop_price)
        assert value < Decimal(1)

    def test_impact_charges_half_the_end_impact(self) -> None:
        """One modeled swap pays size over depth squared over two in USDC."""
        assert_close(
            swap_impact_cost_usd(Decimal("20"), Decimal("20000")),
            Decimal("20") * (Decimal("20") / Decimal("20000")) / Decimal(2),
        )
        assert swap_impact_cost_usd(Decimal("20"), Decimal(0)) == Decimal(0)

    def test_fee_window_normalization_is_exact(self) -> None:
        """A half window with half the notional yields the same daily stream."""
        _, baseline = solved_solution()
        _, half_window = solved_solution(
            fee_window_seconds=43_200, fee_window_notional_usd=Decimal("500000")
        )
        assert_close(
            baseline.evaluations[0].fee_yield_per_day,
            half_window.evaluations[0].fee_yield_per_day,
        )

    def test_zero_notional_removes_the_fee_stream(self) -> None:
        """No observed notional means no fee yield at any width."""
        _, solution = solved_solution(fee_window_notional_usd=Decimal(0))
        assert solution.evaluations[0].fee_yield_per_day == Decimal(0)

    def test_flat_windows_restore_full_week_uptime(self) -> None:
        """Zero flat hours leaves uptime at exactly one minus downtime."""
        _, solution = solved_solution(flat_hours_per_weekday=Decimal(0))
        row = solution.evaluations[0]
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            width = row.half_width_fraction
            volatility = Decimal("0.005")
            stop_distance = Decimal(1) - (Decimal(1) - width) * Decimal("0.995")
            stop_gap = stop_distance - width
            exit_days = width ** Decimal(2) / volatility ** Decimal(2)
            excursion_days = width * stop_gap / volatility ** Decimal(2)
            stop_probability = width / (width + stop_gap)
            wait_days = Decimal(900) / Decimal(86_400)
            downside_days = excursion_days + stop_probability * wait_days
            cycle_days = exit_days + (wait_days + downside_days) / Decimal(2)
            downtime = (wait_days + downside_days) / (Decimal(2) * cycle_days)
            expected_uptime = Decimal(1) - downtime
        assert_close(row.uptime_fraction, expected_uptime)


class TestDeterminismAndMonotonicity:
    """Tests for determinism and the economics' directional behavior."""

    def test_solve_is_deterministic(self) -> None:
        """Two solves over identical inputs produce identical solutions."""
        observations = make_observations()
        assert solve_range_width(observations) == solve_range_width(observations)

    def test_higher_volatility_lowers_the_tightest_net(self) -> None:
        """Quadrupling realized volatility raises every modeled cost rate."""
        _, calm = solved_solution(realized_daily_volatility=Decimal("0.005"))
        _, wild = solved_solution(realized_daily_volatility=Decimal("0.02"))
        calm_row = calm.evaluations[0]
        wild_row = wild.evaluations[0]
        assert wild_row.net_yield_per_day < calm_row.net_yield_per_day
        assert wild_row.stop_rate_per_day > calm_row.stop_rate_per_day
        assert wild_row.recenter_rate_per_day > calm_row.recenter_rate_per_day
        assert wild.mode == WidthSolveMode.TARGET_UNREACHABLE

    def test_higher_targets_flip_the_solve_to_unreachable(self) -> None:
        """Targets above the tightest net report unreachable, never widen."""
        _, near = solved_solution(target_net_daily_yield=Decimal("0.011"))
        _, far = solved_solution(target_net_daily_yield=Decimal("0.012"))
        assert near.mode == WidthSolveMode.SOLVED
        assert near.half_width_ticks == 10
        assert far.mode == WidthSolveMode.TARGET_UNREACHABLE
        assert far.half_width_ticks == 10

    def test_higher_apr_never_loosens_the_solved_width(self) -> None:
        """More emissions never justify a wider range."""
        _, richer = solved_solution(emissions_apr=Decimal("800"))
        assert richer.mode == WidthSolveMode.SOLVED
        assert richer.half_width_ticks == 10

    def test_diagnostics_carry_inputs_and_the_implied_width(self) -> None:
        """Every solve's diagnostics echo its inputs and the staked book's width."""
        _, solution = solved_solution()
        joined = "\n".join(solution.diagnostics)
        assert "Implied average staked half width" in joined
        assert "Solved half width" in joined
        assert "Target net daily yield 0.01" in joined
        assert "Realized daily volatility 0.005" in joined
