"""Behavior tests for the pure Slipstream LP mint planning layer."""

import math
from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest

from aero_bot.concentrated import PositionRangeState
from aero_bot.domain import normalize_evm_address
from aero_bot.lp_calldata import (
    LP_MINT_SELECTOR,
    LpMintParams,
    build_lp_mint_calldata,
)
from aero_bot.lp_plan import (
    DEFAULT_MINT_SLIPPAGE_TOLERANCE,
    MATH_PRECISION,
    MAX_POSITION_USDC_PER_POOL,
    MAX_TOTAL_PILOT_EXPOSURE_USDC,
    QUOTE_TOKEN_DECIMALS,
    X96_SCALE,
    LpExecutionPolicy,
    LpPlanRefusalCode,
    LpPlanRefusalError,
    LpPoolObservation,
    MintDirective,
    SafeInventory,
    WidthSource,
    derive_position_range,
    estimate_in_range_depth_usdc,
    plan_balancing_swap,
    plan_mint_composition,
    plan_mint_entry,
    plan_swap_back,
    position_amounts_at_sqrt_ratio,
    position_amounts_for_liquidity,
    position_range_state,
)
from aero_bot.ranging import TICK_PRICE_RATIO

# Native Base USDC, the token-zero side of the AAPLc-like fixture pool.
USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# The Coinbase-issued stock token, the pool's token-one side.
STOCK_ADDRESS = "0xb200000000000000000000c2e324d24d7eecd1fb"
# The canary Safe whose inventory the plans draw from.
SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
# The Gauges V3 Slipstream NFPM the fixture pool's Sugar record names.
NFPM_ADDRESS = "0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53"
# The fixture pool's gauge.
GAUGE_ADDRESS = "0x43021fbbd01b967704ab2379f6e90e2d367042f3"
# The fixture pool's live-like snapshot tick, one step inside the anchor grid.
POOL_CURRENT_TICK = -11657
# The fixture pool's Slipstream tick spacing.
POOL_TICK_SPACING = 10
# The stock token's decimal count.
STOCK_DECIMALS = 8
# One USDC in raw six-decimal units.
ONE_USDC_UNITS = 10**QUOTE_TOKEN_DECIMALS
# A pool-scale USDC reserve for impact bounds, roughly seven hundred thousand.
POOL_USDC_RESERVE_UNITS = 700_000 * ONE_USDC_UNITS
# The pool's stock-side reserve, the swap-back impact base in unit fixtures.
POOL_STOCK_RESERVE_UNITS = 20_000_000 * 10**STOCK_DECIMALS
# A pool-scale active liquidity in raw L units.
POOL_ACTIVE_LIQUIDITY = 10**14
# A fixed snapshot timestamp shared by the observation fixtures.
OBSERVED_AT = datetime(2026, 9, 8, tzinfo=UTC)


