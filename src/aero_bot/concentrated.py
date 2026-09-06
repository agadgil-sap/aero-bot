"""Position-aware concentrated-liquidity math for Aerodrome Slipstream."""

from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, NonNegativeDecimal

# Aerodrome documents that Slipstream positions use explicit tick-defined price ranges.
AERODROME_LIQUIDITY_DOCS_URL = (
    "https://github.com/aerodrome-finance/docs/blob/main/content/liquidity.mdx"
)
# Uniswap's canonical v3 position implementation supplies the inherited amount formulas.
UNISWAP_V3_POSITION_SOURCE_URL = (
    "https://github.com/Uniswap/v3-sdk/blob/main/src/entities/position.ts"
)
# High internal precision prevents routine square-root rounding from dominating risk estimates.
MATH_PRECISION = 60
# One basis point is one ten-thousandth of a unit ratio.
BASIS_POINTS_PER_UNIT = Decimal(10_000)
# A fixed 365-day year matches the deterministic risk engine annualization convention.
SECONDS_PER_YEAR = Decimal(365 * 24 * 60 * 60)


class PositionRangeState(StrEnum):
    """Describe where the current price sits relative to a position range."""

    # Below range means the position is entirely token0 and earns no active-range fees.
    BELOW_RANGE = "below_range"
    # In range means the position contains both assets and participates in active liquidity.
    IN_RANGE = "in_range"
    # Above range means the position is entirely token1 and earns no active-range fees.
    ABOVE_RANGE = "above_range"


class ConcentratedPositionSnapshot(BaseModel):
    """Capture range, liquidity, valuation, and holding-period evidence for one position."""

    # Frozen strict fields preserve one coherent position-analysis input.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Liquidity is the position's Uniswap v3-style L value in normalized token units.
    liquidity: Annotated[Decimal, Field(gt=0)]
    # Lower price is the inclusive token1-per-token0 boundary of the active range.
    lower_price: Annotated[Decimal, Field(gt=0)]
    # Upper price is the exclusive token1-per-token0 boundary of the active range.
    upper_price: Annotated[Decimal, Field(gt=0)]
    # Entry price determines the token inventory initially committed to the position.
    entry_price: Annotated[Decimal, Field(gt=0)]
    # Current price determines the position's current token inventory and range state.
    current_price: Annotated[Decimal, Field(gt=0)]
    # Entry token0 value records the independent US-dollar price at position inception.
    entry_token0_usd: Annotated[Decimal, Field(gt=0)]
    # Entry token1 value records the independent US-dollar price at position inception.
    entry_token1_usd: Annotated[Decimal, Field(gt=0)]
    # Current token0 value prices both the LP and hold comparison consistently.
    current_token0_usd: Annotated[Decimal, Field(gt=0)]
    # Current token1 value prices both the LP and hold comparison consistently.
    current_token1_usd: Annotated[Decimal, Field(gt=0)]
    # Holding period supports a simple deterministic annualized loss-cost estimate.
    holding_period_seconds: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def require_ordered_range(self) -> Self:
        """Reject an empty or inverted concentrated-liquidity range."""
        if self.lower_price >= self.upper_price:
            raise ValueError("lower_price must be less than upper_price")
        return self


class TokenAmounts(BaseModel):
    """Represent normalized token inventory for one liquidity value and price."""

    # Frozen strict fields prevent calculated inventory from changing after valuation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Token0 amount follows the v3 amount0 square-root-price delta formula.
    token0: NonNegativeDecimal
    # Token1 amount follows the v3 amount1 square-root-price delta formula.
    token1: NonNegativeDecimal


