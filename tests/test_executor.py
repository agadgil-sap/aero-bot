"""Behavior tests for the capped manual swap execution module."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from eth_abi import decode
from eth_account import Account
from eth_utils.crypto import keccak
from pydantic import ValidationError

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.executor import (
    AERODROME_ROUTER_ADDRESS,
    DEFAULT_APPROVAL_STANDING_CAP_USDC,
    DEFAULT_CANARY_SAFE_ADDRESS,
    ERC20_ALLOWANCE_SELECTOR,
    ERC20_DECIMALS_SELECTOR,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REFUSED,
    SAFE_ADDRESS_ENV,
    BroadcastTimeoutError,
    BuiltExecutionTransaction,
    ExecutionMode,
    ExecutionPolicy,
    ExecutionReceiptOutcome,
    ExecutionRefusalError,
    ExecutionRole,
    ExecutionUnavailableError,
    ExecutorRpcBackend,
    ExecutorRpcRevertError,
    SwapExecutor,
    SwapQuote,
    build_allowance_calldata,
    build_approval_calldata,
    build_swap_calldata,
    build_swap_path,
    main,
    usdc_units,
)
from aero_bot.history import SWAP_EVENT_TOPIC0
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
SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
# The real AAPLc contract anchors the golden reference-calldata vector.
AAPLC_ADDRESS = "0xb200000000000000000000c2e324d24d7eecd1fb"
# The fixed fixture clock makes deadlines and audit timestamps deterministic.
BASE_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
# The fixture stock uses eight decimals like every real B20 stock.
STOCK_DECIMALS = 8
# A one-to-one raw ratio with eight/six decimals prices the stock at 100 USDC.
FIXTURE_SQRT_RATIO = 1 << 96
FIXTURE_STOCK_PRICE = Decimal(100)
# One million raw USDC (one whole USDC) is the fixture swap size.
ONE_USDC_UNITS = 1_000_000
# The fixture USDC reserve keeps the conservative impact bound tiny.
FIXTURE_USDC_RESERVE = 1_000_000_000_000
# The fixture standing gas price sits far below the one-gwei cap.
FIXTURE_GAS_PRICE_WEI = 100_000_000
# The fixture Safe and relayer balances clear every floor and gas cost.
FIXTURE_SAFE_ETH_WEI = 10**16
FIXTURE_RELAYER_ETH_WEI = 10**16


def word_hex(value: int) -> str:
    """Encode one integer as the 0x-prefixed 32-byte word JSON-RPC returns."""
    return "0x" + value.to_bytes(32, "big").hex()


def signed_word(value: int) -> bytes:
    """Encode one signed integer as its two's-complement 32-byte word."""
    return (value % 2**256).to_bytes(32, "big")


def make_candidate(
    pool_address: str = POOL_ADDRESS,
    token_address: str = B20_ADDRESS,
    **overrides: object,
) -> PoolCandidate:
    """Build one accepted official B20/native-USDC Slipstream candidate.

    Args:
        pool_address: Fixture pool contract address.
        token_address: Fixture B20 stock token address.
        **overrides: Candidate fields changed for one behavior test.

    Returns:
        A validated immutable pool candidate with USDC as token0.
    """
    values: dict[str, object] = {
        "pool_address": pool_address,
        "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "token0_address": BASE_USDC_ADDRESS,
        "token1_address": token_address,
        "pool_kind": PoolKind.SLIPSTREAM,
        "tick_spacing": 10,
        "current_tick": -10,
        "sqrt_ratio": FIXTURE_SQRT_RATIO,
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
    }
    values.update(overrides)
    return PoolCandidate.model_validate(values)