def sqrt_ratio_at_tick(tick: int) -> int:
    """Compute the truncated raw sqrtPriceX96 at one exact tick.

    Args:
        tick: The tick whose exact price provides the fixture price.

    Returns:
        The integer raw square-root price at the tick.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        return int((TICK_PRICE_RATIO**tick).sqrt() * X96_SCALE)


def pool_observation(**overrides: object) -> LpPoolObservation:
    """Build the live-like AAPLc-shaped pool observation fixture.

    Args:
        **overrides: Model fields changed to exercise one behavior.

    Returns:
        A coherent block-pinned observation of the fixture pool.
    """
    values: dict[str, object] = {
        "symbol": "AAPLc",
        "pool_address": "0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0",
        "nfpm_address": NFPM_ADDRESS,
        "gauge_address": GAUGE_ADDRESS,
        "token0_address": USDC_ADDRESS,
        "token1_address": STOCK_ADDRESS,
        "stock_is_token0": False,
        "stock_decimals": STOCK_DECIMALS,
        "quote_decimals": QUOTE_TOKEN_DECIMALS,
        "tick_spacing": POOL_TICK_SPACING,
        "current_tick": POOL_CURRENT_TICK,
        "sqrt_ratio": sqrt_ratio_at_tick(POOL_CURRENT_TICK),
        "pool_active_liquidity": POOL_ACTIVE_LIQUIDITY,
        "usdc_reserve_units": POOL_USDC_RESERVE_UNITS,
        "stock_reserve_units": POOL_STOCK_RESERVE_UNITS,
        "snapshot_block": 30_000_000,
        "observed_at": OBSERVED_AT,
    }
    values.update(overrides)
    return LpPoolObservation.model_validate(values)


def mint_directive(**overrides: object) -> MintDirective:
    """Build the canary-shaped mint directive: seven USDC, one spacing.

    Args:
        **overrides: Model fields changed to exercise one behavior.

    Returns:
        A coherent explicit-override directive.
    """
    values: dict[str, object] = {
        "budget_usdc": Decimal(7),
        "half_width_spacings": 1,
        "width_source": WidthSource.EXPLICIT_OVERRIDE,
    }
    values.update(overrides)
    return MintDirective.model_validate(values)


def safe_inventory(**overrides: object) -> SafeInventory:
    """Build the canary Safe's live-like inventory: seven USDC and 0.0062 stock.

    Args:
        **overrides: Model fields changed to exercise one behavior.

    Returns:
        A coherent inventory snapshot.
    """
    values: dict[str, object] = {
        "usdc_units": 7 * ONE_USDC_UNITS,
        "stock_units": 620_000,
    }
    values.update(overrides)
    return SafeInventory.model_validate(values)


def test_derived_range_aligns_around_the_anchored_current_tick() -> None:
    """One spacing per side floors the anchor onto the grid and contains the tick."""
    band = derive_position_range(
        POOL_CURRENT_TICK, POOL_TICK_SPACING, 1, WidthSource.EXPLICIT_OVERRIDE
    )

    # The anchor rounds -11657 down onto the grid at -11660.
    assert band.tick_lower == -11670
    assert band.tick_upper == -11650
    assert band.half_width_ticks == 10
    with localcontext() as decimal_context:
        # The exact power needs the module's working precision to compare.
        decimal_context.prec = MATH_PRECISION
        assert band.half_width_fraction == TICK_PRICE_RATIO**10 - Decimal(1)
    assert band.clamped_to_ceiling is False
    assert band.tick_lower <= POOL_CURRENT_TICK < band.tick_upper


def test_derived_range_clamps_above_ceiling_to_the_widest_aligned_width() -> None:
    """Five spacings on a ten grid clamp to the two spacings the ceiling permits."""
    # The 0.3 percent ceiling spans 29 whole ticks, so a ten grid holds two.
    band = derive_position_range(
        POOL_CURRENT_TICK, POOL_TICK_SPACING, 5, WidthSource.EXPLICIT_OVERRIDE
    )

    assert band.half_width_ticks == 20
    assert band.tick_lower == -11680
    assert band.tick_upper == -11640
    assert band.clamped_to_ceiling is True
    assert band.requested_half_width_spacings == 5


def test_derived_range_accepts_width_exactly_at_the_ceiling() -> None:
    """Two spacings on a ten grid is the unclamped ceiling width."""
    band = derive_position_range(
        POOL_CURRENT_TICK, POOL_TICK_SPACING, 2, WidthSource.SOLVER_DERIVED_APR
    )

    assert band.half_width_ticks == 20
    assert band.clamped_to_ceiling is False
    assert band.width_source is WidthSource.SOLVER_DERIVED_APR


def test_derived_range_clamps_to_twenty_nine_ticks_on_a_unit_grid() -> None:
    """A unit grid keeps twenty-nine of thirty requested ticks."""
    band = derive_position_range(-11657, 1, 30, WidthSource.EXPLICIT_OVERRIDE)

    assert band.half_width_ticks == 29
    assert band.clamped_to_ceiling is True


def test_derived_range_rejects_malformed_directives() -> None:
    """Non-positive spacings and widths refuse before any range exists."""
    for spacing, width in ((0, 1), (-10, 1), (10, 0), (10, -1)):
        try:
            derive_position_range(POOL_CURRENT_TICK, spacing, width, WidthSource.EXPLICIT_OVERRIDE)
        except ValueError:
            pass
        else:
            raise AssertionError(f"spacing {spacing} width {width} did not refuse")


def test_position_amounts_invert_exactly_on_both_sides() -> None:
    """Feeding the amounts back through each side's formula recovers the liquidity."""
    liquidity = Decimal(24_943_143_011_231_143)
    sqrt_ratio = sqrt_ratio_at_tick(POOL_CURRENT_TICK)
    amount0, amount1 = position_amounts_for_liquidity(sqrt_ratio, -11670, -11650, liquidity)

    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        sqrt_lower = (TICK_PRICE_RATIO**-11670).sqrt() * X96_SCALE
        sqrt_upper = (TICK_PRICE_RATIO**-11650).sqrt() * X96_SCALE
        sqrt_current = Decimal(sqrt_ratio)
        # Each inversion is the independent rearrangement of the other formula.
        liquidity0 = amount0 * sqrt_current * sqrt_upper / (X96_SCALE * (sqrt_upper - sqrt_current))
        liquidity1 = amount1 * X96_SCALE / (sqrt_current - sqrt_lower)
        assert abs(liquidity0 - liquidity) / liquidity < Decimal("1e-45")
        assert abs(liquidity1 - liquidity) / liquidity < Decimal("1e-45")