class ConcentratedPositionAnalysis(BaseModel):
    """Expose position inventory, range use, valuation, and conservative loss cost."""

    # Frozen strict fields keep every derived value on one coherent snapshot.
    model_config = IMMUTABLE_MODEL_CONFIG

    # State identifies whether the position currently earns active-range fees.
    range_state: PositionRangeState
    # Entry amounts are the hold benchmark inventory before fees or emissions.
    entry_amounts: TokenAmounts
    # Current amounts are the same liquidity revalued at the current pool price.
    current_amounts: TokenAmounts
    # Entry value is the initial position inventory valued with entry USD evidence.
    entry_value_usd: NonNegativeDecimal
    # Current position value excludes uncollected fees and emissions for conservatism.
    current_position_value_usd: NonNegativeDecimal
    # Hold value prices the entry inventory at current independent USD prices.
    hold_value_usd: NonNegativeDecimal
    # Impermanent-loss fraction is a non-negative cost relative to holding entry inventory.
    impermanent_loss_fraction: NonNegativeDecimal
    # Annualized impermanent loss linearly scales the observed cost over the holding period.
    impermanent_loss_apr: NonNegativeDecimal
    # Token0 value fraction exposes current single-sided or mixed inventory concentration.
    token0_value_fraction: Annotated[Decimal, Field(ge=0, le=1)]
    # Square-root range progress is clamped from zero below to one above the range.
    sqrt_range_progress: Annotated[Decimal, Field(ge=0, le=1)]
    # Pair-price deviation compares pool price with independent current USD valuations.
    valuation_price_deviation_bps: NonNegativeDecimal
    # Diagnostics explain fee activity and the deliberately excluded return components.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class ConcentratedLiquidityAnalyzer:
    """Calculate deterministic Slipstream position amounts and conservative loss estimates."""

    def analyze(self, snapshot: ConcentratedPositionSnapshot) -> ConcentratedPositionAnalysis:
        """Analyze one concentrated-liquidity position without float arithmetic.

        Args:
            snapshot: Coherent range, price, valuation, and holding-period evidence.

        Returns:
            Inventory, range state, valuation, and conservative impermanent-loss evidence.
        """
        with localcontext() as decimal_context:
            # Local precision isolates deterministic square-root math from process-wide settings.
            decimal_context.prec = MATH_PRECISION
            # Entry inventory forms the no-fee, no-emissions hold benchmark.
            entry_amounts = self.amounts_at_price(snapshot, snapshot.entry_price)
            # Current inventory captures range-dependent conversion between the two assets.
            current_amounts = self.amounts_at_price(snapshot, snapshot.current_price)
            # Range state determines whether this position currently participates in swap fees.
            range_state = self.range_state(snapshot, snapshot.current_price)
            # Entry value uses only contemporaneous entry valuation evidence.
            entry_value_usd = (
                entry_amounts.token0 * snapshot.entry_token0_usd
                + entry_amounts.token1 * snapshot.entry_token1_usd
            )
            # Current LP value deliberately excludes fees and AERO emissions.
            current_position_value_usd = (
                current_amounts.token0 * snapshot.current_token0_usd
                + current_amounts.token1 * snapshot.current_token1_usd
            )
            # Hold benchmark keeps the entry token quantities through the current valuation time.
            hold_value_usd = (
                entry_amounts.token0 * snapshot.current_token0_usd
                + entry_amounts.token1 * snapshot.current_token1_usd
            )
            # A zero hold value is impossible under validated positive inputs and liquidity.
            relative_position_value = current_position_value_usd / hold_value_usd
            # Positive outperformance is not treated as negative risk cost.
            impermanent_loss_fraction = max(Decimal(0), Decimal(1) - relative_position_value)
            # Linear annualization is explicit and can exceed one for severe short observations.
            impermanent_loss_apr = (
                impermanent_loss_fraction
                * SECONDS_PER_YEAR
                / Decimal(snapshot.holding_period_seconds)
            )
            # Current asset value split makes range-driven concentration visible.
            token0_value = current_amounts.token0 * snapshot.current_token0_usd
            # Validated positive liquidity guarantees a positive current position value.
            token0_value_fraction = token0_value / current_position_value_usd
            # Square-root progress reflects v3 liquidity geometry rather than linear price space.
            sqrt_range_progress = self._sqrt_range_progress(snapshot, snapshot.current_price)
            # Independent USD prices imply a comparable token1-per-token0 reference price.
            valuation_pair_price = snapshot.current_token0_usd / snapshot.current_token1_usd
            # Absolute deviation is passed onward in the same basis-point unit as risk policy.
            valuation_price_deviation_bps = (
                abs(valuation_pair_price / snapshot.current_price - Decimal(1))
                * BASIS_POINTS_PER_UNIT
            )
            # Out-of-range positions receive an explicit no-active-fees diagnostic.
            activity_diagnostic = (
                "Position is in range and participates in active Slipstream liquidity."
                if range_state is PositionRangeState.IN_RANGE
                else "Position is out of range and does not participate in active liquidity."
            )
            return ConcentratedPositionAnalysis(
                range_state=range_state,
                entry_amounts=entry_amounts,
                current_amounts=current_amounts,
                entry_value_usd=entry_value_usd,
                current_position_value_usd=current_position_value_usd,
                hold_value_usd=hold_value_usd,
                impermanent_loss_fraction=impermanent_loss_fraction,
                impermanent_loss_apr=impermanent_loss_apr,
                token0_value_fraction=token0_value_fraction,
                sqrt_range_progress=sqrt_range_progress,
                valuation_price_deviation_bps=valuation_price_deviation_bps,
                diagnostics=(
                    activity_diagnostic,
                    "Valuation excludes uncollected fees and AERO emissions so they remain "
                    "separate return components.",
                ),
            )

    def amounts_at_price(
        self,
        snapshot: ConcentratedPositionSnapshot,
        price: Decimal,
    ) -> TokenAmounts:
        """Calculate position token amounts at one token1-per-token0 price.

        Args:
            snapshot: Position liquidity and ordered range boundaries.
            price: Positive token1-per-token0 pool price to evaluate.

        Returns:
            Normalized token0 and token1 inventory for the fixed liquidity.
        """
        if price <= 0:
            raise ValueError("price must be positive")
        with localcontext() as decimal_context:
            # Local precision makes this public calculation stable when called independently.
            decimal_context.prec = MATH_PRECISION
            # Square-root prices are the native geometry of v3-style liquidity formulas.
            sqrt_lower = snapshot.lower_price.sqrt()
            # Upper square-root price caps the token0 delta.
            sqrt_upper = snapshot.upper_price.sqrt()
            # Evaluated square-root price selects the below, within, or above-range formula.
            sqrt_price = price.sqrt()

            if price < snapshot.lower_price:
                # Below range uses the complete lower-to-upper amount0 delta.
                token0 = snapshot.liquidity * (sqrt_upper - sqrt_lower) / (sqrt_lower * sqrt_upper)
                # No token1 remains after conversion below the active range.
                token1 = Decimal(0)
            elif price < snapshot.upper_price:
                # In-range token0 covers the current-to-upper square-root-price delta.
                token0 = snapshot.liquidity * (sqrt_upper - sqrt_price) / (sqrt_price * sqrt_upper)
                # In-range token1 covers the lower-to-current square-root-price delta.
                token1 = snapshot.liquidity * (sqrt_price - sqrt_lower)
            else:
                # No token0 remains after conversion above the active range.
                token0 = Decimal(0)
                # Above range uses the complete lower-to-upper amount1 delta.
                token1 = snapshot.liquidity * (sqrt_upper - sqrt_lower)
            return TokenAmounts(token0=token0, token1=token1)

    def range_state(
        self,
        snapshot: ConcentratedPositionSnapshot,
        price: Decimal,
    ) -> PositionRangeState:
        """Classify one price using inclusive-lower and exclusive-upper semantics.

        Args:
            snapshot: Position with validated ordered boundaries.
            price: Token1-per-token0 price to classify.

        Returns:
            Below-range, in-range, or above-range state.
        """
        if price <= 0:
            raise ValueError("price must be positive")
        if price < snapshot.lower_price:
            return PositionRangeState.BELOW_RANGE
        if price < snapshot.upper_price:
            return PositionRangeState.IN_RANGE
        return PositionRangeState.ABOVE_RANGE

    def _sqrt_range_progress(
        self,
        snapshot: ConcentratedPositionSnapshot,
        price: Decimal,
    ) -> Decimal:
        """Calculate clamped square-root-price progress through a range.

        Args:
            snapshot: Position with validated ordered boundaries.
            price: Positive token1-per-token0 price to locate.

        Returns:
            Zero below, one above, or square-root progress within the range.
        """
        # Prices below the lower boundary clamp to zero progress.
        if price <= snapshot.lower_price:
            return Decimal(0)
        # Prices at or above the exclusive upper boundary clamp to full progress.
        if price >= snapshot.upper_price:
            return Decimal(1)
        # Square-root geometry matches the token amount calculations.
        sqrt_lower = snapshot.lower_price.sqrt()
        # Upper root supplies the full range denominator.
        sqrt_upper = snapshot.upper_price.sqrt()
        # Current root supplies progress through the active interval.
        sqrt_price = price.sqrt()
        return (sqrt_price - sqrt_lower) / (sqrt_upper - sqrt_lower)