def make_discovery(
    pools: tuple[PoolCandidate, ...],
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
        pools=pools,
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
    """Serve the registry, discovery, and decimals one executor run consumes."""

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
        self._discovery = (
            discovery if discovery is not None else make_discovery((make_candidate(),))
        )
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


class FakeClock:
    """Provide deterministic monotonic time that only sleep advances."""

    def __init__(self) -> None:
        """Start the monotonic clock at zero."""
        self.seconds = 0.0

    def timer(self) -> float:
        """Return the current monotonic timestamp."""
        return self.seconds

    def sleep(self, seconds: float) -> None:
        """Advance the clock instead of blocking the test."""
        self.seconds += seconds


class ExecutorRpcScript:
    """Serve scripted JSON-RPC results for the executor's read and send calls."""

    def __init__(
        self,
        *,
        gas_price_wei: int = FIXTURE_GAS_PRICE_WEI,
        safe_eth_wei: int = FIXTURE_SAFE_ETH_WEI,
        relayer_eth_wei: int = FIXTURE_RELAYER_ETH_WEI,
        allowance_units: int = ONE_USDC_UNITS,
        relayer_nonce: int = 7,
        approval_gas_estimate: int | None = 60_000,
        swap_gas_estimate: int | None = 200_000,
        receipts: list[dict[str, object] | None] | None = None,
    ) -> None:
        """Configure every scripted answer the executor's calls receive.

        Args:
            gas_price_wei: Suggested gas price served to eth_gasPrice.
            safe_eth_wei: Balance served for the Safe address.
            relayer_eth_wei: Balance served for the relaying EOA.
            allowance_units: Standing USDC allowance served to eth_call.
            relayer_nonce: Pending count served to eth_getTransactionCount.
            approval_gas_estimate: Gas estimate served for the approval build,
                or None to make that estimate revert.
            swap_gas_estimate: Gas estimate served for the swap build, or None
                to make the estimate revert.
            receipts: Receipt per broadcast index, or None for missing.
        """
        self.gas_price_wei = gas_price_wei
        self.safe_eth_wei = safe_eth_wei
        self.relayer_eth_wei = relayer_eth_wei
        self.allowance_units = allowance_units
        self.relayer_nonce = relayer_nonce
        self.approval_gas_estimate = approval_gas_estimate
        self.swap_gas_estimate = swap_gas_estimate
        self.receipts = receipts if receipts is not None else []
        self.broadcasts: list[str] = []
        self.calls: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC request from the scripted state."""
        call = json.loads(request.content)
        self.calls.append(call)
        method = str(call["method"])
        params = call["params"]
        result: object
        if method == "eth_gasPrice":
            result = hex(self.gas_price_wei)
        elif method == "eth_getBalance":
            address = str(params[0]).lower()
            if address == SAFE_ADDRESS:
                result = hex(self.safe_eth_wei)
            else:
                result = hex(self.relayer_eth_wei)
        elif method == "eth_getTransactionCount":
            result = hex(self.relayer_nonce)
        elif method == "eth_call":
            data = str(params[0]["data"])
            if data.startswith(f"0x{ERC20_ALLOWANCE_SELECTOR}"):
                result = word_hex(self.allowance_units)
            elif data.startswith(ERC20_DECIMALS_SELECTOR):
                result = word_hex(STOCK_DECIMALS)
            else:
                raise AssertionError(f"unexpected eth_call payload {data[:10]}")
        elif method == "eth_estimateGas":
            data = str(params[0]["data"])
            if "3593564c" in data:
                if self.swap_gas_estimate is None:
                    return self._revert("execution reverted: too little")
                result = hex(self.swap_gas_estimate)
            else:
                if self.approval_gas_estimate is None:
                    return self._revert("execution reverted: no allowance")
                result = hex(self.approval_gas_estimate)
        elif method == "eth_sendRawTransaction":
            raw = str(params[0])
            self.broadcasts.append(raw)
            result = "0x" + keccak(bytes.fromhex(raw[2:])).hex()
        elif method == "eth_getTransactionReceipt":
            index = len(self.broadcasts) - 1
            receipt = self.receipts[index] if index < len(self.receipts) else None
            result = receipt
        else:
            raise AssertionError(f"unexpected executor RPC method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def transport(self) -> httpx.MockTransport:
        """Build the deterministic HTTP transport over this script."""
        return httpx.MockTransport(self.handler)

    @staticmethod
    def _revert(message: str) -> httpx.Response:
        """Build one JSON-RPC revert-error response."""
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": message}},
        )


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


def make_swap_log(
    amount0: int = ONE_USDC_UNITS,
    amount1: int = -998_000,
) -> dict[str, object]:
    """Build one pool Swap log entry for a USDC-to-stock buy receipt."""
    data = (
        signed_word(amount0)
        + signed_word(amount1)
        + FIXTURE_SQRT_RATIO.to_bytes(32, "big")
        + (10**18).to_bytes(32, "big")
        + signed_word(-10)
    )
    return {
        "address": POOL_ADDRESS,
        "topics": [SWAP_EVENT_TOPIC0, word_hex(1), word_hex(2)],
        "data": "0x" + data.hex(),
        "blockNumber": hex(101),
        "logIndex": hex(0),
    }


def make_receipt(
    *,
    status: int = 1,
    logs: list[object] | None = None,
) -> dict[str, object]:
    """Build one raw transaction receipt object."""
    return {
        "status": hex(status),
        "blockNumber": hex(101),
        "gasUsed": hex(120_000),
        "effectiveGasPrice": hex(FIXTURE_GAS_PRICE_WEI),
        "logs": logs if logs is not None else [],
    }


def make_executor(
    *,
    sources: FakeSources | None = None,
    rpc_script: ExecutorRpcScript | None = None,
    safe_script: SafeRpcScript | None = None,
    audit_path: Path | None = None,
) -> tuple[SwapExecutor, ExecutorRpcScript, SafeRpcScript]:
    """Assemble one executor over fully scripted transport boundaries.

    Args:
        sources: Fake quote sources, verified by default.
        rpc_script: Executor RPC script, defaulting to one confirmed swap.
        safe_script: Safe RPC script, matching nonces and accepted signatures.
        audit_path: Optional real audit-store path for persistence tests.

    Returns:
        The executor plus both scripts so tests can inspect the exchanges.
    """
    clock = FakeClock()
    if rpc_script is None:
        rpc_script = ExecutorRpcScript(receipts=[make_receipt(logs=[make_swap_log()])])
    if safe_script is None:
        safe_script = SafeRpcScript(nonce_reads=[4], signature_verdicts=[True])
    audit_sink = AuditStore(audit_path) if audit_path is not None else None
    executor = SwapExecutor(
        policy=ExecutionPolicy(),
        safe_address=SAFE_ADDRESS,
        sources=sources if sources is not None else FakeSources(),
        rpc=ExecutorRpcBackend(
            rpc_url="https://fixture.example",
            transport=rpc_script.transport(),
            sleep=clock.sleep,
            timer=clock.timer,
            receipt_poll_seconds=0.01,
            receipt_timeout_seconds=1.0,
        ),
        safe_rpc=SafeTransactionRpcBackend(
            rpc_url="https://fixture.example",
            safe_address=SAFE_ADDRESS,
            transport=safe_script.transport(),
            sleep=clock.sleep,
        ),
        audit_sink=audit_sink,
        now=lambda: BASE_NOW,
        timer=clock.timer,
    )
    return executor, rpc_script, safe_script


def expected_deadline() -> int:
    """Return the deterministic deadline the fixture clock produces."""
    return int(BASE_NOW.timestamp()) + 480


# ---------------------------------------------------------------------------
# Pure encoding helpers
# ---------------------------------------------------------------------------


def test_usdc_units_converts_exactly_and_rejects_finer_precision() -> None:
    """Whole and six-place amounts convert exactly; seven places refuse."""
    assert usdc_units(Decimal("1")) == ONE_USDC_UNITS
    assert usdc_units(Decimal("0.000001")) == 1
    assert usdc_units(Decimal("20")) == 20_000_000
    with pytest.raises(ValueError, match="finer than 6 decimals"):
        usdc_units(Decimal("0.0000001"))


def test_swap_path_matches_the_reference_shape() -> None:
    """The path is exactly token, flag, tick spacing, token."""
    path = build_swap_path(BASE_USDC_ADDRESS, AAPLC_ADDRESS, 10)
    assert path == (
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        + "08"
        + "000a"
        + "b200000000000000000000c2e324d24d7eecd1fb"
    )
    assert len(path) == 2 + 86


def test_swap_path_rejects_out_of_range_tick_spacing() -> None:
    """Tick spacing must fit the path's unsigned 16-bit segment."""
    with pytest.raises(ValueError, match="unsigned 16-bit"):
        build_swap_path(BASE_USDC_ADDRESS, AAPLC_ADDRESS, 0)
    with pytest.raises(ValueError, match="unsigned 16-bit"):
        build_swap_path(BASE_USDC_ADDRESS, AAPLC_ADDRESS, 0x10000)


def test_swap_calldata_encodes_length_prefixed_router_input() -> None:
    """Each bytes-array element includes its length before the swap payload."""
    path = build_swap_path(BASE_USDC_ADDRESS, AAPLC_ADDRESS, 10)
    calldata = build_swap_calldata(SAFE_ADDRESS, ONE_USDC_UNITS, 311_381, path, 0x6A9E34E0)
    expected = (
        "0x3593564c"
        # Head: commands offset, inputs offset, deadline.
        + format(0x60, "064x")
        + format(0xA0, "064x")
        + format(0x6A9E34E0, "064x")
        # Commands: length one, V3_SWAP_EXACT_IN padded.
        + format(1, "064x")
        + format(0, "064x")
        # Inputs: length one, element offset one word.
        + format(1, "064x")
        + format(0x20, "064x")
        # Dynamic bytes element: payload length, distinct from array length.
        + format(0x120, "064x")
        # Params: recipient, amountIn, amountOutMin, path offset, payer, zero.
        + "b69ab6c7e73f711d5f2d10fed8f0d09b1d028c28".rjust(64, "0")
        + format(0xF4240, "064x")
        + format(0x4C055, "064x")
        + format(0xC0, "064x")
        + format(1, "064x")
        + format(0, "064x")
        # Path: length 43, path padded to 64 bytes.
        + format(43, "064x")
        + (
            "833589fcd6edb6e08f4c7c32d4f71b54bda02913"
            + "08"
            + "000a"
            + "b200000000000000000000c2e324d24d7eecd1fb"
        ).ljust(128, "0")
    )
    assert calldata == expected


def test_swap_calldata_decodes_with_independent_abi_decoder() -> None:
    """A standard decoder recovers the intended command and nested payload."""
    path = build_swap_path(BASE_USDC_ADDRESS, AAPLC_ADDRESS, 10)
    calldata = build_swap_calldata(SAFE_ADDRESS, ONE_USDC_UNITS, 311_381, path, 2_000_000_000)
    commands, inputs, deadline = decode(
        ["bytes", "bytes[]", "uint256"], bytes.fromhex(calldata[10:])
    )
    assert commands == b"\x00"
    assert deadline == 2_000_000_000
    assert len(inputs) == 1
    recipient, amount, minimum, decoded_path, payer, extra = decode(
        ["address", "uint256", "uint256", "bytes", "bool", "uint256"], inputs[0]
    )
    assert recipient == SAFE_ADDRESS.lower()
    assert (amount, minimum, payer, extra) == (ONE_USDC_UNITS, 311_381, True, 0)
    assert decoded_path == bytes.fromhex(path[2:])


def test_swap_calldata_rejects_malformed_arguments() -> None:
    """Non-positive amounts, deadlines, and wrong-size paths refuse."""
    path = build_swap_path(BASE_USDC_ADDRESS, AAPLC_ADDRESS, 10)
    with pytest.raises(ValueError, match="amount_in_units must be positive"):
        build_swap_calldata(SAFE_ADDRESS, 0, 1, path, 10)
    with pytest.raises(ValueError, match="amount_out_min_units must be positive"):
        build_swap_calldata(SAFE_ADDRESS, 1, 0, path, 10)
    with pytest.raises(ValueError, match="deadline must be positive"):
        build_swap_calldata(SAFE_ADDRESS, 1, 1, path, 0)
    with pytest.raises(ValueError, match="exactly 43 bytes"):
        build_swap_calldata(SAFE_ADDRESS, 1, 1, "0x00" * 42, 10)


def test_approval_and_allowance_calldata_are_exact() -> None:
    """The bounded approval and allowance reads encode hand-checkable words."""
    approval = build_approval_calldata(AERODROME_ROUTER_ADDRESS, 20_000_000)
    assert approval == (
        "0x095ea7b3"
        "000000000000000000000000caf22ce31298cf2bf1d152862f80216478ad7c67"
        "0000000000000000000000000000000000000000000000000000000001312d00"
    )
    allowance = build_allowance_calldata(SAFE_ADDRESS, AERODROME_ROUTER_ADDRESS)
    assert allowance.startswith("0xdd62ed3e")
    assert len(allowance) == 10 + 128
    with pytest.raises(ValueError, match="non-negative"):
        build_approval_calldata(AERODROME_ROUTER_ADDRESS, -1)


# ---------------------------------------------------------------------------
# Policy caps
# ---------------------------------------------------------------------------


def test_policy_defaults_carry_the_documented_caps() -> None:
    """The default policy holds every documented cap."""
    policy = ExecutionPolicy()
    assert policy.router_address == AERODROME_ROUTER_ADDRESS
    assert policy.max_swap_usdc == Decimal("1.00")
    assert policy.approval_standing_cap_usdc == DEFAULT_APPROVAL_STANDING_CAP_USDC
    assert policy.max_swap_usdc_units == ONE_USDC_UNITS
    assert policy.approval_standing_cap_units == 20_000_000
    assert policy.gas_price_cap_wei == 1_000_000_000
    assert policy.safe_eth_floor_wei == 5 * 10**13
    assert policy.quote_max_age_seconds == 120
    assert policy.slippage_tolerance_fraction == Decimal("0.001")


def test_policy_rejects_every_ceiling_violation() -> None:
    """The whitelist and every hard ceiling refuse at validation time."""
    with pytest.raises(ValidationError, match="outside the execution whitelist"):
        ExecutionPolicy(router_address="0x" + "1" * 40)
    with pytest.raises(ValidationError, match="hard ceiling"):
        ExecutionPolicy(max_swap_usdc=Decimal("5.01"))
    with pytest.raises(ValidationError, match="never infinite"):
        ExecutionPolicy(approval_standing_cap_usdc=Decimal("20.01"))
    with pytest.raises(ValidationError, match="at least max_swap_usdc"):
        ExecutionPolicy(max_swap_usdc=Decimal("5"), approval_standing_cap_usdc=Decimal("4"))
    with pytest.raises(ValidationError, match="slippage_tolerance_fraction"):
        ExecutionPolicy(slippage_tolerance_fraction=Decimal("0.02"))


def test_quote_model_rejects_incoherent_amounts() -> None:
    """A floor above the quoted output or naive time refuses validation."""
    base: dict[str, object] = {
        "symbol": "FIXc",
        "token_address": B20_ADDRESS,
        "pool_address": POOL_ADDRESS,
        "tick_spacing": 10,
        "stock_decimals": STOCK_DECIMALS,
        "usdc_in_units": ONE_USDC_UNITS,
        "price_usdc_per_stock": FIXTURE_STOCK_PRICE,
        "expected_stock_units": 1_000_000,
        "amount_out_min_units": 1_000_000,
        "modeled_impact_fraction": Decimal("0.000001"),
        "snapshot_block": 123,
        "observed_at": BASE_NOW,
        "quote_age_seconds": 0,
    }
    with pytest.raises(ValidationError, match="exceeds the quoted output"):
        SwapQuote.model_validate({**base, "amount_out_min_units": 1_000_001})
    with pytest.raises(ValidationError, match="timezone-aware"):
        SwapQuote.model_validate({**base, "observed_at": BASE_NOW.replace(tzinfo=None)})


# ---------------------------------------------------------------------------
# Executor RPC backend
# ---------------------------------------------------------------------------


def test_rpc_backend_decodes_every_served_quantity() -> None:
    """Gas price, balances, allowance, decimals, nonce, and estimate decode."""
    script = ExecutorRpcScript()
    backend = ExecutorRpcBackend(
        rpc_url="https://fixture.example", transport=script.transport(), sleep=lambda _: None
    )
    assert backend.fetch_gas_price() == FIXTURE_GAS_PRICE_WEI
    assert backend.fetch_eth_balance(SAFE_ADDRESS) == FIXTURE_SAFE_ETH_WEI
    assert backend.fetch_usdc_allowance(SAFE_ADDRESS, AERODROME_ROUTER_ADDRESS) == (ONE_USDC_UNITS)
    assert backend.fetch_token_decimals(B20_ADDRESS) == STOCK_DECIMALS
    assert backend.fetch_relayer_nonce("0x" + "2" * 40) == 7
    assert backend.estimate_gas(SAFE_ADDRESS, "0x3593564c00") == 200_000
    # Every request carried the fixed identifier and documented user agent.
    assert all(call["id"] == 1 for call in script.calls)


def test_rpc_backend_retries_rate_limit_responses_then_succeeds() -> None:
    """A 429 then success serves the answer after exponential backoff."""
    sleeps: list[float] = []
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x5"})

    backend = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    assert backend.fetch_gas_price() == 5
    assert sleeps == [0.5]


def test_rpc_backend_raises_revert_errors_immediately() -> None:
    """A contract revert surfaces as the typed revert error, not a retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "reverted"}},
        )

    backend = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    with pytest.raises(ExecutorRpcRevertError, match="reverted"):
        backend.estimate_gas(SAFE_ADDRESS, "0x00")


def test_rpc_backend_fails_closed_on_garbage_responses() -> None:
    """Malformed quantities, short words, and unknown errors fail closed."""

    def handler(request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        if call["method"] == "eth_gasPrice":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 5})
        if call["method"] == "eth_call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0xdead"})
        if call["method"] == "eth_getBalance":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0xzz"})
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "nope"}},
        )

    backend = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    with pytest.raises(ExecutionUnavailableError, match="hexadecimal quantity"):
        backend.fetch_gas_price()
    with pytest.raises(ExecutionUnavailableError, match="instead of 32"):
        backend.fetch_usdc_allowance(SAFE_ADDRESS, AERODROME_ROUTER_ADDRESS)
    with pytest.raises(ExecutionUnavailableError, match="malformed quantity"):
        backend.fetch_eth_balance(SAFE_ADDRESS)
    with pytest.raises(ExecutionUnavailableError, match="RPC error -1"):
        backend.fetch_relayer_nonce(SAFE_ADDRESS)


def test_rpc_backend_fails_closed_on_unusable_responses() -> None:
    """Bad status, non-JSON, and result-less bodies refuse immediately."""
    bodies = [
        httpx.Response(404, text="gone"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1}),
    ]
    calls = {"index": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        response = bodies[calls["index"]]
        calls["index"] += 1
        return response

    backend = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )
    with pytest.raises(ExecutionUnavailableError, match="unexpected HTTP status 404"):
        backend.fetch_gas_price()
    with pytest.raises(ExecutionUnavailableError, match="not valid JSON"):
        backend.fetch_gas_price()
    with pytest.raises(ExecutionUnavailableError, match="neither result nor error"):
        backend.fetch_gas_price()


def test_rpc_backend_exhausts_retries_on_persistent_failures() -> None:
    """Transport errors and rate-limit errors fail closed after five attempts."""
    sleeps: list[float] = []
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32016, "message": "slow"}},
        )

    rate_limited = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    with pytest.raises(ExecutionUnavailableError, match="failed after 5 attempts"):
        rate_limited.fetch_gas_price()
    assert len(sleeps) == 4
    attempts["count"] = 0

    def broken_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    unreachable = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(broken_handler),
        sleep=lambda _: None,
    )
    with pytest.raises(ExecutionUnavailableError, match="transport error"):
        unreachable.fetch_gas_price()


def test_rpc_backend_bounds_response_size() -> None:
    """An oversized response body refuses without retry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    backend = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        max_response_bytes=1024,
        sleep=lambda _: None,
    )
    with pytest.raises(ExecutionUnavailableError, match="above the limit"):
        backend.fetch_gas_price()


