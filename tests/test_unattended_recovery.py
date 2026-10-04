"""Unattended restart and crash-recovery proof over the full cycle/CLI path.

These tests run the real ``aero_bot.cycle.main()`` entry - the same CLI the
systemd timer starts - as real subprocesses against an isolated faithful
JSON-RPC chain fixture serving every Aerodrome Slipstream view the cycle
consumes, plus the real durable audit store and cycle-book state files on
disk. Interruptions are injected exactly where unattended operation meets
them: SIGTERM kills at confirmed-action boundaries (the systemd timeout
shape), RPC rate-limit storms, audit-store lock contention, and incomplete
journal evidence. The restart must prove exactly-once financial effects,
audit-proven custody adoption, correct full equity, preserved real loss
latches, and no phantom loss latch - never blindly adopting stray
positions or resetting a latch.

Nothing here touches the real network: the chain serves a loopback HTTP
endpoint, the executor's fallback receipt endpoints are pinned to the same
loopback, and the signing key is a deterministic fixture.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
import rlp  # type: ignore[import-untyped]  # fixture decodes a signed EIP-1559 envelope
from eth_abi.abi import decode as abi_decode
from pydantic import BaseModel

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.cycle import CYCLE_REWARD_POSTURE_ENV, CYCLE_STATE_PATH_ENV
from aero_bot.known_pool import staked_sides_for_gauge_liquidity
from aero_bot.lp_executor import EXIT_OK
from aero_bot.lp_plan import _sqrt_price_at_tick, position_amounts_at_sqrt_ratio
from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    BASE_USDC_ADDRESS,
    SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
)

# The deterministic fixture signing key; a test artifact, never a secret.
FIXTURE_SIGNING_KEY_HEX = "11" * 32

# The fixture Safe mirrors the canary deployment constant.
FIXTURE_SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"

# The shared official Slipstream gauge factory and its one NFPM.
FIXTURE_GAUGE_FACTORY_ADDRESS = "0x385293cae378c813f16f0c1334d774adddf56abb"
FIXTURE_NFPM_ADDRESS = "0xe1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"
FIXTURE_VOTER_ADDRESS = "0x" + "55" * 20

# The canonical classic USDC/AERO pricing pair: one whole AERO at 0.5 USDC.
FIXTURE_AERO_CLASSIC_POOL_ADDRESS = "0x" + "ee" * 20
FIXTURE_AERO_PRICE_USDC = Decimal("0.5")
# The Slipstream AERO/USDC pool the reward conversion routes through.
FIXTURE_AERO_SLIPSTREAM_POOL_ADDRESS = "0x" + "ef" * 20

# Board pool geometry: the in-range anchor every fixture pool shares.
POOL_TICK_SPACING = 10
POOL_CURRENT_TICK = -15
with localcontext() as _ctx:
    _ctx.prec = 60
    POOL_SQRT_RATIO = int((Decimal("1.0001") ** Decimal(-10) * (1 << 96)).to_integral_value())
POOL_ACTIVE_LIQUIDITY = 10**20
POOL_FEE_GROWTH_GLOBAL = 1 << 128

SECONDS_PER_YEAR = 365 * 24 * 3600

NFPM_INCREASE_LIQUIDITY_TOPIC0 = (
    "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
)

_SELECTORS = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "tick_spacing": "0xd0c93a7c",
    "gauge_of_pool": "0xa6f19c84",
    "factory_of_pool": "0xc45a0155",
    "slot0": "0x3850c7bd",
    "liquidity": "0x1a686502",
    "staked_liquidity": "0x3ab04b20",
    "fee_growth_global0": "0xf3058399",
    "fee_growth_global1": "0x46141319",
    "ticks": "0xf30dba93",
    "gauge_earned": "0x3e491d47",
    "gauge_rewards": "0xf301af42",
    "gauge_factory_of_gauge": "0x0d52333c",
    "gauge_reward_token": "0xf7c618c1",
    "gauge_reward_rate": "0x7b0a47ee",
    "gauge_deposit_timestamp": "0x4ede8c85",
    "gauge_penalty_rate": "0xd6b7494f",
    "gauge_min_stake_times": "0xe782453b",
    "gauge_factory_nft": "0x47ccca02",
    "factory_voter": "0x46c96aac",
    "voter_is_alive": "0x1703e5f9",
    "factory_is_pool": "0x5b16ebb7",
    "factory_get_swap_fee": "0x35458dcc",
    "factory_get_unstaked_fee": "0x48cf7a43",
    "erc20_balance_of": "0x70a08231",
    "erc20_allowance": "0xdd62ed3e",
    "erc20_decimals": "0x313ce567",
    "erc721_owner_of": "0x6352211e",
    "erc721_token_of_owner_by_index": "0x2f745c59",
    "erc721_is_approved_for_all": "0xe985e9c5",
    "nfpm_positions": "0x99fbab88",
    "pool_factory_get_pool": "0x79bc57d5",
    "classic_reserve0": "0x443cb4bc",
    "classic_reserve1": "0x5a76f25e",
    "safe_nonce": "0xaffed0e0",
    "safe_domain_separator": "0xf698da25",
    "safe_check_signatures": "0x934f3a11",
    "sugar_all": "0xb10daf7b",
}

_INNER_SELECTORS = {
    "095ea7b3": "approve",
    "3593564c": "router_swap",
    "b5007d1f": "mint",
    "a22cb465": "set_approval_for_all",
    "b6b55f25": "gauge_deposit",
    "2e1a7d4d": "gauge_withdraw",
    "0c49ccbe": "decrease_liquidity",
    "fc6f7865": "collect",
    "42966c68": "burn",
    "1c4b774b": "get_reward",
}


class ChainRevertError(Exception):
    """Signal one in-contract revert from inside the fixture chain."""


class ChainFaultError(Exception):
    """One scripted RPC-level fault (an HTTP status or an error payload)."""


def _word(value: int) -> str:
    """Render one unsigned 32-byte ABI word."""
    return f"{value:064x}"


def _signed_word(value: int) -> str:
    """Render one signed 32-byte ABI word (two's complement)."""
    return f"{value & ((1 << 256) - 1):064x}"


def _address_word(address: str) -> str:
    """Render one right-padded address word."""
    return _word(int(address, 16))


def _arg_word(calldata: str, index: int) -> int:
    """Read the index-th 32-byte argument word of one calldata payload."""
    start = 2 + 8 + index * 64
    return int(calldata[start : start + 64], 16)


def _arg_address(calldata: str, index: int) -> str:
    """Read the index-th address argument of one calldata payload."""
    start = 2 + 8 + index * 64 + 24
    return "0x" + calldata[start : start + 40]


def _arg_signed(calldata: str, index: int) -> int:
    """Read the index-th signed argument word of one calldata payload."""
    raw = _arg_word(calldata, index)
    return raw - (1 << 256) if raw >= (1 << 255) else raw


def _solve_liquidity_for_amount0(
    sqrt_ratio: int, tick_lower: int, tick_upper: int, amount0_units: int
) -> int:
    """Solve the liquidity holding ``amount0_units`` of token zero.

    A binary search over the exact two-sided amount math keeps the minted
    position's marked value aligned with its committed entry amounts.
    """
    if amount0_units <= 0:
        return 1
    low, high = 1, 10**24
    for _ in range(200):
        mid = (low + high) // 2
        amount0, _ = position_amounts_at_sqrt_ratio(
            sqrt_ratio, tick_lower, tick_upper, Decimal(mid)
        )
        if int(amount0) < amount0_units:
            low = mid
        else:
            high = mid
    return (low + high) // 2


def load_registry_stocks() -> dict[str, str]:
    """Map the official registry symbols to their B20 contract addresses."""
    registry_path = Path(__file__).parent.parent / "src" / "aero_bot" / "official_b20_registry.toml"
    with registry_path.open("rb") as handle:
        parsed = tomllib.load(handle)
    return {str(asset["symbol"]): str(asset["address"]).lower() for asset in parsed["assets"]}


@dataclass
class FixturePool:
    """One board pool's identity and live state on the fixture chain."""

    symbol: str
    stock_address: str
    pool_address: str
    gauge_address: str
    reward_rate_units: int
    usdc_reserve_units: int = 1_000_000_000_000
    stock_reserve_units: int = 1_000_000_000_000
    staked_liquidity: int = 0
    staked0: int = 0
    staked1: int = 0
    current_tick: int = POOL_CURRENT_TICK
    sqrt_ratio: int = POOL_SQRT_RATIO

    def calibrate_staked_value(self, target_staked_usdc: Decimal) -> None:
        """Pick the staked liquidity whose single cell holds the target value.

        Args:
            target_staked_usdc: The USDC value the current cell should hold.
        """
        low, high = 1, 10**24
        for _ in range(200):
            mid = (low + high) // 2
            staked0, _ = staked_sides_for_gauge_liquidity(
                self.sqrt_ratio, self.current_tick, POOL_TICK_SPACING, mid
            )
            if Decimal(staked0).scaleb(-6) < target_staked_usdc:
                low = mid
            else:
                high = mid
        self.staked_liquidity = (low + high) // 2
        self.staked0, self.staked1 = staked_sides_for_gauge_liquidity(
            self.sqrt_ratio, self.current_tick, POOL_TICK_SPACING, self.staked_liquidity
        )

    def reward_rate_for_apr(self, apr: Decimal, aero_price: Decimal) -> int:
        """Return the reward rate whose emissions APR reads exactly ``apr``.

        Args:
            apr: The target emissions APR as a decimal fraction.
            aero_price: The USDC price of one whole AERO.

        Returns:
            The gauge's reward rate in raw units per second.
        """
        staked_usdc = Decimal(self.staked0).scaleb(-6)
        rate = apr * staked_usdc / (Decimal(SECONDS_PER_YEAR) * aero_price)
        with localcontext() as ctx:
            ctx.prec = 50
            return int((rate * (Decimal(10) ** 18)).to_integral_value())


@dataclass
class ChainPosition:
    """One NFPM position the fixture chain tracks."""

    token_id: int
    pool: FixturePool
    owner: str
    liquidity: int
    tick_lower: int
    tick_upper: int
    entry_usdc_units: int = 0
    entry_stock_units: int = 0
    tokens_owed0: int = 0
    tokens_owed1: int = 0
    accrued_aero_units: int = 0
    deposit_timestamp_epoch: int = 0


@dataclass
class BroadcastRecord:
    """One applied on-chain inner action, for exactly-once assertions."""

    kind: str
    token_id: int | None = None
    detail: str = ""
    amount_units: int = 0

    def matches(self, kind: str, *, token_id: int | None = None) -> bool:
        """Whether this record is one ``kind`` optionally scoped to one NFT.

        Args:
            kind: The applied inner action's kind.
            token_id: An optional NFT scope.

        Returns:
            True when this record matches both filters.
        """
        return self.kind == kind and (token_id is None or self.token_id == token_id)


def encode_sugar_page(records: list[dict[str, Any]]) -> str:
    """ABI-encode one LP Sugar all() page over the given raw record fields.

    The tuple is the 28-word static head followed by the symbol string tail,
    matching ``decode_lp_page`` exactly.
    """
    head_fields = [
        "lp",
        "symbol",
        "decimals",
        "liquidity",
        "type",
        "tick",
        "sqrt_ratio",
        "token0",
        "reserve0",
        "staked0",
        "token1",
        "reserve1",
        "staked1",
        "gauge",
        "gauge_liquidity",
        "gauge_alive",
        "fee",
        "bribe",
        "factory",
        "emissions",
        "emissions_token",
        "pool_fee",
        "unstaked_fee",
        "token0_fees",
        "token1_fees",
        "nfpm",
        "alm",
        "root",
    ]
    elements: list[str] = []
    for record in records:
        symbol_bytes = str(record["symbol"]).encode("utf-8")
        # The symbol offset counts from the tuple head start to the string's
        # own length word, which begins immediately after the static head.
        symbol_offset = len(head_fields) * 32
        padded_symbol_len = 32 * ((len(symbol_bytes) + 31) // 32)
        tail = _word(len(symbol_bytes)) + symbol_bytes.hex().ljust(padded_symbol_len * 2, "0")
        words: list[str] = []
        for name in head_fields:
            if name == "symbol":
                words.append(_word(symbol_offset))
            elif name == "type":
                words.append(_signed_word(int(record.get("tick_spacing", POOL_TICK_SPACING))))
            elif name == "tick":
                words.append(_signed_word(int(record.get("current_tick", POOL_CURRENT_TICK))))
            elif name in {
                "lp",
                "gauge",
                "factory",
                "emissions_token",
                "nfpm",
                "fee",
                "bribe",
                "alm",
                "root",
                "token0",
                "token1",
            }:
                words.append(_address_word(str(record.get(name, "0x" + "00" * 20))))
            elif name == "gauge_alive":
                words.append(_word(1 if record.get("gauge_alive", True) else 0))
            elif name == "decimals":
                words.append(_word(int(record.get("decimals", 18))))
            else:
                words.append(_word(int(record.get(name, 0))))
        elements.append("".join(words) + tail)
    # Element offsets are relative to the offset table's own start (the
    # word after the array length), not to the first element's position.
    cursor = 32 * len(elements)
    offsets: list[int] = []
    for element in elements:
        offsets.append(cursor)
        cursor += len(element) // 2
    return (
        "0x"
        + _word(32)
        + _word(len(elements))
        + "".join(_word(offset) for offset in offsets)
        + "".join(elements)
    )


class FaithfulChain:
    """Serve every JSON-RPC read and apply every broadcast as chain state.

    The chain models the contracts the cycle actually touches: ERC20
    balances and allowances, the shared NFPM's positions, the per-pool
    gauges and their custody, the Slipstream pool views, the LP Sugar
    board, the Safe's nonce and signature view, and the classic USDC/AERO
    pricing pair. Broadcasts decode the Safe execTransaction wrapper and
    apply the inner call's real effect, so restarts observe exactly the
    state a real chain would have reached.

    A deterministic test barrier can freeze the next RPC request after a
    chosen broadcast has been applied, so a parent process can terminate
    the cycle at exactly that confirmed boundary without racing fast
    loopback execution.
    """

    def __init__(self, pools: list[FixturePool], *, safe_usdc_units: int) -> None:
        """Build the chain over the given pools with a funded Safe.

        Args:
            pools: The board's pools, each with its own gauge and stock.
            safe_usdc_units: The Safe's starting USDC balance in raw units.
        """
        self._cond = threading.Condition(threading.RLock())
        self.pools = {pool.pool_address.lower(): pool for pool in pools}
        self.pools_by_gauge = {pool.gauge_address.lower(): pool for pool in pools}
        self.block_number = 51_000_000
        self.safe_nonce = 0
        self.relayer_nonce = 3
        self.gas_price_wei = 10_000_000
        self.tokens: dict[str, dict[str, int]] = {}
        self.allowances: dict[tuple[str, str, str], int] = {}
        self.operator_approved = False
        self.positions: dict[int, ChainPosition] = {}
        self.next_token_id = 7_300_000
        self.broadcasts: list[BroadcastRecord] = []
        self.receipts: dict[str, dict[str, object]] = {}
        self.sent_hashes: list[str] = []
        # The deterministic test barrier: once a broadcast matching the
        # predicate has been applied, the NEXT RPC request blocks until
        # released, pinning the cycle at exactly that confirmed boundary.
        self.barrier_predicate: Callable[[BroadcastRecord], bool] | None = None
        self.barrier_engaged = threading.Event()
        self.barrier_release = threading.Event()
        # Scriptable faults, consumed per RPC request in arrival order.
        self.http_status_queue: list[int] = []
        # An optional chain-id override for wrong-chain refusal controls.
        self.chain_id_override: int | None = None
        # Scripted lost broadcast acknowledgements: the next send whose
        # inner action kind matches still applies on-chain - keyed under
        # the keccak hash of its raw bytes, exactly the transaction a node
        # accepts before losing the response - but its JSON-RPC reply
        # fails, so the executor journals the broadcast-unknown row.
        self.lose_send_ack_kind: str | None = None
        # Scripted classic-pool read failures, consumed per eth_call, so a
        # live AERO price read can refuse transiently and nothing else.
        self.fail_aero_price_reads = 0
        self.rpc_error_after_request: tuple[int, str] | None = None
        self.request_count = 0
        self.estimate_revert_message: str | None = None
        self.estimate_gas_units: dict[str, int | None] = {
            "approve": 60_000,
            "router_swap": 200_000,
            "mint": 400_000,
            "set_approval_for_all": 60_000,
            "gauge_deposit": 120_000,
            "gauge_withdraw": 130_000,
            "decrease_liquidity": 160_000,
            "collect": 90_000,
            "burn": 60_000,
            "get_reward": 110_000,
        }
        self._set_balance(BASE_USDC_ADDRESS, FIXTURE_SAFE_ADDRESS, safe_usdc_units)
        self._set_balance(AERO_TOKEN_ADDRESS, FIXTURE_SAFE_ADDRESS, 0)
        for pool in pools:
            self._set_balance(pool.stock_address, FIXTURE_SAFE_ADDRESS, 0)
            self._set_balance(BASE_USDC_ADDRESS, pool.pool_address, pool.usdc_reserve_units)
            self._set_balance(pool.stock_address, pool.pool_address, pool.stock_reserve_units)
        self._set_balance(AERO_TOKEN_ADDRESS, FIXTURE_AERO_CLASSIC_POOL_ADDRESS, 10**18)
        self._set_balance(BASE_USDC_ADDRESS, FIXTURE_AERO_CLASSIC_POOL_ADDRESS, 500_000)
        # Receipts carry the delivering relayer's public address.
        self._relayer = CycleHarness._relayer_address()

    # -- ledger helpers ---------------------------------------------------
    def _set_balance(self, token: str, owner: str, units: int) -> None:
        ledger = self.tokens.setdefault(token.lower(), {})
        ledger[owner.lower()] = units

    def balance(self, token: str, owner: str) -> int:
        """Return one owner's raw balance of one token."""
        return self.tokens.get(token.lower(), {}).get(owner.lower(), 0)

    def credit(self, token: str, owner: str, units: int) -> None:
        """Credit one owner's raw balance of one token."""
        self._set_balance(token, owner, self.balance(token, owner) + units)

    def debit(self, token: str, owner: str, units: int) -> None:
        """Debit one owner's raw balance, refusing an overdraft."""
        remaining = self.balance(token, owner) - units
        if remaining < 0:
            raise ChainRevertError(f"insufficient {token} balance for {owner}")
        self._set_balance(token, owner, remaining)

    def count_broadcasts(self, kind: str, *, token_id: int | None = None) -> int:
        """Count applied inner actions, optionally scoped to one NFT."""
        with self._cond:
            return sum(1 for record in self.broadcasts if record.matches(kind, token_id=token_id))

    def staked_token_ids(self) -> list[int]:
        """Return every NFT currently held by its gauge."""
        with self._cond:
            return sorted(
                token_id
                for token_id, position in self.positions.items()
                if position.owner == position.pool.gauge_address
            )

    def safe_token_ids(self) -> list[int]:
        """Return every NFT currently held by the Safe."""
        with self._cond:
            return sorted(
                token_id
                for token_id, position in self.positions.items()
                if position.owner == FIXTURE_SAFE_ADDRESS
            )

    def wait_for_broadcast(
        self, predicate: Callable[[BroadcastRecord], bool], timeout: float = 180.0
    ) -> BroadcastRecord | None:
        """Block until one applied broadcast satisfies the predicate."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                for record in self.broadcasts:
                    if predicate(record):
                        return record
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def _maybe_engage_barrier(self) -> None:
        """Arm the freeze once the barrier's trigger broadcast has landed."""
        if (
            self.barrier_predicate is not None
            and not self.barrier_engaged.is_set()
            and any(self.barrier_predicate(record) for record in self.broadcasts)
        ):
            self.barrier_engaged.set()

    def accrue_rewards(self, units_per_position: int) -> None:
        """Credit every staked position's gauge accrual, as time would."""
        with self._cond:
            for position in self.positions.values():
                if position.owner == position.pool.gauge_address:
                    position.accrued_aero_units += units_per_position

    def arm_barrier(
        self,
        predicate: Callable[[BroadcastRecord], bool] | None = None,
        *,
        after_receipt_tx: str | None = None,
    ) -> None:
        """Freeze the next request after the matching boundary.

        Args:
            predicate: Engage once a broadcast satisfying it has applied.
            after_receipt_tx: Engage once this transaction's receipt has
                been SERVED - the boundary between an action's on-chain
                confirmation and its successor's first read, where the
                audit chain already carries the confirmed delivery.
        """
        with self._cond:
            self.barrier_predicate = predicate
            self.barrier_after_receipt_tx = after_receipt_tx
            self.barrier_engaged.clear()
            self.barrier_release.clear()

    def release_barrier(self) -> None:
        """Unfreeze the chain and clear the barrier for later cycles."""
        with self._cond:
            self.barrier_release.set()
            self.barrier_engaged.clear()
            self.barrier_predicate = None
            self.barrier_after_receipt_tx = None

    # -- JSON-RPC surface -------------------------------------------------
    def dispatch(self, method: str, params: list[Any]) -> Any:  # noqa: ANN401 - JSON-RPC values are inherently Any
        """Answer one JSON-RPC request or raise a scripted fault."""
        with self._cond:
            self._maybe_engage_barrier()
            frozen = self.barrier_engaged.is_set() and not self.barrier_release.is_set()
        if frozen and not self.barrier_release.wait(timeout=300.0):
            # Deterministic freeze, held OUTSIDE the chain lock so the
            # parent can release it: the request stays parked until the
            # parent has terminated the cycle at the intended boundary.
            raise ChainFaultError("the test barrier was never released")
        with self._cond:
            if self.rpc_error_after_request is not None:
                threshold, message = self.rpc_error_after_request
                if self.request_count >= threshold:
                    raise ChainFaultError(message)
            self.request_count += 1
            if method == "eth_gasPrice":
                return hex(self.gas_price_wei)
            if method == "eth_chainId":
                return hex(self.chain_id_override or 8453)
            if method == "eth_blockNumber":
                return hex(self.block_number)
            if method == "eth_getBalance":
                return hex(10**16)
            if method == "eth_getTransactionCount":
                result = hex(self.relayer_nonce)
                self.relayer_nonce += 1
                return result
            if method == "eth_call":
                block_tag = str(params[1]) if len(params) > 1 else "latest"
                result = self._eth_call(
                    str(params[0]["to"]).lower(), str(params[0]["data"]), block_tag
                )
                # Every eth_call result is a 0x-prefixed hex string.
                return result if result.startswith("0x") else "0x" + result
            if method == "eth_estimateGas":
                return self._estimate_gas(str(params[0]["data"]))
            if method == "eth_sendRawTransaction":
                return self._send_raw_transaction(str(params[0]))
            if method == "eth_getTransactionReceipt":
                return self.receipts.get(str(params[0]))
            raise AssertionError(f"unexpected RPC method {method}")

    def _eth_call(self, to: str, data: str, block_tag: str) -> str:
        selector = data[:10]
        if to == FIXTURE_SAFE_ADDRESS.lower():
            if selector == _SELECTORS["safe_nonce"]:
                return _word(self.safe_nonce)
            if selector == _SELECTORS["safe_domain_separator"]:
                return "0x" + "ab" * 32
            if selector == _SELECTORS["safe_check_signatures"]:
                return "0x"
            raise AssertionError(f"unexpected Safe view {selector}")
        if to == FIXTURE_GAUGE_FACTORY_ADDRESS.lower():
            if selector == _SELECTORS["gauge_penalty_rate"]:
                return _word(10_000)
            if selector == _SELECTORS["gauge_min_stake_times"]:
                return _word(300)
            if selector == _SELECTORS["gauge_factory_nft"]:
                return _address_word(FIXTURE_NFPM_ADDRESS)
            raise AssertionError(f"unexpected gauge-factory view {selector}")
        if to == FIXTURE_VOTER_ADDRESS.lower() and selector == _SELECTORS["voter_is_alive"]:
            return _word(1)
        if to in self.pools_by_gauge:
            return self._gauge_view(self.pools_by_gauge[to], data, selector)
        if to in self.pools:
            return self._pool_view(self.pools[to], data, selector)
        if to == FIXTURE_NFPM_ADDRESS.lower():
            return self._nfpm_view(data, selector)
        if selector == _SELECTORS["erc20_decimals"]:
            if to == BASE_USDC_ADDRESS.lower() or to == AERO_TOKEN_ADDRESS.lower():
                return _word(6 if to == BASE_USDC_ADDRESS.lower() else 18)
            # Every B20 stock carries eight decimals like the real registry.
            return _word(8 if any(p.stock_address == to for p in self.pools.values()) else 18)
        if selector == _SELECTORS["erc20_balance_of"]:
            owner = _arg_address(data, 0)
            if to == FIXTURE_NFPM_ADDRESS.lower():
                pass
            return _word(self.balance(to, owner))
        if selector == _SELECTORS["erc20_allowance"]:
            owner = _arg_address(data, 0)
            spender = _arg_address(data, 1)
            return _word(self.allowances.get((to, owner, spender), 0))
        if selector == _SELECTORS["factory_is_pool"]:
            return _word(1 if _arg_address(data, 0) in self.pools else 0)
        if selector == _SELECTORS["factory_voter"]:
            return _address_word(FIXTURE_VOTER_ADDRESS)
        if selector == _SELECTORS["factory_get_swap_fee"]:
            return _word(500)
        if selector == _SELECTORS["factory_get_unstaked_fee"]:
            return _word(100_000)
        if to == FIXTURE_AERO_CLASSIC_POOL_ADDRESS.lower():
            if self.fail_aero_price_reads > 0:
                self.fail_aero_price_reads -= 1
                raise ChainFaultError("the fixture refuses the classic-pool price read")
            if selector == _SELECTORS["token0"]:
                return _address_word(AERO_TOKEN_ADDRESS)
            if selector == _SELECTORS["classic_reserve0"]:
                return _word(self.balance(AERO_TOKEN_ADDRESS, to))
            if selector == _SELECTORS["classic_reserve1"]:
                return _word(self.balance(BASE_USDC_ADDRESS, to))
        if selector == _SELECTORS["pool_factory_get_pool"]:
            return _address_word(FIXTURE_AERO_CLASSIC_POOL_ADDRESS)
        if selector == _SELECTORS["sugar_all"]:
            return self._sugar_page(data)
        raise AssertionError(f"unexpected eth_call to {to}: {selector}")

    def _nfpm_view(self, data: str, selector: str) -> str:
        if selector == _SELECTORS["erc721_owner_of"]:
            token_id = _arg_word(data, 0)
            position = self.positions.get(token_id)
            if position is None:
                raise ChainRevertError("ERC721: invalid token ID")
            return _address_word(position.owner)
        if selector == _SELECTORS["erc721_token_of_owner_by_index"]:
            owner = _arg_address(data, 0)
            index = _arg_word(data, 1)
            held = [
                token_id
                for token_id in sorted(self.positions)
                if self.positions[token_id].owner == owner
            ]
            if index >= len(held):
                raise ChainRevertError("ERC721: owner index out of range")
            return _word(held[index])
        if selector == _SELECTORS["erc721_is_approved_for_all"]:
            return _word(1 if self.operator_approved else 0)
        if selector == _SELECTORS["erc20_balance_of"]:
            owner = _arg_address(data, 0)
            held = [
                token_id
                for token_id in sorted(self.positions)
                if self.positions[token_id].owner == owner
            ]
            return _word(len(held))
        if selector == _SELECTORS["nfpm_positions"]:
            token_id = _arg_word(data, 0)
            position = self.positions.get(token_id)
            if position is None:
                raise ChainRevertError("NFPM: unknown token ID")
            pool = position.pool
            words = [
                _word(0),
                _address_word("0x" + "00" * 20),
                _address_word(BASE_USDC_ADDRESS),
                _address_word(pool.stock_address),
                _word(POOL_TICK_SPACING),
                _signed_word(position.tick_lower),
                _signed_word(position.tick_upper),
                _word(position.liquidity),
                _word(0),
                _word(0),
                _word(position.tokens_owed0),
                _word(position.tokens_owed1),
            ]
            return "0x" + "".join(words)
        raise AssertionError(f"unexpected NFPM view {selector}")

    def _gauge_view(self, pool: FixturePool, data: str, selector: str) -> str:
        if selector == _SELECTORS["gauge_earned"]:
            token_id = _arg_word(data, 1)
            position = self.positions.get(token_id)
            if position is None or position.owner != pool.gauge_address:
                raise ChainRevertError("gauge: unknown token")
            return _word(position.accrued_aero_units)
        if selector == _SELECTORS["gauge_rewards"]:
            token_id = _arg_word(data, 0)
            position = self.positions.get(token_id)
            if position is None:
                raise ChainRevertError("gauge: unknown token")
            return _word(0)
        if selector == _SELECTORS["gauge_factory_of_gauge"]:
            return _address_word(FIXTURE_GAUGE_FACTORY_ADDRESS)
        if selector == _SELECTORS["gauge_reward_token"]:
            return _address_word(AERO_TOKEN_ADDRESS)
        if selector == _SELECTORS["gauge_reward_rate"]:
            return _word(pool.reward_rate_units)
        if selector == _SELECTORS["gauge_deposit_timestamp"]:
            token_id = _arg_word(data, 0)
            position = self.positions.get(token_id)
            if position is None:
                raise ChainRevertError("gauge: unknown token")
            return _word(position.deposit_timestamp_epoch)
        raise AssertionError(f"unexpected gauge view {selector}")

    def _pool_view(self, pool: FixturePool, data: str, selector: str) -> str:
        if selector == _SELECTORS["token0"]:
            return _address_word(BASE_USDC_ADDRESS)
        if selector == _SELECTORS["token1"]:
            return _address_word(pool.stock_address)
        if selector == _SELECTORS["tick_spacing"]:
            return _word(POOL_TICK_SPACING)
        if selector == _SELECTORS["gauge_of_pool"]:
            return _address_word(pool.gauge_address)
        if selector == _SELECTORS["factory_of_pool"]:
            return _address_word(SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS)
        if selector == _SELECTORS["slot0"]:
            return "0x" + _word(pool.sqrt_ratio) + _signed_word(pool.current_tick)
        if selector == _SELECTORS["liquidity"]:
            return _word(POOL_ACTIVE_LIQUIDITY)
        if selector == _SELECTORS["staked_liquidity"]:
            return _word(pool.staked_liquidity)
        if selector == _SELECTORS["fee_growth_global0"]:
            return _word(POOL_FEE_GROWTH_GLOBAL)
        if selector == _SELECTORS["fee_growth_global1"]:
            return _word(POOL_FEE_GROWTH_GLOBAL)
        if selector == _SELECTORS["ticks"]:
            _tick = _arg_signed(data, 0)
            # The Slipstream ten-word Tick.Info; the fee words live at three
            # and four and the boundary stays flat for the day's baseline.
            return "0x" + "".join(_word(word) for word in [1, 0, 0, 0, 0, 0, 0, 0, 0, 1])
        raise AssertionError(f"unexpected pool view {selector}")

    def _sugar_page(self, data: str) -> str:
        limit = _arg_word(data, 0)
        offset = _arg_word(data, 1)
        return encode_sugar_page(self._sugar_records()[offset : offset + limit])

    def _sugar_records(self) -> list[dict[str, Any]]:
        records = []
        for pool in sorted(self.pools.values(), key=lambda item: item.symbol):
            records.append(
                {
                    "lp": pool.pool_address,
                    "symbol": pool.symbol,
                    "decimals": 18,
                    "liquidity": POOL_ACTIVE_LIQUIDITY,
                    "tick_spacing": POOL_TICK_SPACING,
                    "tick": pool.current_tick,
                    "sqrt_ratio": pool.sqrt_ratio,
                    "token0": BASE_USDC_ADDRESS,
                    "reserve0": self.balance(BASE_USDC_ADDRESS, pool.pool_address),
                    "staked0": pool.staked0,
                    "token1": pool.stock_address,
                    "reserve1": self.balance(pool.stock_address, pool.pool_address),
                    "staked1": pool.staked1,
                    "gauge": pool.gauge_address,
                    "gauge_liquidity": pool.staked_liquidity,
                    "gauge_alive": True,
                    "fee": "0x" + "00" * 20,
                    "bribe": "0x" + "00" * 20,
                    "factory": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
                    "emissions": pool.reward_rate_units,
                    "emissions_token": AERO_TOKEN_ADDRESS,
                    "pool_fee": 500,
                    "unstaked_fee": 100_000,
                    "token0_fees": 0,
                    "token1_fees": 0,
                    "nfpm": FIXTURE_NFPM_ADDRESS,
                    "alm": "0x" + "00" * 20,
                    "root": "0x" + "00" * 20,
                }
            )
        records.append(
            {
                # The Slipstream AERO/USDC pool the reward conversion uses.
                "lp": FIXTURE_AERO_SLIPSTREAM_POOL_ADDRESS,
                "symbol": "AERO-USDC",
                "decimals": 18,
                "liquidity": 10**20,
                "tick_spacing": 2000,
                "tick": 0,
                # AERO sorts as token one here, and the squared raw ratio
                # of 2e12 prices one whole AERO at exactly 0.5 USDC.
                "sqrt_ratio": int(Decimal(2).sqrt() * Decimal(10) ** 6 * (1 << 96)),
                "token0": BASE_USDC_ADDRESS,
                "reserve0": 2_000_000_000_000,
                "staked0": 500_000_000_000,
                "token1": AERO_TOKEN_ADDRESS,
                "reserve1": 4_000_000 * 10**18,
                "staked1": 1_000_000 * 10**18,
                "gauge": "0x" + "00" * 20,
                "gauge_liquidity": 0,
                "gauge_alive": False,
                "fee": "0x" + "00" * 20,
                "bribe": "0x" + "00" * 20,
                "factory": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
                "emissions": 0,
                "emissions_token": AERO_TOKEN_ADDRESS,
                "pool_fee": 500,
                "unstaked_fee": 100_000,
                "token0_fees": 0,
                "token1_fees": 0,
                "nfpm": FIXTURE_NFPM_ADDRESS,
                "alm": "0x" + "00" * 20,
                "root": "0x" + "00" * 20,
            }
        )
        return records

    def _estimate_gas(self, calldata: str) -> str:
        if self.estimate_revert_message is not None:
            raise ChainRevertError(self.estimate_revert_message)
        inner = self._inner_calldata(calldata)
        kind = _INNER_SELECTORS.get(inner.hex()[:8])
        if kind is None:
            raise AssertionError(f"unexpected inner selector {inner.hex()[:8]}")
        units = self.estimate_gas_units.get(kind)
        if units is None:
            raise ChainRevertError(f"{kind} refused")
        return hex(units)

    @staticmethod
    def _inner_calldata(calldata: str) -> bytes:
        """Extract the inner call bytes from one Safe execTransaction payload."""
        words = abi_decode(
            [
                "address",
                "uint256",
                "bytes",
                "uint8",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "address",
                "bytes",
            ],
            bytes.fromhex(calldata[10:]),
        )
        return cast("bytes", words[2])

    def _send_raw_transaction(self, raw: str) -> str:
        payload = bytes.fromhex(raw[2:])
        if payload[0] != 2:
            raise AssertionError(f"unexpected transaction type {payload[0]}")
        # EIP-1559 envelope: chainId, nonce, priority, maxFee, gas, to,
        # value, data, accessList, yParity, r, s - the data word is seventh.
        items = rlp.decode(payload[1:])
        to_address = "0x" + bytes(items[5]).hex()
        data = bytes(items[7])
        tx_hash = "0x" + f"{len(self.sent_hashes) + 1:064x}"
        selector = data.hex()[:8]
        if selector != "6a761202":
            raise AssertionError(f"unexpected Safe call {selector}")
        if self.lose_send_ack_kind is not None:
            # The node accepted the transaction: state applies under the
            # keccak hash the executor derives locally from the same signed
            # bytes, but the acknowledgement never reaches the client.
            from eth_utils.crypto import keccak

            ack_hash = "0x" + keccak(payload).hex()
            record = self._apply_safe_execution(to_address, "0x" + data.hex(), ack_hash)
            self.sent_hashes.append(ack_hash)
            if record.kind == self.lose_send_ack_kind:
                self.lose_send_ack_kind = None
                raise ChainFaultError("the broadcast acknowledgement was lost")
            return ack_hash
        self._apply_safe_execution(to_address, "0x" + data.hex(), tx_hash)
        self.sent_hashes.append(tx_hash)
        return tx_hash

    def _apply_safe_execution(self, to: str, data: str, tx_hash: str) -> BroadcastRecord:
        """Apply one Safe execTransaction's inner call as chain state."""
        from aero_bot.domain import normalize_evm_address
        from aero_bot.safe_tx import SafeTransaction, build_safe_transaction

        words = abi_decode(
            [
                "address",
                "uint256",
                "bytes",
                "uint8",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "address",
                "bytes",
            ],
            bytes.fromhex(data[10:]),
        )
        inner = words[2]
        inner_target = normalize_evm_address(str(words[0]))
        # The executed transaction occupies the Safe's CURRENT nonce - the
        # one the executor read before building - so the derived
        # safe_tx_hash matches the executor's own EIP-712 computation.
        occupied_nonce = self.safe_nonce
        self.safe_nonce += 1
        self.block_number += 1
        record = self._apply_inner(inner_target, inner.hex()[:8], "0x" + inner.hex())
        self.broadcasts.append(record)
        # The Safe emits ExecutionSuccess naming the executed call's own
        # safe_tx_hash - derived through the repository's own builder so
        # the recovery's exact-match proof consumes real event shape.
        built = build_safe_transaction(
            SafeTransaction(
                to_address=inner_target,
                data="0x" + inner.hex(),
                nonce=occupied_nonce,
            ),
            FIXTURE_SAFE_ADDRESS,
        )
        safe_tx_hash = built.safe_tx_hash
        logs: list[dict[str, Any]] = [
            {
                "address": FIXTURE_SAFE_ADDRESS,
                "topics": [
                    "0x442e715f626346e8c54381002da614f62bee8d27386535b2521ec8540898556e",
                    safe_tx_hash,
                ],
            }
        ]
        if record.kind == "mint" and record.token_id is not None:
            logs.append(
                {
                    "topics": [
                        NFPM_INCREASE_LIQUIDITY_TOPIC0,
                        "0x" + record.token_id.to_bytes(32, "big").hex(),
                    ]
                }
            )
        self.receipts[tx_hash] = {
            "status": "0x1",
            "to": FIXTURE_SAFE_ADDRESS,
            "from": self._relayer,
            "transactionHash": tx_hash,
            "blockNumber": hex(self.block_number),
            "gasUsed": hex(80_000),
            "effectiveGasPrice": hex(self.gas_price_wei),
            "logs": logs,
        }
        self._cond.notify_all()
        return record

    def _apply_inner(self, to: str, selector: str, calldata: str) -> BroadcastRecord:
        if selector == _INNER_SELECTORS.get("095ea7b3") or selector == "095ea7b3":
            spender = _arg_address(calldata, 0)
            amount = _arg_word(calldata, 1)
            self.allowances[(to.lower(), FIXTURE_SAFE_ADDRESS, spender)] = amount
            return BroadcastRecord("approve", detail=spender)
        if selector == "3593564c":
            # The router's single-command exact-input swap. The layout is
            # fixed by build_swap_calldata: a three-word head, the commands
            # section, the inputs array header, then the params struct whose
            # first three words are recipient, amountIn, and amountOutMinimum
            # with the length-prefixed 43-byte path trailing the struct.
            body = bytes.fromhex(calldata[2:])
            # selector + head (3) + commands len + padded command + inputs
            # len + element offset + element length = eight words after the
            # selector, so the params struct starts at byte 4 + 8*32.
            struct_start = 4 + 8 * 32
            recipient = "0x" + body[struct_start + 12 : struct_start + 32].hex()
            amount_in = int.from_bytes(body[struct_start + 32 : struct_start + 64], "big")
            amount_out_min = int.from_bytes(body[struct_start + 64 : struct_start + 96], "big")
            path_len = int.from_bytes(body[struct_start + 192 : struct_start + 224], "big")
            path = body[struct_start + 224 : struct_start + 224 + path_len]
            input_token = "0x" + path[:20].hex()
            output_token = "0x" + path[-20:].hex()
            self.debit(input_token, FIXTURE_SAFE_ADDRESS, amount_in)
            credited = amount_out_min * 100 // 99
            self.credit(output_token, recipient, credited)
            return BroadcastRecord(
                "swap", detail=f"{input_token}->{output_token}", amount_units=amount_in
            )
        if selector == "a22cb465":
            self.operator_approved = True
            return BroadcastRecord("set_approval_for_all")
        if selector == "b5007d1f":
            token0 = _arg_address(calldata, 0)
            token1 = _arg_address(calldata, 1)
            tick_lower = _arg_signed(calldata, 3)
            tick_upper = _arg_signed(calldata, 4)
            amount0 = _arg_word(calldata, 5)
            amount1 = _arg_word(calldata, 6)
            recipient = _arg_address(calldata, 9)
            pool = next(
                (
                    p
                    for p in self.pools.values()
                    if {token0.lower(), token1.lower()}
                    == {BASE_USDC_ADDRESS.lower(), p.stock_address}
                ),
                None,
            )
            if pool is None:
                raise ChainRevertError("mint: unknown pool pair")
            self.debit(BASE_USDC_ADDRESS, FIXTURE_SAFE_ADDRESS, amount0)
            self.debit(pool.stock_address, FIXTURE_SAFE_ADDRESS, amount1)
            token_id = self.next_token_id
            self.next_token_id += 1
            # Solve the liquidity whose two-sided amounts match the mint's
            # desired amounts, so the status read marks the position at its
            # committed value exactly like the real pool would.
            liquidity = _solve_liquidity_for_amount0(
                pool.sqrt_ratio, tick_lower, tick_upper, amount0
            )
            self.positions[token_id] = ChainPosition(
                token_id=token_id,
                pool=pool,
                owner=recipient,
                liquidity=liquidity,
                tick_lower=tick_lower,
                tick_upper=tick_upper,
                entry_usdc_units=amount0,
                entry_stock_units=amount1,
            )
            return BroadcastRecord("mint", token_id=token_id, detail=pool.symbol)
        if selector == "b6b55f25":
            token_id = _arg_word(calldata, 0)
            position = self.positions.get(token_id)
            if position is None or position.owner != FIXTURE_SAFE_ADDRESS:
                raise ChainRevertError("gauge deposit: not the Safe's token")
            position.owner = position.pool.gauge_address
            position.deposit_timestamp_epoch = int(time.time()) - 1_000
            return BroadcastRecord("stake", token_id=token_id, detail=position.pool.symbol)
        if selector == "2e1a7d4d":
            token_id = _arg_word(calldata, 0)
            position = self.positions.get(token_id)
            if position is None or position.owner != position.pool.gauge_address:
                raise ChainRevertError("gauge withdraw: not staked")
            position.owner = FIXTURE_SAFE_ADDRESS
            # The gauge's withdraw auto-claims accrued emissions.
            self.credit(AERO_TOKEN_ADDRESS, FIXTURE_SAFE_ADDRESS, position.accrued_aero_units)
            position.accrued_aero_units = 0
            return BroadcastRecord("unstake", token_id=token_id, detail=position.pool.symbol)
        if selector == "0c49ccbe":
            token_id = _arg_word(calldata, 0)
            position = self.positions.get(token_id)
            if position is None or position.liquidity <= 0:
                raise ChainRevertError("decrease: empty position")
            position.liquidity = 0
            position.tokens_owed0 += position.entry_usdc_units
            position.tokens_owed1 += position.entry_stock_units
            return BroadcastRecord("decrease_liquidity", token_id=token_id)
        if selector == "fc6f7865":
            token_id = _arg_word(calldata, 0)
            position = self.positions.get(token_id)
            if position is None:
                raise ChainRevertError("collect: unknown token")
            self.credit(BASE_USDC_ADDRESS, FIXTURE_SAFE_ADDRESS, position.tokens_owed0)
            self.credit(position.pool.stock_address, FIXTURE_SAFE_ADDRESS, position.tokens_owed1)
            position.tokens_owed0 = 0
            position.tokens_owed1 = 0
            return BroadcastRecord("collect", token_id=token_id)
        if selector == "42966c68":
            token_id = _arg_word(calldata, 0)
            position = self.positions.get(token_id)
            if position is None or position.owner != FIXTURE_SAFE_ADDRESS:
                raise ChainRevertError("burn: not the Safe's token")
            del self.positions[token_id]
            return BroadcastRecord("burn", token_id=token_id)
        if selector == "1c4b774b":
            token_id = _arg_word(calldata, 0)
            position = self.positions.get(token_id)
            if position is None:
                raise ChainRevertError("getReward: unknown token")
            self.credit(AERO_TOKEN_ADDRESS, FIXTURE_SAFE_ADDRESS, position.accrued_aero_units)
            claimed = position.accrued_aero_units
            position.accrued_aero_units = 0
            return BroadcastRecord("claim_rewards", token_id=token_id, amount_units=claimed)
        raise AssertionError(f"unexpected inner selector {selector}")


def make_board_pools(
    aprs: tuple[Decimal, ...] = (Decimal("3.4"), Decimal("2.0"), Decimal("1.6"), Decimal("1.5")),
) -> list[FixturePool]:
    """Build the three-name board at the given qualifying APRs."""
    stocks = load_registry_stocks()
    pools = [
        FixturePool(
            symbol="MSTRc",
            stock_address=stocks["MSTRc"],
            pool_address="0x" + "a1" * 20,
            gauge_address="0x" + "b1" * 20,
            reward_rate_units=0,
        ),
        FixturePool(
            symbol="SNDKc",
            stock_address=stocks["SNDKc"],
            pool_address="0x" + "a2" * 20,
            gauge_address="0x" + "b2" * 20,
            reward_rate_units=0,
        ),
        FixturePool(
            symbol="TSLAc",
            stock_address=stocks["TSLAc"],
            pool_address="0x" + "a3" * 20,
            gauge_address="0x" + "b3" * 20,
            reward_rate_units=0,
        ),
        # The switch target: qualified but out of the entry band at first,
        # so a later reward-rate bump drives a reallocation exit-before-entry.
        FixturePool(
            symbol="GOOGLc",
            stock_address=stocks["GOOGLc"],
            pool_address="0x" + "a4" * 20,
            gauge_address="0x" + "b4" * 20,
            reward_rate_units=0,
        ),
    ]
    # A shared staked value keeps the APR ordering exactly as configured.
    for pool, apr in zip(pools, aprs, strict=True):
        pool.calibrate_staked_value(Decimal("10000"))
        pool.reward_rate_units = pool.reward_rate_for_apr(apr, FIXTURE_AERO_PRICE_USDC)
    return pools


class LoopbackRpcServer:
    """Serve the faithful chain over a real loopback HTTP endpoint."""

    def __init__(self, chain: FaithfulChain) -> None:
        """Bind the server to the faithful chain it serves.

        Args:
            chain: The stateful chain answering every request.
        """
        self.chain = chain
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the stdlib contract
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length))
                    if outer.chain.http_status_queue:
                        status = outer.chain.http_status_queue.pop(0)
                        if status != 200:
                            self.send_response(status)
                            self.end_headers()
                            return
                    result = outer.chain.dispatch(str(body["method"]), list(body["params"]))
                    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
                except ChainRevertError as revert:
                    payload = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "error": {
                                "code": 3,
                                "message": f"execution reverted: {revert}",
                            },
                        }
                    )
                except ChainFaultError as fault:
                    payload = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "error": {"code": -32005, "message": str(fault)},
                        }
                    )
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(payload.encode())
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN401 - the stdlib's own contract
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        """The loopback endpoint URL the cycle's settings point at."""
        host, port = self._httpd.server_address[:2]
        return f"http://{host.decode() if isinstance(host, bytes) else host}:{port}"

    def start(self) -> None:
        """Start serving on the loopback socket."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and join the server thread."""
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


