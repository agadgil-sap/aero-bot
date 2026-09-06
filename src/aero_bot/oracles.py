"""Deterministic Chainlink health evaluation for Coinbase B20 feeds on Base."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, MarketRegime

# Chainlink's product guide documents the Coinbase B20 total-return feed semantics.
CHAINLINK_B20_DOCS_URL: Literal[
    "https://docs.chain.link/data-feeds/tokenized-equity-feeds/coinbase"
] = "https://docs.chain.link/data-feeds/tokenized-equity-feeds/coinbase"
# Chainlink's API guide documents latestRoundData fields and proxy consumption.
CHAINLINK_FEED_API_URL: Literal["https://docs.chain.link/data-feeds/api-reference"] = (
    "https://docs.chain.link/data-feeds/api-reference"
)
# WAD scaling converts the issuer's 18-decimal redemption multiplier into a decimal value.
WAD_SCALE = Decimal(10**18)


class OracleHealthStatus(StrEnum):
    """Describe one deterministic Chainlink feed-health outcome."""

    # Healthy means every required open-market safety check passed.
    HEALTHY = "healthy"
    # Market closed means the conservative policy does not require a fresh trading observation.
    MARKET_CLOSED = "market_closed"
    # Issuer paused means Coinbase has stopped advancing the redemption multiplier.
    ISSUER_PAUSED = "issuer_paused"
    # Sequencer down means Base ordering is unavailable and L2 feed reads are unsafe to use.
    SEQUENCER_DOWN = "sequencer_down"
    # Sequencer grace period prevents immediate use after Base ordering resumes.
    SEQUENCER_GRACE_PERIOD = "sequencer_grace_period"
    # Invalid feed covers malformed round identity, value, multiplier, or timestamp ordering.
    INVALID_FEED = "invalid_feed"
    # Future timestamp detects a feed update outside the permitted local-clock skew.
    FUTURE_TIMESTAMP = "future_timestamp"
    # Stale means an otherwise valid market-open observation exceeded the age policy.
    STALE = "stale"
    # Unavailable means no verified proxy snapshot or read-only observation path exists.
    UNAVAILABLE = "unavailable"


class OracleCoverageStatus(StrEnum):
    """Describe whether the configured B20 oracle set is safe to consume."""

    # Verified means every configured feed was evaluated from a coherent observation set.
    VERIFIED = "verified"
    # Unavailable means the application cannot make any live feed-health claims.
    UNAVAILABLE = "unavailable"


class OracleHealthPolicy(BaseModel):
    """Define immutable thresholds for open-market feed and sequencer evaluation."""

    # Frozen strict fields make one policy stable for every assessment it produces.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Open-market observations older than one hour are rejected as stale.
    max_open_age_seconds: Annotated[int, Field(gt=0)] = 3_600
    # Thirty seconds accommodates small clock differences without accepting future data.
    max_future_skew_seconds: Annotated[int, Field(ge=0)] = 30
    # Feed and sequencer reads farther than 30 seconds apart are not one coherent snapshot.
    max_observation_skew_seconds: Annotated[int, Field(ge=0)] = 30
    # One hour after sequencer recovery limits unsafe use of temporarily stale L2 state.
    sequencer_grace_period_seconds: Annotated[int, Field(ge=0)] = 3_600


class ChainlinkFeedObservation(BaseModel):
    """Capture one B20 proxy round plus its Coinbase registry evidence."""

    # Frozen strict fields preserve the exact evidence used by the health evaluator.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Symbol links this observation to an official Coinbase B20 listing.
    symbol: str
    # Token address provides contract-level identity rather than ticker-only matching.
    token_address: EvmAddress
    # Feed address identifies the Chainlink proxy queried on Base.
    feed_address: EvmAddress
    # Description is retained from the proxy for operator review.
    description: str
    # Decimals defines the fixed-point scale used by the proxy answer.
    decimals: Annotated[int, Field(ge=0, le=36)]
    # Round ID is allowed through as zero so the evaluator can return a diagnostic outcome.
    round_id: Annotated[int, Field(ge=0)]
    # Answer is the Chainlink total return value and may be invalid until evaluated.
    answer: int
    # Started time retains the complete latestRoundData evidence.
    started_at: datetime
    # Updated time drives the explicit freshness check.
    updated_at: datetime
    # Answered-in-round is retained for evidence but is deprecated and not used as a gate.
    answered_in_round: Annotated[int, Field(ge=0)]
    # Observation time fixes when the read-only backend obtained this round.
    observed_at: datetime
    # Issuer pause state detects when Coinbase freezes multiplier advancement.
    issuer_paused: bool
    # Issuer multiplier uses Coinbase's documented 18-decimal WAD representation.
    issuer_multiplier_wad: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def require_timezone_aware_timestamps(self) -> "ChainlinkFeedObservation":
        """Reject naive datetimes that would make freshness calculations ambiguous."""
        # All three instants must include a UTC offset before subtraction is safe.
        timestamps = (self.started_at, self.updated_at, self.observed_at)
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("Chainlink feed timestamps must be timezone-aware")
        return self


class SequencerObservation(BaseModel):
    """Capture the Base sequencer uptime feed state used for one evaluation."""

    # Frozen strict fields prevent status changes during a feed assessment.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Feed address identifies the official Base sequencer uptime proxy queried.
    feed_address: EvmAddress
    # Chainlink defines zero as up and one as down for the sequencer answer.
    answer: int
    # Status start time supports the mandatory post-recovery grace period.
    started_at: datetime
    # Observation time fixes when the read-only backend obtained sequencer state.
    observed_at: datetime

    @model_validator(mode="after")
    def require_timezone_aware_timestamps(self) -> "SequencerObservation":
        """Reject naive datetimes in sequencer evidence."""
        # Both instants need offsets so recovery age is deterministic.
        timestamps = (self.started_at, self.observed_at)
        if any(timestamp.utcoffset() is None for timestamp in timestamps):
            raise ValueError("sequencer timestamps must be timezone-aware")
        return self


class OracleHealthResult(BaseModel):
    """Expose a stable health result with derived value and ordered diagnostics."""

    # Frozen strict fields keep API output aligned with the evaluated observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status is the primary machine-readable result of the evaluation.
    status: OracleHealthStatus
    # Healthy is true only when every open-market safety check passes.
    healthy: bool
    # Symbol identifies the official B20 listing under evaluation.
    symbol: str
    # Token address retains contract-level issuer identity.
    token_address: EvmAddress
    # Feed address retains contract-level Chainlink proxy identity.
    feed_address: EvmAddress
    # Total return value is absent unless the answer and scale are positive and valid.
    total_return_value: Decimal | None
    # Issuer multiplier is absent unless its WAD input is positive and valid.
    issuer_multiplier: Decimal | None
    # Age is absent when timestamp integrity prevents a meaningful calculation.
    age_seconds: int | None
    # Diagnostics explain the first fail-closed outcome or the successful evidence path.
    diagnostics: tuple[str, ...]

    @model_validator(mode="after")
    def require_consistent_health_flag(self) -> Self:
        """Reject output whose boolean health flag contradicts its status."""
        # The redundant boolean is safe for consumers only when derived status agrees with it.
        if self.healthy is not (self.status is OracleHealthStatus.HEALTHY):
            raise ValueError("healthy must be true exactly when oracle status is healthy")
        if not self.diagnostics:
            raise ValueError("oracle health result requires at least one diagnostic")
        return self


class ChainlinkCoverageReport(BaseModel):
    """Summarize B20 Chainlink coverage without overstating live availability."""

    # Frozen strict fields make one coverage snapshot coherent across API and dashboard routes.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status determines whether assessments may inform later risk decisions.
    status: OracleCoverageStatus
    # Product documentation is the primary source for B20 feed semantics.
    source_url: str
    # API documentation identifies the exact proxy interface expected by a future backend.
    api_reference_url: str
    # Expected assets counts verified official B20 identities requiring coverage.
    expected_assets: Annotated[int, Field(ge=0)]
    # Configured feeds counts only proxies backed by a reviewed address snapshot.
    configured_feeds: Annotated[int, Field(ge=0)]
    # Healthy feeds counts assessments that passed every open-market gate.
    healthy_feeds: Annotated[int, Field(ge=0)]
    # Assessments remain empty when live evidence is unavailable or incomplete.
    assessments: tuple[OracleHealthResult, ...]
    # Diagnostics give explicit operator evidence for the coverage state.
    diagnostics: tuple[str, ...]

    @model_validator(mode="after")
    def require_consistent_coverage_counts(self) -> Self:
        """Reject partial or contradictory coverage claims."""
        # Assessment count must exactly equal the number of configured proxy identities.
        if self.configured_feeds != len(self.assessments):
            raise ValueError("configured feed count must equal assessment count")
        # Healthy count is derived exclusively from assessment outcomes.
        assessed_healthy = sum(assessment.healthy for assessment in self.assessments)
        if self.healthy_feeds != assessed_healthy:
            raise ValueError("healthy feed count must equal healthy assessment count")
        if self.configured_feeds > self.expected_assets:
            raise ValueError("configured feed count cannot exceed expected B20 assets")
        if self.status is OracleCoverageStatus.VERIFIED and (
            self.expected_assets == 0 or self.configured_feeds != self.expected_assets
        ):
            raise ValueError("verified coverage requires one assessment per expected B20 asset")
        if self.status is OracleCoverageStatus.UNAVAILABLE and self.configured_feeds != 0:
            raise ValueError("unavailable coverage cannot expose partial feed assessments")
        if not self.diagnostics:
            raise ValueError("Chainlink coverage report requires at least one diagnostic")
        return self


class ChainlinkHealthEvaluator:
    """Evaluate B20 feed rounds with deterministic fail-closed gate ordering."""

    def __init__(self, policy: OracleHealthPolicy | None = None) -> None:
        """Create an evaluator around one immutable policy.

        Args:
            policy: Optional explicit freshness and sequencer thresholds.
        """
        # Default policy values remain explicit and inspectable through the model.
        self._policy = policy or OracleHealthPolicy()

    def evaluate(
        self,
        observation: ChainlinkFeedObservation,
        sequencer: SequencerObservation,
        market_regime: MarketRegime,
    ) -> OracleHealthResult:
        """Return one deterministic feed-health outcome.

        Args:
            observation: Complete B20 proxy and Coinbase registry evidence.
            sequencer: Base sequencer status read for the same decision cycle.
            market_regime: Current reference-equity trading regime.

        Returns:
            A fail-closed health result with normalized values and diagnostics.
        """
        # A positive answer can be safely scaled for display even if another gate later fails.
        total_return_value = (
            Decimal(observation.answer) / (Decimal(10) ** observation.decimals)
            if observation.answer > 0
            else None
        )
        # A positive multiplier exposes Coinbase's redemption ratio without float conversion.
        issuer_multiplier = (
            Decimal(observation.issuer_multiplier_wad) / WAD_SCALE
            if observation.issuer_multiplier_wad > 0
            else None
        )
        # Future timestamps do not produce a misleading negative observation age.
        raw_age_seconds = (observation.observed_at - observation.updated_at).total_seconds()
        # Whole elapsed seconds match the integer policy boundary used by the risk engine.
        age_seconds = max(0, int(raw_age_seconds))

        # Independently timestamped reads outside the allowed skew are not one safe snapshot.
        observation_skew_seconds = abs(
            (observation.observed_at - sequencer.observed_at).total_seconds()
        )
        if observation_skew_seconds > self._policy.max_observation_skew_seconds:
            return self._result(
                observation,
                OracleHealthStatus.INVALID_FEED,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Chainlink B20 and Base sequencer reads are outside the observation-time skew "
                "limit.",
            )

        if sequencer.answer != 0:
            return self._result(
                observation,
                OracleHealthStatus.SEQUENCER_DOWN,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Base sequencer uptime feed does not report the required up value of zero.",
            )

        # Negative recovery age means the sequencer status timestamp is not trustworthy.
        sequencer_recovery_age = (sequencer.observed_at - sequencer.started_at).total_seconds()
        if sequencer_recovery_age < 0:
            return self._result(
                observation,
                OracleHealthStatus.INVALID_FEED,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Base sequencer status start time is later than its observation time.",
            )
        if sequencer_recovery_age < self._policy.sequencer_grace_period_seconds:
            return self._result(
                observation,
                OracleHealthStatus.SEQUENCER_GRACE_PERIOD,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Base sequencer is up but remains inside the configured recovery grace period.",
            )
        if observation.issuer_paused:
            return self._result(
                observation,
                OracleHealthStatus.ISSUER_PAUSED,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Coinbase oracle registry reports the B20 redemption multiplier as paused.",
            )
        if (
            observation.round_id == 0
            or observation.answer <= 0
            or observation.issuer_multiplier_wad <= 0
            or observation.updated_at < observation.started_at
        ):
            return self._result(
                observation,
                OracleHealthStatus.INVALID_FEED,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Chainlink round, answer, multiplier, or timestamp ordering is invalid.",
            )
        if raw_age_seconds < -self._policy.max_future_skew_seconds:
            return self._result(
                observation,
                OracleHealthStatus.FUTURE_TIMESTAMP,
                total_return_value,
                issuer_multiplier,
                None,
                "Chainlink updatedAt is later than the permitted observation-time clock skew.",
            )
        if market_regime is not MarketRegime.MARKET_OPEN:
            return self._result(
                observation,
                OracleHealthStatus.MARKET_CLOSED,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Reference market is closed; the conservative policy does not treat a held "
                "last value as trade-eligible.",
            )
        if age_seconds > self._policy.max_open_age_seconds:
            return self._result(
                observation,
                OracleHealthStatus.STALE,
                total_return_value,
                issuer_multiplier,
                age_seconds,
                "Chainlink updatedAt exceeds the configured open-market freshness limit.",
            )
        return self._result(
            observation,
            OracleHealthStatus.HEALTHY,
            total_return_value,
            issuer_multiplier,
            age_seconds,
            "Chainlink round, Coinbase multiplier, freshness, and Base sequencer gates passed.",
        )

    def _result(
        self,
        observation: ChainlinkFeedObservation,
        status: OracleHealthStatus,
        total_return_value: Decimal | None,
        issuer_multiplier: Decimal | None,
        age_seconds: int | None,
        diagnostic: str,
    ) -> OracleHealthResult:
        """Build a health result without duplicating immutable identity evidence.

        Args:
            observation: Source observation providing symbol and addresses.
            status: Deterministic gate outcome.
            total_return_value: Scaled positive proxy answer when available.
            issuer_multiplier: Scaled positive Coinbase multiplier when available.
            age_seconds: Non-negative observation age when timestamps permit it.
            diagnostic: Human-readable explanation of the selected outcome.

        Returns:
            A complete immutable oracle-health result.
        """
        return OracleHealthResult(
            status=status,
            healthy=status is OracleHealthStatus.HEALTHY,
            symbol=observation.symbol,
            token_address=observation.token_address,
            feed_address=observation.feed_address,
            total_return_value=total_return_value,
            issuer_multiplier=issuer_multiplier,
            age_seconds=age_seconds,
            diagnostics=(diagnostic,),
        )


def unavailable_chainlink_coverage(expected_assets: int) -> ChainlinkCoverageReport:
    """Return an honest no-observation report for the unconfigured live connector.

    Args:
        expected_assets: Number of verified official B20 identities needing feed coverage.

    Returns:
        Evidence-backed unavailable status with no proxy or health claims.
    """
    return ChainlinkCoverageReport(
        status=OracleCoverageStatus.UNAVAILABLE,
        source_url=CHAINLINK_B20_DOCS_URL,
        api_reference_url=CHAINLINK_FEED_API_URL,
        expected_assets=expected_assets,
        configured_feeds=0,
        healthy_feeds=0,
        assessments=(),
        diagnostics=(
            "Official Chainlink documentation confirms Coinbase B20 total-return feeds on Base, "
            "but no reviewed proxy-address snapshot or read-only observation backend is "
            "configured; no feed-health claims were made.",
        ),
    )