def test_rpc_backend_receipt_polling_times_out_fail_closed() -> None:
    """A missing receipt raises the typed timeout after the bounded wait."""
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})

    backend = ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        timer=clock.timer,
        receipt_poll_seconds=2.0,
        receipt_timeout_seconds=6.0,
    )
    with pytest.raises(BroadcastTimeoutError, match="did not confirm within 6"):
        backend.await_transaction_receipt("0x" + "ab" * 32)
    assert clock.seconds >= 6.0


def test_rpc_backend_constructor_rejects_non_positive_bounds() -> None:
    """Every configured bound must be positive."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        ExecutorRpcBackend(rpc_url="https://fixture.example", timeout_seconds=0)
    with pytest.raises(ValueError, match="max_attempts"):
        ExecutorRpcBackend(rpc_url="https://fixture.example", max_attempts=0)
    with pytest.raises(ValueError, match="max_response_bytes"):
        ExecutorRpcBackend(rpc_url="https://fixture.example", max_response_bytes=0)
    with pytest.raises(ValueError, match="receipt_poll_seconds"):
        ExecutorRpcBackend(rpc_url="https://fixture.example", receipt_poll_seconds=0)
    with pytest.raises(ValueError, match="receipt_timeout_seconds"):
        ExecutorRpcBackend(rpc_url="https://fixture.example", receipt_timeout_seconds=0)


# ---------------------------------------------------------------------------
# Quote stage
# ---------------------------------------------------------------------------


def test_quote_prices_one_capped_swap_from_first_principles() -> None:
    """The quote's price, output, floor, and impact recompute exactly."""
    executor, _, _ = make_executor()
    quote = executor.quote("FIXc", Decimal("1"))
    # A one-to-one raw ratio over eight/six decimals prices the stock at 100.
    assert quote.price_usdc_per_stock == FIXTURE_STOCK_PRICE
    assert quote.usdc_in_units == ONE_USDC_UNITS
    # One USDC at 100 per stock is exactly 10_000 whole-stock micro-units.
    assert quote.expected_stock_units == 1_000_000
    assert quote.amount_out_min_units == 999_000
    assert quote.modeled_impact_fraction == Decimal(ONE_USDC_UNITS) / Decimal(
        FIXTURE_USDC_RESERVE + ONE_USDC_UNITS
    )
    assert quote.snapshot_block == 123
    assert quote.quote_age_seconds == 0
    assert quote.usdc_amount == Decimal("1")
    assert quote.expected_stock_amount == Decimal("0.01")
    assert executor.safe_address == SAFE_ADDRESS
    # A case-insensitive symbol still resolves through the registry.
    assert executor.quote("fixc", Decimal("1")).symbol == "FIXc"


