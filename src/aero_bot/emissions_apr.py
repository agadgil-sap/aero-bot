"""The Aerodrome emissions-APR convention, shared by runtime and rehearsal.

Aerodrome's own displayed emissions APR divides the gauge's annualized reward
value by the value of the gauge's staked liquidity compressed into the pool's
current grid cell. The authoritative source is the LP Sugar contract the UI
reads: ``LpSugar.vy``'s concentrated branch (``_cl_lp``) computes the pool's
``staked0``/``staked1`` fields as
``cl_helper.getAmountsForLiquidity(slot.sqrtPriceX96, sqrtRatioAtTick(tick_low),
sqrtRatioAtTick(tick_high), gauge_liquidity)`` where ``tick_low`` is the
current tick floored onto the spacing grid and ``tick_high = tick_low +
tickSpacing`` - exactly one grid cell. The displayed emission APR is the
reward stream over the value of those staked amounts, which is why it is a
per-cell concentration number: it inflates as the staked liquidity's value
per unit concentrates near the current price, and it is Aerodrome's own
screening indicator rather than a pool-wide average yield.

Live verification (2026-09-08, formula vs the Aerodrome frontend minutes
apart): reproduced within about one percent for MSFTc (3,128.8 vs 3,106.9),
SPCXc (4,125.6 vs 4,078.87), TSLAc (3,692.2 vs 3,653.54), and within about
six percent for AAPLc and GOOGLc on a pool whose displayed number moved
five-fold inside an hour; the captain's anchor pool wtSGOV/USDC read
357.21 percent live and computes 346.5 percent forty minutes later, inside
the pool's own display drift. Pools shown as "+9,000%" (AMZNc, MSTRc,
SNDKc, wtSPYM) are the frontend's display clamp, not an anomaly: the
convention computes values at or above the clamp for each. The historical
119.7-percent "naive" number divided instead by the UI's separately
displayed staked-TVL column - a different, wider quantity - which is
exactly the factor-6.94 reconciliation puzzle.

The width family below generalizes the same convention to any tick window
centered on the grid anchor, because emissions accrue per unit of staked
liquidity regardless of range: halving the window halves the value carried
per unit and doubles the concentration APR.
"""

from decimal import Decimal, localcontext

from aero_bot.concentrated import MATH_PRECISION

# The frontend annualizes with 24 * 60 * 60 * 365 seconds.
SECONDS_PER_YEAR = Decimal(31_536_000)
# AERO, the emissions token, uses 18 decimals on Base.
AERO_DECIMALS = 18


def staked_value_usdc(
    staked_reserve0_units: int,
    staked_reserve1_units: int,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
    price: Decimal,
) -> Decimal:
    """Value one pool's gauge-staked balances at the snapshot price.

    Args:
        staked_reserve0_units: Raw staked token-zero balance from Sugar.
        staked_reserve1_units: Raw staked token-one balance from Sugar.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.
        price: USDC price of one whole stock token at the same snapshot.

    Returns:
        The staked value in USDC - the denominator of Aerodrome's displayed
        emissions APR.
    """
    stock_units = Decimal(staked_reserve0_units if stock_is_token0 else staked_reserve1_units)
    quote_units = Decimal(staked_reserve1_units if stock_is_token0 else staked_reserve0_units)
    with localcontext() as context:
        context.prec = MATH_PRECISION
        return +(
            stock_units / Decimal(10) ** stock_decimals * price
            + quote_units / Decimal(10) ** quote_decimals
        )


