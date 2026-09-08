"""Captain-authorized exit swap: the Safe's ENTIRE remaining AAPLc balance to USDC.

Reuses the proven audited swap path (universal-router execute through the Safe)
in the reverse direction: exact-input AAPLc, exact-output USDC, recipient Safe.
Discipline is identical to the productized surfaces: registry-verified token,
known verified pool (pinned from tonight's six Sugar discoveries; slot0 read
fresh for the quote), per-step Safe nonce, byte-pinned hash, live
checkSignatures validation, fresh eth_estimateGas, bounded delivery (type 2,
gas = estimate + 20 percent, fee at the observed price capped at 1 gwei,
relayer PENDING nonce, relayer ETH preflight), hash printed before any wait,
audit records appended before receipt waits, receipt confirmation across
rotating endpoints before the next step, and a final all-USDC verification.
"""

import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext

from eth_account import Account
from eth_utils.address import to_checksum_address
from pydantic import BaseModel

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.config import Settings
from aero_bot.executor import (
    AERODROME_ROUTER_ADDRESS,
    ExecutorRpcBackend,
    LiveExecutionSources,
    build_approval_calldata,
    build_swap_calldata,
    build_swap_path,
)
from aero_bot.history import price_usdc_per_stock
from aero_bot.keychain import KeychainKeySource
from aero_bot.lp_executor import EXECUTE_RECEIPT_ENDPOINT_URLS
from aero_bot.safe_tx import (
    SafeTransaction,
    SafeTransactionRpcBackend,
    build_exec_transaction_calldata,
    build_safe_transaction,
    sign_safe_tx_hash,
)

SAFE = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
RELAYER = "0x0c49cc4d53423ccd6be2bcf115a25f418649c5c9"
STOCK = "0xb200000000000000000000c2e324d24d7eecd1fb"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
POOL = "0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0"
TICK_SPACING = 10
STOCK_DECIMALS = 8
TOLERANCE = Decimal("0.01")  # the captain's 1 percent calibration
GAS_BUFFER = Decimal("1.2")
GAS_PRICE_CAP_WEI = 1_000_000_000
RELAYER_ETH_FLOOR_WEI = 200_000_000_000_000  # 0.0002 ETH
RECEIPT_WAIT_SECONDS = 600
SLOT0 = "0x3850c7bd"
Q96 = Decimal(2) ** 96

SETTINGS = Settings()
RPC = ExecutorRpcBackend(rpc_url=SETTINGS.base_rpc_url)
SAFE_RPC = SafeTransactionRpcBackend(rpc_url=SETTINGS.base_rpc_url, safe_address=SAFE)
AUDIT = AuditStore(SETTINGS.audit_database_path)
RECEIPT_BACKENDS = [RPC] + [
    ExecutorRpcBackend(rpc_url=url)
    for url in EXECUTE_RECEIPT_ENDPOINT_URLS
    if url != SETTINGS.base_rpc_url
]

TIMINGS: dict[str, object] = {"stages": {}}


def mark(stage: str, **fields: object) -> None:
    TIMINGS["stages"][stage] = {"epoch": time.time(), **fields}
    print(f"[{time.strftime('%H:%M:%S', time.gmtime())}] {stage}: {fields}", flush=True)


class QuotePayload(BaseModel):
    """Audit payload for the exit-swap quote; no credential-bearing fields."""

    action: str = "swap_exit"
    symbol: str
    pool_address: str
    amount_in_units: int
    expected_out_units: int
    amount_out_min_units: int
    price_usdc_per_stock: str
    router_allowance_units: int


class StepPayload(BaseModel):
    """Audit payload for one exit-swap step's lifecycle; no secrets."""

    action: str = "swap_exit"
    symbol: str
    step_role: str
    safe_nonce: int
    transaction_hash: str
    gas_used: int | None = None
    fee_wei: int | None = None
    block_number: int | None = None
    diagnostic: str = ""


def audit(event: AuditEventType, payload: BaseModel) -> None:
    AUDIT.append(event, payload, datetime.now(timezone.utc))


def now_ms() -> float:
    return time.time() * 1000


def await_receipt(tx_hash: str) -> dict[str, object]:
    deadline = time.monotonic() + RECEIPT_WAIT_SECONDS
    backend_index = 0
    while time.monotonic() < deadline:
        backend = RECEIPT_BACKENDS[backend_index % len(RECEIPT_BACKENDS)]
        try:
            receipt = backend.fetch_transaction_receipt(tx_hash)
        except Exception as error:  # noqa: BLE001
            print(f"receipt poll failed on backend: {error}", file=sys.stderr, flush=True)
            receipt = None
        if receipt is not None:
            return receipt
        backend_index += 1
        time.sleep(3)
    raise TimeoutError(f"no receipt for {tx_hash} within {RECEIPT_WAIT_SECONDS}s")


