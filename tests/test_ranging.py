"""Behavior tests for the adaptive executable-range width solver."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from aero_bot.history import PoolPricePath, PoolPricePoint
from aero_bot.ranging import (
    MATH_PRECISION,
    AlignedBound,
    RangingObservations,
    WidthSolution,
    WidthSolveMode,
    band_dwell_fraction,
    band_shape,
    ceiling_width_solution,
    enumerate_aligned_bounds,
    human_price_from_raw_tick,
    implied_average_half_width_fraction,
    liquidity_per_deployed_dollar,
    position_liquidity_at_price,
    raw_tick_for_human_price,
    realized_daily_volatility,
    realized_volatility_from_points,
    solve_range_width,
)
from aero_bot.ranging import (
    _position_value_fraction_at_price as value_fraction_at_price,
)
from aero_bot.ranging import (
    _range_value_fraction_at_price as range_value_fraction_at_price,
)
from aero_bot.ranging import (
    _swap_impact_cost_usd as swap_impact_cost_usd,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb200000000000000000000000000000000000000"
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
# The APR that keeps the default fixture's best candidate clearly positive;
# a low APR exercises the cash-hold path instead.
DEFAULT_EMISSIONS_APR = Decimal("200")
# First-principles comparisons run in the same high-precision context as the
# solver so independent recomputation meets exact Decimal equality.
TOLERANCE = Decimal("1e-30")
# The captain's adaptive band in fixture form: 0.1 to 0.3 percent per side.
BAND_MIN = Decimal("0.001")
BAND_MAX = Decimal("0.003")
# The pools' Slipstream grid spacing.
TICK_SPACING = 10


def assert_close(actual: Decimal, expected: Decimal) -> None:
    """Compare two Decimals with a tolerance far below any rounding noise.

    Args:
        actual: The solver-produced value under test.
        expected: The independently recomputed value.
    """
    assert abs(actual - expected) < TOLERANCE


def make_points(
    prices: list[Decimal],
    step_seconds: int = 600,
    start: datetime = BASE_TIME,
) -> tuple[PoolPricePoint, ...]:
    """Build one synthetic trailing point window at a constant liquidity.

    Args:
        prices: Ordered human prices, one per reconstructed swap.
        step_seconds: Wall-clock seconds between observations.
        start: The window's first timestamp.

    Returns:
        The immutable ordered trailing points.
    """
    sqrt_ratio = int((Decimal(100) * Decimal(1 << 192)).sqrt())
    return tuple(
        PoolPricePoint(
            timestamp=start + timedelta(seconds=step_seconds * index),
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


def make_path(prices: list[Decimal], step_seconds: int = 86_400) -> PoolPricePath:
    """Build one synthetic price path with a constant raw liquidity level.

    Args:
        prices: Ordered human prices, one per reconstructed swap.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        An immutable price path over the synthetic session.
    """
    points = make_points(prices, step_seconds=step_seconds)
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


def trailing_window(
    spot: Decimal, wiggle_ticks: int = 2, count: int = 30
) -> tuple[PoolPricePoint, ...]:
    """Build one gentle trailing window wandering a few ticks around a spot.

    Args:
        spot: The center price the window wanders around.
        wiggle_ticks: The wander radius in raw ticks.
        count: The number of observed points.

    Returns:
        Points whose prices oscillate inside a few-tick band of the spot.
    """
    ratio = Decimal("1.0001")
    prices = [
        spot * ratio ** Decimal((index % (2 * wiggle_ticks + 1)) - wiggle_ticks)
        for index in range(count)
    ]
    return make_points(prices)


def make_observations(**overrides: object) -> RangingObservations:
    """Build a solvable observation fixture and apply explicit per-test overrides.

    Args:
        **overrides: Observation fields changed to exercise one solver behavior.

    Returns:
        A validated immutable observation whose default inputs solve with a
        clearly positive best candidate.
    """
    values: dict[str, object] = {
        "pool_price_usdc": Decimal("100"),
        # The observation instant defaults to the trailing window's newest
        # point, so the default fixture's evidence is exactly fresh.
        "observed_at": BASE_TIME + timedelta(seconds=600 * 29),
        # The raw grid anchor: a coherent raw tick for the human price at
        # the fixture's token1/6-6 orientation (USDC is token0).
        "pool_tick_raw": raw_tick_for_human_price(Decimal("100"), False, 6, 6),
        "stock_is_token0": False,
        "emissions_apr": DEFAULT_EMISSIONS_APR,
        "gauge_liquidity_raw": DEFAULT_POOL_LIQUIDITY,
        "staked_tvl_usd": DEFAULT_STAKED_TVL_USD,
        "active_liquidity_raw": DEFAULT_POOL_LIQUIDITY,
        "pool_depth_usd": Decimal("20000"),
        "fee_window_seconds": 86_400,
        "fee_window_notional_usd": DEFAULT_FEE_WINDOW_NOTIONAL_USD,
        "pool_fee_ppm": 500,
        "realized_daily_volatility": Decimal("0.005"),
        "trailing_path": trailing_window(Decimal("100")),
        "stock_decimals": FIXTURE_STOCK_DECIMALS,
        "quote_decimals": FIXTURE_QUOTE_DECIMALS,
        "position_size_usd": Decimal("40"),
        "gas_price_gwei": Decimal("0.006"),
    }
    values.update(overrides)
    return RangingObservations.model_validate(values)


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
        assert abs(shape - width / Decimal(2)) / shape < Decimal("1e-6")

    def test_invalid_widths_raise(self) -> None:
        """Widths at or outside the unit interval are rejected."""
        with pytest.raises(ValueError, match="between zero and one"):
            band_shape(Decimal(0))
        with pytest.raises(ValueError, match="between zero and one"):
            band_shape(Decimal(1))


class TestLiquidityPerDollar:
    """Tests for the liquidity-per-deployed-dollar helpers."""

    def test_centered_matches_first_principles(self) -> None:
        """A centered range's liquidity matches the exact composition rule."""
        price = Decimal("100")
        width = Decimal("0.002")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_lower = (price * (Decimal(1) - width)).sqrt()
            sqrt_upper = (price * (Decimal(1) + width)).sqrt()
            sqrt_center = (sqrt_lower * sqrt_upper).sqrt()
            expected = Decimal(1) / (Decimal(2) * (sqrt_center - sqrt_lower))
        assert_close(liquidity_per_deployed_dollar(price, width), expected)

    def test_at_price_matches_exact_composition(self) -> None:
        """Entering off-center buys the exact composition-implied liquidity."""
        price = Decimal("100")
        lower = Decimal("99.8")
        upper = Decimal("100.3")
        liquidity = position_liquidity_at_price(price, lower, upper)
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_price = price.sqrt()
            value_per_liquidity = (Decimal(1) / sqrt_price - Decimal(1) / upper.sqrt()) * price + (
                sqrt_price - lower.sqrt()
            )
            assert_close(liquidity, Decimal(1) / value_per_liquidity)
            # And the value marked back at the entry price is exactly one.
            recovered = (
                (Decimal(1) / sqrt_price - Decimal(1) / upper.sqrt()) * price
                + (sqrt_price - lower.sqrt())
            ) * liquidity
            assert_close(recovered, Decimal(1))

    def test_at_price_rejects_outside_entry(self) -> None:
        """An entry price on or outside the range is rejected."""
        with pytest.raises(ValueError, match="strictly inside"):
            position_liquidity_at_price(Decimal("100"), Decimal("100"), Decimal("101"))
        with pytest.raises(ValueError, match="strictly inside"):
            position_liquidity_at_price(Decimal("100"), Decimal("99"), Decimal("100"))

    def test_centered_at_geometric_center_agrees_with_centered_helper(self) -> None:
        """The at-price form degenerates to the centered form at the center."""
        price = Decimal("100")
        width = Decimal("0.002")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_lower = (price * (Decimal(1) - width)).sqrt()
            sqrt_upper = (price * (Decimal(1) + width)).sqrt()
            center = sqrt_lower * sqrt_upper
        centered = liquidity_per_deployed_dollar(price, width)
        at_price = position_liquidity_at_price(
            center, price * (Decimal(1) - width), price * (Decimal(1) + width)
        )
        # Both express liquidity per deployed dollar at (nearly) the same
        # geometry; the at-price form at the geometric center agrees.
        assert abs(at_price - centered) / centered < Decimal("1e-12")