def test_quote_treats_a_naive_snapshot_time_as_utc() -> None:
    """A snapshot without a timezone still quotes with its UTC interpretation."""
    naive = make_discovery((make_candidate(),), observed_at=BASE_NOW.replace(tzinfo=None))
    executor, _, _ = make_executor(sources=FakeSources(discovery=naive))
    quote = executor.quote("FIXc", Decimal("1"))
    assert quote.observed_at.tzinfo is not None
    assert quote.observed_at == BASE_NOW
    assert quote.quote_age_seconds == 0


@pytest.mark.parametrize(
    ("sources", "diagnostic"),
    [
        (
            FakeSources(registry=make_registry(status=RegistryStatus.INVALID)),
            "registry did not validate",
        ),
        (FakeSources(registry=make_registry(assets=())), "not in the official"),
        (
            FakeSources(discovery=make_discovery((), status=PoolDiscoveryStatus.UNAVAILABLE)),
            "no live Sugar-verified",
        ),
        (
            FakeSources(
                discovery=make_discovery((make_candidate(),), snapshot_block=None, observed_at=None)
            ),
            "no observation evidence",
        ),
    ],
)
def test_quote_refuses_every_fail_closed_source_shape(
    sources: FakeSources, diagnostic: str
) -> None:
    """Each broken source shape refuses with its own explanation."""
    executor, _, _ = make_executor(sources=sources)
    with pytest.raises(ExecutionRefusalError, match=diagnostic):
        executor.quote("FIXc", Decimal("1"))