def test_position_amounts_hold_the_geometric_mean_identity() -> None:
    """At the geometric-mean price the amounts satisfy the exact raw-unit identity."""
    liquidity = Decimal(10**18)
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        sqrt_lower = (TICK_PRICE_RATIO**-11670).sqrt() * X96_SCALE
        sqrt_upper = (TICK_PRICE_RATIO**-11650).sqrt() * X96_SCALE
        geometric_mean = (sqrt_lower * sqrt_upper).sqrt()
    # An integer raw price just below the exact geometric mean keeps it inside.
    sqrt_ratio = int(geometric_mean) - 1
    amount0, amount1 = position_amounts_for_liquidity(sqrt_ratio, -11670, -11650, liquidity)

    # amount0 = amount1 * 2**192 / (Sa * Sb) holds exactly at the geometric
    # mean of the raw bound prices.
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        expected0 = amount1 * X96_SCALE**2 / (sqrt_lower * sqrt_upper)
        # The one-unit truncation of sqrt_ratio moves the identity only slightly.
        assert abs(amount0 - expected0) / expected0 < Decimal("1e-20")


def test_position_amounts_match_float_reference_values() -> None:
    """The Decimal formulas agree with an independent float computation."""
    liquidity = Decimal(123_456_789_012_345)
    tick_lower, tick_upper = -11670, -11650
    sqrt_ratio = sqrt_ratio_at_tick(POOL_CURRENT_TICK)
    amount0, amount1 = position_amounts_for_liquidity(sqrt_ratio, tick_lower, tick_upper, liquidity)

    sqrt_lower = math.sqrt(1.0001**tick_lower) * 2**96
    sqrt_upper = math.sqrt(1.0001**tick_upper) * 2**96
    sqrt_current = float(sqrt_ratio)
    float0 = float(liquidity) * 2**96 * (sqrt_upper - sqrt_current) / (sqrt_current * sqrt_upper)
    float1 = float(liquidity) * (sqrt_current - sqrt_lower) / 2**96
    # Float cancellation across the ~2.7e28-scale square-root differences
    # bounds the agreement near 1e-10; formula-scale errors are orders larger.
    assert abs(float(amount0) - float0) / float0 < 1e-9
    assert abs(float(amount1) - float1) / float1 < 1e-9


def test_position_amounts_refuse_an_out_of_range_price() -> None:
    """A price at or beyond either bound refuses instead of going one-sided."""
    below = sqrt_ratio_at_tick(-11680)
    # One above the upper bound's truncated sqrt sits at or beyond the bound.
    at_upper = sqrt_ratio_at_tick(-11650) + 1
    for sqrt_ratio in (below, at_upper):
        try:
            position_amounts_for_liquidity(sqrt_ratio, -11670, -11650, Decimal(1))
        except ValueError:
            pass
        else:
            raise AssertionError("an out-of-range price did not refuse")


def test_position_range_state_classifies_every_tick_region() -> None:
    """Tick comparison over inclusive-lower and exclusive-upper semantics."""
    classifications = {
        -11681: PositionRangeState.BELOW_RANGE,
        -11680: PositionRangeState.IN_RANGE,
        -11671: PositionRangeState.IN_RANGE,
        -11670: PositionRangeState.ABOVE_RANGE,
        -11669: PositionRangeState.ABOVE_RANGE,
    }
    for tick, expected in classifications.items():
        assert position_range_state(-11680, -11670, tick) is expected


def test_position_range_state_refuses_an_inverted_range() -> None:
    """A lower bound at or above the upper bound refuses classification."""
    with pytest.raises(ValueError, match="tick_lower must be below tick_upper"):
        position_range_state(-11670, -11680, -11675)
    with pytest.raises(ValueError, match="tick_lower must be below tick_upper"):
        position_range_state(-11670, -11670, -11670)