def aerodrome_display_emissions_apr(
    emissions_per_second_units: int,
    aero_price_usdc: Decimal,
    staked_reserve0_units: int,
    staked_reserve1_units: int,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
    price: Decimal,
) -> Decimal:
    """Quote the emissions APR exactly as Aerodrome's frontend displays it.

    Args:
        emissions_per_second_units: Raw gauge reward rate per second.
        aero_price_usdc: Live USDC price of one whole AERO token.
        staked_reserve0_units: Raw staked token-zero balance from Sugar.
        staked_reserve1_units: Raw staked token-one balance from Sugar.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.
        price: USDC price of one whole stock token at the same snapshot.

    Returns:
        The displayed emissions APR as a decimal fraction (8.3 is 830
        percent).

    Raises:
        ValueError: If the staked value floors to zero or inputs are
            non-positive where the APR is undefined.
    """
    if emissions_per_second_units <= 0:
        raise ValueError("emissions_per_second_units must be positive")
    if aero_price_usdc <= 0:
        raise ValueError("aero_price_usdc must be positive")
    denominator = staked_value_usdc(
        staked_reserve0_units,
        staked_reserve1_units,
        stock_is_token0,
        stock_decimals,
        quote_decimals,
        price,
    )
    if denominator <= 0:
        raise ValueError("the staked value floors to zero; the APR is undefined")
    with localcontext() as context:
        context.prec = MATH_PRECISION
        annual_reward_usdc = (
            Decimal(emissions_per_second_units)
            / Decimal(10) ** AERO_DECIMALS
            * aero_price_usdc
            * SECONDS_PER_YEAR
        )
        return +(annual_reward_usdc / denominator)


def emissions_apr_at_tick_width(
    emissions_per_second_units: int,
    aero_price_usdc: Decimal,
    gauge_liquidity_units: int,
    sqrt_ratio: int,
    anchor_tick: int,
    half_width_ticks: int,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Quote the emissions APR for staked liquidity held at one tick width.

    Emissions accrue per unit of staked liquidity regardless of range, so a
    position spanning the window carries the same reward stream over the
    window's value: the same convention as the display APR, generalized from
    the one-cell window to any half width in ticks around the grid anchor.

    Args:
        emissions_per_second_units: Raw gauge reward rate per second.
        aero_price_usdc: Live USDC price of one whole AERO token.
        gauge_liquidity_units: Raw staked liquidity sharing the emissions.
        sqrt_ratio: The pool's positive raw sqrtPriceX96 snapshot.
        anchor_tick: The grid anchor tick (current tick floored to spacing).
        half_width_ticks: Half the window width in ticks; the window spans
            ``[anchor - half_width, anchor + half_width]``.
        stock_is_token0: True when the stock token sorts before USDC.
        stock_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The concentration emissions APR as a decimal fraction.

    Raises:
        ValueError: If any input leaves the value or APR undefined.
    """
    if emissions_per_second_units <= 0:
        raise ValueError("emissions_per_second_units must be positive")
    if aero_price_usdc <= 0:
        raise ValueError("aero_price_usdc must be positive")
    if gauge_liquidity_units <= 0:
        raise ValueError("gauge_liquidity_units must be positive")
    if half_width_ticks <= 0:
        raise ValueError("half_width_ticks must be positive")
    # Imported lazily: the planner and history modules consume this module's
    # shared convention, so a module-level import would cycle.
    from aero_bot.history import price_usdc_per_stock
    from aero_bot.lp_plan import position_amounts_for_liquidity

    price = price_usdc_per_stock(sqrt_ratio, stock_is_token0, stock_decimals, quote_decimals)
    with localcontext() as context:
        context.prec = MATH_PRECISION
        unit0, unit1 = position_amounts_for_liquidity(
            sqrt_ratio,
            anchor_tick - half_width_ticks,
            anchor_tick + half_width_ticks,
            Decimal(1),
        )
        # Whichever side is USDC values at one; the stock side at the price.
        unit_value = +(
            (
                unit0
                * (
                    Decimal(10) ** -stock_decimals
                    if stock_is_token0
                    else Decimal(10) ** -quote_decimals
                )
            )
            * (price if stock_is_token0 else Decimal(1))
            + (
                unit1
                * (
                    Decimal(10) ** -quote_decimals
                    if stock_is_token0
                    else Decimal(10) ** -stock_decimals
                )
            )
            * (Decimal(1) if stock_is_token0 else price)
        )
        denominator = Decimal(gauge_liquidity_units) * unit_value
        if denominator <= 0:
            raise ValueError("the width's staked value floors to zero; the APR is undefined")
        annual_reward_usdc = (
            Decimal(emissions_per_second_units)
            / Decimal(10) ** AERO_DECIMALS
            * aero_price_usdc
            * SECONDS_PER_YEAR
        )
        return +(annual_reward_usdc / denominator)
