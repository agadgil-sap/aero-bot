"""Behavior tests for the bounded Aerodrome B20 live yield screen."""

from decimal import Decimal

from aero_bot.market_data import (
    DefiLlamaYieldScanner,
    YieldScreenStatus,
    YieldScreenUnavailableError,
)
from aero_bot.registry import load_official_b20_registry
from aero_bot.venues import AERO_TOKEN_ADDRESS, BASE_USDC_ADDRESS

# The official registry fixture contains this Coinbase-issued NVDAc contract.
NVDAC_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
# A stable UUID mirrors DefiLlama's public pool identifier format.
SOURCE_POOL_ID = "f07ec582-f302-5fab-9531-eabc3f8f291c"


class FixtureBackend:
    """Return one immutable decoded payload for deterministic scanner tests."""

    def __init__(self, payload: object) -> None:
        """Store the exact untrusted payload returned by the fixture.

        Args:
            payload: JSON-compatible source response supplied to the scanner.
        """
        # Payload remains private so tests exercise only the backend protocol.
        self._payload = payload

    def fetch(self) -> object:
        """Return the stored decoded payload without performing network I/O."""
        return self._payload


class UnavailableBackend:
    """Raise the reviewed availability error for fail-closed behavior tests."""

    def fetch(self) -> object:
        """Signal a deterministic external-source outage."""
        raise YieldScreenUnavailableError("fixture timeout")


def source_record(**overrides: object) -> dict[str, object]:
    """Build one valid in-scope Aerodrome Slipstream source record.

    Args:
        **overrides: Source fields changed for a specific boundary test.

    Returns:
        JSON-compatible record using official NVDAc, native USDC, and AERO addresses.
    """
    # Baseline record mirrors the public fields used by the read-only scanner.
    record: dict[str, object] = {
        "chain": "Base",
        "project": "aerodrome-slipstream",
        "symbol": "USDC-NVDAC",
        "tvlUsd": 2_392_054,
        "apyBase": 18.91475,
        "apyReward": 163.4859,
        "apy": 182.40065,
        "rewardTokens": [AERO_TOKEN_ADDRESS],
        "pool": SOURCE_POOL_ID,
        "poolMeta": "CL10 - 0.05%",
        "count": 20,
        "outlier": True,
        "underlyingTokens": [BASE_USDC_ADDRESS, NVDAC_ADDRESS],
        "volumeUsd1d": 2_479_183.64018,
    }
    record.update(overrides)
    return record