def test_position_amounts_at_sqrt_ratio_agree_inside_the_range() -> None:
    """A strictly inside price matches the two-sided entry formula exactly."""
    liquidity = Decimal(135_048_209_692)
    sqrt_ratio = sqrt_ratio_at_tick(-11660)
    assert position_amounts_at_sqrt_ratio(sqrt_ratio, -11680, -11650, liquidity) == (
        position_amounts_for_liquidity(sqrt_ratio, -11680, -11650, liquidity)
    )


def test_position_amounts_at_sqrt_ratio_go_one_sided_outside_the_range() -> None:
    """A price past either bound collapses the position to its single side."""
    liquidity = Decimal(10**18)
    below0, below1 = position_amounts_at_sqrt_ratio(
        sqrt_ratio_at_tick(-11690), -11680, -11650, liquidity
    )
    assert below1 == 0
    # The whole below-range position is token zero across the full band.
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        sqrt_lower = (TICK_PRICE_RATIO**-11680).sqrt() * X96_SCALE
        sqrt_upper = (TICK_PRICE_RATIO**-11650).sqrt() * X96_SCALE
        expected_below0 = (
            liquidity * X96_SCALE * (sqrt_upper - sqrt_lower) / (sqrt_lower * sqrt_upper)
        )
    assert abs(below0 - expected_below0) / expected_below0 < Decimal("1e-45")

    above0, above1 = position_amounts_at_sqrt_ratio(
        sqrt_ratio_at_tick(-11640), -11680, -11650, liquidity
    )
    assert above0 == 0
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        sqrt_lower = (TICK_PRICE_RATIO**-11680).sqrt() * X96_SCALE
        sqrt_upper = (TICK_PRICE_RATIO**-11650).sqrt() * X96_SCALE
        expected_above1 = liquidity * (sqrt_upper - sqrt_lower) / X96_SCALE
    assert abs(above1 - expected_above1) / expected_above1 < Decimal("1e-45")


def test_position_amounts_at_sqrt_ratio_are_continuous_at_the_boundaries() -> None:
    """Approaching either bound from both sides converges on the boundary."""
    liquidity = Decimal(987_654_321_098_765)
    sqrt_lower = sqrt_ratio_at_tick(-11680)
    sqrt_upper = sqrt_ratio_at_tick(-11650)
    boundary0, boundary1 = position_amounts_at_sqrt_ratio(sqrt_lower, -11680, -11650, liquidity)
    inside0, inside1 = position_amounts_at_sqrt_ratio(sqrt_lower + 1, -11680, -11650, liquidity)
    far_below0, _ = position_amounts_at_sqrt_ratio(sqrt_lower - 1, -11680, -11650, liquidity)
    # The token-zero amount changes only infinitesimally across the boundary,
    # while the token-one amount leaves zero continuously from inside. The
    # raw bound prices sit near 4e28, so one integer step bounds the relative
    # movement near 1e-26; any real discontinuity would be orders larger.
    assert abs(boundary0 - inside0) / boundary0 < Decimal("1e-24")
    assert abs(boundary0 - far_below0) / boundary0 < Decimal("1e-24")
    assert boundary1 == 0
    assert inside1 / boundary0 < Decimal("1e-24")

    # One above the upper tick's truncated sqrt sits at or beyond the exact
    # bound, mirroring the out-of-range refusal convention.
    at_upper = sqrt_upper + 1
    top0, top1 = position_amounts_at_sqrt_ratio(at_upper, -11680, -11650, liquidity)
    upper_inside0, upper_inside1 = position_amounts_at_sqrt_ratio(
        at_upper - 1, -11680, -11650, liquidity
    )
    _, far_above1 = position_amounts_at_sqrt_ratio(at_upper + 1, -11680, -11650, liquidity)
    assert abs(top1 - upper_inside1) / top1 < Decimal("1e-24")
    assert abs(top1 - far_above1) / top1 < Decimal("1e-24")
    assert top0 == 0
    assert upper_inside0 / top1 < Decimal("1e-24")


