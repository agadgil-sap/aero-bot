"""Pin Aerodrome's displayed emissions-APR convention and its width family."""

from decimal import Decimal

import pytest

from aero_bot.emissions_apr import (
    aerodrome_display_emissions_apr,
    conservative_income_apr,
    emissions_apr_at_tick_width,
    format_apr_percent,
    median_decimal,
    staked_value_usdc,
)
from aero_bot.history import price_usdc_per_stock
from aero_bot.lp_plan import position_amounts_for_liquidity

# The frozen 2026-09-07 cross-section the captain captured live: Aerodrome's
# UI displayed 830.0% for AAPLc/USDC while the naive annualization over the
# separately displayed staked-TVL column gave 119.7%. The identified base is
# the Sugar snapshot's current-cell staked value (the UI's own APR
# denominator), which the display's staked column is not.
FROZEN_REWARD_RATE_UNITS = 102_277_628_210_565_102  # 0.10227763 AERO/s
FROZEN_AERO_PRICE_USDC = Decimal("0.5428")
FROZEN_CELL_STAKED_VALUE_USDC = Decimal("210935")


def test_format_apr_percent_renders_the_fraction_with_its_percent() -> None:
    """The operator annotation keeps the raw fraction and adds percent.

    The gnhf 35 postmortem: every reader of a bare ``raw APR 542.14`` line
    read the decimal fraction as percent - a one-hundred-x misread. The
    annotation renders the raw value unchanged (the audited,
    machine-comparable figure) with the percent reading beside it.
    """
    assert format_apr_percent(Decimal("2.0")) == "2.0 (about 200 percent)"
    assert format_apr_percent(Decimal("1.5")) == "1.5 (about 150 percent)"
    # The overnight SNDKc reading: 542.14 as a fraction is 54,214 percent,
    # never "542 percent".
    assert format_apr_percent(Decimal("542.14")) == "542.14 (about 54,214 percent)"
    # Large readings carry thousands separators so their magnitude reads.
    assert (
        format_apr_percent(Decimal("347.907697331990933767452574"))
        == "347.907697331990933767452574 (about 34,791 percent)"
    )
    # Rounding is half-up and zero still renders.
    assert format_apr_percent(Decimal("0.005")) == "0.005 (about 1 percent)"
    assert format_apr_percent(Decimal("0")) == "0 (about 0 percent)"


def test_frozen_aaplc_case_reproduces_the_displayed_830_percent() -> None:
    """The frozen live inputs reproduce Aerodrome's displayed 830.0%."""
    apr = (
        FROZEN_REWARD_RATE_UNITS
        / Decimal(10**18)
        * FROZEN_AERO_PRICE_USDC
        * Decimal(31_536_000)
        / FROZEN_CELL_STAKED_VALUE_USDC
    )
    assert Decimal(100) * apr == pytest.approx(Decimal("830.24"), rel=Decimal("0.005"))


def test_display_convention_matches_the_annualization_over_staked_value() -> None:
    """The shared conversion is the annualized reward over the staked value."""
    # USDC as token0, stock as token1: 2 USDC-side plus 0.01 stock at 300.
    apr = aerodrome_display_emissions_apr(
        10**18,  # one AERO per second
        Decimal("0.5"),
        2_000_000,  # 2 USDC staked
        1_000_000,  # 0.01 stock staked
        False,
        8,
        6,
        Decimal("300"),
    )
    staked_value = staked_value_usdc(2_000_000, 1_000_000, False, 8, 6, Decimal("300"))
    assert staked_value == Decimal("5")
    # Annual reward value: 1 AERO/s * 0.5 USDC * 31,536,000s = 15,768,000.
    assert apr == Decimal(15_768_000) / Decimal(5)


def test_display_convention_rejects_degenerate_inputs() -> None:
    """Zero emissions, price, or staked value refuses rather than guessing."""
    with pytest.raises(ValueError):
        aerodrome_display_emissions_apr(0, Decimal("0.5"), 1, 1, False, 8, 6, Decimal("1"))
    with pytest.raises(ValueError):
        aerodrome_display_emissions_apr(1, Decimal("0.5"), 0, 0, False, 8, 6, Decimal("1"))
    with pytest.raises(ValueError):
        aerodrome_display_emissions_apr(1, Decimal("0"), 1, 1, False, 8, 6, Decimal("1"))


