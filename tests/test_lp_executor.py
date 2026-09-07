"""Behavior tests for the capped manual LP lifecycle execution module."""

import json
from datetime import UTC, date, datetime
from decimal import ROUND_FLOOR, Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from eth_abi.abi import decode
from eth_account import Account

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.executor import (
    AERODROME_ROUTER_ADDRESS,
    DEFAULT_APPROVAL_STANDING_CAP_USDC,
    ERC20_ALLOWANCE_SELECTOR,
    ERC20_BALANCE_OF_SELECTOR,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REFUSED,
    SAFE_ADDRESS_ENV,
    ExecutionUnavailableError,
    ExecutorRpcBackend,
    build_approval_calldata,
)
from aero_bot.lp_calldata import LpMintParams, build_lp_mint_calldata
from aero_bot.lp_executor import (
    LpExecutionRefusalCode,
    LpExecutionRefusalError,
    LpExecutionRole,
    LpLifecycleExecutor,
    LpSafeExecutionPolicy,
    main,
)
from aero_bot.lp_plan import LpExecutionPolicy, LpPlanRefusalError
from aero_bot.registry import B20AssetListing, B20RegistryResult, RegistryStatus
from aero_bot.safe_tx import (
    CHECK_SIGNATURES_SELECTOR,
    SAFE_NONCE_SELECTOR,
    SafeTransactionRpcBackend,
)
from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    BASE_USDC_ADDRESS,
    SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
    PoolCandidate,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
    PoolKind,
    VenueId,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
GAUGE_ADDRESS = "0x3111111111111111111111111111111111111111"
NFPM_ADDRESS = "0xe1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"
SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
# A distinct Safe the environment-override test threads through the CLI.
OVERRIDE_SAFE_ADDRESS = "0x5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5"
# The fixed fixture clock makes deadlines and audit timestamps deterministic.
BASE_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
# The fixture stock uses eight decimals like every real B20 stock.
STOCK_DECIMALS = 8
# The fixture pool grids on ten-tick spacings like the live AAPLc pool.
LP_TICK_SPACING = 10
# A current tick of -15 anchors at -20, so a one-spacing range spans [-30, -10).
LP_CURRENT_TICK = -15
LP_ANCHOR_TICK = -20
LP_RANGE_LOWER = -30
LP_RANGE_UPPER = -10
# The fixture sqrt price sits exactly at the anchor tick, strictly inside the
# derived range, mirroring the planner's exact per-tick formula.
with localcontext() as _fixture_context:
    _fixture_context.prec = 60
    LP_SQRT_RATIO = int((Decimal("1.0001") ** Decimal(-10) * (1 << 96)).to_integral_value())
# A large active liquidity keeps the one-percent depth cap far from binding.
LP_ACTIVE_LIQUIDITY = 10**20
# One million whole USDC of reserve keeps the swap impact a single small tranche.
FIXTURE_USDC_RESERVE = 1_000_000_000_000
# A reserve this size pushes the modeled impact just over the tranche threshold
# while staying below the refusal ceiling.
TRANCHE_USDC_RESERVE = 4_600_000_000
# The fixture Safe holds ten whole USDC, enough for a seven-USDC entry.
FIXTURE_SAFE_USDC_UNITS = 10_000_000
# Held stock large enough to cover the stock side without any balancing swap.
FIXTURE_SAFE_STOCK_UNITS = 10**9
# The fixture gas price sits far below the one-gwei cap.
FIXTURE_GAS_PRICE_WEI = 100_000_000
# The fixture Safe balance clears the ETH floor.
FIXTURE_SAFE_ETH_WEI = 10**16
# The canary mint budget for every fixture dry run.
MINT_BUDGET_USDC = Decimal("7")
# The explicit one-spacing half width every fixture directive carries.
MINT_WIDTH_SPACINGS = 1
# The whole-word allowance fixtures that satisfy every skip condition.
SATISFIED_ALLOWANCE_UNITS = 10**9


def word_hex(value: int) -> str:
    """Encode one integer as the 0x-prefixed 32-byte word JSON-RPC returns."""
    return "0x" + value.to_bytes(32, "big").hex()


def signed_word(value: int) -> bytes:
    """Encode one signed integer as its two's-complement 32-byte word."""
    return (value % 2**256).to_bytes(32, "big")


def address_word(address: str) -> str:
    """Encode one lowercase address as its low-160-bit word hex."""
    return address[2:].rjust(64, "0")


def address_argument(calldata: str, index: int) -> str:
    """Read one address argument out of a word-aligned calldata payload."""
    word = calldata[2 + 8 + 64 * index : 2 + 8 + 64 * (index + 1)]
    return "0x" + word[24:]