def test_position_amounts_at_sqrt_ratio_accept_zero_liquidity() -> None:
    """An emptied position's amounts read zero on both sides everywhere."""
    for sqrt_ratio in (
        sqrt_ratio_at_tick(-11690),
        sqrt_ratio_at_tick(-11660),
        sqrt_ratio_at_tick(-11640),
    ):
        assert position_amounts_at_sqrt_ratio(sqrt_ratio, -11680, -11650, Decimal(0)) == (
            Decimal(0),
            Decimal(0),
        )


def test_position_amounts_at_sqrt_ratio_refuse_malformed_arguments() -> None:
    """Non-positive prices, negative liquidity, and inverted ranges refuse."""
    with pytest.raises(ValueError, match="sqrt_ratio must be positive"):
        position_amounts_at_sqrt_ratio(0, -11680, -11650, Decimal(1))
    with pytest.raises(ValueError, match="liquidity must not be negative"):
        position_amounts_at_sqrt_ratio(sqrt_ratio_at_tick(-11660), -11680, -11650, Decimal(-1))
    with pytest.raises(ValueError, match="tick_lower must be below tick_upper"):
        position_amounts_at_sqrt_ratio(sqrt_ratio_at_tick(-11660), -11650, -11680, Decimal(1))


def test_mint_composition_splits_the_budget_into_both_sides() -> None:
    """A seven-USDC budget funds both sides with floors below the budget."""
    observation = pool_observation()
    band = derive_position_range(
        observation.current_tick,
        observation.tick_spacing,
        1,
        WidthSource.EXPLICIT_OVERRIDE,
    )
    amounts = plan_mint_composition(
        observation.sqrt_ratio,
        band,
        Decimal(7),
        observation.stock_is_token0,
        observation.stock_decimals,
        observation.quote_decimals,
        DEFAULT_MINT_SLIPPAGE_TOLERANCE,
    )

    total_value = amounts.token0_value_usdc + amounts.token1_value_usdc
    # Floors never overstate the budget, and lose at most one raw unit a side.
    assert total_value <= Decimal(7)
    assert Decimal(7) - total_value < Decimal("0.00001")
    assert amounts.amount0_desired_units > 0
    assert amounts.amount1_desired_units > 0
    # The snapshot tick sits 7 of 20 ticks up the range, so the USDC side
    # carries roughly a third of the budget and the stock side the rest.
    assert Decimal("0.2") < amounts.token0_value_usdc / Decimal(7) < Decimal("0.45")
    assert Decimal("0.55") < amounts.token1_value_usdc / Decimal(7) < Decimal("0.8")
    # The minima sit exactly one tolerance below the desired amounts; the
    # 1-percent convention is the captain's approved calibration ruling, and
    # the plan call above consumes the same default being pinned here.
    tolerance = Decimal("0.01")
    assert amounts.amount0_min_units == int(
        Decimal(amounts.amount0_desired_units) * (1 - tolerance)
    )
    assert amounts.amount1_min_units == int(
        Decimal(amounts.amount1_desired_units) * (1 - tolerance)
    )


def test_mint_composition_refuses_a_budget_that_floors_a_side_to_zero() -> None:
    """A micro-budget that cannot fund both raw sides refuses with its code."""
    observation = pool_observation()
    band = derive_position_range(
        observation.current_tick,
        observation.tick_spacing,
        1,
        WidthSource.EXPLICIT_OVERRIDE,
    )
    try:
        plan_mint_composition(
            observation.sqrt_ratio,
            band,
            Decimal("0.0000001"),
            observation.stock_is_token0,
            observation.stock_decimals,
            observation.quote_decimals,
            Decimal("0.001"),
        )
    except LpPlanRefusalError as error:
        assert error.code is LpPlanRefusalCode.BUDGET_TOO_SMALL_FOR_BOTH_SIDES
    else:
        raise AssertionError("a zero-flooring budget did not refuse")


def test_mint_composition_refuses_a_price_outside_the_range() -> None:
    """A snapshot price beyond the derived range refuses with its code."""
    observation = pool_observation()
    band = derive_position_range(
        observation.current_tick,
        observation.tick_spacing,
        1,
        WidthSource.EXPLICIT_OVERRIDE,
    )
    try:
        plan_mint_composition(
            sqrt_ratio_at_tick(-11680),
            band,
            Decimal(7),
            observation.stock_is_token0,
            observation.stock_decimals,
            observation.quote_decimals,
            Decimal("0.001"),
        )
    except LpPlanRefusalError as error:
        assert error.code is LpPlanRefusalCode.PRICE_OUTSIDE_RANGE
    else:
        raise AssertionError("an out-of-range price did not refuse")


