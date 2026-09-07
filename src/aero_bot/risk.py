"""Deterministic, fail-closed risk policy evaluation."""

from decimal import Decimal

from aero_bot.domain import (
    CompensationAnalysis,
    DecisionStatus,
    MarketRegime,
    OpportunitySnapshot,
    RiskDecision,
    RiskPolicy,
    RiskReason,
)

# A fixed 365-day divisor keeps annual-to-daily conversion deterministic.
DAYS_PER_YEAR = Decimal(365)


class RiskEngine:
    """Evaluate opportunities using explicit policy gates and decimal arithmetic."""

    def __init__(self, policy: RiskPolicy) -> None:
        """Create an engine around one immutable policy.

        Args:
            policy: Explicit allowlists, thresholds, caps, and emergency state.
        """
        # The immutable policy remains fixed for the lifetime of this evaluator.
        self._policy = policy

    @property
    def policy(self) -> RiskPolicy:
        """Return the immutable policy required to reproduce audited decisions."""
        return self._policy

    def evaluate(self, snapshot: OpportunitySnapshot) -> RiskDecision:
        """Return hold unless every configured safety and opportunity gate passes.

        Args:
            snapshot: Coherent market, oracle, pool, and portfolio observations.

        Returns:
            A deterministic decision with ordered reason codes and yield calculations.
        """
        # Additive compensation credits both streams Aerodrome pays the same staked position.
        compensation = self._analyze_compensation(snapshot)
        # Net APR accounts for both LP divergence and informed-flow costs.
        net_apr = (
            compensation.total_compensation_apr
            - snapshot.impermanent_loss_apr
            - snapshot.adverse_selection_apr
        )
        # Daily rate is compared with the opportunity threshold without implying a promise.
        net_daily_rate = net_apr / DAYS_PER_YEAR
        # Reasons accumulate in fixed order for reproducible output from identical inputs.
        reasons: list[RiskReason] = []

        if self._policy.emergency_halt:
            reasons.append(RiskReason.EMERGENCY_HALT)
        if snapshot.token_address not in self._policy.allowed_token_addresses:
            reasons.append(RiskReason.TOKEN_NOT_ALLOWLISTED)
        if snapshot.pool_address not in self._policy.allowed_pool_addresses:
            reasons.append(RiskReason.POOL_NOT_ALLOWLISTED)
        if snapshot.token_paused:
            reasons.append(RiskReason.TOKEN_PAUSED)
        if snapshot.market_regime is not MarketRegime.MARKET_OPEN:
            reasons.append(RiskReason.MARKET_CLOSED)
        if not snapshot.oracle_healthy:
            reasons.append(RiskReason.ORACLE_UNHEALTHY)
        if snapshot.oracle_age_seconds > self._policy.max_oracle_age_seconds:
            reasons.append(RiskReason.ORACLE_STALE)
        if snapshot.oracle_deviation_bps > self._policy.max_oracle_deviation_bps:
            reasons.append(RiskReason.ORACLE_DEVIATION)
        if snapshot.pool_tvl_usd < self._policy.min_pool_tvl_usd:
            reasons.append(RiskReason.INSUFFICIENT_LIQUIDITY)
        if snapshot.exit_depth_usd < self._policy.min_exit_depth_usd:
            reasons.append(RiskReason.INSUFFICIENT_EXIT_DEPTH)
        if snapshot.proposed_capital_usd > self._policy.max_capital_usd:
            reasons.append(RiskReason.CAPITAL_CAP_EXCEEDED)
        if snapshot.realized_daily_loss_usd >= self._policy.max_realized_daily_loss_usd:
            reasons.append(RiskReason.DAILY_LOSS_STOP)
        if snapshot.adverse_selection_apr > self._policy.max_adverse_selection_apr:
            reasons.append(RiskReason.ADVERSE_SELECTION_EXCESSIVE)
        if net_daily_rate < self._policy.daily_opportunity_threshold:
            reasons.append(RiskReason.OPPORTUNITY_BELOW_THRESHOLD)

        # Any failed gate produces a first-class hold decision.
        status = DecisionStatus.HOLD if reasons else DecisionStatus.ELIGIBLE
        return RiskDecision(
            status=status,
            reasons=tuple(reasons),
            compensation=compensation,
            net_apr=net_apr,
            net_daily_rate=net_daily_rate,
        )

    def _analyze_compensation(self, snapshot: OpportunitySnapshot) -> CompensationAnalysis:
        """Add retained fees and haircut emissions into one additive return estimate.

        Args:
            snapshot: Opportunity containing observed fee and emission APR evidence.

        Returns:
            Deterministic retained, discounted, and total compensation components.
        """
        # Retained fees account for the observed protocol fee share on swap revenue.
        retained_fee_apr = snapshot.fee_apr * snapshot.fee_retention_fraction
        # Discounted emissions prevent volatile AERO rewards dominating the total.
        adjusted_emissions_apr = snapshot.emissions_apr * self._policy.emissions_reward_haircut
        # Aerodrome pays swap fees and AERO emissions to the same staked in-range
        # position, so the two streams are additive rather than alternatives.
        total_compensation_apr = retained_fee_apr + adjusted_emissions_apr
        return CompensationAnalysis(
            retained_fee_apr=retained_fee_apr,
            adjusted_emissions_apr=adjusted_emissions_apr,
            total_compensation_apr=total_compensation_apr,
        )