def test_width_family_shrinks_as_the_window_widens() -> None:
    """Halving the carried value per unit doubles the concentration APR."""
    sqrt_ratio = int(Decimal("1.0001") ** Decimal("-6") * Decimal(2) ** 96)  # tick -12
    apr_one = emissions_apr_at_tick_width(
        10**18, Decimal("0.5"), 10**18, sqrt_ratio, -10, 10, False, 8, 6
    )
    apr_three = emissions_apr_at_tick_width(
        10**18, Decimal("0.5"), 10**18, sqrt_ratio, -10, 30, False, 8, 6
    )
    apr_ten = emissions_apr_at_tick_width(
        10**18, Decimal("0.5"), 10**18, sqrt_ratio, -10, 100, False, 8, 6
    )
    assert apr_one > apr_three > apr_ten
    # Narrow windows value near-linearly, so tripling the width roughly
    # divides the concentration APR by three.
    assert apr_one / apr_three == pytest.approx(Decimal(3), rel=Decimal("0.02"))
    assert apr_one / apr_ten == pytest.approx(Decimal(10), rel=Decimal("0.05"))


def test_width_family_at_one_cell_equals_the_display_convention() -> None:
    """The centered window family stays on the cell's value band."""
    sqrt_ratio = int(Decimal("1.0001") ** Decimal("-5803") * Decimal(2) ** 96)  # tick -11606
    anchor = -11610
    spacing = 10
    gauge_liquidity = Decimal(2_422_000_000_000_000)
    price = price_usdc_per_stock(sqrt_ratio, False, 8, 6)
    unit0, unit1 = position_amounts_for_liquidity(sqrt_ratio, anchor, anchor + spacing, Decimal(1))
    cell_value = +(unit0 * Decimal(10) ** -6 + unit1 * Decimal(10) ** -8 * price)
    apr_cell = emissions_apr_at_tick_width(
        102_277_628_210_565_102,
        Decimal("0.639216"),
        int(gauge_liquidity),
        sqrt_ratio,
        anchor + spacing // 2,
        spacing,
        False,
        8,
        6,
    )
    # The centered window [anchor, anchor + 2*spacing] spans twice the cell's
    # width and carries roughly twice its value at these narrow widths.
    denominator_centered = gauge_liquidity * cell_value * 2
    annual = (
        Decimal(102_277_628_210_565_102)
        / Decimal(10**18)
        * Decimal("0.639216")
        * Decimal(31_536_000)
    )
    assert apr_cell == pytest.approx(annual / denominator_centered, rel=Decimal("0.05"))


class TestConservativeIncomeExpectation:
    """The captain's 2026-09-28 correction: information, never exclusion.

    The venue's displayed convention is the reference and its high
    readings are real - the boosted-yield thesis deploys into them. The
    conservative income expectation only floors what the yield-ASSUMING
    surfaces read: min(current, median(trailing)), so a transient spike
    never sizes or justifies a position.
    """

    def test_the_median_of_odd_and_even_windows(self) -> None:
        """Odd windows take the middle; even windows average the two."""
        assert median_decimal([Decimal("1"), Decimal("9"), Decimal("5")]) == Decimal("5")
        assert median_decimal(
            [Decimal("2"), Decimal("3"), Decimal("10"), Decimal("40")]
        ) == Decimal("6.5")
        with pytest.raises(ValueError, match="empty"):
            median_decimal([])

    def test_a_spike_reads_as_the_trailing_median(self) -> None:
        """The 2026-09-28 shape: 300 instantaneous against a 2-3 window."""
        basis = conservative_income_apr(
            Decimal("300"), [Decimal("2"), Decimal("3"), Decimal("300")]
        )
        assert basis == Decimal("3")

    def test_a_collapse_reads_as_the_current_lower_reading(self) -> None:
        """The basis never inflates an expectation above the current."""
        assert conservative_income_apr(Decimal("1"), [Decimal("2"), Decimal("3")]) == (Decimal("1"))
        assert conservative_income_apr(Decimal("2"), [Decimal("10")]) == Decimal("2")

    def test_a_cold_start_or_flat_window_reads_unchanged(self) -> None:
        """No history, or a matching window, changes nothing."""
        assert conservative_income_apr(Decimal("5"), []) == Decimal("5")
        assert conservative_income_apr(Decimal("5"), [Decimal("5"), Decimal("5")]) == Decimal("5")