def run_step(role: str, to_address: str, calldata: str, key_bytes: bytes) -> dict[str, object]:
    """Build, validate, estimate, deliver, and confirm one Safe step."""
    stage: dict[str, float] = {}

    # Safe nonce, build, and sign.
    started = now_ms()
    nonce = SAFE_RPC.fetch_live_nonce()
    transaction = SafeTransaction(to_address=to_address, data=calldata, nonce=nonce)
    built = build_safe_transaction(transaction, SAFE)
    signature = sign_safe_tx_hash(key_bytes, built.safe_tx_hash)
    exec_calldata = build_exec_transaction_calldata(transaction, signature)
    stage["build_ms"] = now_ms() - started

    # Byte-exact rebuild pin: the validated hash is the only hash we deliver.
    rebuilt = build_safe_transaction(
        SafeTransaction(to_address=to_address, data=calldata, nonce=nonce), SAFE
    )
    if rebuilt.safe_tx_hash != built.safe_tx_hash:
        raise RuntimeError("rebuild hash mismatch; refusing")

    # Live checkSignatures proof.
    started = now_ms()
    validation = SAFE_RPC.validate_owner_signature(built, signature)
    stage["validate_ms"] = now_ms() - started
    if not validation.verified:
        raise RuntimeError(f"live signature rejected: {validation.diagnostic}")

    # Fresh on-chain estimate with every predecessor mined.
    started = now_ms()
    estimate = None
    for attempt in range(4):
        try:
            estimate = RPC.estimate_gas(SAFE, exec_calldata)
            break
        except Exception as error:  # noqa: BLE001
            message = str(error)
            if "GS026" in message and attempt < 3:
                print(f"GS026 endpoint lag; re-reading in 4s ({attempt + 1}/3)", flush=True)
                time.sleep(4)
                continue
            raise RuntimeError(f"estimate reverted for {role}: {message}") from error
    stage["estimate_ms"] = now_ms() - started
    assert estimate is not None

    # Delivery: type 2, buffered gas, observed price capped, relayer PENDING nonce.
    started = now_ms()
    gas_price = min(RPC.fetch_gas_price(), GAS_PRICE_CAP_WEI)
    relayer_nonce = RPC.fetch_relayer_nonce(RELAYER)
    relayer_balance = RPC.fetch_eth_balance(RELAYER)
    gas_limit = int(
        (Decimal(estimate) * GAS_BUFFER).to_integral_value(rounding=ROUND_CEILING)
    )
    cost = gas_limit * gas_price
    if relayer_balance < max(RELAYER_ETH_FLOOR_WEI, 2 * cost):
        raise RuntimeError(
            f"relayer ETH {relayer_balance} below floor + 2x cost {max(RELAYER_ETH_FLOOR_WEI, 2 * cost)}"
        )
    raw = "0x" + bytes(
        Account.sign_transaction(
            {
                "to": to_checksum_address(SAFE),
                "data": exec_calldata,
                "nonce": relayer_nonce,
                "gas": gas_limit,
                "maxFeePerGas": gas_price,
                "maxPriorityFeePerGas": gas_price,
                "chainId": 8453,
                "type": 2,
            },
            key_bytes,
        ).raw_transaction
    ).hex()
    stage["delivery_ms"] = now_ms() - started

    # Broadcast: print the hash, audit, then wait.
    started = now_ms()
    tx_hash = RPC.send_raw_transaction(raw)
    print(f"[{role}] BROADCAST {tx_hash} (Safe nonce {nonce}, gas {gas_limit} at {gas_price} wei, estimate {estimate})", flush=True)
    audit(
        AuditEventType.EXECUTION_SENT,
        StepPayload(symbol="AAPLc", step_role=role, safe_nonce=nonce, transaction_hash=tx_hash),
    )
    receipt = await_receipt(tx_hash)
    stage["inclusion_ms"] = now_ms() - started
    status = int(str(receipt.get("status", "0x0")), 16)
    gas_used = int(str(receipt.get("gasUsed", "0x0")), 16)
    block_number = int(str(receipt.get("blockNumber", "0x0")), 16)
    effective = int(str(receipt.get("effectiveGasPrice", "0x0")), 16)
    if status != 1:
        audit(
            AuditEventType.EXECUTION_FAILED,
            StepPayload(
                symbol="AAPLc", step_role=role, safe_nonce=nonce, transaction_hash=tx_hash,
                gas_used=gas_used, block_number=block_number, diagnostic="included with status 0",
            ),
        )
        raise RuntimeError(f"{role} delivery reverted on-chain: {tx_hash}")
    audit(
        AuditEventType.EXECUTION_CONFIRMED,
        StepPayload(
            symbol="AAPLc", step_role=role, safe_nonce=nonce, transaction_hash=tx_hash,
            gas_used=gas_used, fee_wei=gas_used * effective, block_number=block_number,
        ),
    )
    print(
        f"[{role}] INCLUDED {tx_hash} block {block_number}, {gas_used} gas at {effective} wei "
        f"({gas_used * effective} wei fee)",
        flush=True,
    )
    return {
        "role": role, "safe_nonce": nonce, "tx_hash": tx_hash, "gas_used": gas_used,
        "gas_price_wei": effective, "fee_wei": gas_used * effective,
        "block_number": block_number, "estimate": estimate, "stage_ms": stage,
    }