def make_candidate(**overrides: object) -> PoolCandidate:
    """Build one accepted official B20/native-USDC Slipstream candidate.

    Args:
        **overrides: Candidate fields changed for one behavior test.

    Returns:
        A validated immutable pool candidate with USDC as token0 and the
        NFPM, gauge, and active liquidity the LP lifecycle resolves.
    """
    values: dict[str, object] = {
        "pool_address": POOL_ADDRESS,
        "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "token0_address": BASE_USDC_ADDRESS,
        "token1_address": B20_ADDRESS,
        "pool_kind": PoolKind.SLIPSTREAM,
        "tick_spacing": LP_TICK_SPACING,
        "current_tick": LP_CURRENT_TICK,
        "sqrt_ratio": LP_SQRT_RATIO,
        "pool_fee_ppm": 500,
        "unstaked_fee_ppm": 100_000,
        "reserve0": FIXTURE_USDC_RESERVE,
        "reserve1": 40_000_000_000,
        "staked0": 250_000_000,
        "staked1": 3 * 10**STOCK_DECIMALS,
        "gauge_address": GAUGE_ADDRESS,
        "gauge_liquidity": 9_999,
        "gauge_alive": True,
        "emissions_per_second": 4_494_371_922_759_724,
        "emissions_token_address": AERO_TOKEN_ADDRESS,
        "nfpm_address": NFPM_ADDRESS,
        "pool_active_liquidity": LP_ACTIVE_LIQUIDITY,
    }
    values.update(overrides)
    return PoolCandidate.model_validate(values)


def make_discovery(
    pools: tuple[PoolCandidate, ...] | None = None,
    status: PoolDiscoveryStatus = PoolDiscoveryStatus.VERIFIED,
    snapshot_block: int | None = 123,
    observed_at: datetime | None = BASE_NOW,
) -> PoolDiscoveryResult:
    """Build one adapter-shaped discovery result.

    Args:
        pools: Accepted pool candidates carried by the result.
        status: Discovery status the executor must handle.
        snapshot_block: Sugar snapshot pin block, or None for the bare shape.
        observed_at: Snapshot observation time, or None when absent.

    Returns:
        A validated immutable discovery result.
    """
    return PoolDiscoveryResult(
        venue=VenueId.AERODROME,
        status=status,
        source=f"lp-sugar:fixture@block:{snapshot_block}",
        observed_at=observed_at,
        snapshot_block=snapshot_block,
        pools=pools if pools is not None else (make_candidate(),),
        diagnostics=("fixture summary",),
    )


def make_registry(
    status: RegistryStatus = RegistryStatus.VERIFIED,
    assets: tuple[B20AssetListing, ...] | None = None,
) -> B20RegistryResult:
    """Build one registry result with one fixture FIXc listing by default.

    Args:
        status: Registry status the executor must gate on.
        assets: Listings exposed, defaulting to one FIXc listing.

    Returns:
        A validated immutable registry result.
    """
    listing = B20AssetListing(
        symbol="FIXc",
        name="Fixture Stock",
        address=B20_ADDRESS,
        explorer_url="https://basescan.org/token/fixture",
    )
    return B20RegistryResult(
        status=status,
        source_url="https://www.base.org/stocks",
        source_observed_at=date(2026, 9, 1) if status is RegistryStatus.VERIFIED else None,
        assets=assets if assets is not None else (listing,),
        diagnostic="fixture registry",
    )


class FakeSources:
    """Serve the registry, discovery, and decimals one LP run consumes."""

    def __init__(
        self,
        registry: B20RegistryResult | None = None,
        discovery: PoolDiscoveryResult | None = None,
        decimals: dict[str, int] | None = None,
    ) -> None:
        """Configure the served sources with verified defaults.

        Args:
            registry: Registry result served to every load_registry call.
            discovery: Discovery result served to every discover_pools call.
            decimals: Decimal counts served per token address.
        """
        self._registry = registry if registry is not None else make_registry()
        self._discovery = discovery if discovery is not None else make_discovery()
        self._decimals = decimals if decimals is not None else {B20_ADDRESS: STOCK_DECIMALS}

    def load_registry(self) -> B20RegistryResult:
        """Return the configured registry result."""
        return self._registry

    def discover_pools(self) -> PoolDiscoveryResult:
        """Return the configured discovery result."""
        return self._discovery

    def read_token_decimals(self, token_address: str) -> int:
        """Return the configured decimal count for one token."""
        return self._decimals[token_address.lower()]


