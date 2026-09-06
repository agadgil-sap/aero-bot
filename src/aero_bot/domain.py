"""Typed, immutable domain models for deterministic LP risk decisions."""

import re
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# A Base address is represented as a 20-byte hexadecimal EVM address.
EVM_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Immutable models prevent inputs from changing after a decision is calculated.
IMMUTABLE_MODEL_CONFIG = ConfigDict(frozen=True, extra="forbid")
# Financial amounts and rates must never be negative at the input boundary.
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
# Haircut fractions remain within the closed interval from zero to one.
UnitDecimal = Annotated[Decimal, Field(ge=0, le=1)]


def normalize_evm_address(value: str) -> str:
    """Validate and normalize an EVM address for case-insensitive allowlist matching.

    Args:
        value: Candidate contract address supplied by a trusted adapter or fixture.

    Returns:
        The validated address in lowercase hexadecimal form.

    Raises:
        ValueError: If the value is not a complete 20-byte hexadecimal address.
    """
    if EVM_ADDRESS_PATTERN.fullmatch(value) is None:
        raise ValueError("address must contain 0x followed by 40 hexadecimal characters")
    return value.lower()


# Validated addresses eliminate ambiguous or malformed allowlist comparisons.
EvmAddress = Annotated[str, AfterValidator(normalize_evm_address)]


class MarketRegime(StrEnum):
    """Identify the trading-hours regime affecting tokenized equity risk."""

    # Market-open observations may proceed through the remaining policy gates.
    MARKET_OPEN = "market_open"
    # Overnight observations are held while underlying equity markets are closed.
    OVERNIGHT = "overnight"
    # Weekend observations are held through the longest routine closure window.
    WEEKEND = "weekend"


class DecisionStatus(StrEnum):
    """Represent the only outcomes available to the deterministic policy engine."""

    # Hold means no liquidity action is eligible and capital remains in USDC.
    HOLD = "hold"
    # Eligible means every configured gate passed, not that execution is required.
    ELIGIBLE = "eligible"


class RiskReason(StrEnum):
    """Provide stable, machine-readable evidence for a hold decision."""

    # A manual or automatic emergency halt blocks every opportunity.
    EMERGENCY_HALT = "emergency_halt"
    # A token outside the explicit contract allowlist is never considered.
    TOKEN_NOT_ALLOWLISTED = "token_not_allowlisted"  # noqa: S105
    # A pool outside the explicit contract allowlist is never considered.
    POOL_NOT_ALLOWLISTED = "pool_not_allowlisted"
    # A paused B20 token cannot enter a new LP position.
    TOKEN_PAUSED = "token_paused"  # noqa: S105
    # A non-market-open regime has unacceptable reference-market risk.
    MARKET_CLOSED = "market_closed"
    # An unhealthy oracle cannot support a valuation-sensitive decision.
    ORACLE_UNHEALTHY = "oracle_unhealthy"
    # A stale oracle observation cannot support a current decision.
    ORACLE_STALE = "oracle_stale"
    # Excessive pool-to-oracle deviation indicates dislocation or manipulation risk.
    ORACLE_DEVIATION = "oracle_deviation"
    # Pool TVL below policy cannot support a sufficiently robust position.
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    # Available exit depth below policy creates unacceptable unwind risk.
    INSUFFICIENT_EXIT_DEPTH = "insufficient_exit_depth"
    # Proposed capital above policy prevents accidental concentration.
    CAPITAL_CAP_EXCEEDED = "capital_cap_exceeded"
    # Realized daily loss at the stop level blocks additional exposure.
    DAILY_LOSS_STOP = "daily_loss_stop"
    # Estimated adverse selection above policy overwhelms quoted headline yield.
    ADVERSE_SELECTION_EXCESSIVE = "adverse_selection_excessive"
    # Conservative net yield below the opportunity threshold is not eligible.
    OPPORTUNITY_BELOW_THRESHOLD = "opportunity_below_threshold"


