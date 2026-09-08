"""Behavior tests for the persisted known-pool pin cache."""

from datetime import UTC, datetime
from pathlib import Path

from aero_bot.lp_pins import (
    PIN_FILE_VERSION,
    LpPoolPin,
    LpPoolPinStore,
    build_pool_pin_from_discovery,
)

# The fixture pool identity mirrors the executor tests' scripted pool.
POOL_ADDRESS = "0x1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
FACTORY_ADDRESS = "0xbafe73f8e88a3ba0fe5dcc52c1a4dfe5b1e9c73a"
TOKEN0_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TOKEN1_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
GAUGE_ADDRESS = "0x3111111111111111111111111111111111111111"
NFPM_ADDRESS = "0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53"
PINNED_AT = datetime(2026, 9, 8, tzinfo=UTC)


def make_pin(**overrides: object) -> LpPoolPin:
    """Build one coherent fixture pin.

    Args:
        **overrides: Pin fields changed for one behavior.

    Returns:
        A validated immutable pin.
    """
    values: dict[str, object] = {
        "symbol": "FIXc",
        "pool_address": POOL_ADDRESS,
        "factory_address": FACTORY_ADDRESS,
        "token0_address": TOKEN0_ADDRESS,
        "token1_address": TOKEN1_ADDRESS,
        "tick_spacing": 10,
        "gauge_address": GAUGE_ADDRESS,
        "nfpm_address": NFPM_ADDRESS,
        "stock_decimals": 8,
        "pinned_at": PINNED_AT,
        "pinned_block": 123,
        "discovery_source": "lp-sugar:fixture@block:123",
    }
    values.update(overrides)
    return LpPoolPin.model_validate(values)


def test_pin_store_round_trips_pins_keyed_by_symbol(tmp_path: Path) -> None:
    """Saved pins reload byte-stable under their lowercase symbol key."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    pin = make_pin()

    store.save_pin(pin)

    reloaded = store.load()
    assert set(reloaded) == {"fixc"}
    assert reloaded["fixc"] == pin


def test_pin_store_upserts_by_symbol(tmp_path: Path) -> None:
    """A same-symbol save replaces the earlier pin instead of accumulating."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pin(pinned_block=100))
    replacement = make_pin(pinned_block=200, symbol="FIXc")

    store.save_pin(replacement)

    reloaded = store.load()
    assert set(reloaded) == {"fixc"}
    assert reloaded["fixc"].pinned_block == 200


def test_pin_store_keeps_distinct_symbols_sorted(tmp_path: Path) -> None:
    """Multiple pools persist side by side in a stable file order."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pin(symbol="ZZZc", pool_address=GAUGE_ADDRESS))
    store.save_pin(make_pin())

    reloaded = store.load()

    assert set(reloaded) == {"fixc", "zzzc"}
    assert reloaded["zzzc"].pool_address == GAUGE_ADDRESS


def test_pin_store_self_heals_over_corrupt_files(tmp_path: Path) -> None:
    """A malformed or missing file yields no pins instead of refusing."""
    path = tmp_path / "lp_pool_pins.json"
    path.write_text("{not json at all", encoding="utf-8")
    store = LpPoolPinStore(path)

    assert store.load() == {}

    # The next successful sweep rewrites the file wholesale.
    store.save_pin(make_pin())
    assert set(store.load()) == {"fixc"}

    path.write_text('{"version": 1, "pools": [{"symbol": 7}]}', encoding="utf-8")
    assert store.load() == {}


def test_pin_store_writes_the_versioned_envelope(tmp_path: Path) -> None:
    """The file carries its format version for future migrations."""
    import json

    path = tmp_path / "lp_pool_pins.json"
    LpPoolPinStore(path).save_pin(make_pin())

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == PIN_FILE_VERSION
    assert document["pools"][0]["symbol"] == "FIXc"


def test_pins_refuse_degenerate_identities() -> None:
    """A naive timestamp or a degenerate pair refuses validation."""
    for overrides in (
        {"pinned_at": datetime(2026, 9, 8)},
        {"token1_address": TOKEN0_ADDRESS},
        {"tick_spacing": 0},
    ):
        try:
            make_pin(**overrides)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{overrides} did not refuse")


def test_build_pool_pin_from_discovery_normalizes_naive_timestamps() -> None:
    """The builder accepts a naive sweep time and pins it as UTC."""
    pin = build_pool_pin_from_discovery(
        symbol="FIXc",
        pool_address=POOL_ADDRESS,
        factory_address=FACTORY_ADDRESS,
        token0_address=TOKEN0_ADDRESS,
        token1_address=TOKEN1_ADDRESS,
        tick_spacing=10,
        gauge_address=GAUGE_ADDRESS,
        nfpm_address=NFPM_ADDRESS,
        stock_decimals=8,
        snapshot_block=123,
        observed_at=datetime(2026, 9, 8, 12),
        discovery_source="lp-sugar:fixture@block:123",
    )

    assert pin.pinned_at == datetime(2026, 9, 8, 12, tzinfo=UTC)
    assert pin.key == "fixc"
    assert pin.pinned_block == 123