class TestImpliedAverageWidth:
    """Tests for the staked-concentration inversion."""

    def test_round_trips_a_centered_width(self) -> None:
        """A book of one centered width inverts back to that width."""
        price = Decimal("100")
        width = Decimal("0.002")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            per_dollar = liquidity_per_deployed_dollar(price, width)
            human_scale = Decimal(10) ** (
                (Decimal(FIXTURE_STOCK_DECIMALS) + Decimal(FIXTURE_QUOTE_DECIMALS)) / Decimal(2)
            )
            gauge_raw = int(per_dollar * human_scale * DEFAULT_STAKED_TVL_USD)
            implied = implied_average_half_width_fraction(
                price,
                gauge_raw,
                DEFAULT_STAKED_TVL_USD,
                FIXTURE_STOCK_DECIMALS,
                FIXTURE_QUOTE_DECIMALS,
            )
            assert implied is not None
            assert abs(implied - width) < Decimal("1e-14")

    def test_thin_book_returns_none(self) -> None:
        """A staked ratio too wide to invert degrades to None."""
        implied = implied_average_half_width_fraction(
            Decimal("100"),
            1,
            Decimal("1000000"),
            FIXTURE_STOCK_DECIMALS,
            FIXTURE_QUOTE_DECIMALS,
        )
        assert implied is None


