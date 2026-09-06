"""Behavior tests for deterministic Chainlink B20 health evaluation."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aero_bot.domain import MarketRegime
from aero_bot.oracles import (
    ChainlinkFeedObservation,
    ChainlinkHealthEvaluator,
    OracleHealthPolicy,
    OracleHealthStatus,
    SequencerObservation,
    unavailable_chainlink_coverage,
)

# A fixture token matches one verified official B20 contract without claiming live price data.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
# A fixture address represents a proxy-shaped identity without making a deployment claim.
FEED_ADDRESS = "0x1111111111111111111111111111111111111111"
# A fixture address represents a sequencer-feed-shaped identity without a deployment claim.
SEQUENCER_ADDRESS = "0x2222222222222222222222222222222222222222"
# Fixed observation time makes every freshness boundary deterministic.
OBSERVED_AT = datetime(2026, 9, 6, 10, 30, tzinfo=UTC)


def feed_observation(**overrides: object) -> ChainlinkFeedObservation:
    """Build a valid open-market B20 feed observation.

    Args:
        **overrides: Fields changed to exercise one health gate.

    Returns:
        A validated immutable proxy and issuer observation.
    """
    # Baseline evidence represents a recent positive eight-decimal answer.
    values: dict[str, object] = {
        "symbol": "AAPLx",
        "token_address": B20_ADDRESS,
        "feed_address": FEED_ADDRESS,
        "description": "AAPLx Total Return Value",
        "decimals": 8,
        "round_id": 12,
        "answer": 23_456_789_012,
        "started_at": OBSERVED_AT - timedelta(minutes=6),
        "updated_at": OBSERVED_AT - timedelta(minutes=5),
        "answered_in_round": 1,
        "observed_at": OBSERVED_AT,
        "issuer_paused": False,
        "issuer_multiplier_wad": 2_500_000_000_000_000_000,
    }
    values.update(overrides)
    return ChainlinkFeedObservation.model_validate(values)


def sequencer_observation(**overrides: object) -> SequencerObservation:
    """Build a valid Base sequencer observation outside the recovery grace period.

    Args:
        **overrides: Fields changed to exercise one sequencer gate.

    Returns:
        A validated immutable sequencer observation.
    """
    # Baseline evidence reports the sequencer up for more than one hour.
    values: dict[str, object] = {
        "feed_address": SEQUENCER_ADDRESS,
        "answer": 0,
        "started_at": OBSERVED_AT - timedelta(hours=2),
        "observed_at": OBSERVED_AT,
    }
    values.update(overrides)
    return SequencerObservation.model_validate(values)


def test_valid_open_market_observation_is_healthy() -> None:
    """A recent positive round outside sequencer recovery passes every health gate."""
    # The default policy supplies the production freshness and recovery thresholds.
    result = ChainlinkHealthEvaluator().evaluate(
        feed_observation(), sequencer_observation(), MarketRegime.MARKET_OPEN
    )

    assert result.status is OracleHealthStatus.HEALTHY
    assert result.healthy is True
    assert result.total_return_value == Decimal("234.56789012")
    assert result.issuer_multiplier == Decimal("2.5")
    assert result.age_seconds == 300


@pytest.mark.parametrize(
    ("feed_overrides", "sequencer_overrides", "expected_status"),
    [
        ({"issuer_paused": True}, {}, OracleHealthStatus.ISSUER_PAUSED),
        ({"answer": 0}, {}, OracleHealthStatus.INVALID_FEED),
        ({"round_id": 0}, {}, OracleHealthStatus.INVALID_FEED),
        ({"issuer_multiplier_wad": 0}, {}, OracleHealthStatus.INVALID_FEED),
        ({}, {"answer": 1}, OracleHealthStatus.SEQUENCER_DOWN),
        (
            {},
            {"observed_at": OBSERVED_AT - timedelta(minutes=1)},
            OracleHealthStatus.INVALID_FEED,
        ),
        (
            {},
            {"started_at": OBSERVED_AT - timedelta(minutes=30)},
            OracleHealthStatus.SEQUENCER_GRACE_PERIOD,
        ),
    ],
)
def test_safety_faults_fail_closed(
    feed_overrides: dict[str, object],
    sequencer_overrides: dict[str, object],
    expected_status: OracleHealthStatus,
) -> None:
    """Feed, issuer, and sequencer faults never produce healthy output.

    Args:
        feed_overrides: Feed evidence modified for the selected gate.
        sequencer_overrides: Sequencer evidence modified for the selected gate.
        expected_status: Exact fail-closed outcome required by policy order.
    """
    # Each table row changes only the evidence needed to reach its expected gate.
    result = ChainlinkHealthEvaluator().evaluate(
        feed_observation(**feed_overrides),
        sequencer_observation(**sequencer_overrides),
        MarketRegime.MARKET_OPEN,
    )

    assert result.status is expected_status
    assert result.healthy is False


def test_open_market_staleness_uses_strict_policy_boundary() -> None:
    """An update one second beyond the limit is stale while the limit itself passes."""
    # A short explicit policy makes both sides of the age comparison easy to inspect.
    evaluator = ChainlinkHealthEvaluator(OracleHealthPolicy(max_open_age_seconds=300))
    # Exact-boundary evidence should remain acceptable under the greater-than rule.
    boundary = evaluator.evaluate(
        feed_observation(updated_at=OBSERVED_AT - timedelta(seconds=300)),
        sequencer_observation(),
        MarketRegime.MARKET_OPEN,
    )
    # One additional second crosses the configured freshness limit.
    stale = evaluator.evaluate(
        feed_observation(updated_at=OBSERVED_AT - timedelta(seconds=301)),
        sequencer_observation(),
        MarketRegime.MARKET_OPEN,
    )

    assert boundary.status is OracleHealthStatus.HEALTHY
    assert stale.status is OracleHealthStatus.STALE
    assert stale.age_seconds == 301


def test_future_feed_timestamp_is_rejected() -> None:
    """An updatedAt beyond permitted clock skew cannot become a zero-age observation."""
    # The update and round start remain consistently ordered but exceed clock-skew policy.
    result = ChainlinkHealthEvaluator().evaluate(
        feed_observation(
            started_at=OBSERVED_AT + timedelta(seconds=31),
            updated_at=OBSERVED_AT + timedelta(seconds=31),
        ),
        sequencer_observation(),
        MarketRegime.MARKET_OPEN,
    )

    assert result.status is OracleHealthStatus.FUTURE_TIMESTAMP
    assert result.healthy is False
    assert result.age_seconds is None


@pytest.mark.parametrize("market_regime", [MarketRegime.OVERNIGHT, MarketRegime.WEEKEND])
def test_closed_market_value_is_not_mislabeled_stale(market_regime: MarketRegime) -> None:
    """Expected held-last-value behavior receives a closed-market outcome.

    Args:
        market_regime: A non-open reference-equity trading regime.
    """
    # Chainlink documents that these feeds hold their last value outside supported sessions.
    result = ChainlinkHealthEvaluator().evaluate(
        feed_observation(
            started_at=OBSERVED_AT - timedelta(days=2, minutes=1),
            updated_at=OBSERVED_AT - timedelta(days=2),
        ),
        sequencer_observation(),
        market_regime,
    )

    assert result.status is OracleHealthStatus.MARKET_CLOSED
    assert result.healthy is False


def test_deprecated_answered_in_round_does_not_override_current_round_health() -> None:
    """Deprecated answeredInRound evidence is retained without acting as a gate."""
    # A lower answered-in-round value proves the deprecated field is not compared to round ID.
    result = ChainlinkHealthEvaluator().evaluate(
        feed_observation(answered_in_round=1, round_id=999),
        sequencer_observation(),
        MarketRegime.MARKET_OPEN,
    )

    assert result.status is OracleHealthStatus.HEALTHY


def test_unconfigured_coverage_makes_no_proxy_or_health_claims() -> None:
    """Missing reviewed proxy evidence returns an explicit empty unavailable report."""
    # Ten expected assets match the current verified official B20 registry snapshot.
    report = unavailable_chainlink_coverage(expected_assets=10)

    assert report.status.value == "unavailable"
    assert report.expected_assets == 10
    assert report.configured_feeds == 0
    assert report.healthy_feeds == 0
    assert report.assessments == ()
    assert "no reviewed proxy-address snapshot" in report.diagnostics[0]
