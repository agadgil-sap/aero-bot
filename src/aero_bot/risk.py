"""Deterministic, fail-closed risk policy evaluation."""

from decimal import Decimal

from aero_bot.domain import (
    CompensationAnalysis,
    CompensationMode,
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

    def evaluate(self, snapshot: OpportunitySnapshot) -> RiskDecision:
        """Return hold unless every configured safety and opportunity gate passes.

        Args:
            snapshot: Coherent market, oracle, pool, and portfolio observations.

        Returns:
            A deterministic decision with ordered reason codes and yield calculations.
        """
        # Compensation comparison prevents mutually exclusive fee and emission returns being added.
        compensation = self._analyze_compensation(snapshot)
        # Net APR accounts for both LP divergence and informed-flow costs.
        net_apr = (
            compensation.selected_apr
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
        """Compare adjusted fees and emissions without combining exclusive streams.

        Args:
            snapshot: Opportunity containing mode and observed headline APR values.

        Returns:
            Deterministic selected, alternative, preferred, and opportunity-cost evidence.
        """
        # Retained fees account for the observed protocol share on unstaked liquidity.
        retained_fee_apr = snapshot.fee_apr * snapshot.fee_retention_fraction
        # Discounted emissions prevent volatile AERO rewards dominating the comparison.
        adjusted_emissions_apr = snapshot.emissions_apr * self._policy.emissions_reward_haircut
        if snapshot.compensation_mode is CompensationMode.UNSTAKED_FEES:
            # Unstaked positions earn retained fees and forgo AERO emissions.
            selected_apr = retained_fee_apr
            # Discounted emissions remain visible only as the alternative.
            alternative_apr = adjusted_emissions_apr
        else:
            # Staked positions earn discounted AERO emissions and relinquish swap fees.
            selected_apr = adjusted_emissions_apr
            # Retained fee yield remains visible only as the alternative.
            alternative_apr = retained_fee_apr
        if retained_fee_apr > adjusted_emissions_apr:
            # Strictly higher retained fees make unstaked compensation preferable.
            preferred_mode = CompensationMode.UNSTAKED_FEES
        elif adjusted_emissions_apr > retained_fee_apr:
            # Strictly higher adjusted emissions make gauge staking preferable.
            preferred_mode = CompensationMode.STAKED_EMISSIONS
        else:
            # A tie preserves the selected mode rather than implying needless churn.
            preferred_mode = snapshot.compensation_mode
        # Opportunity cost is zero when the selected mode is at least as valuable.
        opportunity_cost_apr = max(Decimal(0), alternative_apr - selected_apr)
        return CompensationAnalysis(
            selected_mode=snapshot.compensation_mode,
            retained_fee_apr=retained_fee_apr,
            adjusted_emissions_apr=adjusted_emissions_apr,
            selected_apr=selected_apr,
            alternative_apr=alternative_apr,
            preferred_mode=preferred_mode,
            opportunity_cost_apr=opportunity_cost_apr,
        )