class TestRealizedVolatility:
    """Tests for the realized-volatility estimator."""

    def test_estimates_a_geometric_random_walk(self) -> None:
        """A constant log-return walk estimates its daily volatility exactly."""
        count = 145  # one day of ten-minute observations
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            step = Decimal("1.001").ln()
            prices = [Decimal(100) * (Decimal("1.001") ** index) for index in range(count)]
            expected = step * Decimal(count - 1).sqrt()
        path = make_path(prices, step_seconds=600)
        vol = realized_daily_volatility(path)
        assert vol is not None
        # The span is exactly one day, so the estimate is the root of the
        # summed squared log returns.
        assert_close(vol, expected)

    def test_thin_windows_return_none(self) -> None:
        """Fewer than two points or zero span cannot estimate volatility."""
        assert realized_volatility_from_points(make_points([Decimal("100")])) is None
        same_instant = make_points([Decimal("100"), Decimal("100")], step_seconds=0)
        assert realized_volatility_from_points(same_instant) is None


class TestEnumerateAlignedBounds:
    """Tests for the raw-grid bound enumeration."""

    def _enumerate(
        self,
        spot: Decimal = Decimal("100"),
        raw_tick: int | None = None,
        stock_is_token0: bool = False,
    ) -> tuple[tuple[AlignedBound, ...], tuple[AlignedBound, ...]]:
        """Enumerate at a coherent raw tick for the spot and orientation."""
        if raw_tick is None:
            raw_tick = raw_tick_for_human_price(spot, stock_is_token0, 6, 6)
        return enumerate_aligned_bounds(
            spot, TICK_SPACING, BAND_MIN, BAND_MAX, raw_tick, stock_is_token0, 6, 6
        )

    def test_bounds_are_raw_grid_multiples_inside_the_band(self) -> None:
        """Every enumerated bound is a raw spacing multiple with an in-band real distance."""
        lower, upper = self._enumerate(Decimal("100.0123"))
        assert lower and upper
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            for bound in lower + upper:
                assert bound.tick % TICK_SPACING == 0
                distance = abs(Decimal(1) - bound.price / Decimal("100.0123"))
                assert_close(bound.distance_fraction, distance)
                assert BAND_MIN <= distance <= BAND_MAX

    def test_orientation_inverts_raw_prices_not_distances(self) -> None:
        """Token1 pools flip raw prices against human prices, distances stay real."""
        lower, upper = self._enumerate(Decimal("100"), stock_is_token0=False)
        assert lower and upper
        # Every raw-lower bound carries a HIGHER human price than spot.
        for bound in lower:
            assert bound.price > Decimal("100")
        # Every raw-upper bound carries a lower human price than spot.
        for bound in upper:
            assert bound.price < Decimal("100")
        # And the mirror image holds for the token0 orientation.
        lower0, upper0 = self._enumerate(Decimal("100"), stock_is_token0=True)
        for bound in lower0:
            assert bound.price < Decimal("100")
        for bound in upper0:
            assert bound.price > Decimal("100")

    def test_decimals_scale_the_orientation_flip(self) -> None:
        """An 18-decimal stock shifts the raw grid without breaking parity."""
        spot = Decimal("3436.44")
        raw_tick = raw_tick_for_human_price(spot, True, 18, 6)
        lower, upper = enumerate_aligned_bounds(
            spot, TICK_SPACING, BAND_MIN, BAND_MAX, raw_tick, True, 18, 6
        )
        assert lower and upper
        for bound in lower + upper:
            assert_close(bound.price, human_price_from_raw_tick(bound.tick, True, 18, 6))
            assert BAND_MIN <= bound.distance_fraction <= BAND_MAX

    def test_off_grid_phase_yields_asymmetric_distances(self) -> None:
        """A price mid-cell produces different feasible distance sets per side."""
        lower, upper = self._enumerate(Decimal("100.0123"))
        lower_distances = {row.distance_fraction for row in lower}
        upper_distances = {row.distance_fraction for row in upper}
        assert lower_distances != upper_distances

    def test_infeasible_band_returns_empty_sides(self) -> None:
        """A spacing whose grid step exceeds the band yields no bounds."""
        spot = Decimal("100")
        raw_tick = raw_tick_for_human_price(spot, False, 6, 6)
        lower, upper = enumerate_aligned_bounds(
            spot, 500, BAND_MIN, BAND_MAX, raw_tick, False, 6, 6
        )
        assert lower == ()
        assert upper == ()

    def test_invalid_inputs_raise(self) -> None:
        """A nonpositive price, spacing, or inverted band is rejected."""
        raw_tick = raw_tick_for_human_price(Decimal("100"), False, 6, 6)
        with pytest.raises(ValueError, match="positive"):
            enumerate_aligned_bounds(Decimal(0), 10, BAND_MIN, BAND_MAX, raw_tick, False, 6, 6)
        with pytest.raises(ValueError, match="positive"):
            enumerate_aligned_bounds(Decimal("100"), 0, BAND_MIN, BAND_MAX, raw_tick, False, 6, 6)
        with pytest.raises(ValueError, match="non-inverted"):
            enumerate_aligned_bounds(Decimal("100"), 10, BAND_MAX, BAND_MIN, raw_tick, False, 6, 6)


