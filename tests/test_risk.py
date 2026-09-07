"""Behavior tests for deterministic fail-closed risk decisions."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from aero_bot.domain import (
    DecisionStatus,
    MarketRegime,
    OpportunitySnapshot,
    RiskPolicy,
    RiskReason,
)
from aero_bot.risk import RiskEngine

# A deterministic fixture address represents an allowlisted B20 contract without official claims.
TOKEN_ADDRESS = "0x1111111111111111111111111111111111111111"
# A deterministic fixture address represents an allowlisted Aerodrome pool without official claims.
POOL_ADDRESS = "0x2222222222222222222222222222222222222222"


def eligible_policy() -> RiskPolicy:
    """Build a policy with the fixture contracts explicitly enabled."""
    return RiskPolicy(
        allowed_token_addresses=frozenset({TOKEN_ADDRESS}),
        allowed_pool_addresses=frozenset({POOL_ADDRESS}),
        emergency_halt=False,
    )


def eligible_snapshot(**overrides: object) -> OpportunitySnapshot:
    """Build a high-yield fixture and apply explicit per-test observations.

    Args:
        **overrides: Snapshot fields changed to exercise a particular policy gate.

    Returns:
        A validated immutable opportunity snapshot.
    """
    # Base values pass each default policy gate with conservative yield costs included.
    values: dict[str, object] = {
        "token_address": TOKEN_ADDRESS,
        "pool_address": POOL_ADDRESS,
        "token_paused": False,
        "market_regime": MarketRegime.MARKET_OPEN,
        "oracle_healthy": True,
        "oracle_age_seconds": 60,
        "oracle_deviation_bps": Decimal("25"),
        "pool_tvl_usd": Decimal("2000000"),
        "exit_depth_usd": Decimal("50000"),
        "fee_apr": Decimal("4.50"),
        "fee_retention_fraction": Decimal("0.90"),
        "emissions_apr": Decimal("8.00"),
        "impermanent_loss_apr": Decimal("0.20"),
        "adverse_selection_apr": Decimal("0.15"),
        "proposed_capital_usd": Decimal("5000"),
        "realized_daily_loss_usd": Decimal("0"),
    }
    values.update(overrides)
    return OpportunitySnapshot.model_validate(values)


def test_engine_marks_opportunity_eligible_only_after_every_gate_passes() -> None:
    """A fully allowlisted and healthy opportunity can become eligible without requiring action."""
    # The engine uses the explicit test policy rather than hidden global settings.
    engine = RiskEngine(eligible_policy())
    # The result is produced entirely from immutable deterministic input.
    decision = engine.evaluate(eligible_snapshot())

    assert decision.status is DecisionStatus.ELIGIBLE
    assert decision.reasons == ()
    assert decision.compensation.retained_fee_apr == Decimal("4.0500")
    assert decision.compensation.adjusted_emissions_apr == Decimal("4.000")
    assert decision.compensation.total_compensation_apr == Decimal("8.0500")
    assert decision.net_apr == Decimal("7.7000")
    assert decision.net_daily_rate == Decimal("7.7000") / Decimal(365)


def test_default_policy_fails_closed() -> None:
    """An unconfigured policy halts and rejects contracts even when market data looks attractive."""
    # The default policy intentionally has no trusted contract addresses.
    engine = RiskEngine(RiskPolicy())
    # The decision proves configuration omissions result in hold rather than permissive behavior.
    decision = engine.evaluate(eligible_snapshot())

    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons == (
        RiskReason.EMERGENCY_HALT,
        RiskReason.TOKEN_NOT_ALLOWLISTED,
        RiskReason.POOL_NOT_ALLOWLISTED,
    )


def test_engine_reports_all_failed_gates_in_stable_order() -> None:
    """One evaluation exposes every evidence-backed hold reason in deterministic order."""
    # The policy trusts only the original fixture addresses while retaining normal thresholds.
    policy = eligible_policy()
    # The alternate addresses are valid contracts but intentionally absent from both allowlists.
    unsafe_snapshot = eligible_snapshot(
        token_address="0x3333333333333333333333333333333333333333",
        pool_address="0x4444444444444444444444444444444444444444",
        token_paused=True,
        market_regime=MarketRegime.WEEKEND,
        oracle_healthy=False,
        oracle_age_seconds=3_601,
        oracle_deviation_bps=Decimal("101"),
        pool_tvl_usd=Decimal("999999"),
        exit_depth_usd=Decimal("24999"),
        adverse_selection_apr=Decimal("0.26"),
        proposed_capital_usd=Decimal("10001"),
        realized_daily_loss_usd=Decimal("500"),
        fee_apr=Decimal("0"),
        emissions_apr=Decimal("0"),
        impermanent_loss_apr=Decimal("0"),
    )

    # The decision preserves enough evidence to diagnose every rejection at once.
    decision = RiskEngine(policy).evaluate(unsafe_snapshot)

    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons == (
        RiskReason.TOKEN_NOT_ALLOWLISTED,
        RiskReason.POOL_NOT_ALLOWLISTED,
        RiskReason.TOKEN_PAUSED,
        RiskReason.MARKET_CLOSED,
        RiskReason.ORACLE_UNHEALTHY,
        RiskReason.ORACLE_STALE,
        RiskReason.ORACLE_DEVIATION,
        RiskReason.INSUFFICIENT_LIQUIDITY,
        RiskReason.INSUFFICIENT_EXIT_DEPTH,
        RiskReason.CAPITAL_CAP_EXCEEDED,
        RiskReason.DAILY_LOSS_STOP,
        RiskReason.ADVERSE_SELECTION_EXCESSIVE,
        RiskReason.OPPORTUNITY_BELOW_THRESHOLD,
    )


@pytest.mark.parametrize("regime", [MarketRegime.OVERNIGHT, MarketRegime.WEEKEND])
def test_closed_market_regimes_force_hold(regime: MarketRegime) -> None:
    """Overnight and weekend observations cannot become eligible on headline yield alone."""
    # The closed-market regime is the sole failing observation in this scenario.
    snapshot = eligible_snapshot(market_regime=regime)
    # The decision demonstrates explicit market-hours treatment.
    decision = RiskEngine(eligible_policy()).evaluate(snapshot)

    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons == (RiskReason.MARKET_CLOSED,)


def test_emissions_haircut_can_move_headline_yield_below_threshold() -> None:
    """Quoted AERO rewards are discounted before the one-percent opportunity comparison."""
    # Headline emissions appear sufficient before the configured fifty-percent haircut.
    snapshot = eligible_snapshot(
        fee_apr=Decimal("0"),
        emissions_apr=Decimal("7.20"),
        impermanent_loss_apr=Decimal("0"),
        adverse_selection_apr=Decimal("0"),
    )
    # The decision exposes both adjusted reward yield and the resulting hold.
    decision = RiskEngine(eligible_policy()).evaluate(snapshot)

    assert decision.compensation.adjusted_emissions_apr == Decimal("3.600")
    assert decision.compensation.total_compensation_apr == Decimal("3.600")
    assert decision.net_daily_rate < Decimal("0.01")
    assert decision.status is DecisionStatus.HOLD
    assert decision.reasons == (RiskReason.OPPORTUNITY_BELOW_THRESHOLD,)


def test_threshold_boundary_is_eligible() -> None:
    """A conservative daily rate exactly at the threshold passes the opportunity gate."""
    # An annual fee rate of 3.65 converts exactly to the configured one percent daily threshold.
    snapshot = eligible_snapshot(
        fee_apr=Decimal("3.65"),
        fee_retention_fraction=Decimal("1"),
        emissions_apr=Decimal("0"),
        impermanent_loss_apr=Decimal("0"),
        adverse_selection_apr=Decimal("0"),
    )
    # The result confirms equality is sufficient at the configured threshold boundary.
    decision = RiskEngine(eligible_policy()).evaluate(snapshot)

    assert decision.net_daily_rate == Decimal("0.01")
    assert decision.status is DecisionStatus.ELIGIBLE


def test_staked_position_adds_fees_and_emissions_streams() -> None:
    """A staked in-range position earns both retained swap fees and discounted AERO."""
    # Streams are sized so a max-based rule would credit only the dominant emission side.
    snapshot = eligible_snapshot(
        fee_apr=Decimal("4"),
        fee_retention_fraction=Decimal("0.90"),
        emissions_apr=Decimal("20"),
        impermanent_loss_apr=Decimal("0"),
        adverse_selection_apr=Decimal("0"),
    )
    # Total compensation is the sum, not the larger, of the two adjusted streams.
    decision = RiskEngine(eligible_policy()).evaluate(snapshot)

    assert decision.compensation.retained_fee_apr == Decimal("3.60")
    assert decision.compensation.adjusted_emissions_apr == Decimal("10.0")
    assert decision.compensation.total_compensation_apr == Decimal("13.60")
    assert decision.net_apr == Decimal("13.60")
    assert decision.status is DecisionStatus.ELIGIBLE


def test_fee_dominant_position_still_adds_discounted_emissions() -> None:
    """Large swap-fee yield still receives the discounted AERO stream on top."""
    # Fees dominate the inputs while adjusted emissions remain a visible contribution.
    snapshot = eligible_snapshot(
        fee_apr=Decimal("100"),
        fee_retention_fraction=Decimal("1"),
        emissions_apr=Decimal("8"),
        impermanent_loss_apr=Decimal("0"),
        adverse_selection_apr=Decimal("0"),
    )
    # Total compensation credits 104, not the fee-only value of 100.
    decision = RiskEngine(eligible_policy()).evaluate(snapshot)

    assert decision.compensation.retained_fee_apr == Decimal("100")
    assert decision.compensation.adjusted_emissions_apr == Decimal("4.0")
    assert decision.compensation.total_compensation_apr == Decimal("104.0")
    assert decision.net_apr == Decimal("104.0")
    assert decision.status is DecisionStatus.ELIGIBLE


@pytest.mark.parametrize(
    "address",
    ["", "0x1234", "1111111111111111111111111111111111111111", "0x" + ("z" * 40)],
)
def test_domain_rejects_malformed_contract_addresses(address: str) -> None:
    """Malformed contract identifiers cannot enter policy evaluation or allowlists."""
    with pytest.raises(ValidationError, match="40 hexadecimal"):
        eligible_snapshot(token_address=address)


def test_contract_addresses_are_matched_without_case_ambiguity() -> None:
    """Checksum capitalization differences cannot bypass or break allowlist matching."""
    # The mixed-case input represents a checksummed-style spelling of the fixture address.
    mixed_case_token = "0x111111111111111111111111111111111111111A"
    # The policy uses the normalized form of the same valid hexadecimal address.
    normalized_policy = RiskPolicy(
        allowed_token_addresses=frozenset({mixed_case_token.lower()}),
        allowed_pool_addresses=frozenset({POOL_ADDRESS}),
        emergency_halt=False,
    )
    # The snapshot preserves the mixed-case spelling at its validation boundary.
    snapshot = eligible_snapshot(token_address=mixed_case_token)

    assert RiskEngine(normalized_policy).evaluate(snapshot).status is DecisionStatus.ELIGIBLE