def test_depth_estimate_values_one_spacing_of_active_liquidity() -> None:
    """The depth estimate prices both sides of the pool's active liquidity."""
    observation = pool_observation()
    depth = estimate_in_range_depth_usdc(
        observation.sqrt_ratio,
        observation.tick_spacing,
        observation.current_tick,
        observation.pool_active_liquidity,
        observation.stock_is_token0,
        observation.stock_decimals,
        observation.quote_decimals,
    )

    # The active liquidity is staked-scale, so the one-percent cap dwarfs a
    # canary budget: assert the estimate is positive and pool-scale.
    assert depth > Decimal(0)
    assert depth > Decimal(1000)


def test_balancing_swap_covers_the_shortfall_with_buffer_and_bound() -> None:
    """The swap ceils its USDC input over the buffered shortfall."""
    observation = pool_observation()
    price = observation.price_usdc_per_stock
    shortfall_units = 470_000
    swap = plan_balancing_swap(
        shortfall_units,
        price,
        STOCK_DECIMALS,
        POOL_USDC_RESERVE_UNITS,
        Decimal("0.001"),
        Decimal("0.001"),
        Decimal("0.0005"),
    )

    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        minimum_usdc = (
            Decimal(shortfall_units).scaleb(-STOCK_DECIMALS) * price * 10**QUOTE_TOKEN_DECIMALS
        )
        # The buffered input ceils strictly above the raw shortfall value.
        assert swap.usdc_in_units >= int(minimum_usdc.to_integral_value(rounding="ROUND_CEILING"))
        assert swap.usdc_in_units <= int((minimum_usdc * Decimal("1.002")).to_integral_value())
    assert swap.required is True
    assert swap.expected_stock_units > shortfall_units
    assert swap.tranche_count == 1
    assert swap.modeled_impact_fraction < Decimal("0.0005")


def test_balancing_swap_splits_into_tranches_above_the_threshold() -> None:
    """Impact above the tranche threshold splits into enough tranches."""
    observation = pool_observation()
    price = observation.price_usdc_per_stock
    # A roughly 0.08 percent impact sits above the threshold, below the ceiling.
    shallow_reserve = 1200 * ONE_USDC_UNITS
    swap = plan_balancing_swap(
        300_000,
        price,
        STOCK_DECIMALS,
        shallow_reserve,
        Decimal("0"),
        Decimal("0.001"),
        Decimal("0.0005"),
    )

    assert Decimal("0.0005") < swap.modeled_impact_fraction < Decimal("0.001")
    assert swap.tranche_count == 2


def test_balancing_swap_refuses_impact_at_the_ceiling() -> None:
    """A shallow enough reserve refuses the swap with its code."""
    observation = pool_observation()
    price = observation.price_usdc_per_stock
    try:
        plan_balancing_swap(
            300_000,
            price,
            STOCK_DECIMALS,
            100 * ONE_USDC_UNITS,
            Decimal("0"),
            Decimal("0.001"),
            Decimal("0.0005"),
        )
    except LpPlanRefusalError as error:
        assert error.code is LpPlanRefusalCode.SWAP_IMPACT_ABOVE_CEILING
    else:
        raise AssertionError("a ceiling-reaching swap did not refuse")


def test_swap_back_prices_floors_and_bounds_impact() -> None:
    """The swap-back quotes at the snapshot price with a floored minimum."""
    observation = pool_observation()
    price = observation.price_usdc_per_stock
    stock_in_units = 2_000_000
    swap = plan_swap_back(
        stock_in_units,
        price,
        STOCK_DECIMALS,
        POOL_STOCK_RESERVE_UNITS,
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.0005"),
    )

    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        quoted = Decimal(stock_in_units).scaleb(-STOCK_DECIMALS) * price * 10**QUOTE_TOKEN_DECIMALS
        assert swap.expected_usdc_units == int(quoted.to_integral_value(rounding="ROUND_FLOOR"))
        floored = Decimal(swap.expected_usdc_units) * Decimal("0.99")
        assert swap.min_usdc_units == int(floored.to_integral_value(rounding="ROUND_FLOOR"))
        impact = Decimal(stock_in_units) / Decimal(POOL_STOCK_RESERVE_UNITS + stock_in_units)
        assert swap.modeled_impact_fraction == +impact
    assert swap.tranche_count == 1


