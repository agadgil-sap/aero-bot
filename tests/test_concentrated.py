"""Behavior tests for position-aware Aerodrome Slipstream mathematics."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aero_bot.concentrated import (
    ConcentratedLiquidityAnalyzer,
    ConcentratedPositionSnapshot,
    PositionRangeState,
)

# One full year makes loss-fraction and annualized loss equal in baseline fixtures.
ONE_YEAR_SECONDS = 365 * 24 * 60 * 60


def position_snapshot(**overrides: object) -> ConcentratedPositionSnapshot:
    """Build a deterministic normalized concentrated-liquidity snapshot.

    Args:
        **overrides: Fields changed to exercise one range or valuation behavior.

    Returns:
        A validated immutable position snapshot.
    """
    # Baseline range has exact square roots one and two with a midpoint root of 1.5.
    values: dict[str, object] = {
        "liquidity": Decimal(100),
        "lower_price": Decimal(1),
        "upper_price": Decimal(4),
        "entry_price": Decimal("2.25"),
        "current_price": Decimal("2.25"),
        "entry_token0_usd": Decimal("2.25"),
        "entry_token1_usd": Decimal(1),
        "current_token0_usd": Decimal("2.25"),
        "current_token1_usd": Decimal(1),
        "holding_period_seconds": ONE_YEAR_SECONDS,
    }
    values.update(overrides)
    return ConcentratedPositionSnapshot.model_validate(values)


def test_in_range_amounts_follow_square_root_price_formulas() -> None:
    """Mid-range liquidity contains both assets in their canonical v3 amounts."""
    # Exact-root fixture gives amount0 50/3 and amount1 50 without float tolerance.
    analysis = ConcentratedLiquidityAnalyzer().analyze(position_snapshot())

    assert analysis.range_state is PositionRangeState.IN_RANGE
    assert analysis.current_amounts.token0.quantize(Decimal("1e-26")) == (
        Decimal(50) / Decimal(3)
    ).quantize(Decimal("1e-26"))
    assert analysis.current_amounts.token1 == Decimal(50)
    assert analysis.sqrt_range_progress == Decimal("0.5")
    assert analysis.impermanent_loss_fraction == 0
    assert analysis.impermanent_loss_apr == 0


@pytest.mark.parametrize(
    ("price", "expected_state", "expected_progress"),
    [
        (Decimal("0.25"), PositionRangeState.BELOW_RANGE, Decimal(0)),
        (Decimal(1), PositionRangeState.IN_RANGE, Decimal(0)),
        (Decimal(4), PositionRangeState.ABOVE_RANGE, Decimal(1)),
        (Decimal(9), PositionRangeState.ABOVE_RANGE, Decimal(1)),
    ],
)
def test_range_boundaries_and_single_sided_inventory(
    price: Decimal,
    expected_state: PositionRangeState,
    expected_progress: Decimal,
) -> None:
    """Range boundaries produce the expected active state and one-sided inventory.

    Args:
        price: Current token1-per-token0 price under test.
        expected_state: Inclusive-lower and exclusive-upper state.
        expected_progress: Expected clamped square-root range progress.
    """
    # Matching USD valuation keeps focus on range and inventory behavior.
    analysis = ConcentratedLiquidityAnalyzer().analyze(
        position_snapshot(current_price=price, current_token0_usd=price)
    )

    assert analysis.range_state is expected_state
    assert analysis.sqrt_range_progress == expected_progress
    if expected_state is PositionRangeState.BELOW_RANGE:
        assert analysis.current_amounts.token0 == Decimal(50)
        assert analysis.current_amounts.token1 == 0
        assert analysis.token0_value_fraction == 1
    if expected_state is PositionRangeState.ABOVE_RANGE:
        assert analysis.current_amounts.token0 == 0
        assert analysis.current_amounts.token1 == Decimal(100)
        assert analysis.token0_value_fraction == 0


def test_position_aware_impermanent_loss_compares_with_entry_inventory() -> None:
    """An above-range price move values LP inventory against holding entry amounts."""
    # At price nine the LP contains 100 token1 while entry inventory would now be worth 200.
    analysis = ConcentratedLiquidityAnalyzer().analyze(
        position_snapshot(current_price=Decimal(9), current_token0_usd=Decimal(9))
    )

    assert analysis.current_position_value_usd == Decimal(100)
    assert analysis.hold_value_usd == Decimal(200)
    assert analysis.impermanent_loss_fraction == Decimal("0.5")
    assert analysis.impermanent_loss_apr == Decimal("0.5")
    assert "out of range" in analysis.diagnostics[0]


def test_short_holding_period_scales_conservative_loss_cost() -> None:
    """Linear annualization makes severe short-period divergence visibly expensive."""
    # Half-year holding period doubles the one-year annualized cost for the same loss fraction.
    analysis = ConcentratedLiquidityAnalyzer().analyze(
        position_snapshot(
            current_price=Decimal(9),
            current_token0_usd=Decimal(9),
            holding_period_seconds=ONE_YEAR_SECONDS // 2,
        )
    )

    assert analysis.impermanent_loss_fraction == Decimal("0.5")
    assert analysis.impermanent_loss_apr == Decimal(1)


def test_independent_valuation_deviation_is_reported_in_basis_points() -> None:
    """Pool and independent USD price disagreement remains visible for risk gating."""
    # Independent token values imply price 2.475, ten percent above pool price 2.25.
    analysis = ConcentratedLiquidityAnalyzer().analyze(
        position_snapshot(current_token0_usd=Decimal("2.475"))
    )

    assert analysis.valuation_price_deviation_bps == Decimal(1_000)


@pytest.mark.parametrize(
    ("lower_price", "upper_price"),
    [(Decimal(4), Decimal(4)), (Decimal(5), Decimal(4))],
)
def test_empty_or_inverted_range_is_rejected(lower_price: Decimal, upper_price: Decimal) -> None:
    """Invalid position boundaries never reach amount calculations.

    Args:
        lower_price: Invalid lower range boundary.
        upper_price: Invalid upper range boundary.
    """
    with pytest.raises(ValidationError, match="lower_price must be less"):
        position_snapshot(lower_price=lower_price, upper_price=upper_price)


def test_public_price_calculations_reject_nonpositive_price() -> None:
    """Direct callers cannot classify or calculate impossible nonpositive prices."""
    # One analyzer and valid snapshot isolate validation of the direct price argument.
    analyzer = ConcentratedLiquidityAnalyzer()
    # Both public calculation methods enforce the same positive-price contract.
    with pytest.raises(ValueError, match="price must be positive"):
        analyzer.amounts_at_price(position_snapshot(), Decimal(0))
    with pytest.raises(ValueError, match="price must be positive"):
        analyzer.range_state(position_snapshot(), Decimal(-1))