def main() -> None:
    key_bytes = KeychainKeySource.from_environment().load_signing_key()
    relayer = Account.from_key(key_bytes).address
    if relayer.lower() != RELAYER:
        raise RuntimeError("keychain key is not the canary relayer; refusing")

    # Registry-verified symbol on the proven pool; fresh slot0 for the quote.
    registry = LiveExecutionSources(
        rpc_url=SETTINGS.base_rpc_url, sugar_address=SETTINGS.lp_sugar_address
    ).load_registry()
    listing = next(
        (a for a in registry.assets if a.symbol.lower() == "aaplc"), None
    )
    if listing is None or str(listing.address).lower() != STOCK:
        raise RuntimeError("AAPLc missing from the verified B20 registry; refusing")

    started = now_ms()
    raw_slot0 = RPC.eth_call(POOL, SLOT0)
    sqrt_x96 = int(raw_slot0[2:66], 16)
    # token0 = USDC (6 decimals), token1 = AAPLc (8 decimals), the live shape.
    price = price_usdc_per_stock(sqrt_x96, False, STOCK_DECIMALS, 6)
    balance = RPC.fetch_token_balance(STOCK, SAFE)
    allowance = RPC.fetch_erc20_allowance(STOCK, SAFE, AERODROME_ROUTER_ADDRESS)
    if balance <= 0:
        raise RuntimeError("Safe holds no AAPLc; nothing to swap")
    with localcontext() as ctx:
        ctx.prec = 50
        human_in = Decimal(balance).scaleb(-STOCK_DECIMALS)
        expected_out = int(
            (human_in * price * Decimal(10) ** 6).to_integral_value(rounding=ROUND_FLOOR)
        )
    amount_out_min = int(
        (Decimal(expected_out) * (Decimal(1) - TOLERANCE)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if amount_out_min <= 0:
        raise RuntimeError("quoted output floors to zero; refusing")
    mark(
        "quote", balance_units=balance, price=str(round(price, 4)),
        expected_usdc_units=expected_out, min_units=amount_out_min,
        allowance_units=allowance,
    )
    audit(
        AuditEventType.EXECUTION_QUOTE,
        QuotePayload(
            symbol="AAPLc", pool_address=POOL, amount_in_units=balance,
            expected_out_units=expected_out, amount_out_min_units=amount_out_min,
            price_usdc_per_stock=str(price), router_allowance_units=allowance,
        ),
    )
    TIMINGS["stages"]["quote"]["ms"] = now_ms() - started

    deadline = int(time.time()) + 8 * 60
    path = build_swap_path(STOCK, USDC, TICK_SPACING)
    steps: list[dict[str, object]] = []
    if allowance < balance:
        steps.append(
            run_step(
                "router_stock_allowance",
                STOCK,
                build_approval_calldata(AERODROME_ROUTER_ADDRESS, balance),
                key_bytes,
            )
        )
    swap_calldata = build_swap_calldata(SAFE, balance, amount_out_min, path, deadline)
    steps.append(run_step("swap_aaplc_to_usdc", AERODROME_ROUTER_ADDRESS, swap_calldata, key_bytes))

    # Final all-USDC verification.
    stock_after = RPC.fetch_token_balance(STOCK, SAFE)
    usdc_after = RPC.fetch_token_balance(USDC, SAFE)
    TIMINGS["steps"] = steps
    TIMINGS["final"] = {"stock_units": stock_after, "usdc_units": usdc_after}
    with open("run/swap_exit.timings.json", "w") as handle:
        json.dump(TIMINGS, handle, indent=2)
    print(f"final balances: AAPLc {stock_after} raw, USDC {usdc_after} raw", flush=True)
    if stock_after != 0:
        raise RuntimeError("Safe still holds AAPLc after the exit swap; investigate")


if __name__ == "__main__":
    main()