def test_quote_refuses_non_positive_and_over_cap_amounts() -> None:
    """Amounts at or below zero and above the cap refuse before sources load."""
    executor, _, _ = make_executor()
    with pytest.raises(ExecutionRefusalError, match="swap amount must be positive"):
        executor.quote("FIXc", Decimal("0"))
    with pytest.raises(ExecutionRefusalError, match="exceeds the configured per-swap cap"):
        executor.quote("FIXc", Decimal("1.01"))


def test_quote_refuses_stale_snapshots() -> None:
    """A snapshot older than the staleness bound refuses."""
    stale = make_discovery((make_candidate(),), observed_at=BASE_NOW - timedelta(seconds=121))
    executor, _, _ = make_executor(sources=FakeSources(discovery=stale))
    with pytest.raises(ExecutionRefusalError, match="staleness bound"):
        executor.quote("FIXc", Decimal("1"))


def test_quote_refuses_when_impact_reaches_tolerance() -> None:
    """A thin reserve pushes the conservative impact bound to the tolerance."""
    thin = make_candidate(reserve0=ONE_USDC_UNITS)
    executor, _, _ = make_executor(sources=FakeSources(discovery=make_discovery((thin,))))
    with pytest.raises(ExecutionRefusalError, match="impact bound"):
        executor.quote("FIXc", Decimal("1"))