class TestBandDwell:
    """Tests for the measured anchored-band dwell."""

    def test_counts_inside_intervals_by_left_point(self) -> None:
        """Dwell weights each interval by its opening price's membership."""
        prices = [Decimal("100"), Decimal("100"), Decimal("101"), Decimal("101")]
        points = make_points(prices, step_seconds=100)
        # Band anchored at 100 spanning half a percent per side: intervals 0
        # and 1 are inside (opening 100), 2 is outside (opening 101).
        dwell = band_dwell_fraction(points, Decimal("0.005"), Decimal("0.005"))
        assert dwell is not None
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            assert_close(dwell, Decimal(2) / Decimal(3))

    def test_anchor_is_the_windows_first_price(self) -> None:
        """The band anchors at the first price, never at the latest."""
        prices = [Decimal("100"), Decimal("100.2"), Decimal("100.2"), Decimal("100.2")]
        points = make_points(prices, step_seconds=100)
        # A tenth-percent band around the 100 anchor holds only the first
        # interval; one around the latest 100.2 would hold everything.
        dwell = band_dwell_fraction(points, Decimal("0.001"), Decimal("0.001"))
        assert dwell is not None
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            assert_close(dwell, Decimal(1) / Decimal(3))

    def test_wide_anchor_band_covers_the_window(self) -> None:
        """A band wide enough for the whole wander dwells the whole window."""
        points = trailing_window(Decimal("100"), wiggle_ticks=3)
        dwell = band_dwell_fraction(points, Decimal("0.01"), Decimal("0.01"))
        assert dwell is not None
        assert_close(dwell, Decimal(1))

    def test_thin_windows_return_none(self) -> None:
        """Fewer than two points or zero span cannot measure dwell."""
        single = make_points([Decimal("100")])
        assert band_dwell_fraction(single, Decimal("0.001"), Decimal("0.001")) is None
        same_instant = make_points([Decimal("100"), Decimal("100")], step_seconds=0)
        assert band_dwell_fraction(same_instant, Decimal("0.001"), Decimal("0.001")) is None