def test_swap_back_splits_into_tranches_above_the_threshold() -> None:
    """Impact above the tranche threshold splits the exit symmetrically."""
    observation = pool_observation()
    price = observation.price_usdc_per_stock
    shallow_reserve = 300_000_000
    swap = plan_swap_back(
        200_000,
        price,
        STOCK_DECIMALS,
        shallow_reserve,
        Decimal("0.01"),
        Decimal("0.001"),
        Decimal("0.0005"),
    )

    assert Decimal("0.0005") < swap.modeled_impact_fraction < Decimal("0.001")
    assert swap.tranche_count == 2


def test_swap_back_refuses_impact_at_the_ceiling() -> None:
    """A stock balance exceeding the pool's depth refuses with its code."""
    observation = pool_observation()
    price = observation.price_usdc_per_stock
    try:
        plan_swap_back(
            500_000_000,
            price,
            STOCK_DECIMALS,
            100_000_000,
            Decimal("0.01"),
            Decimal("0.001"),
            Decimal("0.0005"),
        )
    except LpPlanRefusalError as error:
        assert error.code is LpPlanRefusalCode.SWAP_BACK_IMPACT_ABOVE_CEILING
    else:
        raise AssertionError("a ceiling-reaching swap-back did not refuse")


def test_plan_mint_entry_builds_the_canary_plan_end_to_end() -> None:
    """The canary directive plans a capped mint with its balancing swap."""
    plan = plan_mint_entry(
        LpExecutionPolicy(),
        pool_observation(),
        mint_directive(),
        safe_inventory(),
    )

    assert plan.symbol == "AAPLc"
    assert plan.nfpm_address == NFPM_ADDRESS
    assert plan.pool_address == normalize_evm_address(plan.pool_address)
    assert plan.position_range.tick_lower == -11670
    assert plan.position_range.tick_upper == -11650
    assert plan.amounts.liquidity > 0
    # The held 0.0062 stock cannot cover the stock side, so a swap is planned.
    assert plan.balancing_swap.required is True
    assert plan.balancing_swap.usdc_in_units > 0
    # The composition diagnostic names each side with its own raw amounts:
    # token0 is USDC and token1 the stock on this pool, never swapped.
    composition_line = next(line for line in plan.diagnostics if line.startswith("composition:"))
    assert (
        f"{plan.amounts.amount0_desired_units} raw USDC + "
        f"{plan.amounts.amount1_desired_units} raw stock" in composition_line
    )
    # The quote side plus the swap stays inside the Safe's seven USDC.
    usdc_side = plan.amounts.amount0_desired_units
    assert usdc_side + plan.balancing_swap.usdc_in_units <= 7 * ONE_USDC_UNITS
    # Every cap in the documented order is labeled.
    assert plan.caps_enforced[0].startswith("budget at or below")
    assert any("total pilot cap" in cap for cap in plan.caps_enforced)
    assert any("in-range depth" in cap for cap in plan.caps_enforced)
    assert any("balancing swap impact" in cap for cap in plan.caps_enforced)
    assert plan.caps_enforced[-1].startswith("Safe USDC covers")
    assert plan.snapshot_block == 30_000_000


def test_plan_mint_entry_skips_the_swap_when_stock_covers_the_side() -> None:
    """Inventory already holding the stock side plans no balancing swap."""
    plan = plan_mint_entry(
        LpExecutionPolicy(),
        pool_observation(),
        mint_directive(),
        safe_inventory(stock_units=10_000_000),
    )

    assert plan.balancing_swap.required is False
    assert plan.balancing_swap.usdc_in_units == 0
    assert any("no balancing swap required" in cap for cap in plan.caps_enforced)


def test_plan_amounts_build_valid_mint_calldata() -> None:
    """The plan's amounts encode directly into the twelve-field mint call."""
    plan = plan_mint_entry(
        LpExecutionPolicy(),
        pool_observation(),
        mint_directive(),
        safe_inventory(),
    )
    observation = pool_observation()
    params = LpMintParams(
        token0_address=observation.token0_address,
        token1_address=observation.token1_address,
        tick_spacing=observation.tick_spacing,
        tick_lower=plan.position_range.tick_lower,
        tick_upper=plan.position_range.tick_upper,
        amount0_desired_units=plan.amounts.amount0_desired_units,
        amount1_desired_units=plan.amounts.amount1_desired_units,
        amount0_min_units=plan.amounts.amount0_min_units,
        amount1_min_units=plan.amounts.amount1_min_units,
        recipient_address=SAFE_ADDRESS,
        deadline=1_788_800_000,
    )
    calldata = build_lp_mint_calldata(params)

    # The selector plus twelve static words is 388 bytes exactly.
    assert calldata.startswith(f"0x{LP_MINT_SELECTOR}")
    assert len(calldata) == 2 + 8 + 12 * 64


