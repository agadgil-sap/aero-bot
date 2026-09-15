"""Capped manual swap execution through the canary Safe on Base.

This module is the application's only signing and broadcast path, and it is
shaped around containment rather than convenience: every swap is a manual
one-shot CLI request, every hard cap is enforced in code before anything is
signed, the default mode builds and validates a transaction without ever
broadcasting it, and broadcasting exists only behind the explicit execute
subcommand. There is no loop, scheduler, watcher, or policy-driven trigger
anywhere in this module, and the policy engine is never wired to it.

The swap calldata is modeled byte-for-byte on the Safe's already-executed
reference transaction: one universal-router ``execute`` call carrying a single
``V3_SWAP_EXACT_IN`` command whose params struct holds six head words
(including the extra zero word this router fork appends after
``payerIsSender``), with the 43-byte concentrated-liquidity path
``USDC || 0x08 || tickSpacing(uint16) || stock`` and a deadline minutes out.
The approval the reference transaction preceded its swap with was exact, so
this executor's standing USDC allowance is a bounded number - twenty USDC by
default - and never the infinite maximum approval.

Everything the module audits, prints, or reports about the key it uses is the
relaying EOA's public address and the Safe-side hashes; key bytes arrive as
one argument, sign exactly one SafeTx hash per transaction and - when
broadcasting - the outer delivery transaction, and are never stored, logged,
or persisted.
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, cast, runtime_checkable

import httpx
from eth_account import Account
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from pydantic import BaseModel, Field, field_validator, model_validator

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.concentrated import MATH_PRECISION
from aero_bot.config import Settings
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.history import (
    SWAP_EVENT_TOPIC0,
    EventHistoryRpcBackend,
    decode_swap_log,
    price_usdc_per_stock,
)
from aero_bot.registry import B20RegistryResult, RegistryStatus, load_official_b20_registry
from aero_bot.safe_tx import (
    BuiltSafeTransaction,
    SafeSignatureValidation,
    SafeTransaction,
    SafeTransactionRpcBackend,
    StaleNonceError,
    build_exec_transaction_calldata,
    build_safe_transaction,
    ensure_usable_nonce,
    sign_safe_tx_hash,
)
from aero_bot.signing_key import load_signing_key_source
from aero_bot.sugar import DEFAULT_BASE_RPC_URL, LP_SUGAR_ADDRESS, LpSugarRpcBackend
from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    BASE_USDC_ADDRESS,
    AerodromeVenueAdapter,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
)

# The Aerodrome universal router of the user's executed reference swap; this
# address is the complete router whitelist for this release.
AERODROME_ROUTER_ADDRESS = "0xcaf22ce31298cf2bf1d152862f80216478ad7c67"
# The canary Safe whose owner key executes capped swaps.
DEFAULT_CANARY_SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
# Environment variable overriding which Safe the executor targets.
SAFE_ADDRESS_ENV = "AERO_BOT_SAFE_ADDRESS"
# keccak256("execute(bytes,bytes[],uint256)")[0:4], the router entry point.
ROUTER_EXECUTE_SELECTOR = "3593564c"
# The single command byte of the reference swap: V3_SWAP_EXACT_IN.
V3_SWAP_EXACT_IN_COMMAND = "00"
# The concentrated-liquidity path flag marking a tick-spacing segment.
V3_PATH_TICK_SPACING_FLAG = "08"
# One concentrated path segment is 20 + 3 + 20 bytes when it spans two tokens.
PATH_SEGMENT_BYTES = 43
# keccak256("approve(address,uint256)")[0:4], the bounded allowance setter.
ERC20_APPROVE_SELECTOR = "095ea7b3"
# keccak256("allowance(address,address)")[0:4], the standing allowance read.
ERC20_ALLOWANCE_SELECTOR = "dd62ed3e"
# keccak256("decimals()")[0:4], read on the stock token before quoting.
ERC20_DECIMALS_SELECTOR = "0x313ce567"
# keccak256("balanceOf(address)")[0:4], the inventory read this layer shares
# with the LP lifecycle executor.
ERC20_BALANCE_OF_SELECTOR = "70a08231"
# Native Base USDC carries six decimals everywhere in this release.
QUOTE_TOKEN_DECIMALS = 6
# Aerodrome's canonical volatile PoolFactory on Base, the source of the
# USDC/AERO pair the emissions-APR convention prices AERO from.
AERODROME_VOLATILE_FACTORY_ADDRESS = "0x420dd381b31aef6683db6b902084cb0ffece40da"
# keccak256("getPool(address,address,bool)")[0:4], the factory lookup.
POOL_FACTORY_GET_POOL_SELECTOR = "0x79bc57d5"
# keccak256("token0()")[0:4], the pair's token ordering read.
POOL_TOKEN0_SELECTOR = "0x0dfe1681"
# keccak256("reserve0()")[0:4].
POOL_RESERVE0_SELECTOR = "0x443cb4bc"
# keccak256("reserve1()")[0:4].
POOL_RESERVE1_SELECTOR = "0x5a76f25e"
# Every ABI word below is exactly 32 bytes.
WORD_BYTES = 32
# The reference swap's deadline sat eight minutes past its build time.
SWAP_DEADLINE_SECONDS = 8 * 60
# The hard ceiling no swap configuration may ever raise above.
SWAP_AMOUNT_CEILING_USDC = Decimal("5.00")
# The default per-swap cap for the canary phase.
DEFAULT_MAX_SWAP_USDC = Decimal("1.00")
# The default bounded standing allowance; the executor never approves more.
DEFAULT_APPROVAL_STANDING_CAP_USDC = Decimal("20")
# The approval cap cannot be configured above this documented bound.
APPROVAL_CAP_CEILING_USDC = Decimal("20")
# Gas parameters above one gwei effective price are refused before building.
DEFAULT_GAS_PRICE_CAP_WEI = 1_000_000_000
# A Safe ETH balance below this floor refuses the attempt; the floor sits
# just under the canary's observed 0.0001 ETH and only guards the drained
# state because the relaying EOA, not the Safe, pays the delivery gas.
DEFAULT_SAFE_ETH_FLOOR_WEI = 5 * 10**13
# The relaying EOA's broadcast floor: 0.0002 ETH of real headroom on top of
# any bounded gas cost, shared by the LP execute path.
DEFAULT_RELAYER_ETH_FLOOR_WEI = 2 * 10**14
# A quote older than two minutes is stale and refuses execution.
DEFAULT_QUOTE_MAX_AGE_SECONDS = 120
# The reference swap's amountOutMinimum sat about 0.1 percent below its quote.
DEFAULT_SLIPPAGE_TOLERANCE = Decimal("0.001")
# Slippage tolerance is capped at one percent for this release.
SLIPPAGE_TOLERANCE_CEILING = Decimal("0.01")
# Gas limits carry twenty percent headroom over the estimate for inclusion.
GAS_LIMIT_BUFFER_FRACTION = Decimal("0.2")
# Receipt polling runs every two seconds.
RECEIPT_POLL_SECONDS = 2.0
# Inclusion must arrive within two minutes or the attempt fails closed.
RECEIPT_TIMEOUT_SECONDS = 120.0
# Twenty seconds bounds one failed request without blocking the operator.
REQUEST_TIMEOUT_SECONDS = 20.0
# Five attempts with exponential backoff absorb public-RPC rate limiting.
MAX_REQUEST_ATTEMPTS = 5
# Backoff starts at half a second and doubles per retry.
BASE_BACKOFF_SECONDS = 0.5
# A small politeness gap between consecutive requests keeps this backend's
# read bursts under the public endpoint's request-rate limiter, exactly the
# pacing the Sugar enumeration's own page delay provides; twenty reads cost
# at most four seconds while a tripped limiter costs eight per request.
REQUEST_PACING_SECONDS = 0.2
# One MiB bounds every response far above the fixed-word calls made here.
MAX_RESPONSE_BYTES = 1024 * 1024
# Base's public endpoint reports rate limiting with this JSON-RPC error code.
RATE_LIMIT_ERROR_CODE = -32016
# The standard JSON-RPC code reporting an in-contract execution revert.
EXECUTION_REVERT_ERROR_CODE = 3
# The fixed JSON-RPC request identifier keeps calls reproducible.
JSON_RPC_ID = 1
# Timing metrics round to whole milliseconds.
TIMING_PRECISION = Decimal("0.001")
# CLI exit codes: zero on success, one on failures, two on refusals.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2


class ExecutionRefusalError(RuntimeError):
    """Refuse a request before signing with an actionable explanation."""


class ExecutionUnavailableError(RuntimeError):
    """Signal that a read-only or broadcast RPC step could not complete."""


class ExecutorRpcRevertError(ExecutionUnavailableError):
    """Signal that an RPC call reverted inside a contract."""


class BroadcastTimeoutError(ExecutionUnavailableError):
    """Signal that a broadcast transaction did not confirm within the bound."""


class ExecutionRole(StrEnum):
    """Identify which Safe transaction of one execution attempt is described."""

    # The bounded USDC allowance setup, sequenced before its swap.
    APPROVAL = "approval"
    # The router swap carrying the capped USDC amount.
    SWAP = "swap"


class ExecutionMode(StrEnum):
    """Identify whether one attempt stopped before or included broadcasting."""

    # Dry-run builds and validates without ever broadcasting.
    DRY_RUN = "dry_run"
    # Execute broadcasts and tracks inclusion on Base.
    EXECUTE = "execute"


class ExecutionPolicy(BaseModel):
    """Hold every hard execution cap enforced before anything is signed."""

    # Frozen strict fields keep one attempt's caps stable end to end.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The router whitelist is exactly the reference swap's router.
    router_address: EvmAddress = AERODROME_ROUTER_ADDRESS
    # The largest single swap this configuration permits, in USDC.
    max_swap_usdc: Annotated[Decimal, Field(gt=0)] = DEFAULT_MAX_SWAP_USDC
    # The bounded standing USDC allowance set when the current one is short.
    approval_standing_cap_usdc: Annotated[Decimal, Field(gt=0)] = DEFAULT_APPROVAL_STANDING_CAP_USDC
    # Effective gas price above this cap refuses the attempt.
    gas_price_cap_wei: Annotated[int, Field(gt=0)] = DEFAULT_GAS_PRICE_CAP_WEI
    # A Safe ETH balance below this floor refuses the attempt.
    safe_eth_floor_wei: Annotated[int, Field(ge=0)] = DEFAULT_SAFE_ETH_FLOOR_WEI
    # Quotes older than this many seconds refuse execution.
    quote_max_age_seconds: Annotated[int, Field(gt=0)] = DEFAULT_QUOTE_MAX_AGE_SECONDS
    # amountOutMinimum sits this fraction below the quoted output.
    slippage_tolerance_fraction: Annotated[Decimal, Field(gt=0)] = DEFAULT_SLIPPAGE_TOLERANCE

    @field_validator("router_address")
    @classmethod
    def require_whitelisted_router(cls, value: str) -> str:
        """Reject any router outside the single-address whitelist."""
        if value != AERODROME_ROUTER_ADDRESS:
            raise ValueError(
                f"router {value} is outside the execution whitelist "
                f"({AERODROME_ROUTER_ADDRESS} only)"
            )
        return value

    @model_validator(mode="after")
    def require_ceiling_compliance(self) -> "ExecutionPolicy":
        """Enforce the hard ceilings no configuration may exceed."""
        if self.max_swap_usdc > SWAP_AMOUNT_CEILING_USDC:
            raise ValueError(
                f"max_swap_usdc {self.max_swap_usdc} exceeds the hard ceiling of "
                f"{SWAP_AMOUNT_CEILING_USDC} USDC"
            )
        if self.approval_standing_cap_usdc > APPROVAL_CAP_CEILING_USDC:
            raise ValueError(
                f"approval_standing_cap_usdc {self.approval_standing_cap_usdc} exceeds the "
                f"documented bound of {APPROVAL_CAP_CEILING_USDC} USDC; the allowance is "
                "bounded and never infinite"
            )
        if self.approval_standing_cap_usdc < self.max_swap_usdc:
            raise ValueError(
                "approval_standing_cap_usdc must be at least max_swap_usdc or a swap could "
                "exceed its own allowance"
            )
        if self.slippage_tolerance_fraction > SLIPPAGE_TOLERANCE_CEILING:
            raise ValueError(
                f"slippage_tolerance_fraction {self.slippage_tolerance_fraction} exceeds the "
                f"{SLIPPAGE_TOLERANCE_CEILING} ceiling"
            )
        return self

    @property
    def max_swap_usdc_units(self) -> int:
        """Return the per-swap cap in raw six-decimal USDC units."""
        return usdc_units(self.max_swap_usdc)

    @property
    def approval_standing_cap_units(self) -> int:
        """Return the standing allowance cap in raw six-decimal USDC units."""
        return usdc_units(self.approval_standing_cap_usdc)


class SwapQuote(BaseModel):
    """Carry one capped swap's quote with its live pool evidence."""

    # Frozen strict fields keep the quoted numbers bound to their snapshot.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched onchain symbol of the B20 stock token.
    symbol: str
    # The whitelisted B20 stock contract being bought.
    token_address: EvmAddress
    # The live Sugar-discovered pool the quote priced through.
    pool_address: EvmAddress
    # The pool's Slipstream tick spacing, part of the swap path.
    tick_spacing: Annotated[int, Field(gt=0)]
    # The stock token's decimal count read from its contract.
    stock_decimals: Annotated[int, Field(gt=0)]
    # The exact USDC amount to swap, in raw six-decimal units.
    usdc_in_units: Annotated[int, Field(gt=0)]
    # The snapshot spot price of one whole stock token in USDC.
    price_usdc_per_stock: Annotated[Decimal, Field(gt=0)]
    # The quoted stock output in raw stock-decimal units.
    expected_stock_units: Annotated[int, Field(gt=0)]
    # The amountOutMinimum floor derived from the quote and tolerance.
    amount_out_min_units: Annotated[int, Field(gt=0)]
    # The conservative reserve-based price-impact bound as a fraction.
    modeled_impact_fraction: Annotated[Decimal, Field(ge=0, lt=1)]
    # The Sugar snapshot block anchoring every pool number in this quote.
    snapshot_block: Annotated[int, Field(ge=0)]
    # When the Sugar snapshot completed, timezone-aware.
    observed_at: datetime
    # Seconds between the snapshot and this quote's creation.
    quote_age_seconds: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def require_coherent_amounts(self) -> "SwapQuote":
        """Keep the minimum floor at or below the quoted output."""
        if self.amount_out_min_units > self.expected_stock_units:
            raise ValueError("amount_out_min_units exceeds the quoted output")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self

    @property
    def usdc_amount(self) -> Decimal:
        """Return the human USDC amount of this quote."""
        return Decimal(self.usdc_in_units).scaleb(-QUOTE_TOKEN_DECIMALS)

    @property
    def expected_stock_amount(self) -> Decimal:
        """Return the human stock amount of this quote."""
        return Decimal(self.expected_stock_units).scaleb(-self.stock_decimals)