class TestAdaptiveSolve:
    """Tests for the adaptive executable-range solve."""

    def test_solves_with_bounds_matching_a_scored_evaluation(self) -> None:
        """The chosen range is exactly one scored evaluation's geometry."""
        _, solution = solved_solution()
        assert solution.mode is WidthSolveMode.SOLVED
        assert solution.lower_bound is not None
        assert solution.upper_bound is not None
        matching = [
            row
            for row in solution.evaluations
            if row.lower_bound == solution.lower_bound and row.upper_bound == solution.upper_bound
        ]
        assert len(matching) == 1

    def test_every_evaluation_is_executable_and_in_band(self) -> None:
        """Every scored candidate's real per-side distances sit inside the band."""
        observations, solution = solved_solution()
        assert solution.evaluations
        for row in solution.evaluations:
            assert row.lower_bound.tick % observations.tick_spacing == 0
            assert row.upper_bound.tick % observations.tick_spacing == 0
            assert (
                observations.min_range_half_width_fraction
                <= row.lower_bound.distance_fraction
                <= observations.max_range_half_width_fraction
            )
            assert (
                observations.min_range_half_width_fraction
                <= row.upper_bound.distance_fraction
                <= observations.max_range_half_width_fraction
            )
            # The bounds' human prices straddle the spot regardless of the
            # pool's token orientation.
            prices = (row.lower_bound.price, row.upper_bound.price)
            assert min(prices) < observations.pool_price_usdc < max(prices)

    def test_argmax_selection_picks_the_strongest_net(self) -> None:
        """The chosen candidate carries the maximum modeled net."""
        _, solution = solved_solution()
        best_net = max(row.net_yield_per_day for row in solution.evaluations)
        chosen = next(
            row
            for row in solution.evaluations
            if row.lower_bound == solution.lower_bound and row.upper_bound == solution.upper_bound
        )
        assert_close(chosen.net_yield_per_day, best_net)
        assert best_net > 0

    def test_selection_is_argmax_not_satisficing(self) -> None:
        """A higher reported target never changes which candidate wins."""
        _, default_solution = solved_solution()
        _, high_target_solution = solved_solution(target_net_daily_yield=Decimal("0.5"))
        assert default_solution.lower_bound == high_target_solution.lower_bound
        assert default_solution.upper_bound == high_target_solution.upper_bound

    def test_low_volatility_prefers_the_tight_side(self) -> None:
        """At quiet volatility the tightest executable geometry earns the most."""
        _, solution = solved_solution(realized_daily_volatility=Decimal("0.0005"))
        assert solution.mode is WidthSolveMode.SOLVED
        assert solution.lower_bound is not None
        assert solution.upper_bound is not None
        chosen = next(
            row
            for row in solution.evaluations
            if row.lower_bound == solution.lower_bound and row.upper_bound == solution.upper_bound
        )
        # Ascending-span sort plus max: the tight geometry wins only when its
        # net strictly exceeds every wider candidate's.
        wider = [row for row in solution.evaluations if row is not chosen]
        assert all(chosen.net_yield_per_day > row.net_yield_per_day for row in wider)

    def test_cash_hold_when_every_candidate_nets_nonpositive(self) -> None:
        """A starving reward stream holds cash with the evidence."""
        _, solution = solved_solution(emissions_apr=Decimal("0.5"))
        assert solution.mode is WidthSolveMode.CASH_HOLD
        assert solution.lower_bound is None
        assert solution.upper_bound is None
        assert solution.evaluations
        assert all(row.net_yield_per_day <= 0 for row in solution.evaluations)
        assert any("nonpositive" in line for line in solution.diagnostics)

    def test_missing_volatility_defers(self) -> None:
        """An unmeasurable volatility defers with the reason."""
        _, solution = solved_solution(realized_daily_volatility=None)
        assert solution.mode is WidthSolveMode.DEFERRED
        assert solution.evaluations == ()
        assert any("volatility is unavailable" in line for line in solution.diagnostics)

    def test_empty_trailing_path_defers(self) -> None:
        """No measured path means no measured dwell, so the solve defers."""
        _, solution = solved_solution(trailing_path=())
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("trailing measured path is empty" in line for line in solution.diagnostics)

    def test_zero_span_trailing_path_defers(self) -> None:
        """A path whose points share one instant carries no time basis."""
        same_instant = make_points([Decimal("100"), Decimal("100.001")], step_seconds=0)
        _, solution = solved_solution(trailing_path=same_instant)
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("spans no time" in line for line in solution.diagnostics)

    def test_missing_gauge_liquidity_defers(self) -> None:
        """A gauge with no staked liquidity cannot price concentration."""
        _, solution = solved_solution(gauge_liquidity_raw=0)
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("gauge staked liquidity" in line for line in solution.diagnostics)

    def test_missing_depth_defers(self) -> None:
        """Missing executable depth cannot price impact."""
        _, solution = solved_solution(pool_depth_usd=Decimal(0))
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("active liquidity or executable depth" in line for line in solution.diagnostics)

    def test_exactly_at_the_recency_bound_still_solves(self) -> None:
        """A newest point exactly 1800 seconds old is still fresh enough."""
        newest = BASE_TIME + timedelta(seconds=600 * 29)
        _, solution = solved_solution(observed_at=newest + timedelta(seconds=1_800))
        assert solution.mode is WidthSolveMode.SOLVED
        assert solution.lower_bound is not None

    def test_just_past_the_recency_bound_defers(self) -> None:
        """A newest point 1801 seconds old defers as stale evidence."""
        newest = BASE_TIME + timedelta(seconds=600 * 29)
        _, solution = solved_solution(observed_at=newest + timedelta(seconds=1_801))
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("stale" in line for line in solution.diagnostics)

    def test_future_skewed_evidence_defers(self) -> None:
        """A newest point after the observation instant is corrupt evidence."""
        newest = BASE_TIME + timedelta(seconds=600 * 29)
        _, solution = solved_solution(observed_at=newest - timedelta(seconds=1))
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("time-skewed or corrupt" in line for line in solution.diagnostics)

    def test_a_long_span_alone_is_not_recency_proof(self) -> None:
        """A sufficient-count window ending hours early still defers."""
        # The independent reviewer's probe shape: thirty fresh-looking
        # points whose newest evidence is 3.5 hours stale.
        newest = BASE_TIME + timedelta(seconds=600 * 29)
        _, solution = solved_solution(observed_at=newest + timedelta(hours=3, minutes=30))
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("stale" in line for line in solution.diagnostics)

    def test_inconsistent_fee_window_defers(self) -> None:
        """Notional over an empty window is inconsistent evidence."""
        _, solution = solved_solution(fee_window_notional_usd=Decimal(10), fee_window_seconds=0)
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("empty window" in line for line in solution.diagnostics)

    def test_infeasible_grid_defers_with_the_grid_note(self) -> None:
        """A spacing too coarse for the band defers naming the grid phase."""
        _, solution = solved_solution(tick_spacing=500)
        assert solution.mode is WidthSolveMode.DEFERRED
        assert any("no grid-aligned bound" in line for line in solution.diagnostics)

    def test_first_principles_net_recomputation(self) -> None:
        """One candidate's every modeled term recomputes from first principles."""
        observations, solution = solved_solution()
        assert solution.lower_bound is not None
        assert solution.upper_bound is not None
        row = next(
            r
            for r in solution.evaluations
            if r.lower_bound == solution.lower_bound and r.upper_bound == solution.upper_bound
        )
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            # Own-stake dilution: the denominator after our stake.
            human_scale = Decimal(10) ** (
                (Decimal(observations.stock_decimals) + Decimal(observations.quote_decimals))
                / Decimal(2)
            )
            liquidity_raw = int(
                (
                    row.position_liquidity_per_dollar * observations.position_size_usd * human_scale
                ).to_integral_value(rounding="ROUND_FLOOR")
            )
            assert liquidity_raw == row.position_liquidity_raw_added
            band_low = min(row.lower_bound.price, row.upper_bound.price)
            band_high = max(row.lower_bound.price, row.upper_bound.price)
            share = Decimal(liquidity_raw) / Decimal(
                observations.gauge_liquidity_raw + liquidity_raw
            )
            gross = (
                observations.emissions_apr
                / Decimal(365)
                * observations.staked_tvl_usd
                * share
                / observations.position_size_usd
            )
            assert_close(row.gross_emissions_yield_per_day, gross)
            # Uptime is the measured dwell of this exact geometry's
            # anchored band.
            dwell = band_dwell_fraction(
                observations.trailing_path,
                row.lower_bound.distance_fraction,
                row.upper_bound.distance_fraction,
            )
            assert dwell is not None
            assert_close(row.uptime_fraction, dwell)
            assert row.uptime_measured is True
            # The asymmetric renewal frequencies: exit a*b/sigma^2, upside
            # probability a/(a+b), cooldown-paying stop probability.
            a = Decimal(1) - band_low / observations.pool_price_usdc
            b = band_high / observations.pool_price_usdc - Decimal(1)
            sigma = observations.realized_daily_volatility
            assert sigma is not None
            exit_days = a * b / sigma ** Decimal(2)
            upside_probability = a / (a + b)
            stop_probability = a / (a + observations.stop_buffer_fraction)
            excursion_days = a * observations.stop_buffer_fraction / sigma ** Decimal(2)
            acting = min(
                observations.recenter_wait_seconds, observations.out_of_range_grace_seconds
            )
            wait_days = Decimal(acting) / Decimal(86_400)
            cooldown_days = Decimal(observations.reentry_cooldown_seconds) / Decimal(86_400)
            upside_days = wait_days
            downside_days = excursion_days + stop_probability * cooldown_days
            cycle_days = exit_days + (
                upside_probability * upside_days + (Decimal(1) - upside_probability) * downside_days
            )
            recenter_rate = upside_probability / cycle_days
            stop_rate = (Decimal(1) - upside_probability) * stop_probability / cycle_days
            assert_close(row.recenter_rate_per_day, recenter_rate)
            assert_close(row.stop_rate_per_day, stop_rate)
            # Net: gross and fees at dwell, minus churn and stop costs.
            net = (
                (row.gross_emissions_yield_per_day + row.fee_yield_per_day) * dwell
                - row.recenter_cost_per_day
                - row.stop_cost_per_day
            )
            assert_close(row.net_yield_per_day, net)

    def test_own_dilution_denominator_after_our_stake(self) -> None:
        """A concentrated candidate dilutes itself more than L/gauge alone."""
        observations, solution = solved_solution()
        assert solution.evaluations
        for row in solution.evaluations:
            with localcontext() as decimal_context:
                decimal_context.prec = MATH_PRECISION
                raw = Decimal(row.position_liquidity_raw_added)
                ours = raw / Decimal(
                    observations.gauge_liquidity_raw + row.position_liquidity_raw_added
                )
                naive = raw / Decimal(observations.gauge_liquidity_raw)
                # The post-stake share is strictly smaller than the naive
                # pre-stake fraction, and the emissions term used it.
                assert ours < naive
                expected_gross = (
                    observations.emissions_apr
                    / Decimal(365)
                    * observations.staked_tvl_usd
                    * ours
                    / observations.position_size_usd
                )
                assert_close(row.gross_emissions_yield_per_day, expected_gross)

    def test_high_gas_can_flip_selection_wider(self) -> None:
        """Expensive churn at high volatility pushes the argmax wider."""
        _, quiet = solved_solution(realized_daily_volatility=Decimal("0.005"))
        _, wild = solved_solution(
            realized_daily_volatility=Decimal("0.02"), gas_price_gwei=Decimal("0.05")
        )
        assert quiet.mode is WidthSolveMode.SOLVED
        assert wild.mode in {WidthSolveMode.SOLVED, WidthSolveMode.CASH_HOLD}
        if wild.mode is WidthSolveMode.SOLVED:
            assert wild.lower_bound is not None
            assert quiet.lower_bound is not None
            # The wild regime's chosen total span is at least as wide.
            quiet_span = quiet.evaluations[
                next(
                    i
                    for i, r in enumerate(quiet.evaluations)
                    if r.lower_bound == quiet.lower_bound and r.upper_bound == quiet.upper_bound
                )
            ].total_span_fraction
            wild_rows = [
                r
                for r in wild.evaluations
                if r.lower_bound == wild.lower_bound and r.upper_bound == wild.upper_bound
            ]
            assert wild_rows
            assert wild_rows[0].total_span_fraction >= quiet_span

    def test_flat_volatility_never_churns(self) -> None:
        """A measured zero volatility models no recenters and no stops."""
        points = make_points([Decimal("100")] * 10)
        _, solution = solved_solution(
            realized_daily_volatility=Decimal(0),
            trailing_path=points,
            observed_at=BASE_TIME + timedelta(seconds=600 * 9),
        )
        assert solution.mode is WidthSolveMode.SOLVED
        for row in solution.evaluations:
            assert row.recenter_rate_per_day == 0
            assert row.stop_rate_per_day == 0
            assert row.recenter_cost_per_day == 0
            assert row.stop_cost_per_day == 0

    def test_solution_bounds_pair_validation(self) -> None:
        """A solution may not carry one bound without the other."""
        with pytest.raises(ValidationError, match="together or not at all"):
            WidthSolution(
                mode=WidthSolveMode.SOLVED,
                lower_bound=AlignedBound(
                    tick=-10, price=Decimal("99"), distance_fraction=Decimal("0.01")
                ),
                upper_bound=None,
                tick_spacing=10,
                target_net_daily_yield=Decimal("0.01"),
                diagnostics=("line",),
            )