class LpRpcScript:
    """Serve scripted JSON-RPC results for the LP executor's read calls."""

    def __init__(
        self,
        *,
        gas_price_wei: int = FIXTURE_GAS_PRICE_WEI,
        safe_eth_wei: int = FIXTURE_SAFE_ETH_WEI,
        usdc_balance_units: int = FIXTURE_SAFE_USDC_UNITS,
        stock_balance_units: int = 0,
        nfpm_held_positions: int = 0,
        nfpm_balance_hex: str | None = None,
        router_allowance_units: int = 0,
        nfpm_usdc_allowance_units: int = 0,
        nfpm_stock_allowance_units: int = 0,
        owner_addresses: dict[int, str] | None = None,
        operator_approved: bool = False,
        position_words: list[bytes] | None = None,
        approval_gas_estimate: int | None = 60_000,
        swap_gas_estimate: int | None = 200_000,
        mint_gas_estimate: int | None = 400_000,
        deposit_gas_estimate: int | None = 120_000,
    ) -> None:
        """Configure every scripted answer the LP executor's calls receive.

        Args:
            gas_price_wei: Suggested gas price served to eth_gasPrice.
            safe_eth_wei: Balance served for the Safe address.
            usdc_balance_units: The Safe's USDC balanceOf answer.
            stock_balance_units: The Safe's stock balanceOf answer.
            nfpm_held_positions: The Safe's NFPM position-NFT balanceOf answer.
            nfpm_balance_hex: Raw result served for that NFPM balanceOf read,
                bypassing the held-positions word to script malformed returns.
            router_allowance_units: Standing USDC allowance for the router.
            nfpm_usdc_allowance_units: Standing USDC allowance for the NFPM.
            nfpm_stock_allowance_units: Standing stock allowance for the NFPM.
            owner_addresses: ownerOf answers keyed by token id; missing ids
                revert like an unminted position.
            operator_approved: The NFPM isApprovedForAll answer for the gauge.
            position_words: Twelve encoded words for the positions view, or
                None to make that view revert.
            approval_gas_estimate: Gas served for approve-style inner calls,
                or None to make those estimates revert.
            swap_gas_estimate: Gas served for the router swap inner call, or
                None to make that estimate revert.
            mint_gas_estimate: Gas served for the mint inner call, or None to
                make that estimate revert.
            deposit_gas_estimate: Gas served for the deposit inner call, or
                None to make that estimate revert.
        """
        self.gas_price_wei = gas_price_wei
        self.safe_eth_wei = safe_eth_wei
        self.usdc_balance_units = usdc_balance_units
        self.stock_balance_units = stock_balance_units
        self.nfpm_held_positions = nfpm_held_positions
        self.nfpm_balance_hex = nfpm_balance_hex
        self.router_allowance_units = router_allowance_units
        self.nfpm_usdc_allowance_units = nfpm_usdc_allowance_units
        self.nfpm_stock_allowance_units = nfpm_stock_allowance_units
        self.owner_addresses = owner_addresses if owner_addresses is not None else {}
        self.operator_approved = operator_approved
        self.position_words = position_words
        self.approval_gas_estimate = approval_gas_estimate
        self.swap_gas_estimate = swap_gas_estimate
        self.mint_gas_estimate = mint_gas_estimate
        self.deposit_gas_estimate = deposit_gas_estimate
        self.broadcasts: list[str] = []
        self.estimate_requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC request from the scripted state."""
        call = json.loads(request.content)
        method = str(call["method"])
        params = call["params"]
        result: object
        if method == "eth_gasPrice":
            result = hex(self.gas_price_wei)
        elif method == "eth_getBalance":
            result = hex(self.safe_eth_wei)
        elif method == "eth_call":
            result = self._eth_call(str(params[0]["to"]).lower(), str(params[0]["data"]))
        elif method == "eth_estimateGas":
            calldata = str(params[0]["data"])
            self.estimate_requests.append(calldata)
            result = self._estimate(calldata)
        elif method == "eth_sendRawTransaction":
            # A broadcast attempt is a containment failure and fails the test.
            self.broadcasts.append(str(params[0]))
            raise AssertionError("the LP executor must never broadcast anything")
        else:
            raise AssertionError(f"unexpected LP executor RPC method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def _eth_call(self, to_address: str, data: str) -> str:
        """Answer one read-only contract call from the scripted token state."""
        usdc_token = BASE_USDC_ADDRESS.lower()
        if data.startswith(f"0x{ERC20_ALLOWANCE_SELECTOR}"):
            spender = address_argument(data, 1)
            if to_address == usdc_token:
                if spender == AERODROME_ROUTER_ADDRESS:
                    return word_hex(self.router_allowance_units)
                if spender == NFPM_ADDRESS:
                    return word_hex(self.nfpm_usdc_allowance_units)
            elif to_address == B20_ADDRESS and spender == NFPM_ADDRESS:
                return word_hex(self.nfpm_stock_allowance_units)
            raise AssertionError(f"unexpected allowance read to {to_address} for {spender}")
        if data.startswith(f"0x{ERC20_BALANCE_OF_SELECTOR}"):
            if to_address == usdc_token:
                return word_hex(self.usdc_balance_units)
            if to_address == B20_ADDRESS:
                return word_hex(self.stock_balance_units)
            if to_address == NFPM_ADDRESS:
                if self.nfpm_balance_hex is not None:
                    return self.nfpm_balance_hex
                return word_hex(self.nfpm_held_positions)
            raise AssertionError(f"unexpected balanceOf read on {to_address}")
        if data.startswith("0x6352211e"):
            token_id = int(data[2 + 8 : 2 + 8 + 64], 16)
            owner = self.owner_addresses.get(token_id)
            if owner is None:
                raise _ScriptedRevertError("ERC721: invalid token ID")
            return word_hex(int(owner, 16))
        if data.startswith("0xe985e9c5"):
            return word_hex(1 if self.operator_approved else 0)
        if data.startswith("0x99fbab88"):
            if self.position_words is None:
                raise _ScriptedRevertError("NFPM: unknown token ID")
            return "0x" + b"".join(self.position_words).hex()
        raise AssertionError(f"unexpected LP eth_call payload {data[:10]}")

    def _estimate(self, calldata: str) -> str:
        """Answer one gas estimate by the inner call embedded in the exec data."""
        inner = self._inner_calldata(calldata)
        selector = inner[:4].hex()
        if selector == "095ea7b3":
            return self._estimate_or_revert(self.approval_gas_estimate, "no allowance")
        if selector == "3593564c":
            return self._estimate_or_revert(self.swap_gas_estimate, "too little")
        if selector == "b5007d1f":
            return self._estimate_or_revert(self.mint_gas_estimate, "mint refused")
        if selector == "a22cb465":
            return self._estimate_or_revert(self.approval_gas_estimate, "operator refused")
        if selector == "b6b55f25":
            return self._estimate_or_revert(self.deposit_gas_estimate, "deposit refused")
        raise AssertionError(f"unexpected inner selector {selector}")

    @staticmethod
    def _inner_calldata(calldata: str) -> bytes:
        """Extract the inner call bytes from one execTransaction payload."""
        words = decode(
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

    @staticmethod
    def _estimate_or_revert(estimate: int | None, message: str) -> str:
        """Return one estimate hex, or raise the scripted revert marker."""
        if estimate is None:
            raise _ScriptedRevertError(message)
        return hex(estimate)

    def transport(self) -> httpx.MockTransport:
        """Build the deterministic HTTP transport over this script."""
        script = self

        def handler(request: httpx.Request) -> httpx.Response:
            try:
                return script.handler(request)
            except _ScriptedRevertError as revert:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": 3, "message": f"execution reverted: {revert}"},
                    },
                )

        return httpx.MockTransport(handler)


class _ScriptedRevertError(Exception):
    """Signal one scripted in-contract revert from inside the handler."""


class SafeRpcScript:
    """Serve scripted Safe nonces and signature verdicts over read-only calls."""

    def __init__(self, nonce_reads: list[int], signature_verdicts: list[bool]) -> None:
        """Configure the nonce and verdict sequences consumed by one run.

        Args:
            nonce_reads: Safe nonce served per fetch_live_nonce call, in order.
            signature_verdicts: checkSignatures verdict per validation call.
        """
        self.nonce_reads = list(nonce_reads)
        self.signature_verdicts = list(signature_verdicts)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one Safe read from the scripted sequences."""
        call = json.loads(request.content)
        data = str(call["params"][0]["data"])
        if data.startswith(f"0x{SAFE_NONCE_SELECTOR}"):
            return self._result(word_hex(self.nonce_reads.pop(0)))
        if data.startswith(f"0x{CHECK_SIGNATURES_SELECTOR}"):
            if not self.signature_verdicts.pop(0):
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": 3, "message": "GS026"},
                    },
                )
            return self._result("0x")
        raise AssertionError(f"unexpected Safe RPC call {data[:10]}")

    def transport(self) -> httpx.MockTransport:
        """Build the deterministic HTTP transport over this script."""
        return httpx.MockTransport(self.handler)

    @staticmethod
    def _result(result: object) -> httpx.Response:
        """Build one successful JSON-RPC response."""
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def make_lp_executor(
    *,
    sources: FakeSources | None = None,
    rpc_script: LpRpcScript | None = None,
    safe_script: SafeRpcScript | None = None,
    audit_path: Path | None = None,
) -> tuple[LpLifecycleExecutor, LpRpcScript, SafeRpcScript]:
    """Assemble one LP executor over fully scripted transport boundaries.

    Args:
        sources: Fake quote sources, verified by default.
        rpc_script: LP RPC script, defaulting to a clean five-step entry.
        safe_script: Safe RPC script, matching nonces and accepted signatures.
        audit_path: Optional real audit-store path for persistence tests.

    Returns:
        The executor plus both scripts so tests can inspect the exchanges.
    """
    if rpc_script is None:
        rpc_script = LpRpcScript()
    expected_steps = 5
    if safe_script is None:
        safe_script = SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * expected_steps)
    audit_sink = AuditStore(audit_path) if audit_path is not None else None
    executor = LpLifecycleExecutor(
        policy=LpSafeExecutionPolicy(),
        plan_policy=LpExecutionPolicy(),
        safe_address=SAFE_ADDRESS,
        sources=sources if sources is not None else FakeSources(),
        rpc=ExecutorRpcBackend(rpc_url="https://fixture.example", transport=rpc_script.transport()),
        safe_rpc=SafeTransactionRpcBackend(
            rpc_url="https://fixture.example",
            safe_address=SAFE_ADDRESS,
            transport=safe_script.transport(),
        ),
        audit_sink=audit_sink,
        now=lambda: BASE_NOW,
    )
    return executor, rpc_script, safe_script


