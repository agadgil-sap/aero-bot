"""Behavior tests for official B20 identity evidence and diagnostics."""

from datetime import date
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from aero_bot.registry import (
    OFFICIAL_B20_SOURCE_URL,
    B20AssetListing,
    OfficialB20Registry,
    RegistryStatus,
    load_official_b20_registry,
)


def test_packaged_registry_matches_official_base_listing() -> None:
    """The bundled snapshot exposes all official listings with normalized addresses."""
    # Loading through package resources verifies the installed-wheel access path.
    result = load_official_b20_registry()
    # Symbol-to-address mapping makes the official identity evidence easy to assert.
    addresses_by_symbol = {asset.symbol: asset.address for asset in result.assets}

    assert result.status is RegistryStatus.VERIFIED
    assert result.source_url == OFFICIAL_B20_SOURCE_URL
    assert result.source_observed_at is not None
    assert len(result.assets) == 10
    assert addresses_by_symbol["NVDAc"] == "0xb20000000000000000000078ee7ce2fe4908108c"
    assert addresses_by_symbol["AAPLc"] == "0xb200000000000000000000c2e324d24d7eecd1fb"
    assert addresses_by_symbol["TSLAc"] == "0xb2000000000000000000001e800a7f5189430cd0"
    assert "Verified 10 Coinbase-issued B20 listings" in result.diagnostic


def test_registry_rejects_duplicate_identity_evidence() -> None:
    """Duplicate symbols cannot silently create an ambiguous official allowlist."""
    # One valid listing is reused to isolate the duplicate-symbol invariant.
    listing = B20AssetListing(
        symbol="AAPLc",
        name="Apple",
        address="0xb200000000000000000000c2e324d24d7eecd1fb",
        explorer_url=("https://basescan.org/token/0xb200000000000000000000c2e324d24d7eecd1fb"),
    )

    with pytest.raises(ValidationError, match="duplicate symbols"):
        OfficialB20Registry(
            chain_id=8453,
            issuer="Coinbase",
            source_url=OFFICIAL_B20_SOURCE_URL,
            source_observed_at=date(2026, 9, 6),
            assets=(listing, listing),
        )


def test_registry_rejects_duplicate_contract_addresses() -> None:
    """One contract cannot silently represent two official issuer identities."""
    # The first listing establishes one valid identity and normalized address.
    first_listing = B20AssetListing(
        symbol="AAPLc",
        name="Apple",
        address="0xb200000000000000000000c2e324d24d7eecd1fb",
        explorer_url=("https://basescan.org/token/0xb200000000000000000000c2e324d24d7eecd1fb"),
    )
    # The second listing changes identity fields while intentionally retaining the address.
    second_listing = first_listing.model_copy(update={"symbol": "OTHERc", "name": "Other"})

    with pytest.raises(ValidationError, match="duplicate addresses"):
        OfficialB20Registry(
            chain_id=8453,
            issuer="Coinbase",
            source_url=OFFICIAL_B20_SOURCE_URL,
            source_observed_at=date(2026, 9, 6),
            assets=(first_listing, second_listing),
        )


def test_registry_rejects_empty_official_list() -> None:
    """An empty source snapshot cannot masquerade as a verified issuer registry."""
    with pytest.raises(ValidationError, match="at least one asset"):
        OfficialB20Registry(
            chain_id=8453,
            issuer="Coinbase",
            source_url=OFFICIAL_B20_SOURCE_URL,
            source_observed_at=date(2026, 9, 6),
            assets=(),
        )


def test_registry_returns_diagnostic_when_resource_is_unavailable() -> None:
    """A missing package resource returns no permissive identities and explains why."""
    # The resource reader is forced to fail as a damaged installation would.
    with patch("aero_bot.registry.files") as package_files:
        package_files.return_value.joinpath.return_value.read_text.side_effect = OSError(
            "fixture missing"
        )
        # The loader converts the read failure into a stable application result.
        result = load_official_b20_registry()

    assert result.status is RegistryStatus.UNAVAILABLE
    assert result.assets == ()
    assert result.source_observed_at is None
    assert "fixture missing" in result.diagnostic


def test_registry_returns_diagnostic_when_resource_is_invalid() -> None:
    """Malformed packaged evidence returns no identities and includes validation context."""
    # The resource reader returns an intentionally incomplete TOML document.
    with patch("aero_bot.registry.files") as package_files:
        package_files.return_value.joinpath.return_value.read_text.return_value = "chain_id = ["
        # The loader converts parser failure into a stable fail-closed result.
        result = load_official_b20_registry()

    assert result.status is RegistryStatus.INVALID
    assert result.assets == ()
    assert result.source_observed_at is None
    assert "failed validation" in result.diagnostic