class TestModelDriftCorrections:
    """Tests pinning the corrected model conventions."""

    def test_acting_window_charges_min_of_grace_and_wait(self) -> None:
        """The upside downtime charges the production acting window."""
        observations, solution = solved_solution(
            recenter_wait_seconds=900, out_of_range_grace_seconds=600
        )
        assert solution.evaluations
        row = solution.evaluations[0]
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            band_low = min(row.lower_bound.price, row.upper_bound.price)
            band_high = max(row.lower_bound.price, row.upper_bound.price)
            a = Decimal(1) - band_low / observations.pool_price_usdc
            b = band_high / observations.pool_price_usdc - Decimal(1)
            sigma = observations.realized_daily_volatility
            assert sigma is not None
            exit_days = a * b / sigma ** Decimal(2)
            upside_probability = a / (a + b)
            stop_probability = a / (a + observations.stop_buffer_fraction)
            excursion_days = a * observations.stop_buffer_fraction / sigma ** Decimal(2)
            wait_days = Decimal(600) / Decimal(86_400)  # min(900, 600)
            cooldown_days = Decimal(observations.reentry_cooldown_seconds) / Decimal(86_400)
            cycle_days = exit_days + (
                upside_probability * wait_days
                + (Decimal(1) - upside_probability)
                * (excursion_days + stop_probability * cooldown_days)
            )
            assert_close(row.recenter_rate_per_day, upside_probability / cycle_days)

    def test_no_event_flat_time_is_subtracted(self) -> None:
        """The retired event-window flat time no longer reduces uptime."""
        # Uptime is exactly the measured anchored dwell: nothing else scales it.
        observations, solution = solved_solution()
        assert solution.evaluations
        row = solution.evaluations[0]
        dwell = band_dwell_fraction(
            observations.trailing_path,
            row.lower_bound.distance_fraction,
            row.upper_bound.distance_fraction,
        )
        assert dwell is not None
        assert_close(row.uptime_fraction, dwell)