def fixture_deadline() -> int:
    """Return the deterministic deadline the fixture clock produces."""
    return int(BASE_NOW.timestamp()) + 480


def decode_inner(calldata: str) -> bytes:
    """Extract the inner call bytes one execTransaction payload carries."""
    return LpRpcScript._inner_calldata(calldata)


# ---------------------------------------------------------------------------
# Mint dry-run composition
# ---------------------------------------------------------------------------


def test_dry_run_mint_composes_the_full_entry_sequence() -> None:
    """A cash-poor Safe composes allowance, swap, approvals, and the mint."""
    executor, rpc_script, _ = make_lp_executor()
    key = Account.create()

    report = executor.dry_run_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(key.key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (
        LpExecutionRole.ROUTER_ALLOWANCE,
        LpExecutionRole.BALANCING_SWAP,
        LpExecutionRole.NFPM_USDC_ALLOWANCE,
        LpExecutionRole.NFPM_STOCK_ALLOWANCE,
        LpExecutionRole.MINT,
    )
    assert [transaction.nonce for transaction in report.transactions] == [4, 5, 6, 7, 8]
    assert all(transaction.signature_verified for transaction in report.transactions)
    assert all(transaction.gas_estimate is not None for transaction in report.transactions)
    assert report.plan.position_range.tick_lower == LP_RANGE_LOWER
    assert report.plan.position_range.tick_upper == LP_RANGE_UPPER
    assert report.plan.balancing_swap.required
    assert report.relayer_address == key.address.lower()
    assert report.ephemeral_key is False
    assert any("untracked position NFTs" in cap for cap in report.caps_enforced)
    assert any("in-range depth" in cap for cap in report.caps_enforced)
    # Nothing was broadcast anywhere in the attempt.
    assert rpc_script.broadcasts == []
    assert len(rpc_script.estimate_requests) == 5


def test_dry_run_mint_encodes_every_inner_call_from_the_plan() -> None:
    """Each built transaction carries exactly the plan-derived inner calldata."""
    executor, rpc_script, _ = make_lp_executor()

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    plan = report.plan
    usdc_desired = plan.amounts.amount0_desired_units
    stock_desired = plan.amounts.amount1_desired_units
    swap = plan.balancing_swap
    tolerance = plan.amounts.slippage_tolerance_fraction
    amount_out_min = int(
        (Decimal(swap.expected_stock_units) * (Decimal(1) - tolerance)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    expected = {
        LpExecutionRole.ROUTER_ALLOWANCE: build_approval_calldata(
            AERODROME_ROUTER_ADDRESS, int(DEFAULT_APPROVAL_STANDING_CAP_USDC * 10**6)
        ),
        LpExecutionRole.NFPM_USDC_ALLOWANCE: build_approval_calldata(NFPM_ADDRESS, usdc_desired),
        LpExecutionRole.NFPM_STOCK_ALLOWANCE: build_approval_calldata(NFPM_ADDRESS, stock_desired),
        LpExecutionRole.MINT: build_lp_mint_calldata(
            LpMintParams(
                token0_address=BASE_USDC_ADDRESS,
                token1_address=B20_ADDRESS,
                tick_spacing=LP_TICK_SPACING,
                tick_lower=LP_RANGE_LOWER,
                tick_upper=LP_RANGE_UPPER,
                amount0_desired_units=plan.amounts.amount0_desired_units,
                amount1_desired_units=plan.amounts.amount1_desired_units,
                amount0_min_units=plan.amounts.amount0_min_units,
                amount1_min_units=plan.amounts.amount1_min_units,
                recipient_address=SAFE_ADDRESS,
                deadline=fixture_deadline(),
                sqrt_price_x96=0,
            )
        ),
    }
    for transaction, calldata in zip(
        report.transactions, rpc_script.estimate_requests, strict=True
    ):
        inner = decode_inner(calldata)
        if transaction.role is LpExecutionRole.BALANCING_SWAP:
            commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], inner[4:])
            recipient, amount_in, minimum, _, _, _ = decode(
                ["address", "uint256", "uint256", "bytes", "bool", "uint256"], inputs[0]
            )
            assert commands == b"\x00"
            assert deadline == fixture_deadline()
            assert recipient == SAFE_ADDRESS
            assert amount_in == swap.usdc_in_units
            assert minimum == amount_out_min
        else:
            assert "0x" + inner.hex() == expected[transaction.role]


def test_dry_run_mint_skips_satisfied_allowances_and_the_swap() -> None:
    """Held stock and standing allowances collapse the sequence to the mint."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            stock_balance_units=FIXTURE_SAFE_STOCK_UNITS,
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
        ),
        safe_script=SafeRpcScript(nonce_reads=[9], signature_verdicts=[True]),
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    assert not report.plan.balancing_swap.required
    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.MINT,)
    assert report.transactions[0].nonce == 9
    assert report.router_usdc_allowance_units == SATISFIED_ALLOWANCE_UNITS
    assert len(rpc_script.estimate_requests) == 1


def test_dry_run_mint_reports_a_rejected_signature_honestly() -> None:
    """A non-owner key still builds, but every verdict reports rejection."""
    executor, _, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[False] * 5)
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    assert len(report.transactions) == 5
    assert all(not transaction.signature_verified for transaction in report.transactions)
    assert all("GS026" in transaction.signature_diagnostic for transaction in report.transactions)


def test_dry_run_mint_labels_estimate_reverts_behind_predecessors() -> None:
    """A sequenced transaction's reverting estimate carries the expected note."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(swap_gas_estimate=None),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    swap_transaction = report.transactions[1]
    assert swap_transaction.role is LpExecutionRole.BALANCING_SWAP
    assert swap_transaction.gas_estimate is None
    assert "estimate reverted" in swap_transaction.gas_estimate_diagnostic
    assert "predecessors in the sequence remain unexecuted" in (
        swap_transaction.gas_estimate_diagnostic
    )
    # The mint's own estimate still succeeded.
    assert report.transactions[4].gas_estimate is not None


def test_dry_run_mint_labels_a_first_transaction_estimate_revert_plainly() -> None:
    """A lone sequence's reverting estimate carries no predecessor excuse."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            stock_balance_units=FIXTURE_SAFE_STOCK_UNITS,
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            mint_gas_estimate=None,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    mint_transaction = report.transactions[0]
    assert mint_transaction.gas_estimate is None
    assert "estimate reverted" in mint_transaction.gas_estimate_diagnostic
    assert "predecessors" not in mint_transaction.gas_estimate_diagnostic


# ---------------------------------------------------------------------------
# Execution-layer refusals
# ---------------------------------------------------------------------------


def test_mint_refuses_without_an_explicit_width() -> None:
    """A missing width override refuses with the derived-width catalog code."""
    executor, _, _ = make_lp_executor()

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, None)

    assert raised.value.code is LpExecutionRefusalCode.DERIVED_WIDTH_UNAVAILABLE
    assert "--width-ticks" in str(raised.value)


def test_mint_refuses_untracked_existing_positions() -> None:
    """A Safe holding position NFTs refuses entry fail-closed."""
    executor, _, _ = make_lp_executor(rpc_script=LpRpcScript(nfpm_held_positions=2))

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert raised.value.code is LpExecutionRefusalCode.UNTRACKED_EXISTING_POSITIONS


def test_mint_refuses_a_multi_tranche_balancing_swap() -> None:
    """A plan needing tranches refuses before anything is signed."""
    executor, _, _ = make_lp_executor(
        sources=FakeSources(
            discovery=make_discovery((make_candidate(reserve0=TRANCHE_USDC_RESERVE),))
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_mint(
            "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
        )

    assert raised.value.code is LpExecutionRefusalCode.MULTI_TRANCHE_SWAP_UNSUPPORTED


def test_mint_refuses_a_stale_snapshot() -> None:
    """A snapshot older than the staleness bound refuses the attempt."""
    executor, _, _ = make_lp_executor(
        sources=FakeSources(
            discovery=make_discovery(observed_at=datetime(2026, 9, 7, 11, 0, tzinfo=UTC))
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert raised.value.code is LpExecutionRefusalCode.SNAPSHOT_STALE


def test_mint_refuses_an_unknown_symbol() -> None:
    """A symbol outside the official registry refuses before discovery."""
    executor, _, _ = make_lp_executor()

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("NOPEc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert raised.value.code is LpExecutionRefusalCode.SYMBOL_NOT_IN_REGISTRY


def test_mint_refuses_above_the_gas_cap() -> None:
    """An endpoint gas price above the cap refuses before signing."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(gas_price_wei=2_000_000_000),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_mint(
            "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
        )

    assert raised.value.code is LpExecutionRefusalCode.GAS_PRICE_ABOVE_CAP