def test_quote_refuses_sub_unit_output() -> None:
    """A swap too small to deliver one stock unit refuses."""
    # A tiny sqrt ratio prices one stock near 1e12 USDC, so one whole USDC
    # cannot deliver even a single eight-decimal stock unit.
    pricey = make_candidate(sqrt_ratio=(1 << 96) // 100_000)
    executor, _, _ = make_executor(sources=FakeSources(discovery=make_discovery((pricey,))))
    with pytest.raises(ExecutionRefusalError, match="less than one stock unit"):
        executor.quote("FIXc", Decimal("0.000001"))


def test_quote_refuses_finer_than_usdc_precision() -> None:
    """An amount finer than USDC's six decimals refuses before quoting."""
    executor, _, _ = make_executor()
    with pytest.raises(ValueError, match="finer than 6 decimals"):
        executor.quote("FIXc", Decimal("0.0000001"))


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_builds_and_validates_without_broadcasting(tmp_path: Path) -> None:
    """A sufficient allowance builds one verified swap and nothing is sent."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script, safe_script = make_executor(
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
        audit_path=audit_path,
    )
    account = Account.create()
    report = executor.dry_run("FIXc", Decimal("1"), bytes(account.key), ephemeral_key=True)

    assert rpc_script.broadcasts == []
    assert report.mode == ExecutionMode.DRY_RUN
    assert report.relayer_address == account.address.lower()
    assert report.ephemeral_key is True
    assert report.usdc_allowance_units == ONE_USDC_UNITS
    assert report.gas_price_wei == FIXTURE_GAS_PRICE_WEI
    assert report.safe_eth_wei == FIXTURE_SAFE_ETH_WEI
    assert report.approval is None
    swap = report.swap
    assert isinstance(swap, BuiltExecutionTransaction)
    assert swap.role == ExecutionRole.SWAP
    assert swap.nonce == 4
    assert swap.deadline == expected_deadline()
    assert swap.signature_verified is True
    assert swap.gas_estimate == 200_000
    assert swap.usdc_units == ONE_USDC_UNITS
    assert swap.to_address == AERODROME_ROUTER_ADDRESS
    assert report.build_duration_ms == Decimal("0.000")
    # Every cap label, quote gates then preflight gates, reached the report.
    assert report.caps_enforced[0].startswith("swap amount at or below")
    assert report.caps_enforced[-1].startswith("Safe ETH balance at or above")
    # The quote and build events landed on the real audit chain in order.
    store = AuditStore(audit_path)
    records = store.read_records(10)
    assert [record.event_type for record in records] == [
        AuditEventType.EXECUTION_QUOTE,
        AuditEventType.EXECUTION_BUILT,
    ]
    payload = json.loads(records[1].payload_json)
    assert payload["role"] == "swap"
    assert payload["mode"] == "dry_run"
    assert payload["signature_verified"] is True
    assert payload["nonce"] == 4
    # No key material ever reached the audit chain.
    assert bytes(account.key).hex() not in records[0].payload_json
    assert bytes(account.key).hex() not in records[1].payload_json


def test_dry_run_sequences_a_bounded_approval_before_the_swap() -> None:
    """A short standing allowance adds one 20-USDC approval at the next nonce."""
    executor, rpc_script, safe_script = make_executor(
        rpc_script=ExecutorRpcScript(allowance_units=0),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True, True]),
    )
    report = executor.dry_run("FIXc", Decimal("1"), bytes(Account.create().key))

    assert report.approval is not None
    assert report.approval.role == ExecutionRole.APPROVAL
    assert report.approval.nonce == 4
    assert report.approval.usdc_units == 20_000_000
    assert report.approval.deadline == 0
    assert report.approval.to_address == BASE_USDC_ADDRESS.lower()
    assert report.swap.nonce == 5
    # The inner approval calldata reaching the estimate carries the exact cap.
    estimate_calls = [call for call in rpc_script.calls if call["method"] == "eth_estimateGas"]
    first_params = cast("list[dict[str, str]]", estimate_calls[0]["params"])
    assert "1312d00" in first_params[0]["data"]


def test_dry_run_reports_an_honestly_rejected_ephemeral_signature() -> None:
    """A non-owner key is reported rejected instead of failing the dry run."""
    executor, _, _ = make_executor(
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[False]),
    )
    report = executor.dry_run("FIXc", Decimal("1"), bytes(Account.create().key))

    assert report.swap.signature_verified is False
    assert "GS026" in report.swap.signature_diagnostic


def test_dry_run_survives_a_reverted_gas_estimate() -> None:
    """A reverting estimate is reported, not fatal, in the dry run."""
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(
            swap_gas_estimate=None,
            receipts=[make_receipt()],
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
    )
    report = executor.dry_run("FIXc", Decimal("1"), bytes(Account.create().key))

    assert report.swap.gas_estimate is None
    assert "reverted" in report.swap.gas_estimate_diagnostic


def test_dry_run_refuses_on_gas_price_and_eth_floor() -> None:
    """Preflight refusals happen before any transaction is built."""
    hot, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(gas_price_wei=1_000_000_001, receipts=[])
    )
    with pytest.raises(ExecutionRefusalError, match="gas price .* exceeds"):
        hot.dry_run("FIXc", Decimal("1"), bytes(Account.create().key))
    drained, _, _ = make_executor(rpc_script=ExecutorRpcScript(safe_eth_wei=1, receipts=[]))
    with pytest.raises(ExecutionRefusalError, match="below the documented floor"):
        drained.dry_run("FIXc", Decimal("1"), bytes(Account.create().key))


# ---------------------------------------------------------------------------
# Execute
# ---------------------------------------------------------------------------


def test_execute_broadcasts_once_and_tracks_the_confirmed_swap(tmp_path: Path) -> None:
    """A sufficient allowance broadcasts one delivery and folds the receipt."""
    audit_path = tmp_path / "audit.sqlite3"
    account = Account.create()
    executor, rpc_script, _ = make_executor(
        rpc_script=ExecutorRpcScript(
            receipts=[make_receipt(logs=[make_swap_log(amount1=-998_000)])],
        ),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
        audit_path=audit_path,
    )
    outcome = executor.execute("FIXc", Decimal("1"), bytes(account.key))

    assert len(rpc_script.broadcasts) == 1
    # The delivery transaction is a type-2 signed raw transaction.
    assert rpc_script.broadcasts[0].startswith("0x02")
    assert outcome.mode == ExecutionMode.EXECUTE
    assert outcome.succeeded is True
    assert outcome.approval is None
    receipt = outcome.swap
    assert isinstance(receipt, ExecutionReceiptOutcome)
    assert receipt.status == 1
    assert receipt.block_number == 101
    assert receipt.gas_used == 120_000
    assert receipt.effective_gas_price_wei == FIXTURE_GAS_PRICE_WEI
    assert receipt.realized_stock_units == 998_000
    assert receipt.quote_slippage_fraction == Decimal(2_000) / Decimal(1_000_000)
    # Quote, build, sent, and confirmed all reached the audit chain.
    store = AuditStore(audit_path)
    records = store.read_records(10)
    assert [record.event_type for record in records] == [
        AuditEventType.EXECUTION_QUOTE,
        AuditEventType.EXECUTION_BUILT,
        AuditEventType.EXECUTION_SENT,
        AuditEventType.EXECUTION_CONFIRMED,
    ]
    sent_payload = json.loads(records[2].payload_json)
    assert sent_payload["role"] == "swap"
    assert sent_payload["relayer_address"] == account.address.lower()
    confirmed_payload = json.loads(records[3].payload_json)
    assert confirmed_payload["realized_stock_units"] == 998_000
    assert confirmed_payload["expected_stock_units"] == 1_000_000
    for record in records:
        assert bytes(account.key).hex() not in record.payload_json


def test_execute_sequences_bounded_approval_then_swap() -> None:
    """A short allowance broadcasts the approval, re-reads the nonce, swaps."""
    swap_receipt = make_receipt(logs=[make_swap_log()])
    executor, rpc_script, safe_script = make_executor(
        rpc_script=ExecutorRpcScript(
            allowance_units=0,
            receipts=[make_receipt(), swap_receipt],
        ),
        safe_script=SafeRpcScript(
            nonce_reads=[4, 4, 5, 5],
            signature_verdicts=[True, True],
        ),
    )
    outcome = executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))

    assert len(rpc_script.broadcasts) == 2
    assert outcome.approval is not None
    assert outcome.approval.status == 1
    assert outcome.approval.realized_stock_units is None
    assert outcome.swap is not None
    assert outcome.swap.status == 1
    assert outcome.succeeded is True
    # The swap delivery carried the exact next Safe nonce after the approval.
    sent_estimates = [
        call for call in rpc_script.calls if call["method"] == "eth_sendRawTransaction"
    ]
    assert len(sent_estimates) == 2
    # Four nonce reads: preflight, freshness, post-approval, freshness.
    assert safe_script.nonce_reads == []


def test_execute_stops_after_a_reverted_approval() -> None:
    """A failed approval records the revert and never sends the swap."""
    executor, rpc_script, _ = make_executor(
        rpc_script=ExecutorRpcScript(
            allowance_units=0,
            receipts=[make_receipt(status=0)],
        ),
        safe_script=SafeRpcScript(
            nonce_reads=[4, 4],
            signature_verdicts=[True, True],
        ),
    )
    outcome = executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))

    assert len(rpc_script.broadcasts) == 1
    assert outcome.swap is None
    assert outcome.approval is not None
    assert outcome.approval.status == 0
    assert "reverted" in outcome.approval.diagnostic
    assert outcome.succeeded is False


def test_execute_records_a_reverted_swap_receipt(tmp_path: Path) -> None:
    """A reverting swap folds into a failed outcome with its diagnostic."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(receipts=[make_receipt(status=0)]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
        audit_path=audit_path,
    )
    outcome = executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))

    assert outcome.swap is not None
    assert outcome.swap.status == 0
    assert outcome.swap.realized_stock_units is None
    assert outcome.swap.quote_slippage_fraction is None
    assert outcome.succeeded is False
    records = AuditStore(audit_path).read_records(10)
    assert records[-1].event_type == AuditEventType.EXECUTION_FAILED
    assert json.loads(records[-1].payload_json)["outcome"] == "failed"


def test_execute_refuses_a_rejected_owner_signature_before_broadcast() -> None:
    """A non-owner key refuses before any delivery transaction is signed."""
    executor, rpc_script, _ = make_executor(
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[False]),
    )
    with pytest.raises(ExecutionRefusalError, match="checkSignatures rejected"):
        executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))
    assert rpc_script.broadcasts == []


def test_execute_refuses_a_nonce_that_advanced_mid_attempt() -> None:
    """A Safe nonce moving under the attempt refuses with nothing broadcast."""
    executor, rpc_script, _ = make_executor(
        safe_script=SafeRpcScript(nonce_reads=[4, 5], signature_verdicts=[True]),
    )
    with pytest.raises(ExecutionRefusalError, match="nonce advanced"):
        executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))
    assert rpc_script.broadcasts == []


def test_execute_refuses_a_missing_swap_gas_estimate() -> None:
    """A reverting swap estimate refuses before broadcasting."""
    executor, rpc_script, _ = make_executor(
        rpc_script=ExecutorRpcScript(swap_gas_estimate=None, receipts=[]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    with pytest.raises(ExecutionRefusalError, match="no on-chain gas estimate|estimate reverted"):
        executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))
    assert rpc_script.broadcasts == []


