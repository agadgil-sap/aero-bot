"""Read-only Aerodrome B20 yield screening through a bounded public data source."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal, Protocol, cast, runtime_checkable
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, NonNegativeDecimal, UnitDecimal
from aero_bot.registry import B20RegistryResult, RegistryStatus
from aero_bot.venues import AERO_TOKEN_ADDRESS, BASE_USDC_ADDRESS

# DefiLlama supplies a free public cross-protocol yield screen without requiring credentials.
DEFILLAMA_YIELDS_URL = "https://yields.llama.fi/pools"
# Only Aerodrome Slipstream records may enter this market-screening boundary.
ALLOWED_PROJECT: Literal["aerodrome-slipstream"] = "aerodrome-slipstream"
# Base is the sole chain supported by this application release.
ALLOWED_CHAIN: Literal["Base"] = "Base"
# Sixteen MiB bounds memory use while remaining above the observed public response size.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Fifteen seconds bounds a failed external request without blocking the local UI indefinitely.
REQUEST_TIMEOUT_SECONDS = 15.0
# Percent values are converted to fractional rates before financial calculations.
PERCENT_TO_FRACTION = Decimal("0.01")
# A fixed 365-day divisor produces a simple daily screening rate without compounding claims.
DAYS_PER_YEAR = Decimal(365)
# A broad upper bound rejects corrupt or structurally mis-scaled percentage values.
MAX_APY_PERCENT = Decimal("100000")
# A fifty-percent AERO haircut matches the conservative default risk policy.
DEFAULT_EMISSIONS_HAIRCUT = Decimal("0.50")


class YieldScreenStatus(StrEnum):
    """Describe whether a secondary-source market screen is safe to display."""

    # Verified means the response passed source, venue, asset, and numeric validation.
    VERIFIED = "verified"
    # Unavailable means the read-only data source could not provide a complete response.
    UNAVAILABLE = "unavailable"
    # Rejected means a relevant record was malformed or violated a hard trust boundary.
    REJECTED = "rejected"


class YieldScreenUnavailableError(RuntimeError):
    """Signal that the bounded read-only market-data request could not complete."""


class YieldObservation(BaseModel):
    """Represent one validated Aerodrome B20/native-USDC yield observation."""

    # Frozen strict fields prevent screen evidence changing after validation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Source pool ID links directly to DefiLlama's individual pool detail page.
    source_pool_id: UUID
    # Project is fixed to Aerodrome Slipstream rather than arbitrary venues.
    project: Literal["aerodrome-slipstream"]
    # Display symbol is retained exactly as reported by the source.
    source_symbol: str
    # Official token symbol comes from the validated Base B20 registry.
    token_symbol: str
    # Token address must match one official Coinbase-issued B20 identity.
    token_address: EvmAddress
    # Quote address is fixed to native Circle USDC on Base.
    quote_token_address: EvmAddress
    # Total value locked is screening evidence rather than executable exit depth.
    tvl_usd: NonNegativeDecimal
    # Base APY is the source's swap-fee yield estimate and is not treated as APR.
    fee_apy_percent: NonNegativeDecimal
    # Reward APY is the source's AERO emissions estimate and remains separate from fees.
    aero_emissions_apy_percent: NonNegativeDecimal
    # Combined APY is retained only to reconcile the external source display.
    source_combined_apy_percent: NonNegativeDecimal
    # Fee daily rate is a simple 365-day screen before IL and adverse-selection costs.
    fee_simple_daily_rate: NonNegativeDecimal
    # Haircut emissions daily rate discounts volatile AERO before daily conversion.
    haircut_emissions_simple_daily_rate: NonNegativeDecimal
    # Best screen rate compares mutually exclusive modes and never adds them together.
    best_haircut_simple_daily_rate: NonNegativeDecimal
    # Pool metadata describes the reported concentrated-liquidity configuration.
    pool_meta: str
    # One-day volume is optional because the source may omit it.
    volume_usd_1d: NonNegativeDecimal | None
    # Outlier preserves the source's warning for unusually unstable yield history.
    outlier: bool
    # Sample count exposes how little or how much history underlies the estimate.
    sample_count: Annotated[int, Field(ge=0)]


class YieldScreenResult(BaseModel):
    """Expose one complete, source-stamped Aerodrome B20 market screen."""

    # Frozen strict fields preserve one coherent screen result.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status determines whether observations may be displayed as validated screen evidence.
    status: YieldScreenStatus
    # Source URL identifies the exact public endpoint queried.
    source_url: str
    # Observed time records when the complete HTTP response was accepted.
    observed_at: datetime | None
    # Expected assets counts the verified official B20 identities supplied to the scan.
    expected_assets: Annotated[int, Field(ge=0)]
    # Covered assets counts distinct official tokens represented by accepted observations.
    covered_assets: Annotated[int, Field(ge=0)]
    # Emissions haircut makes the conservative screen assumption machine-readable.
    emissions_haircut: UnitDecimal
    # Observations are empty unless the entire relevant subset passes validation.
    observations: tuple[YieldObservation, ...]
    # Diagnostics explain source limitations and any fail-closed outcome.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


@runtime_checkable
class YieldScreenBackend(Protocol):
    """Define the narrow read-only operation required by the yield scanner."""

    def fetch(self) -> object:
        """Return a decoded public yield payload without any mutation capability."""
        ...


class DefiLlamaHttpBackend:
    """Fetch the fixed DefiLlama yields endpoint with strict resource bounds."""

    def __init__(
        self,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        """Configure bounded request behavior for the fixed public endpoint.

        Args:
            timeout_seconds: Complete request timeout in seconds.
            max_response_bytes: Maximum accepted response body size.
        """
        # Positive timeouts prevent invalid client configuration.
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        # Positive response bounds prevent silently disabling memory protection.
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        # Timeout is immutable for the lifetime of this backend.
        self._timeout_seconds = timeout_seconds
        # Maximum bytes are immutable for the lifetime of this backend.
        self._max_response_bytes = max_response_bytes

    def fetch(self) -> object:
        """Fetch and decode the fixed HTTPS endpoint or raise an availability error."""
        try:
            # A dedicated client applies one complete timeout and rejects redirects.
            with httpx.Client(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                headers={"User-Agent": "aero-bot/0.1 read-only-yield-screen"},
            ) as client:
                # The URL is a fixed constant and cannot be supplied by an API caller.
                response = client.get(DEFILLAMA_YIELDS_URL)
                # Non-success status codes are handled as unavailable external evidence.
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise YieldScreenUnavailableError(str(error)) from error
        # Complete body length is checked before JSON parsing allocates nested structures.
        response_size = len(response.content)
        if response_size > self._max_response_bytes:
            raise YieldScreenUnavailableError(
                f"response contained {response_size} bytes, above the configured limit"
            )
        try:
            # Decoding returns untrusted JSON that receives complete structural validation later.
            decoded_payload = cast(object, response.json())
        except ValueError as error:
            raise YieldScreenUnavailableError("response was not valid JSON") from error
        return decoded_payload


class DefiLlamaYieldScanner:
    """Filter a public yield snapshot through Aerodrome and official-asset boundaries."""

    def __init__(
        self,
        backend: YieldScreenBackend | None = None,
        emissions_haircut: Decimal = DEFAULT_EMISSIONS_HAIRCUT,
    ) -> None:
        """Create a scanner with an optional deterministic backend.

        Args:
            backend: Read-only payload source, defaulting to the bounded HTTPS backend.
            emissions_haircut: Fraction of reported AERO emissions credited by the screen.
        """
        # Haircut validation prevents rewards being amplified or made negative.
        if emissions_haircut < 0 or emissions_haircut > 1:
            raise ValueError("emissions_haircut must be between zero and one")
        # Default backend performs only one fixed public HTTPS GET.
        self._backend = backend or DefiLlamaHttpBackend()
        # Immutable Decimal haircut is exposed in every result for reproducibility.
        self._emissions_haircut = emissions_haircut

    def scan(self, registry: B20RegistryResult) -> YieldScreenResult:
        """Return validated B20/native-USDC yield observations or fail closed.

        Args:
            registry: Validated official B20 identities used for exact contract matching.

        Returns:
            Complete source-stamped screen evidence and limitations.
        """
        if registry.status is not RegistryStatus.VERIFIED or not registry.assets:
            return self._failure(
                YieldScreenStatus.REJECTED,
                len(registry.assets),
                "Yield screen blocked because the official B20 registry is not verified.",
            )
        try:
            # External I/O is isolated behind a read-only backend for deterministic tests.
            payload = self._backend.fetch()
        except YieldScreenUnavailableError as error:
            return self._failure(
                YieldScreenStatus.UNAVAILABLE,
                len(registry.assets),
                f"Live yield screen unavailable: {error}",
            )
        try:
            # Parsed observations are returned only when every relevant record is valid.
            observations = self._parse_payload(payload, registry)
        except ValueError as error:
            return self._failure(
                YieldScreenStatus.REJECTED,
                len(registry.assets),
                f"Live yield screen rejected: {error}",
            )
        # Covered addresses avoid double-counting a token with multiple valid pools.
        covered_token_addresses = frozenset(
            observation.token_address for observation in observations
        )
        # Freshness time is captured only after the full relevant subset passes validation.
        observed_at = datetime.now(UTC)
        return YieldScreenResult(
            status=YieldScreenStatus.VERIFIED,
            source_url=DEFILLAMA_YIELDS_URL,
            observed_at=observed_at,
            expected_assets=len(registry.assets),
            covered_assets=len(covered_token_addresses),
            emissions_haircut=self._emissions_haircut,
            observations=observations,
            diagnostics=(
                f"Validated {len(observations)} Aerodrome Slipstream B20/native-USDC yield "
                f"records covering {len(covered_token_addresses)} of {len(registry.assets)} "
                "official assets.",
                "Screening data is secondary-source APY evidence, not executable APR, and is "
                "not eligible for automated trading until onchain pool identity, oracle, exit "
                "depth, IL, and adverse-selection checks also pass.",
            ),
        )

    def _parse_payload(
        self,
        payload: object,
        registry: B20RegistryResult,
    ) -> tuple[YieldObservation, ...]:
        """Validate the root response and parse only in-scope Aerodrome records.

        Args:
            payload: Untrusted decoded JSON from the fixed public endpoint.
            registry: Verified official identities required for contract matching.

        Returns:
            Validated observations ordered by conservative daily screen rate.

        Raises:
            ValueError: If the root or any relevant record is malformed.
        """
        if not isinstance(payload, dict):
            raise ValueError("response root must be an object")
        # Raw data remains untrusted until its collection type is confirmed.
        raw_data = payload.get("data")
        if not isinstance(raw_data, list):
            raise ValueError("response data must be a list")
        # Registry mapping connects exact contract identities with approved display symbols.
        assets_by_address = {asset.address: asset for asset in registry.assets}
        # Parsed observations accumulate only after every hard source filter passes.
        observations: list[YieldObservation] = []
        # Source pool IDs prevent duplicated external rows from entering the result.
        seen_source_pool_ids: set[UUID] = set()
        for raw_record in raw_data:
            if not isinstance(raw_record, dict):
                continue
            # Chain and project filters exclude all non-Aerodrome venue records immediately.
            if (
                raw_record.get("chain") != ALLOWED_CHAIN
                or raw_record.get("project") != ALLOWED_PROJECT
            ):
                continue
            # Underlying token list is required for contract-first matching.
            raw_underlying_tokens = raw_record.get("underlyingTokens")
            if not isinstance(raw_underlying_tokens, list):
                continue
            # Only string addresses can participate in normalized pair comparison.
            if not all(isinstance(address, str) for address in raw_underlying_tokens):
                continue
            # Lowercase addresses align with validated registry normalization.
            underlying_tokens = frozenset(address.lower() for address in raw_underlying_tokens)
            # Matching official addresses identify whether this record is in scope.
            matching_assets = underlying_tokens.intersection(assets_by_address)
            # Non-B20 records are irrelevant and safely ignored.
            if not matching_assets:
                continue
            # Official B20 pools quoted in assets other than native USDC are outside this screen.
            if BASE_USDC_ADDRESS.lower() not in underlying_tokens:
                continue
            # A relevant record must contain exactly native USDC and one official B20 token.
            if (
                len(raw_underlying_tokens) != 2
                or len(underlying_tokens) != 2
                or len(matching_assets) != 1
            ):
                raise ValueError("a relevant record was not an exact B20/native-USDC pair")
            # The sole matched identity supplies canonical address and symbol evidence.
            token_address = next(iter(matching_assets))
            # Positive reward yield must be denominated in the official AERO contract.
            reward_apy_percent = self._decimal_field(
                raw_record,
                "apyReward",
                maximum=MAX_APY_PERCENT,
            )
            # Reward token list is validated only when emissions are reported.
            raw_reward_tokens = raw_record.get("rewardTokens")
            if reward_apy_percent > 0 and (
                not isinstance(raw_reward_tokens, list)
                or AERO_TOKEN_ADDRESS.lower()
                not in {reward.lower() for reward in raw_reward_tokens if isinstance(reward, str)}
            ):
                raise ValueError("a relevant reward APY was not linked to official AERO")
            # UUID parsing makes the external record link unambiguous.
            try:
                source_pool_id = UUID(self._string_field(raw_record, "pool"))
            except ValueError as error:
                raise ValueError("a relevant record had an invalid source pool ID") from error
            if source_pool_id in seen_source_pool_ids:
                raise ValueError("a relevant source pool ID appeared more than once")
            seen_source_pool_ids.add(source_pool_id)
            # Base APY remains separate from the mutually exclusive emissions stream.
            fee_apy_percent = self._decimal_field(
                raw_record,
                "apyBase",
                maximum=MAX_APY_PERCENT,
            )
            # Combined value is retained for external-source reconciliation only.
            combined_apy_percent = self._decimal_field(
                raw_record,
                "apy",
                maximum=MAX_APY_PERCENT,
            )
            # TVL is a screen metric and never substituted for executable exit depth.
            tvl_usd = self._decimal_field(raw_record, "tvlUsd")
            # Fee APY converts to a simple daily fraction without compounding assumptions.
            fee_daily_rate = fee_apy_percent * PERCENT_TO_FRACTION / DAYS_PER_YEAR
            # Emissions are discounted before conversion to a daily fraction.
            haircut_emissions_daily_rate = (
                reward_apy_percent * self._emissions_haircut * PERCENT_TO_FRACTION / DAYS_PER_YEAR
            )
            # Best rate compares exclusive compensation modes rather than adding them.
            best_daily_rate = max(fee_daily_rate, haircut_emissions_daily_rate)
            # Optional one-day volume stays absent when the source has no usable value.
            volume_usd_1d = self._optional_decimal_field(raw_record, "volumeUsd1d")
            # Boolean outlier is required because it is a first-class stability warning.
            raw_outlier = raw_record.get("outlier")
            if not isinstance(raw_outlier, bool):
                raise ValueError("a relevant record had no valid outlier flag")
            # Integer history count excludes booleans despite Python's numeric inheritance.
            raw_sample_count = raw_record.get("count")
            if isinstance(raw_sample_count, bool) or not isinstance(raw_sample_count, int):
                raise ValueError("a relevant record had no valid sample count")
            # Canonical registry record is stable because exactly one address matched.
            official_asset = assets_by_address[token_address]
            observations.append(
                YieldObservation(
                    source_pool_id=source_pool_id,
                    project=ALLOWED_PROJECT,
                    source_symbol=self._string_field(raw_record, "symbol"),
                    token_symbol=official_asset.symbol,
                    token_address=token_address,
                    quote_token_address=BASE_USDC_ADDRESS,
                    tvl_usd=tvl_usd,
                    fee_apy_percent=fee_apy_percent,
                    aero_emissions_apy_percent=reward_apy_percent,
                    source_combined_apy_percent=combined_apy_percent,
                    fee_simple_daily_rate=fee_daily_rate,
                    haircut_emissions_simple_daily_rate=haircut_emissions_daily_rate,
                    best_haircut_simple_daily_rate=best_daily_rate,
                    pool_meta=self._string_field(raw_record, "poolMeta"),
                    volume_usd_1d=volume_usd_1d,
                    outlier=raw_outlier,
                    sample_count=raw_sample_count,
                )
            )
        # Descending rate makes the strongest screened opportunity visible first.
        observations.sort(
            key=lambda observation: observation.best_haircut_simple_daily_rate, reverse=True
        )
        return tuple(observations)

    def _decimal_field(
        self,
        record: dict[object, object],
        field_name: str,
        maximum: Decimal | None = None,
    ) -> Decimal:
        """Parse one required finite non-negative numeric field.

        Args:
            record: Untrusted source record containing the field.
            field_name: Exact source field name to parse.
            maximum: Optional inclusive upper bound for scale-sensitive fields.

        Returns:
            Validated Decimal value.

        Raises:
            ValueError: If the field is missing, non-numeric, non-finite, or implausible.
        """
        # Raw booleans are rejected because they otherwise stringify as numeric-looking values.
        raw_value = record.get(field_name)
        if isinstance(raw_value, bool) or raw_value is None:
            raise ValueError(f"a relevant record had no valid {field_name}")
        try:
            # String conversion avoids binary floating-point arithmetic in later calculations.
            parsed_value = Decimal(str(raw_value))
        except (InvalidOperation, ValueError) as error:
            raise ValueError(f"a relevant record had no valid {field_name}") from error
        if not parsed_value.is_finite() or parsed_value < 0:
            raise ValueError(f"a relevant record had an out-of-range {field_name}")
        if maximum is not None and parsed_value > maximum:
            raise ValueError(f"a relevant record had an out-of-range {field_name}")
        return parsed_value

    def _optional_decimal_field(
        self,
        record: dict[object, object],
        field_name: str,
    ) -> Decimal | None:
        """Parse an optional non-negative numeric field.

        Args:
            record: Untrusted source record containing the optional field.
            field_name: Exact source field name to parse.

        Returns:
            Validated Decimal value or None when the source omitted the field.
        """
        # Missing optional source metrics remain explicitly absent.
        if record.get(field_name) is None:
            return None
        return self._decimal_field(record, field_name)

    def _string_field(self, record: dict[object, object], field_name: str) -> str:
        """Return one required non-empty string field.

        Args:
            record: Untrusted source record containing the field.
            field_name: Exact source field name to validate.

        Returns:
            Source string stripped of surrounding whitespace.

        Raises:
            ValueError: If the field is missing, non-string, or empty.
        """
        # Raw string must be present before whitespace normalization.
        raw_value = record.get(field_name)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"a relevant record had no valid {field_name}")
        return raw_value.strip()

    def _failure(
        self,
        status: YieldScreenStatus,
        expected_assets: int,
        diagnostic: str,
    ) -> YieldScreenResult:
        """Build a consistent unavailable or rejected result.

        Args:
            status: Non-verified failure status.
            expected_assets: Count of official identities available to the scan.
            diagnostic: Human-readable failure evidence.

        Returns:
            Empty fail-closed yield screen result.
        """
        return YieldScreenResult(
            status=status,
            source_url=DEFILLAMA_YIELDS_URL,
            observed_at=None,
            expected_assets=expected_assets,
            covered_assets=0,
            emissions_haircut=self._emissions_haircut,
            observations=(),
            diagnostics=(diagnostic,),
        )
