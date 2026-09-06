"""Load the bundled issuer registry with explicit provenance and diagnostics."""

import tomllib
from datetime import date
from enum import StrEnum
from importlib.resources import files
from typing import Literal, Self

from pydantic import BaseModel, ValidationError, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress

# The packaged TOML file is a reviewable snapshot of Base's official issuer list.
OFFICIAL_B20_REGISTRY_RESOURCE = "official_b20_registry.toml"
# The official Base page is the authoritative identity source for this snapshot.
OFFICIAL_B20_SOURCE_URL: Literal["https://www.base.org/stocks"] = "https://www.base.org/stocks"


class RegistryStatus(StrEnum):
    """Describe whether official registry evidence is safe to consume."""

    # Verified means the packaged evidence passed schema and uniqueness validation.
    VERIFIED = "verified"
    # Invalid means the packaged document exists but fails integrity validation.
    INVALID = "invalid"
    # Unavailable means the packaged evidence could not be read.
    UNAVAILABLE = "unavailable"


class B20AssetListing(BaseModel):
    """Represent one issuer identity from the official Base listing."""

    # Frozen strict fields preserve exact source evidence after validation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Symbol is the onchain-style ticker displayed by Base.
    symbol: str
    # Name identifies the underlying company displayed by Base.
    name: str
    # Address is the normalized Base token contract linked by the source.
    address: EvmAddress
    # Explorer URL retains the source page's direct evidence link.
    explorer_url: str


class OfficialB20Registry(BaseModel):
    """Represent a validated snapshot of Base's complete Coinbase-issued list."""

    # Frozen strict fields prevent registry mutation after integrity checks.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Chain ID fixes this registry to Base mainnet.
    chain_id: Literal[8453]
    # Issuer prevents a generic B20 token from being mistaken for a Coinbase product.
    issuer: Literal["Coinbase"]
    # Source URL must remain the reviewed official Base registry page.
    source_url: Literal["https://www.base.org/stocks"]
    # Observation date makes the age of the bundled identity evidence visible.
    source_observed_at: date
    # Assets contain every listing present on the official page at observation time.
    assets: tuple[B20AssetListing, ...]

    @model_validator(mode="after")
    def require_unique_assets(self) -> Self:
        """Reject duplicate symbols or addresses that would make identity ambiguous."""
        # Symbols are case-folded because ticker identity is case-insensitive in this registry.
        symbols = [asset.symbol.casefold() for asset in self.assets]
        # Addresses are already normalized by the shared EVM address validator.
        addresses = [asset.address for asset in self.assets]
        if len(symbols) != len(set(symbols)):
            raise ValueError("official B20 registry contains duplicate symbols")
        if len(addresses) != len(set(addresses)):
            raise ValueError("official B20 registry contains duplicate addresses")
        if not self.assets:
            raise ValueError("official B20 registry must contain at least one asset")
        return self


class B20RegistryResult(BaseModel):
    """Expose verified records or a precise fail-closed diagnostic."""

    # Frozen strict fields make the diagnostic safe to share across routes.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status determines whether assets may populate a contract allowlist.
    status: RegistryStatus
    # Source URL identifies the authority the application expected to use.
    source_url: str
    # Observation date is absent when the document cannot be trusted or read.
    source_observed_at: date | None
    # Assets are empty unless the complete bundled registry validates.
    assets: tuple[B20AssetListing, ...]
    # Diagnostic gives an evidence-backed operator explanation without a traceback.
    diagnostic: str


def parse_official_b20_registry(document: str) -> OfficialB20Registry:
    """Parse and validate a registry TOML document.

    Args:
        document: UTF-8 TOML content from the bundled evidence resource.

    Returns:
        A schema-valid registry with unique normalized identities.

    Raises:
        TOMLDecodeError: If the document is not valid TOML.
        ValidationError: If registry fields or identity invariants are invalid.
    """
    # The standard-library parser avoids adding a networked or mutable registry dependency.
    parsed_document = tomllib.loads(document)
    return OfficialB20Registry.model_validate(parsed_document)


def load_official_b20_registry() -> B20RegistryResult:
    """Load packaged official evidence or return a non-permissive diagnostic."""
    try:
        # The resource API works from both a source checkout and an installed wheel.
        document = (
            files("aero_bot").joinpath(OFFICIAL_B20_REGISTRY_RESOURCE).read_text(encoding="utf-8")
        )
    except OSError as error:
        return B20RegistryResult(
            status=RegistryStatus.UNAVAILABLE,
            source_url=OFFICIAL_B20_SOURCE_URL,
            source_observed_at=None,
            assets=(),
            diagnostic=f"Bundled official B20 registry could not be read: {error}",
        )

    try:
        # Complete schema validation happens before any address becomes available to callers.
        registry = parse_official_b20_registry(document)
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        return B20RegistryResult(
            status=RegistryStatus.INVALID,
            source_url=OFFICIAL_B20_SOURCE_URL,
            source_observed_at=None,
            assets=(),
            diagnostic=f"Bundled official B20 registry failed validation: {error}",
        )

    return B20RegistryResult(
        status=RegistryStatus.VERIFIED,
        source_url=registry.source_url,
        source_observed_at=registry.source_observed_at,
        assets=registry.assets,
        diagnostic=(
            f"Verified {len(registry.assets)} Coinbase-issued B20 listings against the bundled "
            f"Base source snapshot observed {registry.source_observed_at.isoformat()}."
        ),
    )