def test_execute_refuses_a_missing_approval_gas_estimate() -> None:
    """A reverting approval estimate refuses before any broadcast."""
    executor, rpc_script, _ = make_executor(
        rpc_script=ExecutorRpcScript(allowance_units=0, approval_gas_estimate=None, receipts=[]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True, True]),
    )
    with pytest.raises(ExecutionRefusalError, match="approval.*no on-chain gas estimate"):
        executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))
    assert rpc_script.broadcasts == []


def test_execute_refuses_when_the_relayer_cannot_afford_the_gas() -> None:
    """An unfunded relayer refuses with the exact shortfall."""
    executor, rpc_script, _ = make_executor(
        rpc_script=ExecutorRpcScript(relayer_eth_wei=1_000, receipts=[]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    with pytest.raises(ExecutionRefusalError, match="fund the"):
        executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))
    assert rpc_script.broadcasts == []


def test_execute_surfaces_a_broadcast_timeout() -> None:
    """A never-including transaction raises the typed timeout."""
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(receipts=[None]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    with pytest.raises(BroadcastTimeoutError):
        executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))


def test_execute_is_byte_deterministic_for_identical_inputs() -> None:
    """Identical runs produce identical hashes and identical delivery bytes."""
    account = Account.create()
    receipts_a: list[dict[str, object] | None] = [make_receipt(logs=[make_swap_log()])]
    first, first_script, _ = make_executor(
        rpc_script=ExecutorRpcScript(receipts=list(receipts_a)),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    first_outcome = first.execute("FIXc", Decimal("1"), bytes(account.key))
    second, second_script, _ = make_executor(
        rpc_script=ExecutorRpcScript(receipts=list(receipts_a)),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    second_outcome = second.execute("FIXc", Decimal("1"), bytes(account.key))

    assert first_script.broadcasts == second_script.broadcasts
    assert first_outcome.swap is not None and second_outcome.swap is not None
    assert first_outcome.swap.safe_tx_hash == second_outcome.swap.safe_tx_hash
    assert first_outcome.swap.transaction_hash == second_outcome.swap.transaction_hash


def test_execute_skips_realized_amounts_when_no_swap_log_is_present() -> None:
    """A confirmed swap without a matching log reports no realized units."""
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(
            receipts=[make_receipt(logs=[{**make_swap_log(), "address": "0x" + "9" * 40}])]
        ),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    outcome = executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))
    assert outcome.swap is not None
    assert outcome.swap.realized_stock_units is None
    assert outcome.swap.quote_slippage_fraction is None
    assert outcome.succeeded is True


def test_execute_reads_realized_units_from_the_token0_side() -> None:
    """A stock-token0 pool reports its output from the negative token0 delta."""
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(
            receipts=[
                make_receipt(
                    logs=[
                        "not-a-log",
                        {**make_swap_log(amount0=-997_000, amount1=ONE_USDC_UNITS)},
                    ]
                )
            ]
        ),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    outcome = executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))
    assert outcome.swap is not None
    assert outcome.swap.realized_stock_units == 997_000


@pytest.mark.parametrize(
    ("receipt", "diagnostic"),
    [
        ({**make_receipt(), "status": None}, "carried no status field"),
        (
            {k: v for k, v in make_receipt().items() if k != "blockNumber"},
            "carried no blockNumber field",
        ),
        ({**make_receipt(), "gasUsed": "0xzz"}, "gasUsed.*was malformed"),
    ],
)
def test_execute_fails_closed_on_unusable_receipts(
    receipt: dict[str, object], diagnostic: str
) -> None:
    """Every malformed receipt field refuses with its own explanation."""
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(receipts=[receipt]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    with pytest.raises(ExecutionUnavailableError, match=diagnostic):
        executor.execute("FIXc", Decimal("1"), bytes(Account.create().key))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def make_cli_settings(tmp_path: Path) -> SimpleNamespace:
    """Build the settings-shaped object the CLI reads."""
    return SimpleNamespace(
        base_rpc_url="https://fixture.example",
        lp_sugar_address="0x27fc745390d1f4baf8d184fbd97748340f786634",
        audit_database_path=tmp_path / "audit.sqlite3",
    )


def test_cli_quote_prints_the_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The quote subcommand prices through the wired executor and exits zero."""
    executor, _, _ = make_executor()
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend"),
        patch("aero_bot.executor.SwapExecutor", return_value=executor),
    ):
        exit_code = main(["quote", "--symbol", "FIXc", "--amount", "1"])

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "FIXc" in output
    assert POOL_ADDRESS in output
    assert "amountOutMinimum 999000" in output


def test_cli_dry_run_uses_an_ephemeral_key_without_the_keychain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The --ephemeral-key dry run never reads the Keychain."""
    executor, _, _ = make_executor()
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend"),
        patch("aero_bot.executor.SwapExecutor", return_value=executor),
        patch("aero_bot.executor.KeychainKeySource") as keychain,
    ):
        exit_code = main(["dry-run", "--symbol", "FIXc", "--amount", "1", "--ephemeral-key"])

    assert exit_code == EXIT_OK
    keychain.from_environment.assert_not_called()
    output = capsys.readouterr().out
    assert "ephemeral" in output
    assert "nothing broadcast" in output


def test_cli_dry_run_defaults_to_the_keychain_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run without the flag reads the Keychain key and says so."""
    account = Account.create()
    executor, _, _ = make_executor()

    class FakeKeychain:
        @classmethod
        def from_environment(cls) -> "FakeKeychain":
            return cls()

        def load_signing_key(self) -> bytes:
            return bytes(account.key)

    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend"),
        patch("aero_bot.executor.SwapExecutor", return_value=executor),
        patch("aero_bot.executor.KeychainKeySource", FakeKeychain),
    ):
        exit_code = main(["dry-run", "--symbol", "FIXc", "--amount", "1"])

    assert exit_code == EXIT_OK
    assert "Keychain key" in capsys.readouterr().out


def test_cli_dry_run_prints_the_bounded_approval_sequence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A short standing allowance prints the approval build before the swap."""
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(allowance_units=0),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True, True]),
    )
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend"),
        patch("aero_bot.executor.SwapExecutor", return_value=executor),
    ):
        exit_code = main(["dry-run", "--symbol", "FIXc", "--amount", "1", "--ephemeral-key"])

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "approval:" in output
    assert "swap:" in output