class BuiltExecutionTransaction(BaseModel):
    """Describe one fully built and signed Safe transaction of an attempt."""

    # Frozen strict fields bind the report to the exact built content.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Which transaction of the attempt this is.
    role: ExecutionRole
    # The EIP-712 SafeTx hash the signature covers.
    safe_tx_hash: str
    # The contract the Safe transaction calls.
    to_address: EvmAddress
    # keccak256 of the complete execTransaction calldata.
    calldata_digest: str
    # The Safe nonce this transaction occupies.
    nonce: Annotated[int, Field(ge=0)]
    # The USDC amount in raw units: the allowance set or the swap input.
    usdc_units: Annotated[int, Field(gt=0)]
    # The router swap deadline, zero for the approval role.
    deadline: Annotated[int, Field(ge=0)] = 0
    # The read-only contract verdict on the produced owner signature.
    signature_verified: bool
    # The verdict's diagnostic, carrying revert evidence when rejected.
    signature_diagnostic: str
    # The on-chain gas estimate, absent when the estimate reverted.
    gas_estimate: Annotated[int, Field(gt=0)] | None
    # Why the gas estimate is absent, empty when it succeeded.
    gas_estimate_diagnostic: str = ""


class DryRunReport(BaseModel):
    """Report one complete build-and-validate attempt without broadcasting."""

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The capped quote the attempt priced.
    quote: SwapQuote
    # The approval transaction, absent when the standing allowance suffices.
    approval: BuiltExecutionTransaction | None
    # The swap transaction, always present on a successful dry run.
    swap: BuiltExecutionTransaction
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The live standing USDC allowance the Safe held at build time.
    usdc_allowance_units: Annotated[int, Field(ge=0)]
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: tuple[str, ...]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Annotated[Decimal, Field(ge=0)]


class ExecutionReceiptOutcome(BaseModel):
    """Carry one broadcast Safe transaction's inclusion evidence."""

    # Frozen strict fields preserve the exact on-chain outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Which broadcast transaction this receipt describes.
    role: ExecutionRole
    # The SafeTx hash of the executed Safe transaction.
    safe_tx_hash: str
    # The Base transaction hash that delivered execTransaction.
    transaction_hash: str
    # One when the transaction succeeded on-chain, zero when it reverted.
    status: Literal[0, 1]
    # The block that included the transaction.
    block_number: Annotated[int, Field(ge=0)]
    # Gas units the inclusion consumed.
    gas_used: Annotated[int, Field(ge=0)]
    # The effective gas price paid, in wei.
    effective_gas_price_wei: Annotated[int, Field(ge=0)]
    # The realized stock output in raw units; present for confirmed swaps.
    realized_stock_units: Annotated[int, Field(ge=0)] | None = None
    # The quote-versus-realized slippage fraction; present for swaps.
    quote_slippage_fraction: Decimal | None = None
    # Failure diagnostic for reverted transactions, empty on success.
    diagnostic: str = ""