def test_mint_refuses_below_the_eth_floor() -> None:
    """A Safe ETH balance below the floor refuses before signing."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(safe_eth_wei=1),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_mint(
            "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
        )

    assert raised.value.code is LpExecutionRefusalCode.SAFE_ETH_BELOW_FLOOR


def test_planner_refusals_surface_with_their_own_codes() -> None:
    """A planner cap refusal passes through with its catalog code intact."""
    executor, _, _ = make_lp_executor()

    with pytest.raises(LpPlanRefusalError) as raised:
        executor.plan_mint("FIXc", Decimal("60"), MINT_WIDTH_SPACINGS)

    assert "per-pool cap" in str(raised.value)


# ---------------------------------------------------------------------------
# Stake dry-run composition
# ---------------------------------------------------------------------------


def make_position_words() -> list[bytes]:
    """Encode one coherent twelve-word positions view for the fixture pool."""
    return [
        (0).to_bytes(32, "big"),
        bytes.fromhex(address_word("0x" + "00" * 20)),
        bytes.fromhex(address_word(BASE_USDC_ADDRESS)),
        bytes.fromhex(address_word(B20_ADDRESS)),
        LP_TICK_SPACING.to_bytes(32, "big"),
        signed_word(LP_RANGE_LOWER),
        signed_word(LP_RANGE_UPPER),
        (12_345).to_bytes(32, "big"),
        (0).to_bytes(32, "big"),
        (0).to_bytes(32, "big"),
        (10).to_bytes(32, "big"),
        (11).to_bytes(32, "big"),
    ]


def test_dry_run_stake_builds_the_pre_mint_sequence() -> None:
    """A not-yet-minted token still proves the machinery with honest labels."""
    executor, rpc_script, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True])
    )

    report = executor.dry_run_stake("FIXc", 77, bytes(Account.create().key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.NFPM_GAUGE_APPROVAL, LpExecutionRole.GAUGE_DEPOSIT)
    assert [transaction.nonce for transaction in report.transactions] == [6, 7]
    assert all(transaction.signature_verified for transaction in report.transactions)
    assert report.token_owner_address is None
    assert "not minted yet" in report.ownership_diagnostic
    assert report.position is None
    assert "unavailable" in report.position_diagnostic
    assert report.gauge_operator_approved is False
    assert rpc_script.broadcasts == []
    # The approval targets the gauge and the deposit carries the token id.
    approval_inner = decode_inner(rpc_script.estimate_requests[0])
    operator, approved = decode(["address", "bool"], approval_inner[4:])
    assert operator == GAUGE_ADDRESS
    assert approved is True
    deposit_inner = decode_inner(rpc_script.estimate_requests[1])
    (token_id,) = decode(["uint256"], deposit_inner[4:])
    assert token_id == 77


def test_dry_run_stake_on_an_owned_position_skips_the_approval() -> None:
    """An owned, operator-approved position collapses to the bare deposit."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            operator_approved=True,
            position_words=make_position_words(),
        ),
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True]),
    )

    report = executor.dry_run_stake("FIXc", 77, bytes(Account.create().key))

    assert report.token_owner_address == SAFE_ADDRESS
    assert report.ownership_diagnostic == ""
    assert report.gauge_operator_approved is True
    assert report.position is not None
    assert report.position.liquidity == 12_345
    assert report.position.tick_lower == LP_RANGE_LOWER
    assert report.position.tokens_owed0_units == 10
    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.GAUGE_DEPOSIT,)