CYCLE_RUNNER_SHIM = """\
import sys

import aero_bot.executor as executor_module
import aero_bot.lp_executor as lp_module

# The politeness pacing exists for public endpoints; the loopback fixture
# serves instantly, so tests pin it to zero. Every other behavior - the
# real backends, the real retry ladder, the real CLI assembly - is stock.
executor_module.REQUEST_PACING_SECONDS = 0.0

# Keep every fallback receipt endpoint on the isolated loopback fixture so
# the bounded retry ladder can rotate without ever touching a public node.
lp_module.EXECUTE_RECEIPT_ENDPOINT_URLS = ({url!r},)

from aero_bot import cycle

sys.argv[0] = "aero-bot-cycle"
raise SystemExit(cycle.main())
"""


class CycleHarness:
    """Drive the real cycle CLI against the loopback chain, isolated."""

    def __init__(
        self,
        tmp_path: Path,
        chain: FaithfulChain,
        server: LoopbackRpcServer,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        """Bind subprocess execution to one isolated chain, audit store, and state file."""
        self.chain = chain
        self.server = server
        self.tmp_path = tmp_path
        self.audit_path = tmp_path / "audit.sqlite3"
        self.state_path = tmp_path / "cycle_state.json"
        self.pins_path = tmp_path / "lp_pool_pins.json"
        self.runner_path = tmp_path / "cycle_runner_shim.py"
        self.runner_path.write_text(CYCLE_RUNNER_SHIM.format(url=server.url), encoding="utf-8")
        self.base_env = {
            "AERO_BOT_BASE_RPC_URL": server.url,
            "AERO_BOT_LP_POOL_PINS_PATH": str(self.pins_path),
            CYCLE_STATE_PATH_ENV: str(self.state_path),
            "AERO_BOT_KEY_SOURCE": "env",
            "AERO_BOT_SIGNING_KEY_HEX": FIXTURE_SIGNING_KEY_HEX,
            "AERO_BOT_SAFE_ADDRESS": FIXTURE_SAFE_ADDRESS,
            "AERO_BOT_RELAYER_ADDRESS": self._relayer_address(),
            **(extra_env or {}),
        }

    @staticmethod
    def _relayer_address() -> str:
        """The fixture key's public address; a public fact, never a secret."""
        from eth_account import Account

        return str(Account.from_key(bytes.fromhex(FIXTURE_SIGNING_KEY_HEX)).address)

    def run(
        self,
        args: list[str],
        *,
        timeout: float = 300.0,
        extra_env: dict[str, str] | None = None,
        barrier_kill_after: Callable[[BroadcastRecord], bool] | None = None,
    ) -> tuple[int, dict[str, Any] | None, str]:
        """Run one cycle subprocess, optionally killed at a trigger.

        Args:
            args: The CLI arguments, ``--json`` first for a parseable report.
            timeout: Wall-clock bound on the subprocess.
            barrier_kill_after: Deterministic boundary kill - freeze the
                fixture's next RPC after the matching broadcast applies,
                SIGTERM the parked process at exactly that confirmed
                boundary, then release the chain for later cycles.
            extra_env: Additional sealed-environment overrides for this
                run, like the reward posture under test.

        Returns:
            The exit code (negative when terminated by signal), the parsed
            JSON report when stdout carried one, and the combined stderr.
        """
        env = {
            **os.environ,
            "AERO_BOT_AUDIT_DATABASE_PATH": str(self.audit_path),
            **self.base_env,
            **(extra_env or {}),
        }
        env.pop("AERO_BOT_ALERT_PROVIDER", None)
        if barrier_kill_after is not None:
            self.chain.arm_barrier(barrier_kill_after)
        process = subprocess.Popen(  # noqa: S603 - the suite's own shim binary
            [sys.executable, str(self.runner_path), *args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        killers: list[threading.Thread] = []
        if barrier_kill_after is not None:
            chain = self.chain

            def watch_barrier() -> None:
                if not chain.barrier_engaged.wait(timeout=timeout):
                    return
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                # Give the dying process a moment, then release the parked
                # request so the server thread finishes cleanly.
                process.wait(timeout=30)
                chain.release_barrier()

            thread = threading.Thread(target=watch_barrier, daemon=True)
            thread.start()
            killers.append(thread)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise
        for thread in killers:
            thread.join(timeout=5)
        report: dict[str, Any] | None = None
        if stdout.strip():
            try:
                report = json.loads(stdout)
            except ValueError:
                report = None
        return process.returncode, report, stderr

    def load_book(self) -> dict[str, Any]:
        """The persisted cycle book exactly as the last cycle left it."""
        return cast("dict[str, Any]", json.loads(self.state_path.read_text(encoding="utf-8")))

    def snapshot_book(self) -> str:
        """Capture the persisted book for later incident restoration."""
        return self.state_path.read_text(encoding="utf-8") if self.state_path.exists() else ""

    def restore_book(self, snapshot: str) -> None:
        """Restore one captured book, simulating a checkpoint that never saved."""
        if snapshot:
            self.state_path.write_text(snapshot, encoding="utf-8")
        elif self.state_path.exists():
            self.state_path.unlink()

    def reference_args(self) -> list[str]:
        """Per-symbol reference quotes matching each pool's AMM price."""
        from aero_bot.history import price_usdc_per_stock

        stocks = load_registry_stocks()
        amm_price = price_usdc_per_stock(POOL_SQRT_RATIO, False, 8, 6)
        price_text = format(amm_price, "f")
        pairs = ",".join(f"{symbol}={price_text}" for symbol in stocks)
        return ["--reference-price", pairs, "--reference-age-seconds", "0"]


@pytest.fixture
def board_world(
    tmp_path: Path,
) -> Generator[tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]]:
    """The 500-USDC boundary board over loopback RPC.

    This is the exact scale whose final sibling originally refused on the
    accumulated acquisition buffers (needs 148.222222 against 147.972223
    held): the executable-cost reserve now funds every in-band tier.
    """
    chain = FaithfulChain(make_board_pools(), safe_usdc_units=500_000_000)
    server = LoopbackRpcServer(chain)
    server.start()
    harness = CycleHarness(tmp_path, chain, server)
    try:
        yield harness, chain, server
    finally:
        server.stop()


@pytest.fixture
def permanent_book_world(
    tmp_path: Path,
) -> Generator[tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]]:
    """The roughly-100-USDC permanent book at the live morning's APRs."""
    chain = FaithfulChain(
        make_board_pools(
            aprs=(Decimal("208.12"), Decimal("153.58"), Decimal("100.0"), Decimal("1.5"))
        ),
        safe_usdc_units=102_263_507,
    )
    server = LoopbackRpcServer(chain)
    server.start()
    harness = CycleHarness(tmp_path, chain, server)
    try:
        yield harness, chain, server
    finally:
        server.stop()


class TestUnattendedEntryCycles:
    """The entry side of unattended operation, end to end."""

    def test_full_entry_cycle_deploys_and_repeat_cycles_hold(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A green multi-entry cycle deploys across the board; reruns hold."""
        harness, chain, _server = board_world

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])

        assert exit_code == EXIT_OK, stderr
        assert report is not None
        assert report["halted_reason"] == "", report["halted_reason"]
        deployed = len(chain.staked_token_ids())
        assert deployed >= 2, f"expected a genuine multi-position book, got {deployed}"
        book = harness.load_book()
        assert {position["token_id"] for position in book["positions"]} == set(
            chain.staked_token_ids()
        )

        # The idempotent rerun: the funded book holds, nothing re-mints.
        mints_before = chain.count_broadcasts("mint")
        exit_code, repeat, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert repeat is not None
        assert repeat["halted_reason"] == ""
        assert chain.count_broadcasts("mint") == mints_before

    def test_the_permanent_book_deploys_and_reruns_hold(
        self, permanent_book_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """The roughly-100-USDC permanent book funds its tiers and holds."""
        harness, chain, _server = permanent_book_world

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])

        assert exit_code == EXIT_OK, stderr
        assert report is not None
        assert report["halted_reason"] == "", report["halted_reason"]
        deployed = len(chain.staked_token_ids())
        assert deployed >= 2
        # The idempotent rerun mints nothing new over the funded book.
        mints_before = chain.count_broadcasts("mint")
        exit_code, repeat, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert repeat is not None
        assert repeat["halted_reason"] == ""
        assert chain.count_broadcasts("mint") == mints_before


class TestEntrySideInterruptions:
    """SIGTERM at confirmed entry boundaries, restarted from persisted state."""

    def test_kill_after_the_first_stake_recovers_the_incident_book(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """The 2026-09-30 incident shape: the checkpoint never landed.

        The service dies right after the first sibling's gauge deposit but
        the persisted book still carries nothing - restored to the
        pre-cycle snapshot, exactly the state the timeout left behind. The
        restart must adopt the sibling through audit-proven custody
        recovery, price it into equity, deploy the remaining names, and
        never latch a phantom loss.
        """
        harness, chain, _server = board_world
        pre_cycle_book = harness.snapshot_book()

        exit_code, _report, _stderr = harness.run(
            ["--json", *harness.reference_args()],
            barrier_kill_after=lambda record: record.kind == "stake",
        )
        # The parked process was terminated by SIGTERM at the boundary.
        assert exit_code == -signal.SIGTERM, exit_code
        staked = chain.staked_token_ids()
        assert len(staked) == 1  # exactly one sibling completed before the kill

        # The incident: the checkpoint save never happened. Restore the
        # pre-cycle book so the restart must prove the sibling from the
        # audit chain instead of reading the checkpoint.
        harness.restore_book(pre_cycle_book)

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert report is not None
        assert report["halted_reason"] == "", report["halted_reason"]
        decision = report.get("decision_reconciliation") or {}
        recovered = decision.get("recovered_positions") or []
        assert any(row["token_id"] == staked[0] for row in recovered), recovered

        book = harness.load_book()
        token_ids = {position["token_id"] for position in book["positions"]}
        assert staked[0] in token_ids
        assert token_ids == set(chain.staked_token_ids())
        # No phantom loss latch: the recovery priced the sibling before
        # the equity observation, so nothing reads as a drawdown.
        assert book["halted_day"] is None
        # Exactly-once: the recovered sibling was staked once and never
        # re-minted.
        assert chain.count_broadcasts("stake", token_id=staked[0]) == 1
        assert chain.count_broadcasts("mint") == len(chain.staked_token_ids())

    def test_kill_between_mint_and_stake_adopts_without_reming(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A crashed entry: the minted NFT sits unstaked at the Safe."""
        harness, chain, _server = board_world
        pre_cycle_book = harness.snapshot_book()

        exit_code, _report, _stderr = harness.run(
            ["--json", *harness.reference_args()],
            barrier_kill_after=lambda record: record.kind == "mint",
        )
        assert exit_code == -signal.SIGTERM, exit_code
        minted = chain.safe_token_ids()
        assert len(minted) == 1  # minted, never staked

        # The crashed-entry rule: adoption only through the audit chain's
        # own linked mint evidence; the restored book forces that path.
        harness.restore_book(pre_cycle_book)

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert report is not None
        assert report["halted_reason"] == "", report["halted_reason"]
        # The orphaned NFT was adopted and tracked - not re-minted - and the
        # allocator deployed the remaining names around it. The unstaked
        # adoption rides the HOLD-gated stake-recovery pass, so one more
        # cycle stakes it exactly once.
        assert chain.count_broadcasts("mint") == len(chain.staked_token_ids()) + len(
            chain.safe_token_ids()
        )
        assert minted[0] in chain.safe_token_ids()
        exit_code, followup, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert followup is not None
        assert followup["halted_reason"] == ""
        assert chain.count_broadcasts("stake", token_id=minted[0]) == 1
        assert minted[0] in chain.staked_token_ids()

    def test_a_real_loss_survives_the_recovery(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A genuine drawdown re-latches after recovery clears a phantom."""
        harness, chain, _server = board_world

        # Seed one green cycle: the day anchor forms at healthy marks and
        # the board deploys across its tiers.
        exit_code, seeded, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert seeded is not None and seeded["halted_reason"] == ""
        staked_before = chain.staked_token_ids()
        assert len(staked_before) >= 2
        incident_book = json.loads(harness.snapshot_book())

        # The incident shape: the persisted book loses one deployed
        # sibling (the timeout's missing checkpoint) while keeping the
        # healthy day facts beside the survivor, and the pools' prices
        # collapse below the funded ranges - stock is token one, so a
        # higher raw tick is a LOWER USDC-per-stock price - making the
        # recovered marks read a genuine drawdown.
        dropped = staked_before[-1:]
        incident_book["positions"] = [
            position
            for position in incident_book["positions"]
            if position["token_id"] not in dropped
        ]
        harness.state_path.write_text(json.dumps(incident_book), encoding="utf-8")
        with chain._cond:  # noqa: SLF001 - fixture state, not production
            for pool in chain.pools.values():
                pool.current_tick = POOL_CURRENT_TICK + 600
                pool.sqrt_ratio = int(_sqrt_price_at_tick(pool.current_tick))

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert report is not None
        # The siblings still adopt through the audit proof - and the real
        # drawdown against the healthy anchor latches the halt.
        decision = report.get("decision_reconciliation") or {}
        recovered = decision.get("recovered_positions") or []
        assert {row["token_id"] for row in recovered} == set(dropped), recovered
        assert any(
            "gate daily_loss_halt: FAIL" in line for line in report["decision_diagnostics"]
        ), report["decision_diagnostics"]
        assert harness.load_book()["halted_day"] is not None

    def test_incomplete_journal_evidence_never_adopts(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A stake plan without its linked mint budget adopts nothing.

        The recovery pairs every stake plan with the newest preceding
        execute-mode mint budget as its committed basis; a plan without
        that basis is incomplete journal evidence, and nothing - not the
        custody, not the plan alone - may adopt from it.
        """
        harness, chain, _server = board_world
        from pydantic import BaseModel

        class StakePlanSeed(BaseModel):
            """The stake-plan audit shape the recovery reader consumes."""

            mode: str = "execute"
            symbol: str
            token_id: int
            token_owner_address: str = FIXTURE_SAFE_ADDRESS

        unproven_token = 7_309_001
        mstrc = next(pool for pool in chain.pools.values() if pool.symbol == "MSTRc")
        chain.positions[unproven_token] = ChainPosition(
            token_id=unproven_token,
            pool=mstrc,
            owner=mstrc.gauge_address,
            liquidity=10**14,
            tick_lower=-40,
            tick_upper=20,
        )
        harness.audit_path.parent.mkdir(parents=True, exist_ok=True)
        store = AuditStore(harness.audit_path)
        store.append(
            AuditEventType.LP_STAKE_PLANNED,
            StakePlanSeed(symbol="MSTRc", token_id=unproven_token),
            datetime.now(UTC),
        )

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])

        decision = (report or {}).get("decision_reconciliation") or {}
        recovered = decision.get("recovered_positions") or []
        assert all(row["token_id"] != unproven_token for row in recovered), recovered
        book = harness.load_book()
        assert unproven_token not in {position["token_id"] for position in book["positions"]}
        # The unproven sibling never prices into equity: the observation
        # cannot count custody it cannot attribute.
        equity = (report or {}).get("equity_usd")
        assert equity is not None and Decimal(equity) < Decimal("500.001")

    def test_an_unproven_safe_held_nft_refuses_rather_than_adopts(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A live untracked NFT with no linked evidence refuses the cycle."""
        harness, chain, _server = board_world
        unproven_token = 7_309_002
        mstrc = next(pool for pool in chain.pools.values() if pool.symbol == "MSTRc")
        chain.positions[unproven_token] = ChainPosition(
            token_id=unproven_token,
            pool=mstrc,
            owner=FIXTURE_SAFE_ADDRESS,
            liquidity=10**14,
            tick_lower=-40,
            tick_upper=20,
        )

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])

        # Fail-closed: no audit evidence proves the NFT is ours, so the
        # cycle refuses out-of-band instead of adopting a stray position.
        assert exit_code != EXIT_OK, stderr
        assert (report or {}).get("halted_reason", "") != ""


def chain_mint_of_googlc(chain: FaithfulChain) -> int:
    """The one NFT minted into the GOOGLc pool, for exactly-once checks."""
    mints = [record.token_id for record in chain.broadcasts if record.kind == "mint"]
    googlc = [
        token
        for token in mints
        if token is not None and chain.positions[token].pool.symbol == "GOOGLc"
    ]
    assert len(googlc) == 1, googlc
    return googlc[0]


class TestExitSideInterruptions:
    """SIGTERM inside the exit prefix of a reallocation switch."""

    @staticmethod
    def _seed_funded_book(
        harness: CycleHarness,
    ) -> None:
        """Run one green entry cycle over the four-name board.

        The seeded positions' entry stamps are then backdated past the
        reallocation minimum-hold window, the way a book that has actually
        held its tiers for hours reads, so the armed switch is judged on
        its economics alone.
        """
        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert report is not None and report["halted_reason"] == ""
        book = json.loads(harness.state_path.read_text(encoding="utf-8"))
        for position in book["positions"]:
            entered = datetime.fromisoformat(position["entered_at"].replace("Z", "+00:00"))
            position["entered_at"] = (
                (entered - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
            )
        harness.state_path.write_text(json.dumps(book), encoding="utf-8")

    @staticmethod
    def _arm_switch(chain: FaithfulChain) -> None:
        """Bump the switch target far above the held bottom name."""
        target = next(pool for pool in chain.pools.values() if pool.symbol == "GOOGLc")
        target.reward_rate_units = target.reward_rate_for_apr(
            Decimal("30.0"), FIXTURE_AERO_PRICE_USDC
        )

    def test_kill_after_unstake_completes_the_exit_exactly_once(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A crash between unstake and withdraw leaves custody truth to heal."""
        harness, chain, _server = board_world
        self._seed_funded_book(harness)
        self._arm_switch(chain)
        held_before = sorted(chain.staked_token_ids())

        exit_code, _report, _stderr = harness.run(
            ["--json", *harness.reference_args()],
            barrier_kill_after=lambda record: record.kind == "unstake",
        )
        assert exit_code == -signal.SIGTERM, exit_code
        unstaked = [token for token in held_before if token in chain.safe_token_ids()]
        assert len(unstaked) == 1
        exited_token = unstaked[0]
        assert chain.count_broadcasts("unstake", token_id=exited_token) == 1

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert report is not None
        assert report["halted_reason"] == "", report["halted_reason"]
        # Exactly-once: the unstake never repeats, the position exits fully
        # (zero liquidity, zero owed, its value back in USDC), the book's
        # crashed-exit heal drops the emptied NFT, and the switch target
        # entered.
        assert chain.count_broadcasts("unstake", token_id=exited_token) == 1
        exited_position = chain.positions.get(exited_token)
        assert exited_position is not None  # the empty NFT rests at the Safe
        assert exited_position.liquidity == 0
        assert exited_position.tokens_owed0 == 0 and exited_position.tokens_owed1 == 0
        book = harness.load_book()
        assert exited_token not in {position["token_id"] for position in book["positions"]}
        googlc = [position for position in book["positions"] if position["symbol"] == "GOOGLc"]
        assert googlc, book["positions"]

    def test_kill_after_the_withdraw_leg_completes_the_exit(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A crash after decrease-and-collect leaves an empty tracked NFT."""
        harness, chain, _server = board_world
        self._seed_funded_book(harness)
        self._arm_switch(chain)

        exit_code, _report, _stderr = harness.run(
            ["--json", *harness.reference_args()],
            barrier_kill_after=lambda record: record.kind == "collect",
        )
        assert exit_code == -signal.SIGTERM, exit_code

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert report is not None
        assert report["halted_reason"] == "", report["halted_reason"]
        # The exit completed exactly once per exited position: every
        # Safe-held NFT is fully emptied (zero liquidity, zero owed, its
        # value back in USDC), each exited token collected exactly once,
        # the switch target minted exactly once (no duplicate funded NFT),
        # and the book equals the staked custody.
        for token in chain.safe_token_ids():
            position = chain.positions[token]
            assert position.liquidity == 0, (token, position)
            assert position.tokens_owed0 == 0 and position.tokens_owed1 == 0
            assert chain.count_broadcasts("collect", token_id=token) == 1
        assert chain.count_broadcasts("mint", token_id=chain_mint_of_googlc(chain)) == 1
        book = harness.load_book()
        assert {position["token_id"] for position in book["positions"]} == set(
            chain.staked_token_ids()
        )


class TestRpcFaultRecovery:
    """Rate-limit storms and endpoint refusals, fail-closed then clean."""

    def test_a_429_storm_fails_closed_without_broadcasting(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """Every read answers 429: the cycle fails, nothing is signed."""
        harness, chain, _server = board_world
        chain.http_status_queue = [429] * 200
        broadcasts_before = len(chain.broadcasts)

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])

        assert exit_code != EXIT_OK
        assert len(chain.broadcasts) == broadcasts_before
        # The bounded retry ladder fought the storm honestly.
        assert "HTTP status 429" in stderr

        # The storm clears and the very next cycle runs clean.
        chain.http_status_queue = []
        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert (report or {}).get("halted_reason", "") == ""

    def test_a_mid_act_refusal_halts_and_the_retry_recovers(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """An in-contract revert mid-act halts the cycle fail-closed."""
        harness, chain, _server = board_world
        # The mint's fresh estimate reverts like a filled pool would: the
        # executor refuses before any broadcast for that step.
        original = chain.estimate_gas_units["mint"]
        chain.estimate_gas_units["mint"] = None
        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code != EXIT_OK
        assert (report or {}).get("halted_reason", "") != ""
        chain.estimate_gas_units["mint"] = original

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert (report or {}).get("halted_reason", "") == ""


class TestAuditLockContention:
    """A contended audit store survives through its bounded retry."""

    def test_a_transient_writer_lock_backs_off_and_the_cycle_completes(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A concurrent writer holds the store; the append retries through."""
        harness, chain, _server = board_world
        import sqlite3

        release = threading.Event()

        def hold_then_release() -> None:
            # One thread owns the whole holder lifecycle: SQLite refuses
            # cross-thread connection use.
            holder = sqlite3.connect(harness.audit_path, timeout=30)
            holder.execute("BEGIN IMMEDIATE")
            time.sleep(2.0)  # inside the store's 0.5+1+2s backoff schedule
            holder.execute("ROLLBACK")
            holder.close()
            release.set()

        threading.Thread(target=hold_then_release, daemon=True).start()
        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        release.wait(timeout=10)
        assert exit_code == EXIT_OK, stderr
        assert (report or {}).get("halted_reason", "") == ""


class TestRewardRetention:
    """The retain posture: claims keep running, the conversion never fires."""

    @staticmethod
    def _seed_with_accrued_rewards(harness: CycleHarness, chain: FaithfulChain) -> int:
        """Run one green entry cycle, then accrue AERO past the threshold."""
        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        assert (report or {}).get("halted_reason", "") == ""
        # Forty AERO across the staked positions: twenty USDC at the fixture
        # price, four times the five-USDC conversion threshold.
        per_position = 40 * 10**18 // max(1, len(chain.staked_token_ids()))
        chain.accrue_rewards(per_position)
        return len([record for record in chain.broadcasts if record.kind == "swap"])

    def test_retain_claims_but_never_swaps_across_restarts(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """Under retain, claims run and the AERO stays in the Safe."""
        harness, chain, _server = board_world
        seed_swaps = self._seed_with_accrued_rewards(harness, chain)

        exit_code, report, stderr = harness.run(
            ["--json", *harness.reference_args()],
            extra_env={CYCLE_REWARD_POSTURE_ENV: "retain"},
        )
        assert exit_code == EXIT_OK, stderr
        assert (report or {}).get("halted_reason", "") == ""
        # The claim swept the earned rewards into the Safe...
        assert chain.count_broadcasts("claim_rewards") >= 1
        aero_units = chain.balance(AERO_TOKEN_ADDRESS, FIXTURE_SAFE_ADDRESS)
        assert aero_units >= 39 * 10**18, aero_units
        # ...but no NEW swap fired after the seed's entry balancing legs.
        assert chain.count_broadcasts("swap") == seed_swaps
        attribution = (report or {}).get("yield_attribution") or {}
        assert attribution.get("aero_rewards_units") is not None
        assert attribution.get("aero_rewards_usdc") is not None

        # A restart under the same sealed posture still never converts -
        # the known-failing aero-swap surface cannot trip a restart.
        chain.accrue_rewards(10 * 10**18)
        exit_code, report, stderr = harness.run(
            ["--json", *harness.reference_args()],
            extra_env={CYCLE_REWARD_POSTURE_ENV: "retain"},
        )
        assert exit_code == EXIT_OK, stderr
        assert chain.count_broadcasts("swap") == seed_swaps

    def test_convert_still_swaps_when_the_posture_allows_it(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """The default posture keeps the conversion path alive end to end."""
        harness, chain, _server = board_world
        seed_swaps = self._seed_with_accrued_rewards(harness, chain)

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
        assert exit_code == EXIT_OK, stderr
        swaps = [record for record in chain.broadcasts[seed_swaps:] if record.kind == "swap"]
        assert any(AERO_TOKEN_ADDRESS.lower() in record.detail for record in swaps)
        assert chain.balance(AERO_TOKEN_ADDRESS, FIXTURE_SAFE_ADDRESS) == 0


class TestGasGuardIntegrity:
    """The production gas guards refuse what the fixture never loosened."""

    def test_an_above_ceiling_gas_price_never_broadcasts(
        self, board_world: tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]
    ) -> None:
        """A 0.6-gwei endpoint prices above the 0.5-gwei cap: no entry."""
        harness, chain, _server = board_world
        chain.gas_price_wei = 600_000_000

        exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])

        assert exit_code == EXIT_OK  # a hold is a decision, not a failure
        assert (report or {}).get("halted_reason", "") == ""
        assert chain.count_broadcasts("mint") == 0
        assert any(
            "gate gas_ceiling: FAIL" in line
            for line in (report or {}).get("decision_diagnostics", [])
        )


class TestHealerNegativeControls:
    """The sent-delivery healer grants success only to full proof."""

    @staticmethod
    def _gap_world(
        tmp_path: Path,
        *,
        receipt_mutator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,  # noqa: ANN401 - the mutator owns its shape
        chain_id: int | None = None,
        strip_success_event: bool = False,
        role_override: str | None = None,
    ) -> tuple[CycleHarness, FaithfulChain, LoopbackRpcServer]:
        """Seed one sent-but-unconfirmed mint delivery over the fixture."""
        chain = FaithfulChain(make_board_pools(), safe_usdc_units=500_000_000)
        server = LoopbackRpcServer(chain)
        server.start()
        harness = CycleHarness(tmp_path, chain, server)
        # Kill between the mint's send and its receipt row.
        harness.run(
            ["--json", *harness.reference_args()],
            barrier_kill_after=lambda record: record.kind == "mint",
        )
        if receipt_mutator is not None:
            for tx_hash in list(chain.receipts):
                chain.receipts[tx_hash] = receipt_mutator(chain.receipts[tx_hash])
        if strip_success_event:
            for receipt in chain.receipts.values():
                receipt["logs"] = [{"topics": ["0x" + "99" * 32]}]
        if chain_id is not None:
            chain.chain_id_override = chain_id
        if role_override is not None:
            _rewrite_last_sent_role(harness, role_override)
        return harness, chain, server

    def test_the_gap_heals_only_with_full_proof(self, tmp_path: Path) -> None:
        """The unmutated gap heals: confirmed row, adoption, no re-mint."""
        harness, chain, server = self._gap_world(tmp_path)
        try:
            exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code == EXIT_OK, stderr
            assert (report or {}).get("halted_reason", "") == ""
            assert chain.count_broadcasts("mint") == len(chain.staked_token_ids()) + len(
                chain.safe_token_ids()
            )
        finally:
            server.stop()

    def test_a_lost_acknowledgement_mint_gap_heals_the_same_way(self, tmp_path: Path) -> None:
        """The broadcast-unknown sibling row heals from its on-chain receipt.

        The node accepts the mint but the acknowledgement is lost, the
        process dies during the bounded receipt wait, and the journal holds
        only the broadcast-unknown row: the restart must still prove the
        delivery, adopt its custody exactly once, and never re-enter the
        adopted symbol.
        """
        chain = FaithfulChain(make_board_pools(), safe_usdc_units=500_000_000)
        server = LoopbackRpcServer(chain)
        server.start()
        harness = CycleHarness(tmp_path, chain, server)
        try:
            chain.lose_send_ack_kind = "mint"
            exit_code, _report, _stderr = harness.run(
                ["--json", *harness.reference_args()],
                barrier_kill_after=lambda record: record.kind == "mint",
            )
            assert exit_code == -signal.SIGTERM, exit_code
            minted = chain.safe_token_ids()
            assert len(minted) == 1
            # The faithful gap shape: exactly one broadcast-unknown send row
            # for the mint, and no receipt row for its hash anywhere.
            rows = (
                sqlite3.connect(harness.audit_path)
                .execute("SELECT event_type, payload_json FROM audit_records ORDER BY sequence ASC")
                .fetchall()
            )
            unknown = [
                json.loads(payload)
                for event_type, payload in rows
                if event_type == "lp_execute_broadcast_unknown"
            ]
            assert len(unknown) == 1, unknown
            assert unknown[0]["action"] == "mint"
            gap_hash = unknown[0]["transaction_hash"]
            assert not any(
                event_type in ("lp_execute_confirmed", "lp_execute_failed") and gap_hash in payload
                for event_type, payload in rows
            ), "a receipted row would mask the broadcast-unknown gap"

            # The restart heals the accepted delivery from its own receipt
            # and adopts its custody exactly once.
            exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code == EXIT_OK, stderr
            assert (report or {}).get("halted_reason", "") == ""
            healed = (
                sqlite3.connect(harness.audit_path)
                .execute(
                    "SELECT COUNT(*) FROM audit_records WHERE event_type = "
                    "'lp_execute_confirmed' AND payload_json LIKE ?",
                    (f"%{gap_hash}%",),
                )
                .fetchone()[0]
            )
            assert healed == 1, "the heal never proved the broadcast-unknown delivery"
            book = harness.load_book()
            adopted = next(
                position for position in book["positions"] if position["token_id"] == minted[0]
            )
            assert (
                sum(
                    1
                    for record in chain.broadcasts
                    if record.kind == "mint" and record.detail == adopted["symbol"]
                )
                == 1
            ), "the adopted symbol was re-entered"
            assert minted[0] in chain.safe_token_ids()

            # The unstaked adoption rides the stake-recovery pass: one more
            # cycle stakes it exactly once.
            exit_code, followup, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code == EXIT_OK, stderr
            assert (followup or {}).get("halted_reason", "") == ""
            assert chain.count_broadcasts("stake", token_id=minted[0]) == 1
            assert minted[0] in chain.staked_token_ids()
        finally:
            server.stop()

    def test_a_foreign_receipt_target_never_gains_provenance(self, tmp_path: Path) -> None:
        """A receipt addressed to another contract heals nothing."""

        def foreign(receipt: dict[str, Any]) -> dict[str, Any]:
            receipt["to"] = "0x" + "77" * 20
            return receipt

        harness, chain, server = self._gap_world(tmp_path, receipt_mutator=foreign)
        try:
            exit_code, report, _stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code != EXIT_OK
            assert chain.count_broadcasts("mint") == 1  # only the killed cycle's mint
        finally:
            server.stop()

    def test_an_absent_safe_success_event_never_confirms(self, tmp_path: Path) -> None:
        """Outer status one without the Safe's own success event fails."""
        harness, chain, server = self._gap_world(tmp_path, strip_success_event=True)
        try:
            exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code != EXIT_OK
            assert chain.count_broadcasts("mint") == 1
        finally:
            server.stop()

    def test_a_wrong_chain_endpoint_refuses_with_zero_heals(self, tmp_path: Path) -> None:
        """A foreign-chain endpoint appends nothing and refuses."""
        harness, chain, server = self._gap_world(tmp_path, chain_id=11155111)
        try:
            import sqlite3

            rows_before = (
                sqlite3.connect(harness.audit_path)
                .execute("SELECT COUNT(*) FROM audit_records")
                .fetchone()[0]
            )
            exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code != EXIT_OK
            rows_after = (
                sqlite3.connect(harness.audit_path)
                .execute("SELECT COUNT(*) FROM audit_records")
                .fetchone()[0]
            )
            assert rows_after == rows_before, (rows_before, rows_after)
        finally:
            server.stop()

    def test_a_corrupt_role_refuses_rather_than_crashing(self, tmp_path: Path) -> None:
        """A corrupted role field refuses the cycle with a typed message."""
        harness, chain, server = self._gap_world(tmp_path, role_override="not_a_role")
        try:
            exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code != EXIT_OK
            assert "corrupt audit evidence" in stderr
        finally:
            server.stop()


class TestAdoptionStatusReadDiscipline:
    """The adoption fold's status read is a reconcile-grade read."""

    def test_a_refused_adopted_status_read_halts_rather_than_reenters(self, tmp_path: Path) -> None:
        """A status-read refusal during adoption fails the cycle for retry.

        The adopted position's status read refuses while everything around
        it stays healthy: the cycle must halt before decide - never proceed
        with a book-only adoption the allocator would treat as unheld and
        re-enter - and the next tick's retry adopts through the same
        evidence.
        """
        harness, chain, server = TestHealerNegativeControls._gap_world(tmp_path)
        try:
            chain.fail_aero_price_reads = 1
            mints_before = chain.count_broadcasts("mint")
            exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code != EXIT_OK
            assert "cycle failed" in stderr
            assert "the live AERO price read" in stderr
            assert chain.count_broadcasts("mint") == mints_before

            exit_code, report, stderr = harness.run(["--json", *harness.reference_args()])
            assert exit_code == EXIT_OK, stderr
            assert (report or {}).get("halted_reason", "") == ""
        finally:
            server.stop()


def _rewrite_last_sent_role(harness: CycleHarness, role: str) -> None:
    """Rebuild the journal with the unresolved mint send row's role forged.

    The store's append-only trigger blocks direct edits, so the control
    replays every row through the store's own append - preserving the
    chain's shape - into a fresh, checkpointed journal file, forging the
    role on exactly the one send row the healer must examine: this
    runner's own UNRESOLVED mint delivery, the row whose receipt gap the
    heal exists to close. The forged row's role, its target hash, and the
    absence of any matching confirmed row are asserted in the child's
    journal before the child ever runs.
    """
    from pydantic import ConfigDict

    class RawPayload(BaseModel):
        """One verbatim journal payload, extras included."""

        model_config = ConfigDict(extra="allow")

    audit_path = harness.audit_path
    reader = sqlite3.connect(audit_path)
    rows = reader.execute(
        "SELECT event_type, payload_json, created_at FROM audit_records ORDER BY sequence ASC"
    ).fetchall()
    receipted = {
        json.loads(payload)["transaction_hash"]
        for event_type, payload, _created in rows
        if event_type in ("lp_execute_confirmed", "lp_execute_failed")
        for payload in [payload]
        if "transaction_hash" in json.loads(payload)
    }
    reader.close()
    assert rows, "no journal to rebuild"
    target_hash = None
    for event_type, payload_json, _created_at in reversed(rows):
        if event_type != "lp_execute_sent":
            continue
        payload = json.loads(payload_json)
        if payload.get("action") != "mint":
            continue
        if payload.get("transaction_hash") in receipted:
            continue
        target_hash = payload["transaction_hash"]
        break
    assert target_hash is not None, "no unresolved mint send row to forge"

    fresh_path = audit_path.parent / "journal-forged.sqlite3"
    fresh = AuditStore(fresh_path)
    forged_seen = False
    for event_type, payload_json, created_at in rows:
        payload = json.loads(payload_json)
        if event_type == "lp_execute_sent" and payload.get("transaction_hash") == target_hash:
            payload["role"] = role
            forged_seen = True
        fresh.append(
            AuditEventType(event_type),
            RawPayload.model_validate(payload),
            datetime.fromisoformat(created_at.replace("Z", "+00:00")),
        )
    assert forged_seen, "the unresolved mint row was never forged"
    # Checkpoint the fresh journal so its content lives in the main file.
    checkpoint = sqlite3.connect(fresh_path)
    checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    checkpoint.close()
    # Prove the child's journal: the forged role on the exact unresolved
    # hash, and no confirmed row for it anywhere in the chain.
    verify = sqlite3.connect(fresh_path)
    forged_rows = verify.execute(
        "SELECT payload_json FROM audit_records WHERE event_type = 'lp_execute_sent'"
    ).fetchall()
    matches = [
        json.loads(row[0])
        for row in forged_rows
        if json.loads(row[0]).get("transaction_hash") == target_hash
    ]
    assert len(matches) == 1 and matches[0]["role"] == role, matches
    confirmed = verify.execute(
        "SELECT COUNT(*) FROM audit_records WHERE event_type IN "
        "('lp_execute_confirmed', 'lp_execute_failed') AND payload_json LIKE ?",
        (f"%{target_hash}%",),
    ).fetchone()[0]
    verify.close()
    assert confirmed == 0, "a receipted row would mask the unresolved gap"
    harness.audit_path = fresh_path