def test_verified_screen_filters_and_calculates_mutually_exclusive_rates() -> None:
    """An official pair is accepted without adding fee and emission returns."""
    # One unrelated venue record proves the scanner ignores arbitrary venues.
    unrelated_record = source_record(
        project="uniswap-v3", pool="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    # Fixture source returns one ignored record and one valid Aerodrome record.
    backend = FixtureBackend({"data": [unrelated_record, source_record()]})
    # Fifty-percent haircut is explicit and deterministic for the result.
    scanner = DefiLlamaYieldScanner(backend=backend, emissions_haircut=Decimal("0.50"))

    # Official packaged identities provide contract-first matching evidence.
    result = scanner.scan(load_official_b20_registry())

    assert result.status is YieldScreenStatus.VERIFIED
    assert result.expected_assets == 10
    assert result.covered_assets == 1
    assert len(result.observations) == 1
    # Sole observation contains normalized official identities.
    observation = result.observations[0]
    assert observation.token_symbol == "NVDAc"  # noqa: S105
    assert observation.token_address == NVDAC_ADDRESS
    assert observation.quote_token_address == BASE_USDC_ADDRESS.lower()
    assert observation.outlier is True
    assert observation.sample_count == 20
    # Best screen rate equals haircut emissions because they exceed fee APY.
    expected_emissions_daily = Decimal("163.4859") * Decimal("0.50") / Decimal(100) / Decimal(365)
    assert observation.haircut_emissions_simple_daily_rate == expected_emissions_daily
    assert observation.best_haircut_simple_daily_rate == expected_emissions_daily
    # The external combined value remains evidence only and is never the calculated best rate.
    combined_daily = Decimal("182.40065") / Decimal(100) / Decimal(365)
    assert observation.best_haircut_simple_daily_rate != combined_daily


def test_screen_orders_multiple_official_records_by_conservative_daily_rate() -> None:
    """Strongest mutually exclusive haircut rate appears first without hiding peers."""
    # GOOGLc is another official B20 identity in the packaged registry.
    googlc_address = "0xb2000000000000000000002d0ba3164cc74f58b7"
    # Second source record has a lower fee and reward screen rate.
    googlc_record = source_record(
        pool="383917ff-e71c-5d60-888e-e817b06f5436",
        symbol="USDC-GOOGLC",
        underlyingTokens=[BASE_USDC_ADDRESS, googlc_address],
        apyBase=10,
        apyReward=20,
        apy=30,
    )
    # Reverse input order proves sorting derives from calculated screen evidence.
    backend = FixtureBackend({"data": [googlc_record, source_record()]})

    result = DefiLlamaYieldScanner(backend=backend).scan(load_official_b20_registry())

    assert result.status is YieldScreenStatus.VERIFIED
    assert result.covered_assets == 2
    assert [observation.token_symbol for observation in result.observations] == [
        "NVDAc",
        "GOOGLc",
    ]


def test_non_official_assets_are_ignored_even_on_allowed_project() -> None:
    """Ticker-like data cannot bypass exact official contract matching."""
    # Lookalike address deliberately does not appear in the official B20 registry.
    lookalike_address = "0x1111111111111111111111111111111111111111"
    # Symbol alone resembles NVDAc but the underlying contract is untrusted.
    lookalike_record = source_record(underlyingTokens=[BASE_USDC_ADDRESS, lookalike_address])
    # Scanner receives only the untrusted lookalike record.
    scanner = DefiLlamaYieldScanner(backend=FixtureBackend({"data": [lookalike_record]}))

    result = scanner.scan(load_official_b20_registry())

    assert result.status is YieldScreenStatus.VERIFIED
    assert result.covered_assets == 0
    assert result.observations == ()


def test_official_b20_pool_with_another_quote_asset_is_outside_screen() -> None:
    """An official B20 pool without native USDC is ignored rather than misclassified."""
    # Wrapped Ether is reputable but outside the user's native-USDC quote-asset scope.
    weth_address = "0x4200000000000000000000000000000000000006"
    # Alternate-quote pool contains a real official B20 identity but no native USDC.
    alternate_quote_record = source_record(underlyingTokens=[weth_address, NVDAC_ADDRESS])
    # Scanner must treat the record as irrelevant to this intentionally narrow screen.
    scanner = DefiLlamaYieldScanner(backend=FixtureBackend({"data": [alternate_quote_record]}))

    result = scanner.scan(load_official_b20_registry())

    assert result.status is YieldScreenStatus.VERIFIED
    assert result.covered_assets == 0
    assert result.observations == ()


def test_relevant_record_with_wrong_reward_token_rejects_complete_screen() -> None:
    """Positive emissions cannot be attributed to anything except official AERO."""
    # Wrong reward address turns otherwise in-scope data into unsafe evidence.
    wrong_reward_record = source_record(rewardTokens=["0x1111111111111111111111111111111111111111"])
    # Scanner receives one relevant but invalid record.
    scanner = DefiLlamaYieldScanner(backend=FixtureBackend({"data": [wrong_reward_record]}))

    result = scanner.scan(load_official_b20_registry())

    assert result.status is YieldScreenStatus.REJECTED
    assert result.observations == ()
    assert "official AERO" in result.diagnostics[0]


def test_duplicate_source_pool_rejects_complete_screen() -> None:
    """Duplicated relevant records cannot inflate or ambiguously reorder the screen."""
    # Identical UUIDs create a deterministic duplicate-evidence violation.
    duplicate_records = [source_record(), source_record(symbol="DUPLICATE")]
    # Scanner receives both conflicting rows in one source snapshot.
    scanner = DefiLlamaYieldScanner(backend=FixtureBackend({"data": duplicate_records}))

    result = scanner.scan(load_official_b20_registry())

    assert result.status is YieldScreenStatus.REJECTED
    assert "appeared more than once" in result.diagnostics[0]


def test_unavailable_backend_returns_empty_fail_closed_result() -> None:
    """A source outage produces no observations and makes no current-yield claim."""
    # Backend raises only the reviewed availability exception.
    scanner = DefiLlamaYieldScanner(backend=UnavailableBackend())

    result = scanner.scan(load_official_b20_registry())

    assert result.status is YieldScreenStatus.UNAVAILABLE
    assert result.observed_at is None
    assert result.covered_assets == 0
    assert result.observations == ()
    assert "fixture timeout" in result.diagnostics[0]


def test_invalid_payload_and_invalid_haircut_fail_safely() -> None:
    """Malformed root data and unsafe reward assumptions cannot enter a result."""
    # Malformed root lacks the required data collection.
    malformed_scanner = DefiLlamaYieldScanner(backend=FixtureBackend({"wrong": []}))

    malformed_result = malformed_scanner.scan(load_official_b20_registry())

    assert malformed_result.status is YieldScreenStatus.REJECTED
    assert "response data must be a list" in malformed_result.diagnostics[0]
    # Haircut above one would amplify rewards and is rejected during construction.
    try:
        DefiLlamaYieldScanner(
            backend=FixtureBackend({"data": []}), emissions_haircut=Decimal("1.1")
        )
    except ValueError as error:
        assert "between zero and one" in str(error)
    else:
        raise AssertionError("unsafe emissions haircut was accepted")