# ---------------------------------------------------------------------------
# Audit persistence
# ---------------------------------------------------------------------------


def test_dry_run_mint_appends_its_audit_chain(tmp_path: Path) -> None:
    """One dry run appends the plan then every built transaction in order."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(audit_path=audit_path)

    executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_MINT_PLANNED,
        *([AuditEventType.LP_TRANSACTION_BUILT] * 5),
    ]
    planned = json.loads(records[0].payload_json)
    assert planned["budget_usdc"] == "7"
    assert planned["tick_lower"] == LP_RANGE_LOWER
    assert planned["tick_upper"] == LP_RANGE_UPPER
    assert planned["balancing_swap_required"] is True
    assert planned["snapshot_block"] == 123
    built_roles = [json.loads(record.payload_json)["role"] for record in records[1:]]
    assert built_roles == [
        "router_allowance",
        "balancing_swap",
        "nfpm_usdc_allowance",
        "nfpm_stock_allowance",
        "mint",
    ]
    for record in records[1:]:
        payload = json.loads(record.payload_json)
        assert payload["signature_verified"] is True
        assert payload["mode"] == "dry_run"
    verification = store.verify_chain()
    assert verification.status.value == "verified"
    assert verification.record_count == 6


def test_refusals_append_their_catalog_code(tmp_path: Path) -> None:
    """A refused attempt appends exactly one refusal with its code."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(audit_path=audit_path)

    with pytest.raises(LpExecutionRefusalError):
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, None)

    store = AuditStore(audit_path)
    records = store.read_records(10)
    assert len(records) == 1
    assert records[0].event_type is AuditEventType.LP_REFUSED
    payload = json.loads(records[0].payload_json)
    assert payload["code"] == "derived_width_unavailable"
    assert payload["plan_code"] == ""
    assert payload["action"] == "mint"
    assert payload["symbol"] == "FIXc"