def test_cli_json_flag_prints_every_typed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both dry-run and execute --json print their complete typed models."""
    account = Account.create()

    class FakeKeychain:
        @classmethod
        def from_environment(cls) -> "FakeKeychain":
            return cls()

        def load_signing_key(self) -> bytes:
            return bytes(account.key)

    for command in ("dry-run", "execute"):
        # A fresh executor per command keeps the scripted nonce sequence exact.
        executor, _, _ = make_executor(
            rpc_script=ExecutorRpcScript(receipts=[make_receipt(logs=[make_swap_log()])]),
            safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
        )
        arguments = ["--symbol", "FIXc", "--amount", "1", "--json"]
        if command == "dry-run":
            arguments.append("--ephemeral-key")
        else:
            arguments.append("--confirm-broadcast")
        with (
            patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
            patch("aero_bot.executor.AuditStore"),
            patch("aero_bot.executor.LiveExecutionSources"),
            patch("aero_bot.executor.ExecutorRpcBackend"),
            patch("aero_bot.executor.SafeTransactionRpcBackend"),
            patch("aero_bot.executor.SwapExecutor", return_value=executor),
            patch("aero_bot.executor.KeychainKeySource", FakeKeychain),
        ):
            exit_code = main([command, *arguments])
        assert exit_code == EXIT_OK
        printed = json.loads(capsys.readouterr().out)
        assert printed["mode"] == command.replace("-", "_")
        assert printed["quote"]["symbol"] == "FIXc"


def test_cli_execute_prints_the_failed_approval_diagnostic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reverted approval prints its receipt and diagnostic and exits one."""
    account = Account.create()
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(allowance_units=0, receipts=[make_receipt(status=0)]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True, True]),
    )

    class FakeKeychain:
        @classmethod
        def from_environment(cls) -> "FakeKeychain":
            return cls()

        def load_signing_key(self) -> bytes:
            return bytes(account.key)

    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend"),
        patch("aero_bot.executor.SwapExecutor", return_value=executor),
        patch("aero_bot.executor.KeychainKeySource", FakeKeychain),
    ):
        exit_code = main(["execute", "--symbol", "FIXc", "--amount", "1", "--confirm-broadcast"])

    assert exit_code == EXIT_FAILURE
    output = capsys.readouterr().out
    assert "approval: REVERTED" in output
    assert "reverted on-chain" in output
    assert "FAILED" in output


def test_cli_fails_when_the_audit_store_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unavailable audit store exits one before any executor wiring."""
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore", side_effect=OSError("disk full")),
        patch("aero_bot.executor.SwapExecutor") as executor_factory,
    ):
        exit_code = main(["quote", "--symbol", "FIXc", "--amount", "1"])

    assert exit_code == EXIT_FAILURE
    assert "audit store is unavailable" in capsys.readouterr().err
    executor_factory.assert_not_called()


def test_cli_execute_refuses_without_the_confirmation_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Execute without --confirm-broadcast refuses before any key or broadcast."""
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend"),
        patch("aero_bot.executor.SwapExecutor") as executor_factory,
        patch("aero_bot.executor.KeychainKeySource") as keychain,
    ):
        exit_code = main(["execute", "--symbol", "FIXc", "--amount", "1"])

    assert exit_code == EXIT_REFUSED
    assert "--confirm-broadcast" in capsys.readouterr().err
    # The wired executor was never asked to broadcast anything.
    executor_factory.return_value.execute.assert_not_called()
    keychain.from_environment.assert_not_called()


def test_cli_execute_confirmed_broadcast_uses_the_keychain_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Execute with the flag reads the Keychain key and threads the Safe."""
    account = Account.create()
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(receipts=[make_receipt(logs=[make_swap_log()])]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )

    class FakeKeychain:
        @classmethod
        def from_environment(cls) -> "FakeKeychain":
            return cls()

        def load_signing_key(self) -> bytes:
            return bytes(account.key)

    monkeypatch.setenv(SAFE_ADDRESS_ENV, SAFE_ADDRESS)
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend") as safe_backend_factory,
        patch("aero_bot.executor.SwapExecutor", return_value=executor) as factory,
        patch("aero_bot.executor.KeychainKeySource", FakeKeychain),
    ):
        exit_code = main(["execute", "--symbol", "FIXc", "--amount", "1", "--confirm-broadcast"])

    assert exit_code == EXIT_OK
    # The executor and Safe backend were wired to the environment's Safe.
    assert factory.call_args.kwargs["safe_address"] == SAFE_ADDRESS
    assert safe_backend_factory.call_args.kwargs["safe_address"] == SAFE_ADDRESS
    output = capsys.readouterr().out
    assert "confirmed" in output
    assert "succeeded" in output


def test_cli_execute_without_the_override_targets_the_canary_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default Safe is the canary deployment when no override is set."""
    account = Account.create()

    class FakeKeychain:
        @classmethod
        def from_environment(cls) -> "FakeKeychain":
            return cls()

        def load_signing_key(self) -> bytes:
            return bytes(account.key)

    monkeypatch.delenv(SAFE_ADDRESS_ENV, raising=False)
    executor, _, _ = make_executor(
        rpc_script=ExecutorRpcScript(receipts=[make_receipt(logs=[make_swap_log()])]),
        safe_script=SafeRpcScript(nonce_reads=[4, 4], signature_verdicts=[True]),
    )
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend") as safe_backend_factory,
        patch("aero_bot.executor.SwapExecutor", return_value=executor),
        patch("aero_bot.executor.KeychainKeySource", FakeKeychain),
    ):
        main(["execute", "--symbol", "FIXc", "--amount", "1", "--confirm-broadcast"])

    assert safe_backend_factory.call_args.kwargs["safe_address"] == (DEFAULT_CANARY_SAFE_ADDRESS)


def test_cli_refusals_exit_two_and_failures_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pre-sign refusal exits two; an unavailable read exits one."""
    refusing = SimpleNamespace(
        quote=lambda symbol, amount: (_ for _ in ()).throw(ExecutionRefusalError("over cap"))
    )
    failing = SimpleNamespace(
        quote=lambda symbol, amount: (_ for _ in ()).throw(
            ExecutionUnavailableError("endpoint down")
        )
    )
    for fake_executor, expected in ((refusing, EXIT_REFUSED), (failing, EXIT_FAILURE)):
        with (
            patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
            patch("aero_bot.executor.AuditStore"),
            patch("aero_bot.executor.SwapExecutor", return_value=fake_executor),
        ):
            exit_code = main(["quote", "--symbol", "FIXc", "--amount", "1"])
        assert exit_code == expected
    assert "refused" in capsys.readouterr().err


def test_cli_json_flag_prints_the_typed_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json prints the complete model rather than the human summary."""
    executor, _, _ = make_executor()
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.executor.AuditStore"),
        patch("aero_bot.executor.LiveExecutionSources"),
        patch("aero_bot.executor.ExecutorRpcBackend"),
        patch("aero_bot.executor.SafeTransactionRpcBackend"),
        patch("aero_bot.executor.SwapExecutor", return_value=executor),
    ):
        exit_code = main(["quote", "--symbol", "FIXc", "--amount", "1", "--json"])

    assert exit_code == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["symbol"] == "FIXc"
    assert printed["expected_stock_units"] == 1_000_000


def test_cli_rejects_non_positive_amounts(tmp_path: Path) -> None:
    """A non-positive amount is a usage error."""
    with (
        patch("aero_bot.executor.Settings", return_value=make_cli_settings(tmp_path)),
        pytest.raises(SystemExit) as excinfo,
    ):
        main(["quote", "--symbol", "FIXc", "--amount", "0"])
    assert excinfo.value.code == 2