class ExecutionOutcome(BaseModel):
    """Report one complete execute attempt including its broadcast results."""

    # Frozen strict fields preserve one coherent execution outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker proves this record came from the broadcast path.
    mode: Literal[ExecutionMode.EXECUTE] = ExecutionMode.EXECUTE
    # The capped quote the attempt priced.
    quote: SwapQuote
    # The Safe every broadcast transaction targeted.
    safe_address: EvmAddress
    # The public address of the relaying EOA.
    relayer_address: EvmAddress
    # The approval receipt, absent when no allowance change was needed.
    approval: ExecutionReceiptOutcome | None
    # The swap receipt, always present once the swap was broadcast.
    swap: ExecutionReceiptOutcome | None
    # Wall-clock duration of the build-and-sign phase in milliseconds.
    sign_duration_ms: Annotated[Decimal, Field(ge=0)]
    # Wall-clock duration from first broadcast to last inclusion.
    broadcast_duration_ms: Annotated[Decimal, Field(ge=0)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        """Return whether every broadcast transaction confirmed cleanly."""
        receipts = [receipt for receipt in (self.approval, self.swap) if receipt is not None]
        return bool(receipts) and all(
            receipt.status == 1 and receipt.diagnostic == "" for receipt in receipts
        )


class ExecutionQuotePayload(BaseModel):
    """Persist one quoted attempt's public numbers on the audit chain."""

    # Frozen strict fields keep the audited quote immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this quote belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The B20 stock contract address.
    token_address: EvmAddress
    # The Sugar-discovered pool address the quote priced through.
    pool_address: EvmAddress
    # The Sugar snapshot block anchoring the quote.
    snapshot_block: Annotated[int, Field(ge=0)]
    # The exact USDC input in raw units.
    usdc_in_units: Annotated[int, Field(gt=0)]
    # The quoted stock output in raw units.
    expected_stock_units: Annotated[int, Field(gt=0)]
    # The snapshot spot price of one whole stock token.
    price_usdc_per_stock: Decimal
    # The amountOutMinimum floor in raw units.
    amount_out_min_units: Annotated[int, Field(gt=0)]
    # The conservative reserve-based impact bound.
    modeled_impact_fraction: Decimal
    # The quote's age in seconds at audit time.
    quote_age_seconds: Annotated[int, Field(ge=0)]


class ExecutionBuiltPayload(BaseModel):
    """Persist one fully built Safe transaction's public evidence."""

    # Frozen strict fields keep the audited build immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this build belongs to.
    mode: ExecutionMode
    # Which transaction of the attempt was built.
    role: ExecutionRole
    # The SafeTx hash of the built transaction.
    safe_tx_hash: str
    # The contract the Safe transaction calls.
    to_address: EvmAddress
    # keccak256 of the complete execTransaction calldata.
    calldata_digest: str
    # The Safe nonce the transaction occupies.
    nonce: Annotated[int, Field(ge=0)]
    # The USDC amount in raw units: allowance set or swap input.
    usdc_units: Annotated[int, Field(gt=0)]
    # The router swap deadline, zero for the approval role.
    deadline: Annotated[int, Field(ge=0)] = 0
    # Whether the live contract accepted the owner signature read-only.
    signature_verified: bool
    # The on-chain gas estimate, absent when the estimate reverted.
    gas_estimate: Annotated[int, Field(gt=0)] | None
    # The transaction's build duration in milliseconds.
    build_duration_ms: Annotated[Decimal, Field(ge=0)]


class ExecutionSentPayload(BaseModel):
    """Persist one broadcast submission's public evidence."""

    # Frozen strict fields keep the audited submission immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Which transaction of the attempt was broadcast.
    role: ExecutionRole
    # The SafeTx hash of the broadcast Safe transaction.
    safe_tx_hash: str
    # The Base transaction hash that delivered execTransaction.
    transaction_hash: str
    # The public address of the relaying EOA.
    relayer_address: EvmAddress
    # The Safe contract the delivery transaction called.
    safe_address: EvmAddress


class ExecutionReceiptPayload(BaseModel):
    """Persist one inclusion outcome's public evidence."""

    # Frozen strict fields keep the audited receipt immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Whether the transaction confirmed or failed on-chain.
    outcome: Literal["confirmed", "failed"]
    # Which transaction of the attempt the receipt describes.
    role: ExecutionRole
    # The SafeTx hash of the executed Safe transaction.
    safe_tx_hash: str
    # The Base transaction hash that delivered execTransaction.
    transaction_hash: str
    # The including block.
    block_number: Annotated[int, Field(ge=0)]
    # Gas units the inclusion consumed.
    gas_used: Annotated[int, Field(ge=0)]
    # The effective gas price paid, in wei.
    effective_gas_price_wei: Annotated[int, Field(ge=0)]
    # The realized stock output in raw units; present for swaps.
    realized_stock_units: Annotated[int, Field(ge=0)] | None = None
    # The quoted stock output this receipt compares against.
    expected_stock_units: Annotated[int, Field(gt=0)] | None = None
    # The quote-versus-realized slippage fraction; present for swaps.
    quote_slippage_fraction: Decimal | None = None
    # Inclusion duration in milliseconds from broadcast to receipt.
    inclusion_duration_ms: Annotated[Decimal, Field(ge=0)]
    # Failure diagnostic for reverted transactions, empty on success.
    diagnostic: str = ""


@runtime_checkable
class ExecutionAuditSink(Protocol):
    """Define the append-only audit boundary execution attempts report to."""

    def append(
        self,
        event_type: AuditEventType,
        payload: BaseModel,
        created_at: datetime,
    ) -> object:
        """Persist one canonical payload on the immutable chain.

        Args:
            event_type: The execution event category being recorded.
            payload: Validated model carrying no secret-bearing fields.
            created_at: Time of the source event, including a timezone offset.

        Returns:
            The durable record or another acknowledged result.
        """
        ...


class ExecutionSources(Protocol):
    """Define the live read-only sources one execution attempt consumes."""

    def discover_pools(self) -> PoolDiscoveryResult:
        """Return the accepted B20/USDC pools in one block-pinned snapshot."""
        ...

    def read_token_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count."""
        ...

    def load_registry(self) -> B20RegistryResult:
        """Return the official Coinbase-issued B20 registry."""
        ...


class LiveExecutionSources:
    """Compose the live read-only quote sources one CLI attempt consumes."""

    def __init__(
        self,
        rpc_url: str = DEFAULT_BASE_RPC_URL,
        sugar_address: str = LP_SUGAR_ADDRESS,
        fallback_rpc_urls: Sequence[str] = (),
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        """Configure the live discovery, registry, and decimals sources.

        Args:
            rpc_url: Primary Base JSON-RPC endpoint used for reads.
            sugar_address: LP Sugar contract anchoring pool discovery.
            fallback_rpc_urls: Ordered alternate endpoints for transient read failures.
            transport: Optional injected HTTP transport for tests.
            sleep: Injected delay function used for retry backoff.
            progress: Optional callback receiving one human-readable line per
                enumerated page and per retried request during a Sugar sweep,
                so a slow enumeration reports progress instead of silence.
        """
        self._rpc_url = rpc_url
        self._fallback_rpc_urls = tuple(fallback_rpc_urls)
        self._sugar_address = normalize_evm_address(sugar_address)
        self._transport = transport
        self._sleep = sleep
        self._progress = progress
        # Token decimals are pure metadata, so one read per token is cached.
        self._decimals_cache: dict[str, int] = {}
        # One Sugar sweep per run: the first discovery is pinned (its pages
        # share one snapshot block by construction) and every later call in
        # the same process reuses that block-pinned batch, mirroring the
        # decimals cache.
        self._discovery_cache: PoolDiscoveryResult | None = None

    def load_registry(self) -> B20RegistryResult:
        """Return the packaged official B20 registry.

        Returns:
            The validated registry result with its status evidence.
        """
        return load_official_b20_registry()

    def discover_pools(self) -> PoolDiscoveryResult:
        """Return the accepted B20/USDC pools in one block-pinned snapshot.

        The first call runs the full Sugar enumeration and pins its result
        for the lifetime of this sources object, so every subsequent
        discovery in the same run reuses the same block-pinned sweep instead
        of re-enumerating; per-action freshness comes from the executor's
        staleness gate and the known-pool fast path, never from repeat
        sweeps.

        Returns:
            The venue adapter's verified discovery result.

        Raises:
            PoolDiscoveryUnavailableError: If the Sugar enumeration cannot
                complete with bounded retries.
        """
        if self._discovery_cache is not None:
            return self._discovery_cache
        registry = load_official_b20_registry()
        b20_addresses = frozenset(asset.address for asset in registry.assets)
        backend = LpSugarRpcBackend(
            rpc_url=self._rpc_url,
            sugar_address=self._sugar_address,
            fallback_rpc_urls=self._fallback_rpc_urls,
            transport=self._transport,
            sleep=self._sleep,
            progress=self._progress,
        )
        result = AerodromeVenueAdapter(backend).discover_pools(b20_addresses)
        self._discovery_cache = result
        return result

    def read_token_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count, cached per token.

        Args:
            token_address: ERC20 contract whose decimals() is read once.

        Returns:
            The token's decimal count.

        Raises:
            HistoryUnavailableError: If the read cannot complete or is
                malformed.
        """
        normalized = normalize_evm_address(token_address)
        cached = self._decimals_cache.get(normalized)
        if cached is not None:
            return cached
        backend = EventHistoryRpcBackend(
            rpc_url=self._rpc_url,
            transport=self._transport,
            sleep=self._sleep,
        )
        decimals = backend.read_erc20_decimals(normalized)
        self._decimals_cache[normalized] = decimals
        return decimals


def usdc_units(amount: Decimal) -> int:
    """Convert one human USDC amount into exact raw six-decimal units.

    Args:
        amount: Positive USDC amount with at most six decimal places.

    Returns:
        The raw unit count.

    Raises:
        ValueError: If the amount carries more precision than USDC holds.
    """
    scaled = amount.scaleb(QUOTE_TOKEN_DECIMALS)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"USDC amount {amount} is finer than {QUOTE_TOKEN_DECIMALS} decimals")
    return int(scaled.to_integral_value())


def _word(value: int) -> bytes:
    """Encode one non-negative integer as its 32-byte big-endian ABI word."""
    if value < 0:
        raise ValueError("ABI words encode non-negative integers only")
    return value.to_bytes(WORD_BYTES, "big")


def _address_word(address: str) -> bytes:
    """Encode one normalized address as its low-160-bit ABI word."""
    return bytes.fromhex(normalize_evm_address(address)[2:].rjust(64, "0"))


def _padded_length(length: int) -> int:
    """Round one byte length up to the next whole ABI word boundary."""
    return (length + WORD_BYTES - 1) // WORD_BYTES * WORD_BYTES


def build_swap_path(
    quote_token_address: str,
    stock_token_address: str,
    tick_spacing: int,
) -> str:
    """Build the router's 43-byte concentrated-liquidity swap path.

    The path is exactly the reference transaction's shape: the input token,
    the tick-spacing flag byte, the pool's tick spacing as a big-endian
    uint16, and the output token.

    Args:
        quote_token_address: The exact-input USDC side of the swap.
        stock_token_address: The B20 stock contract being bought.
        tick_spacing: The pool's positive Slipstream tick spacing.

    Returns:
        The 86-character 0x-prefixed path segment.

    Raises:
        ValueError: If the tick spacing is outside the uint16 range.
    """
    if not 1 <= tick_spacing <= 0xFFFF:
        raise ValueError("tick_spacing must fit the path's unsigned 16-bit segment")
    return (
        "0x"
        + normalize_evm_address(quote_token_address)[2:]
        + V3_PATH_TICK_SPACING_FLAG
        + format(tick_spacing, "04x")
        + normalize_evm_address(stock_token_address)[2:]
    )


def build_swap_calldata(
    recipient_address: str,
    amount_in_units: int,
    amount_out_min_units: int,
    path: str,
    deadline: int,
) -> str:
    """ABI-encode the router's execute call for one exact-input swap.

    The encoding mirrors the reference transaction exactly: the head holds
    the commands offset 0x60, the inputs offset 0xa0, and the deadline; the
    commands section holds the single V3_SWAP_EXACT_IN byte; and the params
    struct carries six head words - recipient, amountIn, amountOutMinimum,
    the path offset 0xc0, payerIsSender one, and this fork's trailing zero
    word - before the length-prefixed path.

    Args:
        recipient_address: The address receiving the stock output.
        amount_in_units: The exact input amount in raw token units.
        amount_out_min_units: The minimum accepted output in raw units.
        path: The 0x-prefixed concentrated-liquidity path segment.
        deadline: The unix timestamp after which the router reverts.

    Returns:
        Complete 0x-prefixed calldata for the router's execute function.

    Raises:
        ValueError: If any amount is non-positive or the path is malformed.
    """
    if amount_in_units <= 0:
        raise ValueError("amount_in_units must be positive")
    if amount_out_min_units <= 0:
        raise ValueError("amount_out_min_units must be positive")
    if deadline <= 0:
        raise ValueError("deadline must be positive")
    if not path.startswith("0x") or len(path) != 2 + PATH_SEGMENT_BYTES * 2:
        raise ValueError(f"swap path must be exactly {PATH_SEGMENT_BYTES} bytes")
    path_bytes = bytes.fromhex(path[2:])
    # The params struct head holds six words, so the path starts at 0xc0.
    struct = (
        _address_word(recipient_address)
        + _word(amount_in_units)
        + _word(amount_out_min_units)
        + _word(6 * WORD_BYTES)
        + _word(1)
        + _word(0)
        + _word(len(path_bytes))
        + path_bytes.ljust(_padded_length(len(path_bytes)), b"\x00")
    )
    commands = bytes.fromhex(V3_SWAP_EXACT_IN_COMMAND)
    # The head is three words; the commands section adds its length word and
    # the single padded command byte before the inputs array begins.
    inputs_offset = 3 * WORD_BYTES + WORD_BYTES + _padded_length(len(commands))
    encoded = (
        _word(3 * WORD_BYTES)
        + _word(inputs_offset)
        + _word(deadline)
        + _word(len(commands))
        + commands.ljust(_padded_length(len(commands)), b"\x00")
        + _word(1)
        + _word(WORD_BYTES)
        + _word(len(struct))
        + struct
    )
    return f"0x{ROUTER_EXECUTE_SELECTOR}{encoded.hex()}"


def build_approval_calldata(spender_address: str, amount_units: int) -> str:
    """ABI-encode a bounded ERC20 approval to one spender.

    Args:
        spender_address: The router receiving the standing allowance.
        amount_units: The exact allowance in raw token units; zero revokes.

    Returns:
        Complete 0x-prefixed approve calldata.

    Raises:
        ValueError: If the amount is negative.
    """
    if amount_units < 0:
        raise ValueError("amount_units must be non-negative")
    return (
        f"0x{ERC20_APPROVE_SELECTOR}"
        + _address_word(spender_address).hex()
        + _word(amount_units).hex()
    )


def build_allowance_calldata(owner_address: str, spender_address: str) -> str:
    """ABI-encode the ERC20 allowance read for one owner and spender.

    Args:
        owner_address: The Safe whose allowance is read.
        spender_address: The router the allowance is granted to.

    Returns:
        Complete 0x-prefixed allowance calldata.
    """
    return (
        f"0x{ERC20_ALLOWANCE_SELECTOR}"
        + _address_word(owner_address).hex()
        + _address_word(spender_address).hex()
    )