def test_planner_refusals_audit_their_plan_code(tmp_path: Path) -> None:
    """A planner refusal records its own code in the plan_code field."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(audit_path=audit_path)

    with pytest.raises(LpPlanRefusalError):
        executor.plan_mint("FIXc", Decimal("60"), MINT_WIDTH_SPACINGS)

    store = AuditStore(audit_path)
    records = store.read_records(10)
    assert len(records) == 1
    payload = json.loads(records[0].payload_json)
    assert payload["code"] == "budget_above_pool_cap"
    assert payload["plan_code"] == "budget_above_pool_cap"


def test_stake_dry_run_appends_its_audit_chain(tmp_path: Path) -> None:
    """One stake dry run appends its plan then both built transactions."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True]),
    )

    executor.dry_run_stake("FIXc", 77, bytes(Account.create().key))

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_STAKE_PLANNED,
        AuditEventType.LP_TRANSACTION_BUILT,
        AuditEventType.LP_TRANSACTION_BUILT,
    ]
    planned = json.loads(records[0].payload_json)
    assert planned["token_id"] == 77
    assert planned["token_owner_address"] is None
    assert planned["gauge_operator_approved"] is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def make_cli_settings(tmp_path: Path) -> SimpleNamespace:
    """Build the settings-shaped object the LP CLI reads."""
    return SimpleNamespace(
        base_rpc_url="https://fixture.example",
        lp_sugar_address="0x27fc745390d1f4baf8d184fbd97748340f786634",
        audit_database_path=tmp_path / "audit.sqlite3",
    )