class RiskPolicy(BaseModel):
    """Define explicit, immutable gates for first-release eligibility decisions."""

    # Frozen strict fields make a policy instance stable for one decision and audit record.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Only contracts in this token set may be evaluated.
    allowed_token_addresses: frozenset[EvmAddress] = Field(default_factory=frozenset)
    # Only contracts in this Aerodrome pool set may be evaluated.
    allowed_pool_addresses: frozenset[EvmAddress] = Field(default_factory=frozenset)
    # Emergency halt defaults on so an unconfigured application fails closed.
    emergency_halt: bool = True
    # The maximum acceptable Chainlink observation age is one hour.
    max_oracle_age_seconds: Annotated[int, Field(ge=0)] = 3_600
    # The maximum pool-to-oracle price deviation is one percent.
    max_oracle_deviation_bps: NonNegativeDecimal = Decimal("100")
    # The minimum pool TVL is one million US dollars.
    min_pool_tvl_usd: NonNegativeDecimal = Decimal("1000000")
    # The minimum executable exit depth is twenty-five thousand US dollars.
    min_exit_depth_usd: NonNegativeDecimal = Decimal("25000")
    # A fifty-percent reward haircut discounts volatile AERO emissions.
    emissions_reward_haircut: UnitDecimal = Decimal("0.50")
    # Adverse-selection cost above twenty-five percent annualized is rejected.
    max_adverse_selection_apr: NonNegativeDecimal = Decimal("0.25")
    # Proposed first-release exposure is capped at ten thousand US dollars.
    max_capital_usd: NonNegativeDecimal = Decimal("10000")
    # A five-hundred-dollar realized daily loss stops new exposure.
    max_realized_daily_loss_usd: NonNegativeDecimal = Decimal("500")
    # One percent daily is an opportunity threshold, never a target or promise.
    daily_opportunity_threshold: NonNegativeDecimal = Decimal("0.01")


class OpportunitySnapshot(BaseModel):
    """Capture all evidence required for a deterministic first-release decision."""

    # Frozen strict fields ensure evaluation uses one coherent observation set.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The B20 token contract is matched against the explicit token allowlist.
    token_address: EvmAddress
    # The Aerodrome pool contract is matched against the explicit pool allowlist.
    pool_address: EvmAddress
    # The B20 pause flag prevents entry while transfers or redemptions are constrained.
    token_paused: bool
    # The market regime distinguishes open trading from overnight and weekend risk.
    market_regime: MarketRegime
    # Oracle health combines adapter-level call and round-integrity checks.
    oracle_healthy: bool
    # Oracle age measures seconds since the last complete price observation.
    oracle_age_seconds: Annotated[int, Field(ge=0)]
    # Oracle deviation measures the absolute pool-price difference in basis points.
    oracle_deviation_bps: NonNegativeDecimal
    # Pool TVL measures total supplied value in US dollars.
    pool_tvl_usd: NonNegativeDecimal
    # Exit depth estimates executable US-dollar value within the configured slippage bound.
    exit_depth_usd: NonNegativeDecimal
    # Fee APR annualizes recently observed swap-fee revenue as a decimal rate.
    fee_apr: NonNegativeDecimal
    # Emissions APR annualizes AERO rewards before the conservative reward haircut.
    emissions_apr: NonNegativeDecimal
    # Impermanent-loss APR is a conservative annualized position cost estimate.
    impermanent_loss_apr: NonNegativeDecimal
    # Adverse-selection APR estimates annualized loss to better-informed flow.
    adverse_selection_apr: NonNegativeDecimal
    # Proposed capital is the USDC value considered for this opportunity.
    proposed_capital_usd: NonNegativeDecimal
    # Realized daily loss tracks policy consumption before this decision.
    realized_daily_loss_usd: NonNegativeDecimal


class RiskDecision(BaseModel):
    """Return a reproducible outcome with calculations and ordered evidence."""

    # Frozen strict fields preserve the engine result for later immutable auditing.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status is either hold or eligible, with no implicit trade instruction.
    status: DecisionStatus
    # Reasons are ordered by stable policy-gate order for reproducible diagnostics.
    reasons: tuple[RiskReason, ...]
    # Gross APR combines fee revenue and conservatively discounted emissions.
    gross_apr: Decimal
    # Adjusted emissions APR exposes the reward haircut calculation.
    adjusted_emissions_apr: Decimal
    # Net APR subtracts impermanent-loss and adverse-selection estimates.
    net_apr: Decimal
    # Net daily rate converts the conservative annualized estimate for threshold comparison.
    net_daily_rate: Decimal
