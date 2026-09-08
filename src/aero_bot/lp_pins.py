"""Persisted Sugar-verified pool identities for the known-pool fast path.

The LP lifecycle's dominant cost is the full Sugar enumeration: paging every
Aerodrome pool over the public RPC before every action. A pool's identity,
though, is immutable contract state - its address, factory, token pair, tick
spacing, gauge binding, NFPM, and stock decimals never change after
deployment - so once one full Sugar sweep has verified a pool, that identity
can be pinned locally and every later action can skip enumeration entirely,
reading only the pool's live state (price, liquidity, gauge emissions,
reserves) at one freshly pinned block.

The pin never replaces verification: the executor re-derives the pinned
identity facts live from the pool contract itself before using a pin, and any
mismatch or unreadable view falls back to the full enumeration, which
re-verifies everything the slow way and rewrites the pin. The store is a
cache of verified facts, not a trust root, so a corrupted or hand-edited file
can only ever cost speed, never safety.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address

# The pin-file envelope's format version, so future writes can migrate.
PIN_FILE_VERSION = 1


class LpPoolPin(BaseModel):
    """Hold one Sugar-verified pool identity pinned for the fast path."""

    # Frozen strict fields keep one pinned identity byte-stable on disk.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the pin was verified for, like AAPLc.
    symbol: Annotated[str, Field(min_length=1)]
    # The pool contract whose identity this pin carries.
    pool_address: EvmAddress
    # The pool's creating Slipstream factory.
    factory_address: EvmAddress
    # The pool's lower-address token.
    token0_address: EvmAddress
    # The pool's higher-address token.
    token1_address: EvmAddress
    # The pool's positive tick grid spacing.
    tick_spacing: Annotated[int, Field(gt=0)]
    # The pool's live CLGauge binding.
    gauge_address: EvmAddress
    # The NFPM the pool's gauge factory names, the Sugar's own resolution.
    nfpm_address: EvmAddress
    # The stock token's decimal count.
    stock_decimals: Annotated[int, Field(gt=0)]
    # When the verifying Sugar sweep completed.
    pinned_at: datetime
    # The Sugar snapshot block the verifying sweep was pinned to.
    pinned_block: Annotated[int, Field(ge=0)]
    # The verifying sweep's source string, kept for audit provenance.
    discovery_source: str

    @model_validator(mode="after")
    def require_coherent_pair(self) -> Self:
        """Reject a degenerate token pair or a naive pin timestamp."""
        if normalize_evm_address(self.token0_address) == normalize_evm_address(self.token1_address):
            raise ValueError("the pinned pool pair needs two distinct tokens")
        if self.pinned_at.tzinfo is None:
            raise ValueError("pinned_at must be timezone-aware")
        return self

    @property
    def key(self) -> str:
        """Return the store's lookup key for this pin."""
        return self.symbol.strip().lower()


class LpPoolPinStore:
    """Load and atomically persist the local pool-pin cache.

    The store is deliberately forgiving on load and strict on shape: a
    missing, unreadable, or malformed file yields no pins - the next full
    sweep re-verifies every pool and rewrites the file - because the pins are
    a cache of verified facts whose consumers re-verify identity live anyway.
    """

    def __init__(self, path: Path) -> None:
        """Configure the store's pin-file location.

        Args:
            path: Absolute path of the JSON pin file.
        """
        self._path = path.expanduser()

    @property
    def path(self) -> Path:
        """Return the store's pin-file path."""
        return self._path

    def load(self) -> dict[str, LpPoolPin]:
        """Return every persisted pin keyed by lowercase symbol.

        Any load failure - a missing file, unreadable bytes, malformed JSON,
        or a record that fails validation - yields an empty mapping so the
        caller falls back to full discovery and the file self-heals on the
        next successful sweep.

        Returns:
            The persisted pins keyed by lowercase stock symbol.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            document = json.loads(raw)
            records = document["pools"] if isinstance(document, dict) else None
            if not isinstance(records, list):
                return {}
            pins = (LpPoolPin.model_validate(record) for record in records)
            return {pin.key: pin for pin in pins}
        except (ValueError, KeyError, TypeError):
            return {}

    def save_pin(self, pin: LpPoolPin) -> None:
        """Upsert one pin atomically, replacing any same-symbol entry.

        Args:
            pin: The verified pool identity to persist.

        Raises:
            OSError: If the file cannot be written.
            ValueError: If serialization fails.
        """
        pins = self.load()
        pins[pin.key] = pin
        self._write(pins)

    def _write(self, pins: dict[str, LpPoolPin]) -> None:
        """Write the whole pin set through one atomic file replacement."""
        document = {
            "version": PIN_FILE_VERSION,
            "pools": [
                pin.model_dump(mode="json") for pin in sorted(pins.values(), key=lambda p: p.key)
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self._path)


def build_pool_pin_from_discovery(
    *,
    symbol: str,
    pool_address: str,
    factory_address: str,
    token0_address: str,
    token1_address: str,
    tick_spacing: int,
    gauge_address: str,
    nfpm_address: str,
    stock_decimals: int,
    snapshot_block: int,
    observed_at: datetime,
    discovery_source: str,
) -> LpPoolPin:
    """Build one pin from a verified full-sweep pool resolution.

    Args:
        symbol: The registry-matched stock symbol the sweep verified.
        pool_address: The Sugar-verified pool contract.
        factory_address: The pool's factory as the sweep validated it.
        token0_address: The pool's lower-address token.
        token1_address: The pool's higher-address token.
        tick_spacing: The pool's positive tick spacing.
        gauge_address: The pool's Sugar-record gauge.
        nfpm_address: The pool's Sugar-record NFPM.
        stock_decimals: The stock token's decimal count.
        snapshot_block: The sweep's pinned snapshot block.
        observed_at: The sweep's observation time.
        discovery_source: The sweep's source provenance string.

    Returns:
        The validated immutable pin ready to persist.
    """
    return LpPoolPin.model_validate(
        {
            "symbol": symbol,
            "pool_address": pool_address,
            "factory_address": factory_address,
            "token0_address": token0_address,
            "token1_address": token1_address,
            "tick_spacing": tick_spacing,
            "gauge_address": gauge_address,
            "nfpm_address": nfpm_address,
            "stock_decimals": stock_decimals,
            "pinned_at": observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=UTC),
            "pinned_block": snapshot_block,
            "discovery_source": discovery_source,
        }
    )