class ExecutorRpcBackend:
    """Perform the bounded read-only and broadcast RPC calls execution needs."""

    def __init__(
        self,
        rpc_url: str,
        fallback_rpc_urls: Sequence[str] = (),
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = MAX_REQUEST_ATTEMPTS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        receipt_poll_seconds: float = RECEIPT_POLL_SECONDS,
        receipt_timeout_seconds: float = RECEIPT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timer: Callable[[], float] = time.monotonic,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        """Configure bounded RPC behavior shared by reads and broadcasts.

        Args:
            rpc_url: Primary Base JSON-RPC endpoint for reads and broadcasts.
            fallback_rpc_urls: Ordered alternate endpoints used only after a
                transient transport, rate-limit, forbidden, timeout, or 5xx failure.
            timeout_seconds: Complete per-request timeout in seconds.
            max_attempts: Attempts per request before failing closed.
            max_response_bytes: Maximum accepted size of one response body.
            receipt_poll_seconds: Delay between receipt polls.
            receipt_timeout_seconds: Bound on waiting for one inclusion.
            transport: Optional injected HTTP transport for deterministic tests.
            sleep: Injected delay function used for retry backoff and polling.
            timer: Injected monotonic clock for the receipt timeout.
            progress: Optional callback receiving one human-readable line per
                retried request, so rate-limit backoff never looks like a stall.

        Raises:
            ValueError: If any bound is non-positive.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if receipt_poll_seconds <= 0:
            raise ValueError("receipt_poll_seconds must be positive")
        if receipt_timeout_seconds <= 0:
            raise ValueError("receipt_timeout_seconds must be positive")
        self._rpc_url = rpc_url
        self._rpc_urls = tuple(dict.fromkeys((rpc_url, *fallback_rpc_urls)))
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._receipt_poll_seconds = receipt_poll_seconds
        self._receipt_timeout_seconds = receipt_timeout_seconds
        self._transport = transport
        self._sleep = sleep
        self._timer = timer
        self._progress = progress
        # One persistent connection pool serves every request this backend
        # makes: reusing the keep-alive connection avoids a fresh TCP and TLS
        # handshake per call, which public endpoints rate-limit far harder
        # than requests over an established connection.
        self._client: httpx.Client | None = None
        # The instant the next request may fire under the politeness pacing.
        self._next_request_at: float | None = None

    def _http_client(self) -> httpx.Client:
        """Return the backend's lazily created shared HTTP client.

        Returns:
            The persistent client bound to this backend's transport and
            timeouts; created on first use and reused for the backend's
            lifetime so every request shares one keep-alive connection pool.
        """
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                headers={"User-Agent": "aero-bot/0.1 capped-swap-execution"},
            )
        return self._client

    def eth_call(self, to_address: str, calldata: str) -> str:
        """Perform one read-only eth_call against the latest block.

        Args:
            to_address: The contract being called.
            calldata: Complete 0x-prefixed call payload.

        Returns:
            The 0x-prefixed return bytes.

        Raises:
            ExecutionUnavailableError: If retries are exhausted or the
                response is unusable.
            ExecutorRpcRevertError: If the call reverted inside the contract.
        """
        return self.eth_call_at(to_address, calldata, "latest")

    def eth_call_at(self, to_address: str, calldata: str, block_tag: str) -> str:
        """Perform one read-only eth_call pinned to an explicit block tag.

        Pinning every read of one observation to a single block keeps the
        snapshot coherent exactly like the Sugar backend's own pagination,
        which shares one pinned block tag across every page.

        Args:
            to_address: The contract being called.
            calldata: Complete 0x-prefixed call payload.
            block_tag: The block tag the call is evaluated against, a hex
                quantity like ``0x30a9973`` or the literal ``latest``.

        Returns:
            The 0x-prefixed return bytes.

        Raises:
            ExecutionUnavailableError: If retries are exhausted or the
                response is unusable.
            ExecutorRpcRevertError: If the call reverted inside the contract.
        """
        return cast(
            "str",
            self._rpc_call(
                "eth_call",
                [{"to": normalize_evm_address(to_address), "data": calldata}, block_tag],
            ),
        )

    def fetch_block_number(self) -> int:
        """Read the endpoint's latest block number.

        Returns:
            The latest block number.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        result = cast("str", self._rpc_call("eth_blockNumber", []))
        return self._decode_hex_quantity(result, "eth_blockNumber")

    def fetch_usdc_allowance(self, owner_address: str, spender_address: str) -> int:
        """Read the owner's standing USDC allowance to one spender.

        Args:
            owner_address: The Safe whose allowance is read.
            spender_address: The router holding the allowance.

        Returns:
            The allowance in raw USDC units.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        return self.fetch_erc20_allowance(BASE_USDC_ADDRESS, owner_address, spender_address)

    def fetch_erc20_allowance(
        self, token_address: str, owner_address: str, spender_address: str
    ) -> int:
        """Read one owner's standing ERC20 allowance to one spender.

        Args:
            token_address: The ERC20 contract whose allowance mapping is read.
            owner_address: The account whose allowance is read.
            spender_address: The spender the allowance is granted to.

        Returns:
            The allowance in the token's raw units.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        result = self.eth_call(
            token_address,
            build_allowance_calldata(owner_address, spender_address),
        )
        return self._decode_word_result(result, "ERC20 allowance()")

    def fetch_token_balance(self, token_address: str, owner_address: str) -> int:
        """Read one account's ERC20 balance in the token's raw units.

        Args:
            token_address: The ERC20 contract whose balances are read.
            owner_address: The account whose balance is read.

        Returns:
            The balance in the token's raw units.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        calldata = f"0x{ERC20_BALANCE_OF_SELECTOR}" + _address_word(owner_address).hex()
        result = self.eth_call(token_address, calldata)
        return self._decode_word_result(result, "ERC20 balanceOf()")

    def fetch_token_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count.

        Args:
            token_address: ERC20 contract whose decimals() is read.

        Returns:
            The token's decimal count.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        result = self.eth_call(token_address, ERC20_DECIMALS_SELECTOR)
        return self._decode_word_result(result, "decimals()")

    def fetch_eth_balance(self, address: str) -> int:
        """Read one address's live ETH balance in wei.

        Args:
            address: The account whose balance is read.

        Returns:
            The balance in wei.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        result = cast(
            "str",
            self._rpc_call("eth_getBalance", [normalize_evm_address(address), "latest"]),
        )
        return self._decode_hex_quantity(result, "eth_getBalance")

    def fetch_gas_price(self) -> int:
        """Read the endpoint's suggested gas price in wei.

        Returns:
            The suggested gas price in wei.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        result = cast("str", self._rpc_call("eth_gasPrice", []))
        return self._decode_hex_quantity(result, "eth_gasPrice")

    def fetch_relayer_nonce(self, address: str) -> int:
        """Read the relaying EOA's pending transaction count.

        Args:
            address: The EOA whose nonce is read.

        Returns:
            The pending transaction count.

        Raises:
            ExecutionUnavailableError: If the read cannot complete or is
                malformed.
        """
        result = cast(
            "str",
            self._rpc_call("eth_getTransactionCount", [normalize_evm_address(address), "pending"]),
        )
        return self._decode_hex_quantity(result, "eth_getTransactionCount")

    def fetch_aero_price_usdc(self, block_tag: str = "latest") -> Decimal:
        """Read AERO's USDC price from Aerodrome's own USDC/AERO pool.

        The price comes from the canonical volatile pair on the official
        Aerodrome pool factory - the same pool the frontend prices the
        emissions token from - as the USDC reserve over the AERO reserve at
        the requested block tag. This is the live read the emissions-APR
        convention consumes; it fails closed on any absent or zero-leg
        response.

        Args:
            block_tag: Hex block tag or "latest" pinning the reserve read;
                the rehearsal passes its anchor block so replay assumptions
                match the reconstructed state.

        Returns:
            The USDC price of one whole AERO token.

        Raises:
            ExecutionUnavailableError: If any read cannot complete.
            ExecutorRpcRevertError: If a call reverts on-chain.
            ValueError: If the pool is absent or either reserve is zero.
        """
        pool_address = normalize_evm_address(
            "0x"
            + str(
                self._rpc_call(
                    "eth_call",
                    [
                        {
                            "to": AERODROME_VOLATILE_FACTORY_ADDRESS,
                            "data": (
                                POOL_FACTORY_GET_POOL_SELECTOR
                                + _address_word(BASE_USDC_ADDRESS).hex()
                                + _address_word(AERO_TOKEN_ADDRESS).hex()
                                + "0" * 64
                            ),
                        },
                        block_tag,
                    ],
                )
            )[-40:]
        )
        if int(pool_address, 16) == 0:
            raise ValueError("the Aerodrome USDC/AERO volatile pool does not exist")

        def _pool_word(data: str) -> int:
            return int(
                str(self._rpc_call("eth_call", [{"to": pool_address, "data": data}, block_tag])),
                16,
            )

        token0 = normalize_evm_address(
            "0x"
            + str(
                self._rpc_call(
                    "eth_call", [{"to": pool_address, "data": POOL_TOKEN0_SELECTOR}, block_tag]
                )
            )[-40:]
        )
        reserve0 = _pool_word(POOL_RESERVE0_SELECTOR)
        reserve1 = _pool_word(POOL_RESERVE1_SELECTOR)
        token0_is_usdc = token0 == normalize_evm_address(BASE_USDC_ADDRESS)
        usdc_reserve = Decimal(reserve0 if token0_is_usdc else reserve1)
        aero_reserve = Decimal(reserve1 if token0_is_usdc else reserve0)
        if usdc_reserve <= 0 or aero_reserve <= 0:
            raise ValueError("the USDC/AERO pool carries a zero reserve; the price is undefined")
        with localcontext() as context:
            context.prec = MATH_PRECISION
            return +(
                (usdc_reserve / Decimal(10) ** QUOTE_TOKEN_DECIMALS)
                / (aero_reserve / Decimal(10) ** 18)
            )

    def estimate_gas(self, to_address: str, calldata: str) -> int:
        """Estimate the gas one transaction needs from the endpoint.

        Args:
            to_address: The transaction's target contract.
            calldata: Complete 0x-prefixed transaction data.

        Returns:
            The estimated gas units.

        Raises:
            ExecutorRpcRevertError: If the estimated call reverts on-chain.
            ExecutionUnavailableError: If the estimate cannot complete.
        """
        result = cast(
            "str",
            self._rpc_call(
                "eth_estimateGas",
                [{"to": normalize_evm_address(to_address), "data": calldata}, "latest"],
            ),
        )
        return self._decode_hex_quantity(result, "eth_estimateGas")

    def send_raw_transaction(self, raw_transaction_hex: str) -> str:
        """Broadcast one already-signed raw transaction.

        Retrying the identical raw bytes is safe because the relaying EOA's
        nonce makes every resend the same transaction.

        Args:
            raw_transaction_hex: The 0x-prefixed signed raw transaction.

        Returns:
            The transaction hash the endpoint accepted.

        Raises:
            ExecutionUnavailableError: If the broadcast cannot complete.
        """
        return cast(
            "str",
            self._rpc_call("eth_sendRawTransaction", [raw_transaction_hex]),
        )

    def fetch_transaction_receipt(self, transaction_hash: str) -> dict[str, object] | None:
        """Fetch one transaction's receipt once, without any polling.

        Args:
            transaction_hash: The broadcast transaction's hash.

        Returns:
            The receipt object when included, None when not yet present.

        Raises:
            ExecutionUnavailableError: If the call cannot complete.
        """
        receipt = self._rpc_call("eth_getTransactionReceipt", [transaction_hash])
        if receipt is None:
            return None
        return cast("dict[str, object]", receipt)

    def await_transaction_receipt(self, transaction_hash: str) -> dict[str, object]:
        """Poll for one transaction's receipt until inclusion or timeout.

        Args:
            transaction_hash: The broadcast transaction's hash.

        Returns:
            The receipt object with status, blockNumber, gasUsed, and logs.

        Raises:
            BroadcastTimeoutError: If inclusion does not arrive in time.
            ExecutionUnavailableError: If polling cannot complete.
        """
        started = self._timer()
        while True:
            receipt = self._rpc_call("eth_getTransactionReceipt", [transaction_hash])
            if receipt is not None:
                return cast("dict[str, object]", receipt)
            if self._timer() - started >= self._receipt_timeout_seconds:
                raise BroadcastTimeoutError(
                    f"transaction {transaction_hash} did not confirm within "
                    f"{self._receipt_timeout_seconds} seconds"
                )
            self._sleep(self._receipt_poll_seconds)

    def _decode_word_result(self, result: str, source: str) -> int:
        """Decode one 32-byte ABI word return value into an integer.

        Args:
            result: The 0x-prefixed eth_call return bytes.
            source: Human label naming the call in diagnostics.

        Returns:
            The decoded unsigned integer.

        Raises:
            ExecutionUnavailableError: If the return is not one full word.
        """
        if not result.startswith("0x") or len(result) != 2 + 64:
            raise ExecutionUnavailableError(
                f"{source} returned {max(len(result) - 2, 0)} bytes instead of 32"
            )
        return int(result[2:], 16)

    @staticmethod
    def _decode_hex_quantity(result: object, source: str) -> int:
        """Decode one hexadecimal JSON-RPC quantity value into an integer.

        Args:
            result: The raw JSON-RPC result value.
            source: Human label naming the call in diagnostics.

        Returns:
            The decoded unsigned integer.

        Raises:
            ExecutionUnavailableError: If the value is not a hex quantity.
        """
        if not isinstance(result, str) or not result.startswith("0x"):
            raise ExecutionUnavailableError(f"{source} did not return a hexadecimal quantity")
        try:
            return int(result, 16)
        except ValueError as error:
            raise ExecutionUnavailableError(f"{source} returned a malformed quantity") from error

    def _rpc_call(self, method: str, params: list[object]) -> object:
        """Perform one JSON-RPC request with retry and backoff.

        A null result is a legitimate answer for receipt polling, so None is
        returned verbatim rather than treated as an error.

        Args:
            method: The JSON-RPC method name.
            params: The JSON-RPC parameters for the method.

        Returns:
            The successful JSON-RPC result value, possibly None.

        Raises:
            ExecutionUnavailableError: If the request keeps failing after
                bounded retries or reports a non-transient error.
            ExecutorRpcRevertError: If the call reverted inside a contract.
        """
        payload = {"jsonrpc": "2.0", "id": JSON_RPC_ID, "method": method, "params": params}
        failure = "no attempt was made"
        client = self._http_client()
        for attempt in range(self._max_attempts):
            if attempt > 0:
                backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                if self._progress is not None:
                    self._progress(
                        f"rpc {method} attempt {attempt + 1} of {self._max_attempts} "
                        f"failed ({failure}); backing off {backoff:.1f}s"
                    )
                self._sleep(backoff)
            elif self._next_request_at is not None:
                # The politeness gap keeps back-to-back reads under the
                # public endpoint's request-rate limiter.
                wait = self._next_request_at - self._timer()
                if wait > 0:
                    self._sleep(wait)
            try:
                endpoint = self._rpc_urls[attempt % len(self._rpc_urls)]
                response = client.post(endpoint, json=payload)
            except httpx.TransportError as error:
                failure = f"transport error: {error}"
                continue
            finally:
                self._next_request_at = self._timer() + REQUEST_PACING_SECONDS
            if response.status_code in {403, 408, 425, 429} or response.status_code >= 500:
                failure = f"HTTP status {response.status_code}"
                continue
            response_size = len(response.content)
            if response_size > self._max_response_bytes:
                raise ExecutionUnavailableError(
                    f"RPC response contained {response_size} bytes, above the limit"
                )
            if response.status_code != 200:
                raise ExecutionUnavailableError(
                    f"RPC request failed with unexpected HTTP status {response.status_code}"
                )
            try:
                body = cast(object, response.json())
            except ValueError as error:
                raise ExecutionUnavailableError("RPC response was not valid JSON") from error
            if not isinstance(body, dict) or "result" not in body:
                error_body = body.get("error") if isinstance(body, dict) else None
                if not isinstance(error_body, dict):
                    raise ExecutionUnavailableError("RPC response had neither result nor error")
                error_code = error_body.get("code")
                error_message = str(error_body.get("message", ""))
                if error_code == EXECUTION_REVERT_ERROR_CODE or "revert" in error_message.lower():
                    raise ExecutorRpcRevertError(f"RPC call reverted: {error_message}")
                if error_code == RATE_LIMIT_ERROR_CODE or "rate limit" in error_message.lower():
                    failure = f"RPC error {error_code}: {error_message}"
                    continue
                raise ExecutionUnavailableError(f"RPC error {error_code}: {error_message}")
            return body["result"]
        raise ExecutionUnavailableError(
            f"RPC {method} failed after {self._max_attempts} attempts: {failure}"
        )


class SwapExecutor:
    """Build, validate, and - only when asked - broadcast capped Safe swaps."""

    def __init__(
        self,
        policy: ExecutionPolicy,
        safe_address: str,
        sources: ExecutionSources,
        rpc: ExecutorRpcBackend,
        safe_rpc: SafeTransactionRpcBackend,
        audit_sink: ExecutionAuditSink | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure one executor with every boundary it consumes.

        Args:
            policy: The hard caps enforced before anything is signed.
            safe_address: The canary Safe whose transactions are built.
            sources: Live read-only quote sources.
            rpc: The executor's read and broadcast RPC backend.
            safe_rpc: The Safe read-only backend for nonces and validation.
            audit_sink: Optional append-only audit chain for attempt events.
            now: Injected clock producing timezone-aware event timestamps.
            timer: Injected monotonic clock for duration metrics.
        """
        self._policy = policy
        self._safe_address = normalize_evm_address(safe_address)
        self._sources = sources
        self._rpc = rpc
        self._safe_rpc = safe_rpc
        self._audit_sink = audit_sink
        self._now = now
        self._timer = timer

    @property
    def safe_address(self) -> str:
        """Return the normalized Safe address this executor builds for."""
        return self._safe_address

    def quote(
        self, symbol: str, usdc_amount: Decimal, mode: ExecutionMode = ExecutionMode.DRY_RUN
    ) -> SwapQuote:
        """Price one capped swap through the live Sugar-discovered pool.

        Args:
            symbol: The registry-matched B20 stock symbol, like AAPLc.
            usdc_amount: The human USDC amount to swap.
            mode: The attempt mode this quote belongs to.

        Returns:
            The immutable quote with its complete pool evidence.

        Raises:
            ExecutionRefusalError: If any cap, whitelist, or staleness gate
                refuses the request before anything is built.
            ExecutionUnavailableError: If a live read cannot complete.
        """
        return self._quote_with_caps(symbol, usdc_amount, mode)[0]

    def dry_run(
        self,
        symbol: str,
        usdc_amount: Decimal,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> DryRunReport:
        """Fully build and validate one capped swap without broadcasting.

        Args:
            symbol: The registry-matched B20 stock symbol.
            usdc_amount: The human USDC amount to swap.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            ExecutionRefusalError: If any pre-sign cap refuses the request.
            ExecutionUnavailableError: If a live read cannot complete.
        """
        build_started = self._timer()
        quote, caps = self._quote_with_caps(symbol, usdc_amount, ExecutionMode.DRY_RUN)
        preflight_caps = list(caps)
        gas_price, safe_eth, allowance, live_nonce = self._preflight(preflight_caps)
        relayer_address = normalize_evm_address(Account.from_key(key_bytes).address)
        deadline = int(self._now().timestamp()) + SWAP_DEADLINE_SECONDS
        path = build_swap_path(BASE_USDC_ADDRESS, quote.token_address, quote.tick_spacing)
        approval: BuiltExecutionTransaction | None = None
        swap_nonce = live_nonce
        if allowance < quote.usdc_in_units:
            approval, _ = self._build_transaction(
                role=ExecutionRole.APPROVAL,
                to_address=BASE_USDC_ADDRESS,
                inner_calldata=build_approval_calldata(
                    self._policy.router_address, self._policy.approval_standing_cap_units
                ),
                nonce=live_nonce,
                usdc_units_value=self._policy.approval_standing_cap_units,
                deadline=0,
                key_bytes=key_bytes,
                mode=ExecutionMode.DRY_RUN,
            )
            swap_nonce = live_nonce + 1
        swap, _ = self._build_transaction(
            role=ExecutionRole.SWAP,
            to_address=self._policy.router_address,
            inner_calldata=build_swap_calldata(
                self._safe_address,
                quote.usdc_in_units,
                quote.amount_out_min_units,
                path,
                deadline,
            ),
            nonce=swap_nonce,
            usdc_units_value=quote.usdc_in_units,
            deadline=deadline,
            key_bytes=key_bytes,
            mode=ExecutionMode.DRY_RUN,
        )
        return DryRunReport(
            quote=quote,
            approval=approval,
            swap=swap,
            safe_address=self._safe_address,
            relayer_address=relayer_address,
            ephemeral_key=ephemeral_key,
            usdc_allowance_units=allowance,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            caps_enforced=tuple(preflight_caps),
            build_duration_ms=self._milliseconds_since(build_started),
        )

    def execute(self, symbol: str, usdc_amount: Decimal, key_bytes: bytes) -> ExecutionOutcome:
        """Broadcast one capped swap after every cap and validation passes.

        Args:
            symbol: The registry-matched B20 stock symbol.
            usdc_amount: The human USDC amount to swap.
            key_bytes: Exactly 32 raw signing-key bytes of a Safe owner EOA.

        Returns:
            The complete outcome with every broadcast receipt.

        Raises:
            ExecutionRefusalError: If any pre-sign cap or validation gate
                refuses the request before broadcasting.
            ExecutionUnavailableError: If a live read cannot complete.
            BroadcastTimeoutError: If inclusion does not arrive in time.
        """
        sign_started = self._timer()
        quote, caps = self._quote_with_caps(symbol, usdc_amount, ExecutionMode.EXECUTE)
        preflight_caps = list(caps)
        gas_price, safe_eth, allowance, live_nonce = self._preflight(preflight_caps)
        relayer_address = normalize_evm_address(Account.from_key(key_bytes).address)
        relayer_nonce = self._rpc.fetch_relayer_nonce(relayer_address)
        relayer_balance = self._rpc.fetch_eth_balance(relayer_address)
        deadline = int(self._now().timestamp()) + SWAP_DEADLINE_SECONDS
        path = build_swap_path(BASE_USDC_ADDRESS, quote.token_address, quote.tick_spacing)
        broadcast_started: float | None = None

        approval_receipt: ExecutionReceiptOutcome | None = None
        swap_nonce = live_nonce
        if allowance < quote.usdc_in_units:
            # The live nonce is re-read so the approval never replays a slot.
            self._ensure_fresh_nonce(live_nonce)
            approval_built, approval_calldata = self._build_transaction(
                role=ExecutionRole.APPROVAL,
                to_address=BASE_USDC_ADDRESS,
                inner_calldata=build_approval_calldata(
                    self._policy.router_address, self._policy.approval_standing_cap_units
                ),
                nonce=live_nonce,
                usdc_units_value=self._policy.approval_standing_cap_units,
                deadline=0,
                key_bytes=key_bytes,
                mode=ExecutionMode.EXECUTE,
            )
            self._require_verified(approval_built)
            approval_delivery = self._build_delivery(
                approval_built,
                approval_calldata,
                gas_price,
                relayer_nonce,
                relayer_balance,
                key_bytes,
            )
            broadcast_started = self._timer()
            approval_receipt = self._broadcast_and_track(
                approval_built, approval_delivery, relayer_address, quote
            )
            relayer_nonce += 1
            if approval_receipt.status != 1:
                # A reverted approval consumed its Safe nonce and gas; the
                # swap is never sent after a failed setup.
                return self._outcome(
                    quote,
                    relayer_address,
                    approval_receipt,
                    None,
                    sign_started,
                    broadcast_started,
                    preflight_caps,
                )
            # The confirmed approval advanced the Safe nonce; re-reading it
            # keeps the swap on the exact next slot instead of assuming.
            swap_nonce = self._safe_rpc.fetch_live_nonce()

        self._ensure_fresh_nonce(swap_nonce)
        swap_built, swap_calldata = self._build_transaction(
            role=ExecutionRole.SWAP,
            to_address=self._policy.router_address,
            inner_calldata=build_swap_calldata(
                self._safe_address,
                quote.usdc_in_units,
                quote.amount_out_min_units,
                path,
                deadline,
            ),
            nonce=swap_nonce,
            usdc_units_value=quote.usdc_in_units,
            deadline=deadline,
            key_bytes=key_bytes,
            mode=ExecutionMode.EXECUTE,
        )
        self._require_verified(swap_built)
        if swap_built.gas_estimate is None:
            raise ExecutionRefusalError(
                "the swap's gas estimate reverted on-chain, so the delivery transaction "
                "cannot be safely bounded; nothing was broadcast"
            )
        # The relayer's balance is refreshed after any approval inclusion.
        relayer_balance = self._rpc.fetch_eth_balance(relayer_address)
        swap_delivery = self._build_delivery(
            swap_built,
            swap_calldata,
            gas_price,
            relayer_nonce,
            relayer_balance,
            key_bytes,
        )
        if broadcast_started is None:
            broadcast_started = self._timer()
        swap_receipt = self._broadcast_and_track(swap_built, swap_delivery, relayer_address, quote)
        return self._outcome(
            quote,
            relayer_address,
            approval_receipt,
            swap_receipt,
            sign_started,
            broadcast_started,
            preflight_caps,
        )

    def _quote_with_caps(
        self, symbol: str, usdc_amount: Decimal, mode: ExecutionMode
    ) -> tuple[SwapQuote, tuple[str, ...]]:
        """Price one capped swap and return the quote with its enforced caps.

        Args:
            symbol: The registry-matched B20 stock symbol.
            usdc_amount: The human USDC amount to swap.
            mode: The attempt mode for the audit record.

        Returns:
            The immutable quote plus every cap label enforced in order.

        Raises:
            ExecutionRefusalError: If any quote-stage gate refuses.
            ExecutionUnavailableError: If a live read cannot complete.
        """
        caps: list[str] = []
        if usdc_amount <= 0:
            raise ExecutionRefusalError("swap amount must be positive")
        if usdc_amount > self._policy.max_swap_usdc:
            raise ExecutionRefusalError(
                f"swap amount {usdc_amount} USDC exceeds the configured per-swap cap of "
                f"{self._policy.max_swap_usdc} USDC; lower the amount"
            )
        caps.append(f"swap amount at or below {self._policy.max_swap_usdc} USDC")
        registry = self._sources.load_registry()
        if registry.status is not RegistryStatus.VERIFIED:
            raise ExecutionRefusalError(
                f"the official Coinbase-issued B20 registry did not validate "
                f"({registry.status.value}); the token whitelist is USDC plus that "
                "registry only, so execution is refused until the registry validates"
            )
        listing = next(
            (asset for asset in registry.assets if asset.symbol.lower() == symbol.strip().lower()),
            None,
        )
        if listing is None:
            raise ExecutionRefusalError(
                f"symbol {symbol!r} is not in the official Coinbase-issued B20 registry; "
                "the token whitelist is USDC plus that registry only"
            )
        discovery = self._sources.discover_pools()
        pool = next(
            (
                candidate
                for candidate in discovery.pools
                if listing.address in (candidate.token0_address, candidate.token1_address)
            ),
            None,
        )
        if pool is None or discovery.status is not PoolDiscoveryStatus.VERIFIED:
            raise ExecutionRefusalError(
                f"no live Sugar-verified B20/USDC pool exists for {symbol!r} "
                f"(discovery status {discovery.status.value}); execution requires a pool "
                "from live discovery"
            )
        if discovery.observed_at is None or discovery.snapshot_block is None:
            raise ExecutionRefusalError(
                "the discovery snapshot carries no observation evidence; refusing to "
                "quote without a block-pinned snapshot"
            )
        stock_decimals = self._sources.read_token_decimals(listing.address)
        stock_is_token0 = pool.token0_address == listing.address
        price = price_usdc_per_stock(
            pool.sqrt_ratio, stock_is_token0, stock_decimals, QUOTE_TOKEN_DECIMALS
        )
        units = usdc_units(usdc_amount)
        human_amount = Decimal(units).scaleb(-QUOTE_TOKEN_DECIMALS)
        # The exact-input output at the snapshot spot price, floored to whole
        # stock units so the quote never overstates the deliverable amount.
        expected_units = int(
            (human_amount / price * Decimal(10) ** stock_decimals).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        if expected_units <= 0:
            raise ExecutionRefusalError(
                f"a {human_amount} USDC swap yields less than one stock unit at the pool "
                f"price {price} USDC per {listing.symbol}; increase the amount"
            )
        tolerance = self._policy.slippage_tolerance_fraction
        amount_out_min_units = int(
            (Decimal(expected_units) * (Decimal(1) - tolerance)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        # The conservative constant-product impact bound against the pool's
        # whole USDC reserve; a concentrated active range concentrates
        # liquidity, so the true impact of this size is lower, never higher.
        usdc_reserve = pool.reserve1 if stock_is_token0 else pool.reserve0
        impact = Decimal(units) / Decimal(usdc_reserve + units)
        if impact >= tolerance:
            raise ExecutionRefusalError(
                f"the conservative impact bound {impact:.6f} reaches the slippage "
                f"tolerance {tolerance}; lower the amount below the pool's executable "
                "depth"
            )
        observed_at = discovery.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        quote_age = max(0, int((self._now() - observed_at).total_seconds()))
        if quote_age > self._policy.quote_max_age_seconds:
            raise ExecutionRefusalError(
                f"the pool snapshot is {quote_age} seconds old, above the "
                f"{self._policy.quote_max_age_seconds}-second staleness bound; re-run "
                "discovery for a fresh quote"
            )
        quote = SwapQuote(
            symbol=listing.symbol,
            token_address=listing.address,
            pool_address=pool.pool_address,
            tick_spacing=pool.tick_spacing,
            stock_decimals=stock_decimals,
            usdc_in_units=units,
            price_usdc_per_stock=price,
            expected_stock_units=expected_units,
            amount_out_min_units=amount_out_min_units,
            modeled_impact_fraction=impact,
            snapshot_block=discovery.snapshot_block,
            observed_at=observed_at,
            quote_age_seconds=quote_age,
        )
        caps.append("token within the USDC-plus-registry whitelist")
        caps.append(f"pool {pool.pool_address} from live Sugar discovery")
        caps.append(f"quote fresher than {self._policy.quote_max_age_seconds} seconds")
        self._record_quote(mode, quote)
        return quote, tuple(caps)

    def _preflight(self, caps: list[str]) -> tuple[int, int, int, int]:
        """Run every pre-sign chain gate and return the live preflight state.

        Args:
            caps: The enforced-cap list extended with each passing gate.

        Returns:
            The gas price, Safe ETH balance, USDC allowance, and live Safe
            nonce, all observed before anything was signed.

        Raises:
            ExecutionRefusalError: If the gas cap or ETH floor refuses.
            ExecutionUnavailableError: If a read cannot complete.
        """
        gas_price = self._rpc.fetch_gas_price()
        if gas_price > self._policy.gas_price_cap_wei:
            raise ExecutionRefusalError(
                f"the endpoint's gas price {gas_price} wei exceeds the "
                f"{self._policy.gas_price_cap_wei}-wei cap (1 gwei); wait for calmer "
                "network conditions or consciously raise the cap in configuration"
            )
        caps.append(f"effective gas price at or below {self._policy.gas_price_cap_wei} wei")
        safe_eth = self._rpc.fetch_eth_balance(self._safe_address)
        if safe_eth < self._policy.safe_eth_floor_wei:
            raise ExecutionRefusalError(
                f"the Safe holds {safe_eth} wei, below the documented floor of "
                f"{self._policy.safe_eth_floor_wei} wei; top up the Safe's ETH balance "
                "before executing"
            )
        caps.append(f"Safe ETH balance at or above the {self._policy.safe_eth_floor_wei}-wei floor")
        allowance = self._rpc.fetch_usdc_allowance(self._safe_address, self._policy.router_address)
        live_nonce = self._safe_rpc.fetch_live_nonce()
        return gas_price, safe_eth, allowance, live_nonce

    def _build_transaction(
        self,
        role: ExecutionRole,
        to_address: str,
        inner_calldata: str,
        nonce: int,
        usdc_units_value: int,
        deadline: int,
        key_bytes: bytes,
        mode: ExecutionMode,
    ) -> tuple[BuiltExecutionTransaction, str]:
        """Build, sign, validate, and estimate one Safe transaction.

        Args:
            role: Which transaction of the attempt this is.
            to_address: The contract the Safe transaction calls.
            inner_calldata: The complete inner call payload.
            nonce: The Safe nonce this transaction occupies.
            usdc_units_value: The raw USDC amount this transaction moves.
            deadline: The router deadline, zero for approvals.
            key_bytes: Exactly 32 raw signing-key bytes.
            mode: The attempt mode for the audit record.

        Returns:
            The fully built transaction report plus its complete
            execTransaction calldata; the calldata carries the owner
            signature, so callers persist only the digest.

        Raises:
            ExecutionUnavailableError: If a live read cannot complete.
        """
        build_started = self._timer()
        transaction = SafeTransaction(to_address=to_address, data=inner_calldata, nonce=nonce)
        built = build_safe_transaction(transaction, self._safe_address)
        signature = sign_safe_tx_hash(key_bytes, built.safe_tx_hash)
        calldata = build_exec_transaction_calldata(transaction, signature)
        validation = self._safe_rpc.validate_owner_signature(built, signature)
        calldata_digest = "0x" + keccak(bytes.fromhex(calldata[2:])).hex()
        gas_estimate: int | None = None
        gas_diagnostic = ""
        try:
            gas_estimate = self._rpc.estimate_gas(self._safe_address, calldata)
        except ExecutorRpcRevertError as error:
            gas_diagnostic = f"the on-chain estimate reverted: {error}"
        build_duration = self._milliseconds_since(build_started)
        self._record_build(
            mode,
            role,
            built,
            calldata_digest,
            usdc_units_value,
            deadline,
            validation,
            gas_estimate,
            build_duration,
        )
        return (
            BuiltExecutionTransaction(
                role=role,
                safe_tx_hash=built.safe_tx_hash,
                to_address=normalize_evm_address(to_address),
                calldata_digest=calldata_digest,
                nonce=nonce,
                usdc_units=usdc_units_value,
                deadline=deadline,
                signature_verified=validation.verified,
                signature_diagnostic=validation.diagnostic,
                gas_estimate=gas_estimate,
                gas_estimate_diagnostic=gas_diagnostic,
            ),
            calldata,
        )

    def _require_verified(self, built: BuiltExecutionTransaction) -> None:
        """Refuse to broadcast a transaction whose signature the Safe rejects.

        Args:
            built: The fully built transaction about to be broadcast.

        Raises:
            ExecutionRefusalError: When the live contract rejected the
                signature, so nothing can validly execute.
        """
        if not built.signature_verified:
            raise ExecutionRefusalError(
                f"the Safe's read-only checkSignatures rejected the {built.role.value} "
                f"signature for safeTxHash {built.safe_tx_hash}: "
                f"{built.signature_diagnostic}; the signing key is not an owner of this "
                "Safe, so nothing was broadcast"
            )

    def _ensure_fresh_nonce(self, proposed_nonce: int) -> None:
        """Re-read the live Safe nonce and refuse any replayed slot.

        Args:
            proposed_nonce: The nonce the attempt wants to build with.

        Raises:
            ExecutionRefusalError: If another transaction advanced the Safe
                nonce during this attempt.
        """
        try:
            ensure_usable_nonce(proposed_nonce, self._safe_rpc.fetch_live_nonce())
        except StaleNonceError as error:
            raise ExecutionRefusalError(
                f"the Safe nonce advanced during the attempt ({error}); nothing was "
                "broadcast - re-run the command"
            ) from error

    def _build_delivery(
        self,
        built: BuiltExecutionTransaction,
        exec_calldata: str,
        gas_price_wei: int,
        relayer_nonce: int,
        relayer_balance_wei: int,
        key_bytes: bytes,
    ) -> str:
        """Build and sign the EOA transaction delivering one Safe execution.

        The key bytes sign exactly this one delivery transaction; the signed
        raw bytes are the only product and nothing about the key is retained.

        Both fee parameters sit at the observed gas price, which preflight
        has already bounded at or below the one-gwei cap: the EOA therefore
        never pays more than that price per gas unit, and if the base fee
        rises above it the transaction simply cannot be included and the
        bounded receipt wait fails closed instead of overpaying.

        Args:
            built: The validated Safe transaction being delivered.
            exec_calldata: Its complete execTransaction calldata.
            gas_price_wei: The observed gas price at or below the cap.
            relayer_nonce: The relaying EOA's pending transaction count.
            relayer_balance_wei: The relaying EOA's live ETH balance.
            key_bytes: Exactly 32 raw signing-key bytes.

        Returns:
            The 0x-prefixed signed raw delivery transaction.

        Raises:
            ExecutionRefusalError: If the estimate is missing or the EOA
                cannot afford the bounded gas cost.
        """
        if built.gas_estimate is None:
            raise ExecutionRefusalError(
                f"the {built.role.value} transaction has no on-chain gas estimate; "
                "nothing can be safely broadcast"
            )
        buffered = Decimal(built.gas_estimate) * (Decimal(1) + GAS_LIMIT_BUFFER_FRACTION)
        gas_limit = int(buffered.to_integral_value(rounding=ROUND_CEILING))
        max_fee = gas_price_wei
        required_wei = gas_limit * max_fee
        if relayer_balance_wei < required_wei:
            raise ExecutionRefusalError(
                f"the relaying EOA holds {relayer_balance_wei} wei, below the "
                f"{required_wei} wei needed for {gas_limit} gas at {max_fee} wei; fund the "
                "EOA before executing"
            )
        transaction: dict[str, Any] = {
            # The delivery layer requires the checksummed spelling of the Safe.
            "to": to_checksum_address(self._safe_address),
            "data": exec_calldata,
            "nonce": relayer_nonce,
            "gas": gas_limit,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_fee,
            "chainId": 8453,
            "type": 2,
        }
        signed = Account.sign_transaction(transaction, key_bytes)
        return "0x" + bytes(signed.raw_transaction).hex()

    def _broadcast_and_track(
        self,
        built: BuiltExecutionTransaction,
        raw_transaction: str,
        relayer_address: str,
        quote: SwapQuote,
    ) -> ExecutionReceiptOutcome:
        """Broadcast one delivery transaction and track its inclusion.

        Args:
            built: The validated Safe transaction being broadcast.
            raw_transaction: The signed raw delivery transaction.
            relayer_address: The public address of the relaying EOA.
            quote: The attempt's quote supplying the pool and expectations.

        Returns:
            The inclusion outcome with realized amounts when confirmed.

        Raises:
            ExecutionUnavailableError: If the broadcast cannot complete.
            BroadcastTimeoutError: If inclusion does not arrive in time.
        """
        sent_at = self._timer()
        transaction_hash = self._rpc.send_raw_transaction(raw_transaction)
        self._record_sent(built, transaction_hash, relayer_address)
        receipt = self._rpc.await_transaction_receipt(transaction_hash)
        inclusion_ms = self._milliseconds_since(sent_at)
        return self._receipt_outcome(built, transaction_hash, receipt, quote, inclusion_ms)

    def _receipt_outcome(
        self,
        built: BuiltExecutionTransaction,
        transaction_hash: str,
        receipt: dict[str, object],
        quote: SwapQuote,
        inclusion_ms: Decimal,
    ) -> ExecutionReceiptOutcome:
        """Convert one raw receipt into the outcome model and audit record.

        Args:
            built: The broadcast Safe transaction this receipt belongs to.
            transaction_hash: The delivery transaction's hash.
            receipt: The raw receipt object.
            quote: The attempt's quote supplying realized-amount context.
            inclusion_ms: Broadcast-to-inclusion duration in milliseconds.

        Returns:
            The immutable receipt outcome.

        Raises:
            ExecutionUnavailableError: If the receipt shape is unusable.
        """
        status = self._receipt_status(receipt, transaction_hash)
        block_number = self._receipt_quantity(receipt, "blockNumber", transaction_hash)
        gas_used = self._receipt_quantity(receipt, "gasUsed", transaction_hash)
        effective_gas_price = self._receipt_quantity(receipt, "effectiveGasPrice", transaction_hash)
        realized_units: int | None = None
        slippage: Decimal | None = None
        diagnostic = ""
        if status == 0:
            diagnostic = (
                "the transaction reverted on-chain; the Safe nonce was consumed and the "
                "swap did not execute"
            )
        elif built.role is ExecutionRole.SWAP:
            # A buy sends USDC into the pool and stock out, so the pool's
            # negative delta is exactly the stock this Safe received.
            realized_units = self._realized_stock_units(receipt, quote.pool_address)
            if realized_units is not None and realized_units > 0:
                slippage = (
                    Decimal(quote.expected_stock_units) - Decimal(realized_units)
                ) / Decimal(quote.expected_stock_units)
        self._record_receipt(
            built,
            transaction_hash,
            status,
            block_number,
            gas_used,
            effective_gas_price,
            realized_units,
            quote.expected_stock_units,
            slippage,
            inclusion_ms,
            diagnostic,
        )
        return ExecutionReceiptOutcome(
            role=built.role,
            safe_tx_hash=built.safe_tx_hash,
            transaction_hash=transaction_hash,
            status=status,
            block_number=block_number,
            gas_used=gas_used,
            effective_gas_price_wei=effective_gas_price,
            realized_stock_units=realized_units,
            quote_slippage_fraction=slippage,
            diagnostic=diagnostic,
        )

    def _realized_stock_units(self, receipt: dict[str, object], pool_address: str) -> int | None:
        """Extract the realized stock output from a swap receipt's logs.

        Args:
            receipt: The raw receipt object carrying the logs list.
            pool_address: The swap pool whose logs are being decoded.

        Returns:
            The positive stock amount the pool sent out, or None when no swap
            log is present.

        Raises:
            ExecutionUnavailableError: If a matching log cannot be decoded.
        """
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            return None
        for log in logs:
            if not isinstance(log, dict) or not isinstance(log.get("address"), str):
                continue
            if normalize_evm_address(str(log["address"])) != pool_address:
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or not topics or topics[0] != SWAP_EVENT_TOPIC0:
                continue
            record = decode_swap_log(log)
            # The pool's outflow side carries the negative delta on a buy.
            if record.amount0 < 0:
                return -record.amount0
            if record.amount1 < 0:
                return -record.amount1
        return None

    @staticmethod
    def _receipt_status(receipt: dict[str, object], transaction_hash: str) -> Literal[0, 1]:
        """Decode one receipt's status field fail-closed.

        Args:
            receipt: The raw receipt object.
            transaction_hash: The hash naming this receipt in diagnostics.

        Returns:
            One on success, zero on revert.

        Raises:
            ExecutionUnavailableError: If the status field is missing.
        """
        status = receipt.get("status")
        if not isinstance(status, str):
            raise ExecutionUnavailableError(
                f"receipt for {transaction_hash} carried no status field"
            )
        return 1 if int(status, 16) == 1 else 0

    @staticmethod
    def _receipt_quantity(receipt: dict[str, object], field: str, transaction_hash: str) -> int:
        """Decode one hexadecimal receipt quantity field.

        Args:
            receipt: The raw receipt object.
            field: The receipt field being decoded.
            transaction_hash: The hash naming this receipt in diagnostics.

        Returns:
            The decoded unsigned integer.

        Raises:
            ExecutionUnavailableError: If the field is missing or malformed.
        """
        value = receipt.get(field)
        if not isinstance(value, str):
            raise ExecutionUnavailableError(
                f"receipt for {transaction_hash} carried no {field} field"
            )
        try:
            return int(value, 16)
        except ValueError as error:
            raise ExecutionUnavailableError(
                f"receipt {field} for {transaction_hash} was malformed"
            ) from error

    def _outcome(
        self,
        quote: SwapQuote,
        relayer_address: str,
        approval: ExecutionReceiptOutcome | None,
        swap: ExecutionReceiptOutcome | None,
        sign_started: float,
        broadcast_started: float,
        caps: list[str],
    ) -> ExecutionOutcome:
        """Assemble one execute attempt's outcome with its timing metrics.

        Args:
            quote: The attempt's capped quote.
            relayer_address: The public address of the relaying EOA.
            approval: The approval receipt, absent when none was broadcast.
            swap: The swap receipt, absent when the approval failed first.
            sign_started: Monotonic timestamp of the sign phase's start.
            broadcast_started: Monotonic timestamp of the first broadcast.
            caps: Every cap label enforced before signing.

        Returns:
            The immutable outcome with sign and broadcast durations.
        """
        return ExecutionOutcome(
            quote=quote,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(relayer_address),
            approval=approval,
            swap=swap,
            sign_duration_ms=self._milliseconds_since(sign_started),
            broadcast_duration_ms=self._milliseconds_since(broadcast_started),
            caps_enforced=tuple(caps),
        )

    def _milliseconds_since(self, started: float) -> Decimal:
        """Measure whole milliseconds elapsed since one monotonic timestamp.

        Args:
            started: The monotonic start timestamp.

        Returns:
            The elapsed milliseconds, never negative.
        """
        elapsed = Decimal(str(max(0.0, self._timer() - started))) * 1000
        return elapsed.quantize(TIMING_PRECISION)

    def _record_quote(self, mode: ExecutionMode, quote: SwapQuote) -> None:
        """Append the quote audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.EXECUTION_QUOTE,
            ExecutionQuotePayload(
                mode=mode,
                symbol=quote.symbol,
                token_address=quote.token_address,
                pool_address=quote.pool_address,
                snapshot_block=quote.snapshot_block,
                usdc_in_units=quote.usdc_in_units,
                expected_stock_units=quote.expected_stock_units,
                price_usdc_per_stock=quote.price_usdc_per_stock,
                amount_out_min_units=quote.amount_out_min_units,
                modeled_impact_fraction=quote.modeled_impact_fraction,
                quote_age_seconds=quote.quote_age_seconds,
            ),
            self._now(),
        )

    def _record_build(
        self,
        mode: ExecutionMode,
        role: ExecutionRole,
        built: BuiltSafeTransaction,
        calldata_digest: str,
        usdc_units_value: int,
        deadline: int,
        validation: SafeSignatureValidation,
        gas_estimate: int | None,
        build_duration: Decimal,
    ) -> None:
        """Append the build audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.EXECUTION_BUILT,
            ExecutionBuiltPayload(
                mode=mode,
                role=role,
                safe_tx_hash=built.safe_tx_hash,
                to_address=built.transaction.to_address,
                calldata_digest=calldata_digest,
                nonce=built.transaction.nonce,
                usdc_units=usdc_units_value,
                deadline=deadline,
                signature_verified=validation.verified,
                gas_estimate=gas_estimate,
                build_duration_ms=build_duration,
            ),
            self._now(),
        )

    def _record_sent(
        self,
        built: BuiltExecutionTransaction,
        transaction_hash: str,
        relayer_address: str,
    ) -> None:
        """Append the broadcast audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.EXECUTION_SENT,
            ExecutionSentPayload(
                role=built.role,
                safe_tx_hash=built.safe_tx_hash,
                transaction_hash=transaction_hash,
                relayer_address=normalize_evm_address(relayer_address),
                safe_address=self._safe_address,
            ),
            self._now(),
        )

    def _record_receipt(
        self,
        built: BuiltExecutionTransaction,
        transaction_hash: str,
        status: Literal[0, 1],
        block_number: int,
        gas_used: int,
        effective_gas_price: int,
        realized_units: int | None,
        expected_stock_units: int,
        slippage: Decimal | None,
        inclusion_ms: Decimal,
        diagnostic: str,
    ) -> None:
        """Append the inclusion audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.EXECUTION_CONFIRMED if status == 1 else AuditEventType.EXECUTION_FAILED,
            ExecutionReceiptPayload(
                outcome="confirmed" if status == 1 else "failed",
                role=built.role,
                safe_tx_hash=built.safe_tx_hash,
                transaction_hash=transaction_hash,
                block_number=block_number,
                gas_used=gas_used,
                effective_gas_price_wei=effective_gas_price,
                realized_stock_units=realized_units,
                expected_stock_units=expected_stock_units if realized_units is not None else None,
                quote_slippage_fraction=slippage,
                inclusion_duration_ms=inclusion_ms,
                diagnostic=diagnostic,
            ),
            self._now(),
        )


def _add_execution_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the shared per-attempt arguments to one subcommand parser.

    Args:
        parser: The subcommand parser receiving the shared arguments.
    """
    parser.add_argument(
        "--symbol",
        required=True,
        help="Registry-matched B20 stock symbol to buy, like AAPLc.",
    )
    parser.add_argument(
        "--amount",
        type=Decimal,
        required=True,
        help="Exact USDC amount to swap; refused above the configured cap.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete report as JSON instead of a summary.",
    )


def build_swap_argument_parser() -> argparse.ArgumentParser:
    """Build the swap command's argument parser.

    Returns:
        The configured parser for the aero-bot-swap command.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-swap",
        description=(
            "Manually execute one hard-capped USDC-to-B20 swap through the canary "
            "Safe on Base. Broadcast happens only behind the execute subcommand "
            "with its explicit confirmation flag."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    quote_parser = subparsers.add_parser(
        "quote",
        help="Price one capped swap through live Sugar discovery; nothing is built.",
    )
    _add_execution_arguments(quote_parser)
    dry_run_parser = subparsers.add_parser(
        "dry-run",
        help=("Build, sign, and validate the Safe transactions without broadcasting anything."),
    )
    _add_execution_arguments(dry_run_parser)
    dry_run_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign the dry run with a freshly generated throwaway key instead of "
            "the configured signing-key source; the signature check will honestly "
            "report rejection."
        ),
    )
    execute_parser = subparsers.add_parser(
        "execute",
        help=(
            "Broadcast the capped swap after every cap and validation passes; "
            "requires --confirm-broadcast."
        ),
    )
    _add_execution_arguments(execute_parser)
    execute_parser.add_argument(
        "--confirm-broadcast",
        action="store_true",
        help=(
            "Explicit confirmation that this command may broadcast transactions "
            "on Base; without it the command refuses."
        ),
    )
    return parser


def _print_quote(quote: SwapQuote) -> None:
    """Print one quote's human summary.

    Args:
        quote: The quote being reported.
    """
    print(f"{quote.symbol} pool {quote.pool_address} (snapshot block {quote.snapshot_block})")
    print(
        f"{quote.usdc_amount} USDC -> ~{quote.expected_stock_amount} {quote.symbol} "
        f"at {quote.price_usdc_per_stock} USDC per {quote.symbol}"
    )
    print(
        f"amountOutMinimum {quote.amount_out_min_units} raw units, "
        f"conservative impact bound {quote.modeled_impact_fraction:.6f}, "
        f"quote age {quote.quote_age_seconds}s"
    )


def _print_built(line_prefix: str, built: BuiltExecutionTransaction) -> None:
    """Print one built transaction's human summary.

    Args:
        line_prefix: Role label starting each line.
        built: The built transaction being reported.
    """
    print(f"{line_prefix} safeTxHash {built.safe_tx_hash} (nonce {built.nonce})")
    print(f"{line_prefix} target {built.to_address}, calldata digest {built.calldata_digest}")
    estimate = (
        f"{built.gas_estimate} gas estimated"
        if built.gas_estimate is not None
        else f"no estimate: {built.gas_estimate_diagnostic}"
    )
    verdict = "accepted" if built.signature_verified else "REJECTED"
    print(f"{line_prefix} signature {verdict} by live checkSignatures, {estimate}")


def _print_dry_run(report: DryRunReport) -> None:
    """Print one dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    _print_quote(report.quote)
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        "nothing broadcast)"
    )
    print(
        f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei, "
        f"standing allowance {report.usdc_allowance_units} raw USDC"
    )
    if report.approval is not None:
        _print_built("approval:", report.approval)
    _print_built("swap:", report.swap)
    print(f"build took {report.build_duration_ms} ms")


def _print_receipt(line_prefix: str, receipt: ExecutionReceiptOutcome) -> None:
    """Print one inclusion receipt's human summary.

    Args:
        line_prefix: Role label starting each line.
        receipt: The receipt being reported.
    """
    outcome = "confirmed" if receipt.status == 1 else "REVERTED"
    print(
        f"{line_prefix} {outcome} in tx {receipt.transaction_hash} "
        f"(block {receipt.block_number}, {receipt.gas_used} gas at "
        f"{receipt.effective_gas_price_wei} wei)"
    )
    if receipt.realized_stock_units is not None:
        slippage = (
            f", slippage {receipt.quote_slippage_fraction:.6f}"
            if receipt.quote_slippage_fraction is not None
            else ""
        )
        print(f"{line_prefix} realized {receipt.realized_stock_units} raw units{slippage}")
    if receipt.diagnostic:
        print(f"{line_prefix} {receipt.diagnostic}")


def _print_outcome(outcome: ExecutionOutcome) -> None:
    """Print one execute outcome's human summary.

    Args:
        outcome: The outcome being printed.
    """
    _print_quote(outcome.quote)
    print(f"safe {outcome.safe_address}, relayer {outcome.relayer_address}")
    if outcome.approval is not None:
        _print_receipt("approval:", outcome.approval)
    if outcome.swap is not None:
        _print_receipt("swap:", outcome.swap)
    print(
        f"sign {outcome.sign_duration_ms} ms, broadcast {outcome.broadcast_duration_ms} ms, "
        f"result {'succeeded' if outcome.succeeded else 'FAILED'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one manual swap command.

    The RPC endpoint, Sugar address, and audit database come from the
    application settings, the Safe address defaults to the canary deployment
    behind ``AERO_BOT_SAFE_ADDRESS``, and the signing key comes from the
    platform key source (macOS Keychain, sealed environment variable, or
    owner-only key file - see ``aero_bot.signing_key``) or the dry run's
    explicit ephemeral flag.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on success, one on failure, two on any
        pre-sign refusal.
    """
    settings = Settings()
    parser = build_swap_argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.amount <= 0:
        parser.error("--amount must be positive")
    safe_address = os.environ.get(SAFE_ADDRESS_ENV, DEFAULT_CANARY_SAFE_ADDRESS)
    sources = LiveExecutionSources(
        rpc_url=settings.base_rpc_url,
        sugar_address=settings.lp_sugar_address,
    )
    rpc = ExecutorRpcBackend(rpc_url=settings.base_rpc_url)
    safe_rpc = SafeTransactionRpcBackend(rpc_url=settings.base_rpc_url, safe_address=safe_address)
    try:
        audit_store = AuditStore(settings.audit_database_path)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"the audit store is unavailable: {error}", file=sys.stderr)
        return EXIT_FAILURE
    executor = SwapExecutor(
        policy=ExecutionPolicy(),
        safe_address=safe_address,
        sources=sources,
        rpc=rpc,
        safe_rpc=safe_rpc,
        audit_sink=audit_store,
    )
    try:
        if arguments.command == "quote":
            quote = executor.quote(arguments.symbol, arguments.amount)
            if arguments.json:
                print(quote.model_dump_json(indent=2))
            else:
                _print_quote(quote)
            return EXIT_OK
        if arguments.command == "dry-run":
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            report = executor.dry_run(
                arguments.symbol, arguments.amount, key_bytes, ephemeral_key=ephemeral
            )
            if arguments.json:
                print(report.model_dump_json(indent=2))
            else:
                _print_dry_run(report)
            return EXIT_OK
        if not arguments.confirm_broadcast:
            print(
                "refused: execute requires --confirm-broadcast to acknowledge that this "
                "command broadcasts transactions on Base; nothing was sent",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        key_bytes = load_signing_key_source().load_signing_key()
        outcome = executor.execute(arguments.symbol, arguments.amount, key_bytes)
        if arguments.json:
            print(outcome.model_dump_json(indent=2))
        else:
            _print_outcome(outcome)
        return EXIT_OK if outcome.succeeded else EXIT_FAILURE
    except ExecutionRefusalError as error:
        print(f"refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
        print(f"failed: {error}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