def test_plan_mint_entry_refuses_budgets_above_each_cap() -> None:
    """The per-pool cap and the total-exposure cap each refuse with their code."""
    with pytest.raises(LpPlanRefusalError) as pool_cap:
        plan_mint_entry(
            LpExecutionPolicy(),
            pool_observation(),
            mint_directive(budget_usdc=Decimal("100.01")),
            safe_inventory(usdc_units=10**9),
        )
    assert pool_cap.value.code is LpPlanRefusalCode.BUDGET_ABOVE_POOL_CAP
    with pytest.raises(LpPlanRefusalError) as exposure_cap:
        plan_mint_entry(
            LpExecutionPolicy(),
            pool_observation(),
            mint_directive(budget_usdc=Decimal(7)),
            safe_inventory(existing_position_value_usdc=Decimal(96)),
        )
    assert exposure_cap.value.code is LpPlanRefusalCode.BUDGET_ABOVE_TOTAL_EXPOSURE_CAP


def test_plan_mint_entry_refuses_positions_above_the_depth_share() -> None:
    """A shallow pool refuses a budget above its one-percent depth share."""
    with pytest.raises(LpPlanRefusalError) as depth_share:
        plan_mint_entry(
            LpExecutionPolicy(),
            pool_observation(pool_active_liquidity=10**9),
            mint_directive(),
            safe_inventory(),
        )
    assert depth_share.value.code is LpPlanRefusalCode.POSITION_ABOVE_POOL_DEPTH_FRACTION


def test_plan_mint_entry_refuses_insufficient_usdc_for_entry() -> None:
    """A Safe without USDC for the quote side plus swap refuses with its code."""
    with pytest.raises(LpPlanRefusalError) as insufficient:
        plan_mint_entry(
            LpExecutionPolicy(),
            pool_observation(),
            mint_directive(),
            safe_inventory(usdc_units=ONE_USDC_UNITS),
        )
    assert insufficient.value.code is LpPlanRefusalCode.INSUFFICIENT_USDC_FOR_ENTRY


def test_execution_policy_refuses_configuration_above_its_ceilings() -> None:
    """No configuration may raise any pilot cap above its documented ceiling."""
    for overrides in (
        {"max_position_usdc_per_pool": Decimal("100.01")},
        {"max_total_pilot_exposure_usdc": Decimal("100.01")},
        {"max_position_fraction_of_pool_depth": Decimal("0.02")},
        {"swap_impact_ceiling_fraction": Decimal("0.002")},
    ):
        try:
            LpExecutionPolicy.model_validate(overrides)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{overrides} did not refuse")


def test_pilot_caps_pin_the_calibrated_bounds() -> None:
    """The constants and defaults carry the captain-calibrated 100/100 bounds.

    The captain's calibration ruling (2026-09-07 ~23:45, reconfirmed
    2026-09-08) raised the per-pool cap from 50 to 100 USDC while the
    fleet-wide total stayed 100 USDC, so one pool may now commit the whole
    pilot envelope and no more.
    """
    assert Decimal("100") == MAX_POSITION_USDC_PER_POOL
    assert Decimal("100") == MAX_TOTAL_PILOT_EXPOSURE_USDC
    policy = LpExecutionPolicy()
    assert policy.max_position_usdc_per_pool == MAX_POSITION_USDC_PER_POOL
    assert policy.max_total_pilot_exposure_usdc == MAX_TOTAL_PILOT_EXPOSURE_USDC
    # A budget at exactly the per-pool cap passes it; only strictly above refuses.
    plan = plan_mint_entry(
        policy,
        pool_observation(),
        mint_directive(budget_usdc=Decimal("100")),
        safe_inventory(usdc_units=200 * ONE_USDC_UNITS),
    )
    assert plan.caps_enforced[0] == "budget at or below the 100 USDC per-pool cap"
    assert any(
        cap == "budget at or below the remaining 100 USDC of the 100 USDC total pilot cap"
        for cap in plan.caps_enforced
    )