def test_cli_plan_mint_prints_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plan subcommand prices the mint through the wired executor."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "plan",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert f"[{LP_RANGE_LOWER}, {LP_RANGE_UPPER})" in output
    assert "balancing swap" in output


def test_cli_dry_run_mint_prints_the_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry-run subcommand prints its complete typed model with --json."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
                "--json",
            ]
        )

    assert exit_code == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "dry_run"
    assert printed["ephemeral_key"] is True
    assert [t["role"] for t in printed["transactions"]][0] == "router_allowance"


def test_cli_dry_run_stake_prints_the_pre_mint_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stake dry-run reports the unminted token honestly."""
    executor, _, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True])
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "stake",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "not minted yet" in output
    assert "[nfpm_gauge_approval]" in output
    assert "[gauge_deposit]" in output


def test_cli_refusal_exits_two_with_the_catalog_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused dry run exits two and prints the catalog code."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            ["dry-run", "mint", "--symbol", "FIXc", "--amount", "7", "--ephemeral-key"]
        )

    assert exit_code == EXIT_REFUSED
    assert "refused [derived_width_unavailable]" in capsys.readouterr().err


def test_cli_fails_when_the_audit_store_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unavailable audit store exits one before any executor wiring."""
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore", side_effect=OSError("disk full")),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor") as executor_factory,
    ):
        exit_code = main(
            ["plan", "mint", "--symbol", "FIXc", "--amount", "7", "--width-ticks", "1"]
        )

    assert exit_code == EXIT_FAILURE
    assert "audit store is unavailable" in capsys.readouterr().err
    executor_factory.assert_not_called()


def test_cli_threads_the_environment_safe_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor and Safe backend wire to the environment's Safe."""
    executor, _, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True])
    )
    monkeypatch.setenv(SAFE_ADDRESS_ENV, OVERRIDE_SAFE_ADDRESS)
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend") as safe_backend_factory,
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor) as factory,
    ):
        exit_code = main(
            [
                "dry-run",
                "stake",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_OK
    assert factory.call_args.kwargs["safe_address"] == OVERRIDE_SAFE_ADDRESS
    assert safe_backend_factory.call_args.kwargs["safe_address"] == OVERRIDE_SAFE_ADDRESS


def test_cli_never_attempts_a_broadcast_under_any_subcommand(
    tmp_path: Path,
) -> None:
    """Every wired subcommand path runs over a transport that fails on sends."""
    executor, rpc_script, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[4, 6], signature_verdicts=[True] * 7)
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        for arguments in (
            ["plan", "mint", "--symbol", "FIXc", "--amount", "7", "--width-ticks", "1"],
            [
                "dry-run",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
            ],
            ["dry-run", "stake", "--symbol", "FIXc", "--token-id", "77", "--ephemeral-key"],
        ):
            assert main(arguments) == EXIT_OK
    assert rpc_script.broadcasts == []


def test_malformed_reads_surface_as_unavailability() -> None:
    """A malformed on-chain read raises an unavailable failure, not a refusal."""
    executor, _, _ = make_lp_executor(rpc_script=LpRpcScript(nfpm_balance_hex="0x1234"))

    with pytest.raises(ExecutionUnavailableError):
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)


def test_cli_unavailable_reads_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unavailable read surfaces as a plain failure exit, not a refusal."""
    executor, _, _ = make_lp_executor(rpc_script=LpRpcScript(nfpm_balance_hex="0x1234"))
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_FAILURE
    assert "failed:" in capsys.readouterr().err