class TestCeilingBaseline:
    """Tests for the v1 fixed-ceiling comparison posture."""

    def test_baseline_bounds_are_outward_aligned_around_the_ceiling(self) -> None:
        """The baseline floors and ceils the ceiling width onto the grid."""
        spot = Decimal("100.0123")
        solution = ceiling_width_solution(
            pool_price_usdc=spot,
            tick_spacing=TICK_SPACING,
            max_range_half_width_fraction=BAND_MAX,
            target_net_daily_yield=Decimal("0.01"),
            reason="pinned comparison posture",
        )
        assert solution.mode is WidthSolveMode.FALLBACK_CEILING
        assert solution.lower_bound is not None
        assert solution.upper_bound is not None
        assert solution.lower_bound.tick % TICK_SPACING == 0
        assert solution.upper_bound.tick % TICK_SPACING == 0
        # The outward alignment contains the raw ceiling band.
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            raw_lower = spot * (Decimal(1) - BAND_MAX)
            raw_upper = spot * (Decimal(1) + BAND_MAX)
        assert solution.lower_bound.price <= raw_lower
        assert solution.upper_bound.price >= raw_upper
        assert any("baseline" in line for line in solution.diagnostics)


class TestCompositionValueFraction:
    """Tests for the exact position-value marking helpers."""

    def test_centered_value_fraction_at_edges(self) -> None:
        """The centered helper marks the band edges exactly."""
        width = Decimal("0.002")
        with localcontext() as decimal_context:
            decimal_context.prec = MATH_PRECISION
            sqrt_lower = (Decimal(1) - width).sqrt()
            sqrt_upper = (Decimal(1) + width).sqrt()
            sqrt_center = (sqrt_lower * sqrt_upper).sqrt()
            liquidity = Decimal(1) / (Decimal(2) * (sqrt_center - sqrt_lower))
            # Below the band the position is entirely stock: its value is
            # the stock amount times the below-edge price.
            stock_below = liquidity * (Decimal(1) / sqrt_lower - Decimal(1) / sqrt_upper)
            expected_below = stock_below * (Decimal(1) - width)
            # Above the band it is entirely USDC: the upper USDC amount.
            expected_above = liquidity * (sqrt_upper - sqrt_lower)
        assert_close(value_fraction_at_price(width, Decimal(1) - width), expected_below)
        assert_close(value_fraction_at_price(width, Decimal(1) + width), expected_above)

    def test_range_value_fraction_marks_stop_loss(self) -> None:
        """The range helper marks the stop below the aligned lower edge."""
        entry = Decimal("100")
        lower = Decimal("99.9")
        upper = Decimal("100.2")
        stop = lower * Decimal("0.995")
        fraction = range_value_fraction_at_price(entry, lower, upper, stop)
        assert fraction < 1
        # A price back inside the band at the entry marks exactly one.
        assert_close(range_value_fraction_at_price(entry, lower, upper, entry), Decimal(1))


class TestSwapImpactCost:
    """Tests for the half-impact swap cost convention."""

    def test_half_impact_at_the_depth(self) -> None:
        """A swap pays half its end impact against the observed depth."""
        cost = swap_impact_cost_usd(Decimal("100"), Decimal("20000"))
        assert_close(cost, Decimal("100") * (Decimal("100") / Decimal("20000")) / Decimal(2))

    def test_zero_depth_costs_zero(self) -> None:
        """Zero depth leaves the impact unmodeled at zero."""
        assert swap_impact_cost_usd(Decimal("100"), Decimal(0)) == 0
