"""Capped manual LP lifecycle execution through the canary Safe on Base.

This module is the LP lifecycle's signing and validation layer, shaped on the
swap executor's containment posture: every action is a manual one-shot CLI
request, every hard cap is enforced in code before anything is signed, the
default mode builds and validates Safe transactions without ever broadcasting
them, and broadcasting will exist only behind an explicit execute subcommand
with its own confirmation flag. There is no loop, scheduler, watcher, or
policy-driven trigger anywhere in this module.

An entry composes into a sequence of individual Safe transactions, each built
from the pure planner's capped mint plan and the cast-verified calldata
builders: an optional bounded router allowance and balancing swap when the
Safe lacks stock inventory, exact ERC20 approvals to the pool's own
NonfungiblePositionManager (the NFPM itself executes the mint's
``transferFrom`` pull through its callback, verified from Aerodrome's
LiquidityManagement source), and the twelve-field Slipstream mint. A stake
composes the NFPM operator approval for the gauge and the gauge deposit. The
exit side completes the lifecycle: an unstake reads the gauge's earned and
checkpointed emissions plus the factory's penalty window before the gauge
withdraw (which auto-sweeps fees and auto-claims emissions), a withdraw
decreases the position's full liquidity and collects its checkpointed fees
through the NFPM, a collect routes through the gauge's ``getReward`` while
staked and the NFPM's ``collect`` when not, a recenter recycles one position
into a fresh mint in a single audited batch with the restake as a documented
follow-up command, and a status observes one position completely read-only.
Each transaction is signed over its EIP-712 SafeTx hash, proven read-only
against the live Safe with ``checkSignatures``, gas-estimated, and audited;
nothing is broadcast by anything in this module's current surface.

Everything the module audits, prints, or reports about the key it uses is the
relaying EOA's public address and the Safe-side hashes; key bytes arrive as
one argument, sign only SafeTx hashes, and are never stored, logged, or
persisted.
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Any, Literal

from eth_account import Account
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from pydantic import BaseModel, Field, field_validator, model_validator

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.concentrated import PositionRangeState
from aero_bot.config import Settings
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.emissions_apr import (
    aerodrome_display_emissions_apr,
    emissions_apr_at_tick_width,
    staked_value_usdc,
)
from aero_bot.execution_lock import ExecutionLockUnavailableError, exclusive_execution_lock
from aero_bot.executor import (
    AERODROME_ROUTER_ADDRESS,
    DEFAULT_CANARY_SAFE_ADDRESS,
    DEFAULT_GAS_PRICE_CAP_WEI,
    DEFAULT_QUOTE_MAX_AGE_SECONDS,
    DEFAULT_RELAYER_ETH_FLOOR_WEI,
    DEFAULT_SAFE_ETH_FLOOR_WEI,
    ERC20_BALANCE_OF_SELECTOR,
    GAS_LIMIT_BUFFER_FRACTION,
    SAFE_ADDRESS_ENV,
    ExecutionAuditSink,
    ExecutionMode,
    ExecutionSources,
    ExecutionUnavailableError,
    ExecutorRpcBackend,
    ExecutorRpcRevertError,
    LiveExecutionSources,
    build_approval_calldata,
    build_swap_calldata,
    build_swap_path,
    usdc_units,
)
from aero_bot.lp_calldata import (
    MAX_UINT128,
    LpCollectParams,
    LpDecreaseLiquidityParams,
    LpMintParams,
    LpPositionView,
    build_gauge_deposit_calldata,
    build_gauge_deposit_timestamp_read_calldata,
    build_gauge_earned_read_calldata,
    build_gauge_factory_nft_read_calldata,
    build_gauge_gauge_factory_read_calldata,
    build_gauge_get_reward_calldata,
    build_gauge_min_stake_times_read_calldata,
    build_gauge_penalty_rate_read_calldata,
    build_gauge_reward_rate_read_calldata,
    build_gauge_reward_token_read_calldata,
    build_gauge_rewards_read_calldata,
    build_gauge_withdraw_calldata,
    build_lp_burn_calldata,
    build_lp_collect_calldata,
    build_lp_decrease_liquidity_calldata,
    build_lp_mint_calldata,
    build_lp_positions_read_calldata,
    build_pool_factory_read_calldata,
    build_pool_gauge_read_calldata,
    build_pool_liquidity_read_calldata,
    build_pool_slot0_read_calldata,
    build_pool_staked_liquidity_read_calldata,
    build_pool_tick_spacing_read_calldata,
    build_pool_token0_read_calldata,
    build_pool_token1_read_calldata,
    build_set_approval_for_all_calldata,
    decode_address_view_result,
    decode_lp_positions_view,
    decode_pool_slot0_view,
    decode_uint_view_result,
)
from aero_bot.lp_pins import LpPoolPin, LpPoolPinStore, build_pool_pin_from_discovery
from aero_bot.lp_plan import (
    DEFAULT_MINT_SLIPPAGE_TOLERANCE,
    MAX_POSITION_USDC_PER_POOL,
    QUOTE_TOKEN_DECIMALS,
    BalancingSwapDirection,
    LpExecutionPolicy,
    LpMintPlan,
    LpPlanRefusalError,
    LpPoolObservation,
    MintDirective,
    SafeInventory,
    WidthSource,
    plan_mint_entry,
    position_amounts_at_sqrt_ratio,
    position_range_state,
)
from aero_bot.registry import B20AssetListing, RegistryStatus
from aero_bot.safe_tx import (
    SAFE_CHAIN_ID,
    BuiltSafeTransaction,
    SafeOwnerSignature,
    SafeSignatureValidation,
    SafeTransaction,
    SafeTransactionRpcBackend,
    build_exec_transaction_calldata,
    build_safe_transaction,
    sign_safe_tx_hash,
)
from aero_bot.signing_key import load_signing_key_source
from aero_bot.venues import BASE_USDC_ADDRESS, PoolDiscoveryStatus

# keccak256("ownerOf(uint256)")[0:4], the ERC721 ownership read.
ERC721_OWNER_OF_SELECTOR = "6352211e"
# keccak256("isApprovedForAll(address,address)")[0:4], the operator read.
ERC721_IS_APPROVED_FOR_ALL_SELECTOR = "e985e9c5"
# keccak256("tokenOfOwnerByIndex(address,uint256)")[0:4], the ERC721
# enumeration read backing position reconciliation.
ERC721_TOKEN_OF_OWNER_BY_INDEX_SELECTOR = "2f745c59"  # noqa: S105 - a keccak selector, not a secret
# keccak256("IncreaseLiquidity(uint256,uint128,uint256,uint256)")[0:4], the
# NFPM mint event whose first indexed topic carries the fresh token id.
NFPM_INCREASE_LIQUIDITY_TOPIC0 = (
    "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
)
# The LP deadline sits eight minutes past its build time, mirroring the swap.
LP_DEADLINE_SECONDS = 8 * 60
# LP entries may require balancing swaps materially larger than the manual
# one-shot swap executor's 20 USDC standing allowance. Keep this LP-specific
# so widening LP capacity cannot widen the unrelated manual swap surface.
DEFAULT_LP_ROUTER_ALLOWANCE_USDC = Decimal("200")
LP_ROUTER_ALLOWANCE_CAP_CEILING_USDC = Decimal("200")
# Fresh-estimate re-reads when a receipt landed on one public endpoint but the
# estimating endpoint's latest block still predates it (observed live as a
# transient GS026 with every predecessor already mined).
EXECUTE_ESTIMATE_LAG_RETRIES = 3
EXECUTE_ESTIMATE_LAG_RETRY_SECONDS = 4.0
# Bounded cross-endpoint receipt wait: poll every backend once per round.
EXECUTE_RECEIPT_TOTAL_TIMEOUT_SECONDS = 600.0
EXECUTE_RECEIPT_POLL_SECONDS = 3.0
# Public Base endpoints polled round-robin while awaiting one inclusion; the
# configured primary RPC always leads the rotation.
EXECUTE_RECEIPT_ENDPOINT_URLS: tuple[str, ...] = (
    "https://mainnet.base.org",
    "https://base.publicnode.com",
    "https://1rpc.io/base",
    "https://base.drpc.org",
)
# AERO is a standard eighteen-decimal ERC20; the emissions valuation scales by it.
AERO_DECIMALS = 18
# Timing metrics round to whole milliseconds.
TIMING_PRECISION = Decimal("0.001")
# CLI exit codes: zero on success, one on failures, two on refusals.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2


class LpExecutionRefusalError(RuntimeError):
    """Refuse one LP request before signing with its catalog code."""

    def __init__(self, code: "LpExecutionRefusalCode", message: str) -> None:
        """Store the refusal's catalog code alongside its explanation.

        Args:
            code: The stable refusal-code identifier documented in
                docs/lp_execution.md.
            message: The actionable refusal explanation.
        """
        super().__init__(message)
        self.code = code


class LpExecutionRefusalCode(StrEnum):
    """Catalog every execution-layer refusal the LP lifecycle can raise."""

    # The official B20 registry did not validate, so no token is trusted.
    REGISTRY_UNVERIFIED = "registry_unverified"
    # The requested symbol is absent from the official B20 registry.
    SYMBOL_NOT_IN_REGISTRY = "symbol_not_in_registry"
    # No live Sugar-verified pool exists for the requested symbol.
    POOL_NOT_DISCOVERED = "pool_not_discovered"
    # The discovery snapshot carries no observation evidence to anchor to.
    SNAPSHOT_EVIDENCE_MISSING = "snapshot_evidence_missing"
    # The pool's Sugar record carries no NFPM or gauge, so no lifecycle exists.
    POOL_MISSING_NFPM_OR_GAUGE = "pool_missing_nfpm_or_gauge"
    # The pool snapshot is older than the configured staleness bound.
    SNAPSHOT_STALE = "snapshot_stale"
    # The Safe already holds position NFTs this executor cannot value yet.
    UNTRACKED_EXISTING_POSITIONS = "untracked_existing_positions"
    # No explicit width override was supplied and the solver path is not wired.
    DERIVED_WIDTH_UNAVAILABLE = "derived_width_unavailable"
    # The balancing swap plan needs multiple tranches this surface cannot run.
    MULTI_TRANCHE_SWAP_UNSUPPORTED = "multi_tranche_swap_unsupported"
    # The endpoint's gas price exceeds the configured cap.
    GAS_PRICE_ABOVE_CAP = "gas_price_above_cap"
    # The Safe's ETH balance sits below the documented floor.
    SAFE_ETH_BELOW_FLOOR = "safe_eth_below_floor"
    # The named position NFT does not exist on the pool's NFPM.
    POSITION_UNKNOWN = "position_unknown"
    # The position NFT is owned by an address that is neither the Safe nor
    # this pool's gauge.
    POSITION_NOT_OWNED = "position_not_owned"
    # An NFPM-side operation was requested while the gauge holds the NFT.
    POSITION_STAKED = "position_staked"
    # A gauge-side operation was requested while the Safe itself holds the NFT.
    POSITION_NOT_STAKED = "position_not_staked"
    # A withdraw was requested on a position holding no liquidity and no fees.
    POSITION_EMPTY = "position_empty"
    # The live AERO price read backing an emissions-APR quote failed.
    AERO_PRICE_UNREADABLE = "aero_price_unreadable"
    # A claim or withdrawal would land inside the early-exit penalty window.
    WITHIN_PENALTY_WINDOW = "within_penalty_window"
    # The penalty window could not be resolved from live reads, so any claim
    # or withdrawal is refused rather than guessed at.
    PENALTY_STATE_UNREADABLE = "penalty_state_unreadable"
    # An execute command was issued without its explicit broadcast confirmation.
    BROADCAST_CONFIRMATION_MISSING = "broadcast_confirmation_missing"
    # A rebuilt SafeTx hash no longer equals its report, so the built content
    # cannot be trusted to be what was validated.
    REBUILD_HASH_MISMATCH = "rebuild_hash_mismatch"
    # The live Safe rejected the owner signature at execute time.
    SIGNATURE_REJECTED = "signature_rejected"
    # The fresh on-chain estimate reverted with every predecessor mined.
    ESTIMATE_REVERTED = "estimate_reverted"
    # A confirmed balancing swap still leaves another fresh swap requirement.
    POST_SWAP_REBALANCE_REQUIRED = "post_swap_rebalance_required"
    # The relaying EOA cannot afford the floor plus the bounded gas cost.
    RELAYER_ETH_INSUFFICIENT = "relayer_eth_insufficient"
    # The exit swap found no stock balance to convert back to USDC.
    STOCK_BALANCE_ZERO = "stock_balance_zero"
    # The exit swap's quoted USDC output exceeds the per-pool pilot cap.
    EXIT_OUTPUT_ABOVE_POOL_CAP = "exit_output_above_pool_cap"
    # The Safe's held-NFT enumeration could not be read honestly.
    ENUMERATION_UNREADABLE = "enumeration_unreadable"


class LpExecutionRole(StrEnum):
    """Identify which Safe transaction of one LP attempt is described."""

    # The bounded USDC allowance setup for the router, sequenced first.
    ROUTER_ALLOWANCE = "router_allowance"
    # The balancing swap buying the stock shortfall before the mint.
    BALANCING_SWAP = "balancing_swap"
    # The exact USDC approval the NFPM's mint pull requires.
    NFPM_USDC_ALLOWANCE = "nfpm_usdc_allowance"
    # The exact stock approval the NFPM's mint pull requires.
    NFPM_STOCK_ALLOWANCE = "nfpm_stock_allowance"
    # The twelve-field Slipstream mint through the pool's own NFPM.
    MINT = "mint"
    # The NFPM operator approval letting the gauge pull position NFTs.
    NFPM_GAUGE_APPROVAL = "nfpm_gauge_approval"
    # The CLGauge deposit staking one position NFT.
    GAUGE_DEPOSIT = "gauge_deposit"
    # The CLGauge withdraw unstaking one position NFT.
    GAUGE_WITHDRAW = "gauge_withdraw"
    # The NFPM liquidity decrease returning amounts to the position.
    NFPM_DECREASE = "nfpm_decrease_liquidity"
    # The NFPM collect sweeping fees and leftovers to the Safe.
    NFPM_COLLECT = "nfpm_collect"
    # The NFPM burn clearing one emptied position NFT.
    NFPM_BURN = "nfpm_burn"
    # The CLGauge per-token emissions claim.
    GAUGE_GET_REWARD = "gauge_get_reward"
    # The exact stock approval the router's exit swap pull requires.
    STOCK_ROUTER_ALLOWANCE = "stock_router_allowance"
    # The exact-input stock-to-USDC swap closing one LP exit.
    EXIT_SWAP = "exit_swap"


class LpSafeExecutionPolicy(BaseModel):
    """Hold every hard execution cap the LP lifecycle enforces before signing."""

    # Frozen strict fields keep one attempt's caps stable end to end.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The router whitelist is exactly the swap executor's single router.
    router_address: EvmAddress = AERODROME_ROUTER_ADDRESS
    # Effective gas price above this cap refuses the attempt.
    gas_price_cap_wei: Annotated[int, Field(gt=0)] = DEFAULT_GAS_PRICE_CAP_WEI
    # A Safe ETH balance below this floor refuses the attempt.
    safe_eth_floor_wei: Annotated[int, Field(ge=0)] = DEFAULT_SAFE_ETH_FLOOR_WEI
    # Pool snapshots older than this many seconds refuse the attempt.
    snapshot_max_age_seconds: Annotated[int, Field(gt=0)] = DEFAULT_QUOTE_MAX_AGE_SECONDS
    # The bounded standing USDC allowance for the router, shared with the swap
    # executor's documented bound; never the infinite maximum approval.
    router_allowance_standing_cap_usdc: Annotated[Decimal, Field(gt=0)] = (
        DEFAULT_LP_ROUTER_ALLOWANCE_USDC
    )
    # A relaying EOA balance below this floor refuses any broadcast, separate
    # from the gas-cost check so a cheap delivery still needs real headroom.
    relayer_eth_floor_wei: Annotated[int, Field(gt=0)] = DEFAULT_RELAYER_ETH_FLOOR_WEI

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
    def require_ceiling_compliance(self) -> "LpSafeExecutionPolicy":
        """Enforce the hard ceilings no configuration may exceed."""
        if self.router_allowance_standing_cap_usdc > LP_ROUTER_ALLOWANCE_CAP_CEILING_USDC:
            raise ValueError(
                f"router_allowance_standing_cap_usdc {self.router_allowance_standing_cap_usdc}"
                f" exceeds the documented bound of "
            f"{LP_ROUTER_ALLOWANCE_CAP_CEILING_USDC} USDC; the "
                "allowance is bounded and never infinite"
            )
        return self

    @property
    def router_allowance_standing_cap_units(self) -> int:
        """Return the standing router allowance cap in raw USDC units."""
        return usdc_units(self.router_allowance_standing_cap_usdc)


class BuiltLpTransaction(BaseModel):
    """Describe one fully built and signed LP Safe transaction."""

    # Frozen strict fields bind the report to the exact built content.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Which transaction of the attempt this is.
    role: LpExecutionRole
    # The lifecycle action this transaction belongs to.
    action: str
    # The EIP-712 SafeTx hash the signature covers.
    safe_tx_hash: str
    # The contract the Safe transaction calls.
    to_address: EvmAddress
    # keccak256 of the complete execTransaction calldata.
    calldata_digest: str
    # The Safe nonce this transaction occupies.
    nonce: Annotated[int, Field(ge=0)]
    # Human-readable summary of what the inner call does.
    description: str
    # The read-only contract verdict on the produced owner signature.
    signature_verified: bool
    # The verdict's diagnostic, carrying revert evidence when rejected.
    signature_diagnostic: str
    # The on-chain gas estimate, absent when the estimate reverted.
    gas_estimate: Annotated[int, Field(gt=0)] | None
    # Why the gas estimate is absent, empty when it succeeded.
    gas_estimate_diagnostic: str = ""


class _BuiltLpStep:
    """Carry one built step's report together with its signing artifacts."""

    def __init__(
        self,
        report: BuiltLpTransaction,
        transaction: SafeTransaction,
        built: BuiltSafeTransaction,
        signature: SafeOwnerSignature,
        exec_calldata: str,
    ) -> None:
        """Bind the report to the exact artifacts that produced it."""
        self.report = report
        self.transaction = transaction
        self.built = built
        self.signature = signature
        self.exec_calldata = exec_calldata


class LpStepExecutionReport(BaseModel):
    """Report one broadcast LP Safe transaction's delivery and inclusion."""

    # Frozen strict fields preserve one coherent delivery outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the broadcast explicit in every report.
    mode: Literal[ExecutionMode.EXECUTE] = ExecutionMode.EXECUTE
    # The lifecycle action the delivery belongs to.
    action: str
    # Which transaction of the attempt was delivered.
    role: LpExecutionRole
    # The SafeTx hash of the executed Safe transaction.
    safe_tx_hash: str
    # The Safe nonce the executed transaction occupies.
    nonce: Annotated[int, Field(ge=0)]
    # The Base transaction hash that delivered execTransaction.
    transaction_hash: str
    # confirmed means included with status one; failed means included and
    # reverted; unconfirmed means no receipt arrived within the bounded wait
    # and the audit chain's send record remains the source of truth.
    status: Literal["confirmed", "failed", "unconfirmed"]
    # The block that included the delivery, absent while unconfirmed.
    block_number: Annotated[int, Field(ge=0)] | None
    # Gas units the delivery consumed, absent while unconfirmed.
    gas_used: Annotated[int, Field(ge=0)] | None
    # The effective gas price the delivery paid, absent while unconfirmed.
    effective_gas_price_wei: Annotated[int, Field(ge=0)] | None
    # The total fee the delivery consumed, absent while unconfirmed.
    fee_wei: Annotated[int, Field(ge=0)] | None
    # The delivery transaction's gas limit, estimate buffered by a fifth.
    delivery_gas_limit: Annotated[int, Field(gt=0)]
    # The delivery's max fee per gas, the observed price capped at the policy.
    delivery_max_fee_per_gas_wei: Annotated[int, Field(gt=0)]
    # The relaying EOA's nonce the delivery consumed.
    relayer_nonce: Annotated[int, Field(ge=0)]
    # Broadcast-to-inclusion duration in milliseconds; zero when unconfirmed.
    inclusion_ms: Annotated[Decimal, Field(ge=0)]
    # The rebuild pin's duration in milliseconds.
    rebuild_ms: Annotated[Decimal, Field(ge=0)]
    # The fresh live signature validation's duration in milliseconds.
    validate_ms: Annotated[Decimal, Field(ge=0)]
    # The fresh estimate's duration in milliseconds, retries included.
    estimate_ms: Annotated[Decimal, Field(ge=0)]
    # The delivery build's duration in milliseconds.
    delivery_ms: Annotated[Decimal, Field(ge=0)]
    # The broadcast submission's duration in milliseconds.
    send_ms: Annotated[Decimal, Field(ge=0)]
    # Human-readable evidence for the outcome, empty when plainly confirmed.
    diagnostic: str = ""


class LpMintDryRunReport(BaseModel):
    """Report one complete LP mint build-and-validate attempt."""

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The capped mint plan every built transaction executes.
    plan: LpMintPlan
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The live USDC allowance the Safe held for the router at build time.
    router_usdc_allowance_units: Annotated[int, Field(ge=0)]
    # The live USDC allowance the Safe held for the NFPM at build time.
    nfpm_usdc_allowance_units: Annotated[int, Field(ge=0)]
    # The live stock allowance the Safe held for the NFPM at build time.
    nfpm_stock_allowance_units: Annotated[int, Field(ge=0)]
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every built transaction in execution order.
    transactions: Annotated[tuple[BuiltLpTransaction, ...], Field(min_length=1)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Annotated[Decimal, Field(ge=0)]


class LpStakeDryRunReport(BaseModel):
    """Report one complete LP stake build-and-validate attempt."""

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool whose position is being staked.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge the deposit targets.
    gauge_address: EvmAddress
    # The position NFT being staked.
    token_id: Annotated[int, Field(ge=0)]
    # The live ownerOf answer, absent when the view reverted because the token
    # is not minted yet; a pre-mint dry run proves machinery, not ownership.
    token_owner_address: EvmAddress | None
    # Why token_owner_address is absent or flagged, empty when plainly owned.
    ownership_diagnostic: str = ""
    # The live twelve-word position view, absent when the read reverted.
    position: LpPositionView | None
    # Why position is absent, empty when the view decoded.
    position_diagnostic: str = ""
    # Whether the NFPM already carries the gauge operator approval.
    gauge_operator_approved: bool
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every built transaction in execution order.
    transactions: Annotated[tuple[BuiltLpTransaction, ...], Field(min_length=1)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Annotated[Decimal, Field(ge=0)]


class LpPenaltyWindow(BaseModel):
    """Carry the resolved early-exit penalty window of one staked position."""

    # Frozen strict fields preserve one coherent penalty observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The factory's penalty in basis points; 10000 means total forfeiture.
    penalty_rate_bps: Annotated[int, Field(ge=0)]
    # The pool's minimum stake time in seconds.
    min_stake_seconds: Annotated[int, Field(ge=0)]
    # The unix timestamp of the position's most recent deposit.
    deposit_timestamp: Annotated[int, Field(ge=0)]
    # The unix timestamp at which the window clears.
    window_clears_at_timestamp: Annotated[int, Field(ge=0)]
    # Seconds still inside the window, zero once it has cleared.
    remaining_seconds: Annotated[int, Field(ge=0)]


class LpUnstakeDryRunReport(BaseModel):
    """Report one complete LP unstake build-and-validate attempt."""

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool whose position is being unstaked.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge the withdraw targets.
    gauge_address: EvmAddress
    # The position NFT being unstaked.
    token_id: Annotated[int, Field(ge=0)]
    # The live twelve-word position view.
    position: LpPositionView
    # The live accrued emissions the withdraw auto-claims, in raw AERO units.
    accrued_aero_earned_units: Annotated[int, Field(ge=0)]
    # The checkpointed claimable emissions, in raw AERO units.
    accrued_aero_checkpoint_units: Annotated[int, Field(ge=0)]
    # The resolved early-exit penalty window.
    penalty: LpPenaltyWindow
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every built transaction in execution order.
    transactions: Annotated[tuple[BuiltLpTransaction, ...], Field(min_length=1)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Decimal
    # Human-readable evidence lines covering the unstake.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class LpExitDryRunReport(BaseModel):
    """Report one complete LP withdraw build-and-validate attempt.

    The withdraw action is the unstaked exit: decreaseLiquidity of the
    position's entire live liquidity followed by collect, returning both
    tokens and every checkpointed fee to the Safe.
    """

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool whose position is being exited.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge the position must not be staked in.
    gauge_address: EvmAddress
    # The position NFT being exited.
    token_id: Annotated[int, Field(ge=0)]
    # The live twelve-word position view.
    position: LpPositionView
    # Where the snapshot price sits relative to the position range.
    range_state: PositionRangeState
    # The expected raw token-zero amount the full decrease returns.
    amount0_units: Decimal
    # The expected raw token-one amount the full decrease returns.
    amount1_units: Decimal
    # The minimum accepted token-zero output after slippage.
    amount0_min_units: Annotated[int, Field(ge=0)]
    # The minimum accepted token-one output after slippage.
    amount1_min_units: Annotated[int, Field(ge=0)]
    # The checkpointed token-zero fees the collect sweeps.
    fees_owed0_units: Annotated[int, Field(ge=0)]
    # The checkpointed token-one fees the collect sweeps.
    fees_owed1_units: Annotated[int, Field(ge=0)]
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every built transaction in execution order.
    transactions: Annotated[tuple[BuiltLpTransaction, ...], Field(min_length=1)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Decimal
    # Human-readable evidence lines covering the exit.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class LpCollectDryRunReport(BaseModel):
    """Report one complete LP collect build-and-validate attempt.

    The claim path follows ownership: a staked position claims emissions
    through the gauge's per-token getReward, and an unstaked position sweeps
    checkpointed fees through the NFPM's collect.
    """

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool whose position is being collected.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge when the claim path is the gauge.
    gauge_address: EvmAddress
    # The position NFT being collected.
    token_id: Annotated[int, Field(ge=0)]
    # The live twelve-word position view.
    position: LpPositionView
    # Whether the gauge holds the NFT and the claim path is getReward.
    staked: bool
    # The live accrued emissions when staked, else zero.
    accrued_aero_earned_units: Annotated[int, Field(ge=0)] = 0
    # The checkpointed claimable emissions when staked, else zero.
    accrued_aero_checkpoint_units: Annotated[int, Field(ge=0)] = 0
    # The resolved penalty window when staked, else None.
    penalty: LpPenaltyWindow | None = None
    # The checkpointed token-zero fees when unstaked, else zero.
    fees_owed0_units: Annotated[int, Field(ge=0)] = 0
    # The checkpointed token-one fees when unstaked, else zero.
    fees_owed1_units: Annotated[int, Field(ge=0)] = 0
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every built transaction in execution order.
    transactions: Annotated[tuple[BuiltLpTransaction, ...], Field(min_length=1)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Decimal
    # Human-readable evidence lines covering the collect.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class LpExitSwapDryRunReport(BaseModel):
    """Report one complete LP exit-swap build-and-validate attempt.

    The exit swap converts the Safe's ENTIRE stock balance back to USDC
    through the same whitelisted router every balancing swap uses, in the
    reverse direction: exact-input stock, minimum-output USDC, recipient
    Safe. The quoted output may never exceed the per-pool pilot cap, so an
    out-of-band inventory refuses instead of swapping.
    """

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool whose stock side is being sold.
    pool_address: EvmAddress
    # The pool's live CLGauge naming the pool's lifecycle.
    gauge_address: EvmAddress
    # The whitelisted router executing the reverse swap.
    router_address: EvmAddress
    # The stock token being converted.
    stock_token_address: EvmAddress
    # The Sugar snapshot block anchoring the quote.
    snapshot_block: Annotated[int, Field(ge=0)]
    # The snapshot's USDC price of one whole stock token.
    price_usdc_per_stock: Decimal
    # The Safe's entire live stock balance in raw units.
    stock_balance_units: Annotated[int, Field(ge=0)]
    # The exact-input stock units the swap sells.
    amount_in_units: Annotated[int, Field(ge=0)]
    # The quoted USDC output at the snapshot price.
    expected_out_units: Annotated[int, Field(ge=0)]
    # The minimum accepted USDC output after slippage.
    amount_out_min_units: Annotated[int, Field(ge=0)]
    # The live stock allowance the Safe held for the router at build time.
    router_stock_allowance_units: Annotated[int, Field(ge=0)]
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every built transaction in execution order.
    transactions: Annotated[tuple[BuiltLpTransaction, ...], Field(min_length=1)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Decimal
    # Human-readable evidence lines covering the exit swap.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class LpActionExecutionReport(BaseModel):
    """Report one complete LP action executed through the broadcast path."""

    # Frozen strict fields preserve one coherent execution outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the broadcast explicit.
    mode: Literal[ExecutionMode.EXECUTE] = ExecutionMode.EXECUTE
    # Which lifecycle action ran.
    action: str
    # The dry-run build every broadcast step executed, with its plan evidence.
    build: (
        LpMintDryRunReport
        | LpStakeDryRunReport
        | LpUnstakeDryRunReport
        | LpExitDryRunReport
        | LpCollectDryRunReport
        | LpExitSwapDryRunReport
    )
    # Every broadcast step in execution order, including the halting one.
    steps: Annotated[tuple[LpStepExecutionReport, ...], Field(min_length=0)]
    # Whether every composed step confirmed on-chain.
    completed: bool
    # Why the sequence halted early, empty when it ran to completion.
    halted_reason: str = ""


class LpRecenterDryRunReport(BaseModel):
    """Report one complete LP recenter build-and-validate attempt.

    The recenter is the full management cycle as one audited batch: unstake
    when staked, exit and burn the old position, recycle the returned
    inventory into a fresh capped mint at the requested width, and stage the
    gauge approval for the restake follow-up.
    """

    # Frozen strict fields preserve one coherent dry-run outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker makes the no-broadcast guarantee auditable.
    mode: Literal[ExecutionMode.DRY_RUN] = ExecutionMode.DRY_RUN
    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool whose position is being recentered.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge.
    gauge_address: EvmAddress
    # The old position NFT being exited and burned.
    token_id: Annotated[int, Field(ge=0)]
    # The live twelve-word view of the old position.
    position: LpPositionView
    # Whether the batch opens with a gauge withdraw.
    staked: bool
    # Where the snapshot price sits relative to the old range.
    range_state: PositionRangeState
    # The expected raw token-zero amount the full decrease returns.
    amount0_units: Decimal
    # The expected raw token-one amount the full decrease returns.
    amount1_units: Decimal
    # The checkpointed fees the exit collect sweeps, token zero.
    fees_owed0_units: Annotated[int, Field(ge=0)]
    # The checkpointed fees the exit collect sweeps, token one.
    fees_owed1_units: Annotated[int, Field(ge=0)]
    # The Safe's projected post-exit USDC balance in raw units.
    projected_usdc_units: Annotated[int, Field(ge=0)]
    # The Safe's projected post-exit stock balance in raw units.
    projected_stock_units: Annotated[int, Field(ge=0)]
    # The capped mint plan the recycled inventory funds.
    plan: LpMintPlan
    # How the restake completes once the fresh mint confirms.
    restake_followup: str
    # The Safe every built transaction targets.
    safe_address: EvmAddress
    # The public address of the EOA whose key signed the build.
    relayer_address: EvmAddress
    # Whether the signing key was generated for this dry run only.
    ephemeral_key: bool
    # The Base gas price observed before building.
    gas_price_wei: Annotated[int, Field(ge=0)]
    # The Safe ETH balance observed before building.
    safe_eth_wei: Annotated[int, Field(ge=0)]
    # Every built transaction in execution order.
    transactions: Annotated[tuple[BuiltLpTransaction, ...], Field(min_length=1)]
    # Every cap checked before signing, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Wall-clock duration of the build phase in milliseconds.
    build_duration_ms: Decimal


class LpHeldPosition(BaseModel):
    """Carry one Safe-held position NFT's exposure-relevant live fields."""

    # Frozen strict fields keep one enumerated snapshot immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The enumerated position NFT id.
    token_id: Annotated[int, Field(ge=0)]
    # The position's live liquidity, zero once fully decreased.
    liquidity: Annotated[int, Field(ge=0)]
    # The checkpointed token-zero fees waiting to be collected.
    tokens_owed0_units: Annotated[int, Field(ge=0)]
    # The checkpointed token-one fees waiting to be collected.
    tokens_owed1_units: Annotated[int, Field(ge=0)]

    @property
    def live(self) -> bool:
        """Return whether this NFT still carries liquidity or owed fees."""
        return self.liquidity > 0 or self.tokens_owed0_units > 0 or self.tokens_owed1_units > 0


class LpSafePositionsSnapshot(BaseModel):
    """Report the Safe's complete held-NFT inventory on one pool's NFPM.

    The snapshot is the reconciliation primitive: it enumerates every NFT the
    Safe holds, classifies each as live (liquidity or owed fees) or an empty
    residual, and anchors both to the block-pinned observation.
    """

    # Frozen strict fields keep one coherent reconciliation snapshot.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool whose NFPM was enumerated.
    pool_address: EvmAddress
    # The enumerated NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # Every Safe-held NFT in enumeration order.
    positions: Annotated[tuple[LpHeldPosition, ...], Field(min_length=0)]
    # The Sugar snapshot block anchoring the identity.
    snapshot_block: Annotated[int, Field(ge=0)]
    # When the snapshot completed, timezone-aware.
    observed_at: datetime
    # Every gate checked before the observation, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Human-readable evidence lines covering the enumeration.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]

    @property
    def live_positions(self) -> tuple[LpHeldPosition, ...]:
        """Return only the NFTs still carrying liquidity or owed fees."""
        return tuple(position for position in self.positions if position.live)

    @property
    def empty_count(self) -> int:
        """Return how many held NFTs are empty residuals carrying no exposure."""
        return len(self.positions) - len(self.live_positions)


class LpPositionStatusReport(BaseModel):
    """Report one read-only LP position observation; nothing is signed."""

    # Frozen strict fields preserve one coherent observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol identifying the pool.
    symbol: str
    # The pool holding the position.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge.
    gauge_address: EvmAddress
    # The position NFT being reported on.
    token_id: Annotated[int, Field(ge=0)]
    # The live ownerOf answer.
    token_owner_address: EvmAddress
    # Whether the gauge holds the NFT.
    staked: bool
    # The live twelve-word position view.
    position: LpPositionView
    # Where the snapshot tick sits relative to the position range.
    range_state: PositionRangeState
    # The snapshot's current pool tick.
    current_tick: int
    # The position's raw token-zero composition at the snapshot price.
    amount0_units: Decimal
    # The position's raw token-one composition at the snapshot price.
    amount1_units: Decimal
    # The composition's USDC value on the token-zero side.
    token0_value_usdc: Decimal
    # The composition's USDC value on the token-one side.
    token1_value_usdc: Decimal
    # The composition's total USDC value.
    position_value_usdc: Decimal
    # The checkpointed token-zero fees waiting to be collected.
    fees_owed0_units: Annotated[int, Field(ge=0)]
    # The checkpointed token-one fees waiting to be collected.
    fees_owed1_units: Annotated[int, Field(ge=0)]
    # The live accrued emissions when staked, else None.
    accrued_aero_earned_units: Annotated[int, Field(ge=0)] | None = None
    # The checkpointed claimable emissions when staked, else None.
    accrued_aero_checkpoint_units: Annotated[int, Field(ge=0)] | None = None
    # The resolved penalty window when staked, else None.
    penalty: LpPenaltyWindow | None = None
    # The quoted emissions APR as a decimal fraction in Aerodrome's
    # displayed convention, None when the inputs are absent.
    quoted_emissions_apr: Decimal | None = None
    # How the quoted APR was derived, including its width dependence.
    apr_diagnostic: str = ""
    # The AERO price the quote used, in USDC - a live read from Aerodrome's
    # own USDC/AERO pool unless the operator overrode it.
    aero_price_assumption_usdc: Decimal
    # The entry cost basis when one was supplied, else None.
    entry_cost_usdc: Decimal | None = None
    # The unrealized profit against the entry cost when computable.
    unrealized_pnl_usdc: Decimal | None = None
    # Why the P&L is absent, empty when computed.
    pnl_diagnostic: str = ""
    # The Sugar snapshot block anchoring every derived number.
    snapshot_block: Annotated[int, Field(ge=0)]
    # When the underlying snapshot completed.
    observed_at: datetime
    # Every gate checked before the observation, in enforced order.
    caps_enforced: Annotated[tuple[str, ...], Field(min_length=1)]
    # Human-readable evidence lines covering the observation.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class LpMintPlannedPayload(BaseModel):
    """Persist one accepted mint plan's public numbers on the audit chain."""

    # Frozen strict fields keep the audited plan immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this plan belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge.
    gauge_address: EvmAddress
    # The Sugar snapshot block anchoring the plan.
    snapshot_block: Annotated[int, Field(ge=0)]
    # The total USDC value the position commits.
    budget_usdc: Decimal
    # The derived range's inclusive lower boundary.
    tick_lower: int
    # The derived range's exclusive upper boundary.
    tick_upper: int
    # The half width in whole ticks per side.
    half_width_ticks: int
    # How the half width was chosen.
    width_source: WidthSource
    # The desired raw token-zero and token-one mint amounts.
    amount0_desired_units: Annotated[int, Field(ge=0)]
    amount1_desired_units: Annotated[int, Field(ge=0)]
    # Whether the plan requires a balancing swap before the mint.
    balancing_swap_required: bool
    # Which side the balancing swap sells, or none when inventory already fits.
    balancing_swap_direction: BalancingSwapDirection
    # The balancing swap's exact USDC input, zero unless buying stock.
    swap_usdc_in_units: Annotated[int, Field(ge=0)]
    # The balancing swap's exact stock input, zero unless selling stock.
    swap_stock_in_units: Annotated[int, Field(ge=0)]
    # The quoted USDC output, zero unless selling stock.
    swap_expected_usdc_units: Annotated[int, Field(ge=0)]
    # The balancing swap's conservative impact bound.
    swap_modeled_impact_fraction: Decimal
    # Every cap the planner enforced, in order.
    caps_enforced: tuple[str, ...]


class LpStakePlannedPayload(BaseModel):
    """Persist one stake plan's public evidence on the audit chain."""

    # Frozen strict fields keep the audited plan immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this plan belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge.
    gauge_address: EvmAddress
    # The position NFT being staked.
    token_id: Annotated[int, Field(ge=0)]
    # The live ownerOf answer, absent when the token is not minted yet.
    token_owner_address: EvmAddress | None
    # Whether the NFPM already carries the gauge operator approval.
    gauge_operator_approved: bool


class LpUnstakePlannedPayload(BaseModel):
    """Persist one unstake plan's public evidence on the audit chain."""

    # Frozen strict fields keep the audited plan immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this plan belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge.
    gauge_address: EvmAddress
    # The position NFT being unstaked.
    token_id: Annotated[int, Field(ge=0)]
    # The live accrued emissions the withdraw auto-claims, raw AERO units.
    accrued_aero_earned_units: Annotated[int, Field(ge=0)]
    # The checkpointed claimable emissions, raw AERO units.
    accrued_aero_checkpoint_units: Annotated[int, Field(ge=0)]
    # The penalty rate in basis points at plan time.
    penalty_rate_bps: Annotated[int, Field(ge=0)]
    # Seconds still inside the penalty window at plan time.
    penalty_remaining_seconds: Annotated[int, Field(ge=0)]


class LpExitPlannedPayload(BaseModel):
    """Persist one withdraw plan's public evidence on the audit chain."""

    # Frozen strict fields keep the audited plan immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this plan belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge the exited position is not staked in.
    gauge_address: EvmAddress
    # The position NFT being exited.
    token_id: Annotated[int, Field(ge=0)]
    # Where the snapshot price sat relative to the exited range.
    range_state: PositionRangeState
    # The expected raw token-zero decrease output.
    amount0_units: Decimal
    # The expected raw token-one decrease output.
    amount1_units: Decimal
    # The minimum accepted token-zero output after slippage.
    amount0_min_units: Annotated[int, Field(ge=0)]
    # The minimum accepted token-one output after slippage.
    amount1_min_units: Annotated[int, Field(ge=0)]
    # The checkpointed token-zero fees the collect sweeps.
    fees_owed0_units: Annotated[int, Field(ge=0)]
    # The checkpointed token-one fees the collect sweeps.
    fees_owed1_units: Annotated[int, Field(ge=0)]


class LpCollectPlannedPayload(BaseModel):
    """Persist one collect plan's public evidence on the audit chain."""

    # Frozen strict fields keep the audited plan immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this plan belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge when the claim path is the gauge.
    gauge_address: EvmAddress
    # The position NFT being collected.
    token_id: Annotated[int, Field(ge=0)]
    # Whether the claim runs through the gauge's getReward.
    staked: bool
    # The live accrued emissions when staked, raw AERO units, else zero.
    accrued_aero_earned_units: Annotated[int, Field(ge=0)] = 0
    # The checkpointed emissions when staked, raw AERO units, else zero.
    accrued_aero_checkpoint_units: Annotated[int, Field(ge=0)] = 0
    # The checkpointed token-zero fees when unstaked, else zero.
    fees_owed0_units: Annotated[int, Field(ge=0)] = 0
    # The checkpointed token-one fees when unstaked, else zero.
    fees_owed1_units: Annotated[int, Field(ge=0)] = 0


class LpRecenterPlannedPayload(BaseModel):
    """Persist one recenter batch plan's public evidence on the audit chain."""

    # Frozen strict fields keep the audited plan immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this plan belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The pool's own NonfungiblePositionManager.
    nfpm_address: EvmAddress
    # The pool's live CLGauge.
    gauge_address: EvmAddress
    # The old position NFT being exited and burned.
    token_id: Annotated[int, Field(ge=0)]
    # Whether the batch opens with a gauge withdraw.
    staked: bool
    # The USDC budget the recycled inventory funds.
    budget_usdc: Decimal
    # The projected post-exit USDC balance in raw units.
    projected_usdc_units: Annotated[int, Field(ge=0)]
    # The projected post-exit stock balance in raw units.
    projected_stock_units: Annotated[int, Field(ge=0)]
    # The fresh range's inclusive lower boundary.
    tick_lower: int
    # The fresh range's exclusive upper boundary.
    tick_upper: int
    # How the restake completes once the fresh mint confirms.
    restake_followup: str


class LpStatusReportedPayload(BaseModel):
    """Persist one read-only position report's public numbers."""

    # Frozen strict fields keep the audited observation immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode marker: status never builds or signs anything.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The position NFT being reported on.
    token_id: Annotated[int, Field(ge=0)]
    # Whether the gauge holds the NFT.
    staked: bool
    # The position's total USDC value at the snapshot price.
    position_value_usdc: Decimal
    # The quoted emissions APR as a decimal fraction, None when unavailable.
    quoted_emissions_apr: Decimal | None
    # The AERO price assumption the quote used, in USDC.
    aero_price_assumption_usdc: Decimal


class LpTransactionBuiltPayload(BaseModel):
    """Persist one fully built LP Safe transaction's public evidence."""

    # Frozen strict fields keep the audited build immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this build belongs to.
    mode: ExecutionMode
    # The lifecycle action this transaction belongs to.
    action: str
    # Which transaction of the attempt was built.
    role: LpExecutionRole
    # The SafeTx hash of the built transaction.
    safe_tx_hash: str
    # The contract the Safe transaction calls.
    to_address: EvmAddress
    # keccak256 of the complete execTransaction calldata.
    calldata_digest: str
    # The Safe nonce the transaction occupies.
    nonce: Annotated[int, Field(ge=0)]
    # Human-readable summary of what the inner call does.
    description: str
    # Whether the live contract accepted the owner signature read-only.
    signature_verified: bool
    # The on-chain gas estimate, absent when the estimate reverted.
    gas_estimate: Annotated[int, Field(gt=0)] | None


class LpExitSwapPlannedPayload(BaseModel):
    """Persist one accepted exit-swap plan's public numbers on the audit chain."""

    # Frozen strict fields keep the audited plan immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mode of the attempt this plan belongs to.
    mode: ExecutionMode
    # The registry-matched stock symbol.
    symbol: str
    # The pool contract address.
    pool_address: EvmAddress
    # The whitelisted router executing the reverse swap.
    router_address: EvmAddress
    # The Sugar snapshot block anchoring the quote.
    snapshot_block: Annotated[int, Field(ge=0)]
    # The Safe's entire live stock balance in raw units.
    stock_balance_units: Annotated[int, Field(ge=0)]
    # The snapshot's USDC price of one whole stock token.
    price_usdc_per_stock: str
    # The quoted USDC output in raw units.
    expected_out_units: Annotated[int, Field(ge=0)]
    # The minimum accepted USDC output in raw units.
    amount_out_min_units: Annotated[int, Field(ge=0)]
    # The live stock allowance the Safe held for the router.
    router_stock_allowance_units: Annotated[int, Field(ge=0)]


class LpRefusedPayload(BaseModel):
    """Persist one LP refusal with its catalog code on the audit chain."""

    # Frozen strict fields keep the audited refusal immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The lifecycle action that was refused.
    action: str
    # The mode of the refused attempt.
    mode: ExecutionMode
    # The refusal's stable catalog code.
    code: str
    # The planner's stable code when the planner refused, else empty.
    plan_code: str = ""
    # The actionable refusal explanation.
    message: str
    # The registry-matched symbol when one was resolved, else None.
    symbol: str | None = None


class LpExecuteSentPayload(BaseModel):
    """Persist one LP broadcast submission's public evidence."""

    # Frozen strict fields keep the audited submission immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The lifecycle action the broadcast belongs to.
    action: str
    # Which transaction of the attempt was broadcast.
    role: LpExecutionRole
    # The SafeTx hash of the broadcast Safe transaction.
    safe_tx_hash: str
    # The Base transaction hash that delivered execTransaction.
    transaction_hash: str
    # The Safe nonce the executed transaction occupies.
    nonce: Annotated[int, Field(ge=0)]
    # The public address of the relaying EOA.
    relayer_address: EvmAddress
    # The Safe contract the delivery transaction called.
    safe_address: EvmAddress


class LpExecuteReceiptPayload(BaseModel):
    """Persist one LP delivery inclusion outcome's public evidence."""

    # Frozen strict fields keep the audited outcome immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Whether the delivery confirmed or reverted on-chain.
    outcome: Literal["confirmed", "failed"]
    # The lifecycle action the delivery belongs to.
    action: str
    # Which transaction of the attempt the receipt describes.
    role: LpExecutionRole
    # The SafeTx hash of the executed Safe transaction.
    safe_tx_hash: str
    # The Base transaction hash that delivered execTransaction.
    transaction_hash: str
    # The block that included the delivery.
    block_number: Annotated[int, Field(ge=0)]
    # Gas units the delivery consumed.
    gas_used: Annotated[int, Field(ge=0)]
    # The effective gas price the delivery paid, in wei.
    effective_gas_price_wei: Annotated[int, Field(ge=0)]
    # Broadcast-to-inclusion duration in milliseconds.
    inclusion_ms: Annotated[int, Field(ge=0)]
    # Human-readable evidence for the outcome, empty when plainly confirmed.
    diagnostic: str = ""


class _LpStepSpec:
    """Carry one composed Safe transaction before it is built and signed."""

    def __init__(
        self, role: LpExecutionRole, to_address: str, inner_calldata: str, description: str
    ) -> None:
        """Bind one inner call to its target and human summary.

        Args:
            role: Which transaction of the attempt this is.
            to_address: The contract the Safe transaction calls.
            inner_calldata: The complete inner call payload.
            description: Human-readable summary of what the call does.
        """
        self.role = role
        self.to_address = normalize_evm_address(to_address)
        self.inner_calldata = inner_calldata
        self.description = description


class _LpMintContext:
    """Carry one resolved mint attempt's shared observation and inventory."""

    def __init__(
        self,
        listing: B20AssetListing,
        observation: LpPoolObservation,
        inventory: SafeInventory,
        width_spacings: int,
        caps: list[str],
    ) -> None:
        """Bind the resolved context fields.

        Args:
            listing: The registry listing the symbol resolved to.
            observation: The block-pinned pool observation.
            inventory: The Safe's live token inventory.
            width_spacings: The already-narrowed explicit half width.
            caps: The enforced-cap labels accumulated so far.
        """
        self.listing = listing
        self.observation = observation
        self.inventory = inventory
        self.width_spacings = width_spacings
        self.caps = caps


class _LpPositionContext:
    """Carry one resolved position attempt's observation and live ownership."""

    def __init__(
        self,
        listing: B20AssetListing,
        observation: LpPoolObservation,
        position: LpPositionView,
        owner: str,
        safe_address: str,
        caps: list[str],
    ) -> None:
        """Bind the resolved position context fields.

        Args:
            listing: The registry listing the symbol resolved to.
            observation: The block-pinned pool observation.
            position: The live twelve-word position view.
            owner: The live ownerOf answer, normalized.
            safe_address: The Safe whose positions this executor manages.
            caps: The enforced-cap labels accumulated so far.
        """
        self.listing = listing
        self.observation = observation
        self.position = position
        self.owner = owner
        self.safe_address = normalize_evm_address(safe_address)
        self.caps = caps

    @property
    def staked(self) -> bool:
        """Return whether the pool's gauge holds this position NFT."""
        return self.owner == self.observation.gauge_address

    @property
    def owned(self) -> bool:
        """Return whether the Safe itself holds this position NFT."""
        return self.owner == self.safe_address


class LpLifecycleExecutor:
    """Build and validate capped LP lifecycle transactions for the Safe."""

    def __init__(
        self,
        policy: LpSafeExecutionPolicy,
        plan_policy: LpExecutionPolicy,
        safe_address: str,
        sources: ExecutionSources,
        rpc: ExecutorRpcBackend,
        safe_rpc: SafeTransactionRpcBackend,
        audit_sink: ExecutionAuditSink | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        receipt_backends: Sequence[ExecutorRpcBackend] | None = None,
        pool_pin_store: LpPoolPinStore | None = None,
    ) -> None:
        """Configure one LP executor with every boundary it consumes.

        Args:
            policy: The hard execution caps enforced before anything is signed.
            plan_policy: The hard planning caps the pure planner enforces.
            safe_address: The canary Safe whose transactions are built.
            sources: Live read-only quote sources.
            rpc: The executor's bounded read and broadcast RPC backend.
            safe_rpc: The Safe read-only backend for nonces and validation.
            audit_sink: Optional append-only audit chain for attempt events.
            now: Injected clock producing timezone-aware event timestamps.
            timer: Injected monotonic clock for duration metrics and the
                bounded receipt wait.
            sleep: Injected delay used by bounded estimate retries and receipt
                polling.
            receipt_backends: Backends polled round-robin while awaiting one
                inclusion; defaults to the primary backend alone.
            pool_pin_store: Optional local store of Sugar-verified pool
                identities; when present, known pools skip the full Sugar
                enumeration through the verified fast path and every
                successful full sweep refreshes its pin.
        """
        self._policy = policy
        self._plan_policy = plan_policy
        self._safe_address = normalize_evm_address(safe_address)
        self._sources = sources
        self._rpc = rpc
        self._safe_rpc = safe_rpc
        self._audit_sink = audit_sink
        self._now = now
        self._timer = timer
        self._sleep = sleep
        self._receipt_backends: tuple[ExecutorRpcBackend, ...] = (
            tuple(receipt_backends) if receipt_backends is not None else (rpc,)
        )
        self._pool_pin_store = pool_pin_store

    @property
    def safe_address(self) -> str:
        """Return the normalized Safe address this executor builds for."""
        return self._safe_address

    def plan_mint(
        self, symbol: str, budget_usdc: Decimal, width_spacings: int | None
    ) -> LpMintPlan:
        """Plan one capped mint against live discovery and inventory.

        Args:
            symbol: The registry-matched B20 stock symbol, like AAPLc.
            budget_usdc: The total USDC value the position commits.
            width_spacings: The explicit half width in tick spacings per side;
                None refuses until the solver-derived width path lands.

        Returns:
            The complete capped mint plan with every cap evaluation.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
            LpPlanRefusalError: If any planning cap refuses.
        """
        try:
            context = self._resolve_mint_context(symbol, width_spacings)
            plan = self._plan_from_context(context, budget_usdc)
            self._record_mint_plan(ExecutionMode.DRY_RUN, plan)
            return plan
        except (LpExecutionRefusalError, LpPlanRefusalError) as error:
            self._record_refusal("mint", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def dry_run_mint(
        self,
        symbol: str,
        budget_usdc: Decimal,
        width_spacings: int | None,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> LpMintDryRunReport:
        """Fully build and validate one capped mint sequence without broadcasting.

        Args:
            symbol: The registry-matched B20 stock symbol.
            budget_usdc: The total USDC value the position commits.
            width_spacings: The explicit half width in tick spacings per side.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
            LpPlanRefusalError: If any planning cap refuses.
        """
        try:
            report, _ = self._build_mint_attempt(
                symbol, budget_usdc, width_spacings, key_bytes, ephemeral_key
            )
            return report
        except (LpExecutionRefusalError, LpPlanRefusalError) as error:
            self._record_refusal("mint", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def dry_run_stake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> LpStakeDryRunReport:
        """Fully build and validate one stake sequence without broadcasting.

        The token id may name a position that does not exist yet: a pre-mint
        dry run proves the signing and encoding path, and the report labels
        the missing ownership honestly instead of refusing.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The position NFT being staked.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
        """
        try:
            report, _ = self._build_stake_attempt(symbol, token_id, key_bytes, ephemeral_key)
            return report
        except LpExecutionRefusalError as error:
            self._record_refusal("stake", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def dry_run_unstake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> LpUnstakeDryRunReport:
        """Fully build and validate one unstake sequence without broadcasting.

        The unstake requires the gauge to hold the NFT, reports the accrued
        emissions the withdraw auto-claims, and refuses whenever the claim
        would land inside the factory's early-exit penalty window with
        emissions at stake.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The staked position NFT being unstaked.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
        """
        try:
            report, _ = self._build_unstake_attempt(symbol, token_id, key_bytes, ephemeral_key)
            return report
        except LpExecutionRefusalError as error:
            self._record_refusal("unstake", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def dry_run_exit(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> LpExitDryRunReport:
        """Fully build and validate one withdraw sequence without broadcasting.

        The withdraw is the unstaked exit: the position's entire live
        liquidity is decreased and both tokens plus every checkpointed fee
        are collected back to the Safe.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The unstaked position NFT being exited.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
        """
        try:
            report, _ = self._build_exit_attempt(symbol, token_id, key_bytes, ephemeral_key)
            return report
        except LpExecutionRefusalError as error:
            self._record_refusal("withdraw", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def dry_run_collect(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> LpCollectDryRunReport:
        """Fully build and validate one collect sequence without broadcasting.

        The claim path follows ownership: a staked position claims accrued
        emissions through the gauge's per-token getReward (refusing inside
        the penalty window), and an unstaked position sweeps checkpointed
        fees through the NFPM's collect.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The position NFT being collected.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
        """
        try:
            report, _ = self._build_collect_attempt(symbol, token_id, key_bytes, ephemeral_key)
            return report
        except LpExecutionRefusalError as error:
            self._record_refusal("collect", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def dry_run_recenter(
        self,
        symbol: str,
        token_id: int,
        width_spacings: int | None,
        budget_usdc: Decimal | None,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> LpRecenterDryRunReport:
        """Fully build and validate one recenter batch without broadcasting.

        The recenter is the full management cycle as one audited sequence:
        unstake when staked, decrease and collect the old position, burn its
        emptied NFT, recycle the returned inventory into a fresh capped mint
        at the requested width, and stage the gauge operator approval for
        the restake follow-up.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The old position NFT being recentered.
            width_spacings: The explicit half width in tick spacings per side.
            budget_usdc: The new mint's USDC budget; None recycles the old
                position's snapshot value.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
            LpPlanRefusalError: If any planning cap refuses.
        """
        try:
            return self._dry_run_recenter(
                symbol, token_id, width_spacings, budget_usdc, key_bytes, ephemeral_key
            )
        except (LpExecutionRefusalError, LpPlanRefusalError) as error:
            self._record_refusal("recenter", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def execute_mint(
        self,
        symbol: str,
        budget_usdc: Decimal,
        width_spacings: int | None,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Build and broadcast one capped mint sequence step by step.

        When entry needs a balancing swap, execution is deliberately two-phase.
        The swap is confirmed first, then the pool and Safe inventory are read
        again and the NFPM mint is rebuilt from that fresh post-swap state.
        Pre-swap mint calldata is never broadcast after a pool-changing swap.
        """
        if not confirm_broadcast:
            error = LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "the execute command refuses to broadcast without the explicit "
                "--confirm-broadcast flag; rerun with it to broadcast the built "
                "sequence",
            )
            self._record_refusal("mint", ExecutionMode.EXECUTE, error, symbol)
            raise error

        try:
            build, steps = self._build_mint_attempt(
                symbol,
                budget_usdc,
                width_spacings,
                key_bytes,
                ephemeral_key,
                ExecutionMode.EXECUTE,
            )

            swap_index = next(
                (
                    index
                    for index, step in enumerate(steps)
                    if step.report.role == LpExecutionRole.BALANCING_SWAP
                ),
                None,
            )

            if swap_index is None:
                step_reports, halted_reason = self._execute_steps(
                    "mint", steps, key_bytes
                )
            else:
                # Execute only approvals needed for the swap and the swap itself.
                prefix_steps = steps[: swap_index + 1]
                prefix_reports, halted_reason = self._execute_steps(
                    "mint", prefix_steps, key_bytes
                )

                if halted_reason:
                    return LpActionExecutionReport(
                        action="mint",
                        build=build,
                        steps=prefix_reports,
                        completed=False,
                        halted_reason=halted_reason,
                    )

                print(
                    "[mint] balancing swap confirmed; rebuilding the mint from "
                    "fresh pool and Safe inventory",
                    file=sys.stderr,
                    flush=True,
                )

                # Pool price, Safe balances, allowances and Safe nonce are all
                # re-read here. This is the execution boundary missing from the
                # old single-build sequence.
                try:
                    fresh_build, fresh_steps = self._build_mint_attempt(
                        symbol,
                        budget_usdc,
                        width_spacings,
                        key_bytes,
                        ephemeral_key,
                        ExecutionMode.EXECUTE,
                        inventory_only=True,
                    )
                except (LpExecutionRefusalError, LpPlanRefusalError) as error:
                    previous = tuple(getattr(error, "completed_steps", ()))
                    error.completed_steps = prefix_reports + previous
                    raise

                # One bounded acquisition is allowed per attempt. If the fresh
                # state still genuinely needs another balancing swap, stop and
                # let the next cycle reconcile rather than churn the market.
                if fresh_build.plan.balancing_swap.required:
                    error = LpExecutionRefusalError(
                        LpExecutionRefusalCode.POST_SWAP_REBALANCE_REQUIRED,
                        "the confirmed balancing swap still leaves a fresh "
                        "balancing-swap requirement; refusing a second market "
                        "swap in the same mint attempt so the next cycle can "
                        "reconcile the acquired inventory from live state",
                    )
                    error.completed_steps = prefix_reports
                    raise error

                try:
                    tail_reports, halted_reason = self._execute_steps(
                        "mint", fresh_steps, key_bytes
                    )
                except (LpExecutionRefusalError, LpPlanRefusalError) as error:
                    previous = tuple(getattr(error, "completed_steps", ()))
                    error.completed_steps = prefix_reports + previous
                    raise

                # Keep the fresh plan as the authoritative mint plan, while the
                # build report retains the actually executed swap-prefix builds.
                build = fresh_build.model_copy(
                    update={
                        "transactions": (
                            tuple(step.report for step in prefix_steps)
                            + fresh_build.transactions
                        ),
                        "build_duration_ms": (
                            build.build_duration_ms
                            + fresh_build.build_duration_ms
                        ),
                    }
                )
                step_reports = prefix_reports + tail_reports

        except (LpExecutionRefusalError, LpPlanRefusalError) as error:
            self._record_refusal("mint", ExecutionMode.EXECUTE, error, symbol)
            raise

        return LpActionExecutionReport(
            action="mint",
            build=build,
            steps=step_reports,
            completed=halted_reason == "",
            halted_reason=halted_reason,
        )

    def execute_stake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Build and broadcast one stake sequence step by step.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The position NFT being staked.
            key_bytes: Exactly 32 raw signing-key bytes used for this attempt.
            confirm_broadcast: The explicit operator confirmation.
            ephemeral_key: Whether the key was generated for this attempt.

        Returns:
            The complete execution report with every broadcast step.

        Raises:
            LpExecutionRefusalError: If any gate, preflight, or per-step check
                refuses.
        """
        if not confirm_broadcast:
            error = LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "the execute command refuses to broadcast without the explicit "
                "--confirm-broadcast flag; rerun with it to broadcast the built "
                "sequence",
            )
            self._record_refusal("stake", ExecutionMode.EXECUTE, error, symbol)
            raise error
        try:
            build, steps = self._build_stake_attempt(
                symbol, token_id, key_bytes, ephemeral_key, ExecutionMode.EXECUTE
            )
            step_reports, halted_reason = self._execute_steps("stake", steps, key_bytes)
        except LpExecutionRefusalError as error:
            self._record_refusal("stake", ExecutionMode.EXECUTE, error, symbol)
            raise
        return LpActionExecutionReport(
            action="stake",
            build=build,
            steps=step_reports,
            completed=halted_reason == "",
            halted_reason=halted_reason,
        )

    def execute_unstake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Build and broadcast one unstake sequence step by step.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The staked position NFT being unstaked.
            key_bytes: Exactly 32 raw signing-key bytes used for this attempt.
            confirm_broadcast: The explicit operator confirmation.
            ephemeral_key: Whether the key was generated for this attempt.

        Returns:
            The complete execution report with every broadcast step.

        Raises:
            LpExecutionRefusalError: If any gate, preflight, or per-step check
                refuses.
        """
        if not confirm_broadcast:
            error = LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "the execute command refuses to broadcast without the explicit "
                "--confirm-broadcast flag; rerun with it to broadcast the built "
                "sequence",
            )
            self._record_refusal("unstake", ExecutionMode.EXECUTE, error, symbol)
            raise error
        try:
            build, steps = self._build_unstake_attempt(
                symbol, token_id, key_bytes, ephemeral_key, ExecutionMode.EXECUTE
            )
            step_reports, halted_reason = self._execute_steps("unstake", steps, key_bytes)
        except LpExecutionRefusalError as error:
            self._record_refusal("unstake", ExecutionMode.EXECUTE, error, symbol)
            raise
        return LpActionExecutionReport(
            action="unstake",
            build=build,
            steps=step_reports,
            completed=halted_reason == "",
            halted_reason=halted_reason,
        )

    def execute_withdraw(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Build and broadcast one withdraw sequence step by step.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The unstaked position NFT being exited.
            key_bytes: Exactly 32 raw signing-key bytes used for this attempt.
            confirm_broadcast: The explicit operator confirmation.
            ephemeral_key: Whether the key was generated for this attempt.

        Returns:
            The complete execution report with every broadcast step.

        Raises:
            LpExecutionRefusalError: If any gate, preflight, or per-step check
                refuses.
        """
        if not confirm_broadcast:
            error = LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "the execute command refuses to broadcast without the explicit "
                "--confirm-broadcast flag; rerun with it to broadcast the built "
                "sequence",
            )
            self._record_refusal("withdraw", ExecutionMode.EXECUTE, error, symbol)
            raise error
        try:
            build, steps = self._build_exit_attempt(
                symbol, token_id, key_bytes, ephemeral_key, ExecutionMode.EXECUTE
            )
            step_reports, halted_reason = self._execute_steps("withdraw", steps, key_bytes)
        except LpExecutionRefusalError as error:
            self._record_refusal("withdraw", ExecutionMode.EXECUTE, error, symbol)
            raise
        return LpActionExecutionReport(
            action="withdraw",
            build=build,
            steps=step_reports,
            completed=halted_reason == "",
            halted_reason=halted_reason,
        )

    def execute_collect(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Build and broadcast one collect sequence step by step.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The position NFT being collected.
            key_bytes: Exactly 32 raw signing-key bytes used for this attempt.
            confirm_broadcast: The explicit operator confirmation.
            ephemeral_key: Whether the key was generated for this attempt.

        Returns:
            The complete execution report with every broadcast step.

        Raises:
            LpExecutionRefusalError: If any gate, preflight, or per-step check
                refuses.
        """
        if not confirm_broadcast:
            error = LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "the execute command refuses to broadcast without the explicit "
                "--confirm-broadcast flag; rerun with it to broadcast the built "
                "sequence",
            )
            self._record_refusal("collect", ExecutionMode.EXECUTE, error, symbol)
            raise error
        try:
            build, steps = self._build_collect_attempt(
                symbol, token_id, key_bytes, ephemeral_key, ExecutionMode.EXECUTE
            )
            step_reports, halted_reason = self._execute_steps("collect", steps, key_bytes)
        except LpExecutionRefusalError as error:
            self._record_refusal("collect", ExecutionMode.EXECUTE, error, symbol)
            raise
        return LpActionExecutionReport(
            action="collect",
            build=build,
            steps=step_reports,
            completed=halted_reason == "",
            halted_reason=halted_reason,
        )

    def dry_run_exit_swap(
        self,
        symbol: str,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> LpExitSwapDryRunReport:
        """Fully build and validate one exit swap without broadcasting.

        Args:
            symbol: The registry-matched B20 stock symbol.
            key_bytes: Exactly 32 raw signing-key bytes used for this build.
            ephemeral_key: Whether the key was generated for this dry run.

        Returns:
            The complete dry-run report; nothing was broadcast.

        Raises:
            LpExecutionRefusalError: If any execution-layer gate refuses.
        """
        try:
            report, _ = self._build_exit_swap_attempt(symbol, key_bytes, ephemeral_key)
            return report
        except LpExecutionRefusalError as error:
            self._record_refusal("exit_swap", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def execute_exit_swap(
        self,
        symbol: str,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Build and broadcast one exit-swap sequence step by step.

        Args:
            symbol: The registry-matched B20 stock symbol.
            key_bytes: Exactly 32 raw signing-key bytes used for this attempt.
            confirm_broadcast: The explicit operator confirmation.
            ephemeral_key: Whether the key was generated for this attempt.

        Returns:
            The complete execution report with every broadcast step.

        Raises:
            LpExecutionRefusalError: If any gate, preflight, or per-step check
                refuses.
        """
        if not confirm_broadcast:
            error = LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "the execute command refuses to broadcast without the explicit "
                "--confirm-broadcast flag; rerun with it to broadcast the built "
                "sequence",
            )
            self._record_refusal("exit_swap", ExecutionMode.EXECUTE, error, symbol)
            raise error
        try:
            build, steps = self._build_exit_swap_attempt(
                symbol, key_bytes, ephemeral_key, ExecutionMode.EXECUTE
            )
            step_reports, halted_reason = self._execute_steps("exit_swap", steps, key_bytes)
        except LpExecutionRefusalError as error:
            self._record_refusal("exit_swap", ExecutionMode.EXECUTE, error, symbol)
            raise
        return LpActionExecutionReport(
            action="exit_swap",
            build=build,
            steps=step_reports,
            completed=halted_reason == "",
            halted_reason=halted_reason,
        )

    def position_status(
        self,
        symbol: str,
        token_id: int,
        aero_price_usdc: Decimal | None = None,
        entry_cost_usdc: Decimal | None = None,
    ) -> LpPositionStatusReport:
        """Observe one position read-only; nothing is built or signed.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The position NFT being reported on.
            aero_price_usdc: Optional AERO price assumption in USDC; absent
                means the price is read live from Aerodrome's own USDC/AERO
                pool at the snapshot block.
            entry_cost_usdc: The optional entry cost basis in USDC for the
                unrealized P&L; absent means unknown.

        Returns:
            The complete read-only position report.

        Raises:
            LpExecutionRefusalError: If any registry, discovery, snapshot,
                position-resolution, or live AERO-price gate refuses.
        """
        try:
            return self._position_status(symbol, token_id, aero_price_usdc, entry_cost_usdc)
        except LpExecutionRefusalError as error:
            self._record_refusal("status", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def _build_mint_attempt(
        self,
        symbol: str,
        budget_usdc: Decimal,
        width_spacings: int | None,
        key_bytes: bytes,
        ephemeral_key: bool,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
        *,
        inventory_only: bool = False,
    ) -> tuple[LpMintDryRunReport, tuple[_BuiltLpStep, ...]]:
        """Build, sign, validate, and estimate the complete mint sequence."""
        build_started = self._timer()
        context = self._resolve_mint_context(symbol, width_spacings)
        plan = self._plan_from_context(context, budget_usdc)
        if inventory_only and plan.balancing_swap.required:
            observation = context.observation
            stock_desired = (
                plan.amounts.amount0_desired_units
                if observation.stock_is_token0
                else plan.amounts.amount1_desired_units
            )
            usdc_desired = (
                plan.amounts.amount1_desired_units
                if observation.stock_is_token0
                else plan.amounts.amount0_desired_units
            )
            stock_ratio = Decimal(context.inventory.stock_units) / Decimal(stock_desired)
            usdc_ratio = Decimal(context.inventory.usdc_units) / Decimal(usdc_desired)
            scale = min(Decimal(1), stock_ratio, usdc_ratio) * Decimal("0.999")
            constrained_budget = budget_usdc * scale
            plan = self._plan_from_context(context, constrained_budget)
            if plan.balancing_swap.required:
                raise LpExecutionRefusalError(
                    LpExecutionRefusalCode.POST_SWAP_REBALANCE_REQUIRED,
                    "fresh post-swap inventory cannot fund a two-sided mint without another "
                    "market swap even after inventory-constrained resizing",
                )
        self._record_mint_plan(mode, plan)
        if plan.balancing_swap.required and plan.balancing_swap.tranche_count > 1:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.MULTI_TRANCHE_SWAP_UNSUPPORTED,
                f"the balancing swap plans {plan.balancing_swap.tranche_count} tranches and "
                "this execution surface runs only a single tranche; lower the budget or wait "
                "for calmer conditions so the modeled impact stays under the tranche "
                "threshold",
            )
        caps = list(context.caps) + list(plan.caps_enforced)
        gas_price, safe_eth, live_nonce = self._preflight(caps)
        observation = context.observation
        stock_token = (
            observation.token0_address
            if observation.stock_is_token0
            else observation.token1_address
        )
        router_allowance = self._rpc.fetch_erc20_allowance(
            BASE_USDC_ADDRESS, self._safe_address, self._policy.router_address
        )
        router_stock_allowance = self._rpc.fetch_erc20_allowance(
            stock_token, self._safe_address, self._policy.router_address
        )
        nfpm_usdc_allowance = self._rpc.fetch_erc20_allowance(
            BASE_USDC_ADDRESS, self._safe_address, observation.nfpm_address
        )
        nfpm_stock_allowance = self._rpc.fetch_erc20_allowance(
            stock_token, self._safe_address, observation.nfpm_address
        )
        deadline = int(self._now().timestamp()) + LP_DEADLINE_SECONDS
        steps = self._compose_mint_steps(
            context,
            plan,
            router_allowance,
            router_stock_allowance,
            nfpm_usdc_allowance,
            nfpm_stock_allowance,
            deadline,
        )
        built_steps = self._build_steps(steps, live_nonce, key_bytes, "mint", mode)
        report = LpMintDryRunReport(
            plan=plan,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            router_usdc_allowance_units=router_allowance,
            nfpm_usdc_allowance_units=nfpm_usdc_allowance,
            nfpm_stock_allowance_units=nfpm_stock_allowance,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=tuple(step.report for step in built_steps),
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
        )
        return report, built_steps

    def _compose_mint_steps(
        self,
        context: _LpMintContext,
        plan: LpMintPlan,
        router_allowance_units: int,
        router_stock_allowance_units: int,
        nfpm_usdc_allowance_units: int,
        nfpm_stock_allowance_units: int,
        deadline: int,
    ) -> list[_LpStepSpec]:
        """Compose the mint sequence's inner calls in execution order.

        Args:
            context: The resolved observation and inventory context.
            plan: The capped mint plan being composed.
            router_allowance_units: The live USDC allowance to the router.
            router_stock_allowance_units: The live stock allowance to the router.
            nfpm_usdc_allowance_units: The live USDC allowance to the NFPM.
            nfpm_stock_allowance_units: The live stock allowance to the NFPM.
            deadline: The unix deadline every swap and mint carries.

        Returns:
            The composed steps in execution order; skipped approvals are
            omitted entirely when the live allowance already suffices.
        """
        observation = context.observation
        stock_token = (
            observation.token0_address
            if observation.stock_is_token0
            else observation.token1_address
        )
        usdc_desired = (
            plan.amounts.amount1_desired_units
            if observation.stock_is_token0
            else plan.amounts.amount0_desired_units
        )
        stock_desired = (
            plan.amounts.amount0_desired_units
            if observation.stock_is_token0
            else plan.amounts.amount1_desired_units
        )
        steps: list[_LpStepSpec] = []
        if plan.balancing_swap.required:
            swap = plan.balancing_swap
            tolerance = plan.amounts.slippage_tolerance_fraction
            if swap.direction is BalancingSwapDirection.USDC_TO_STOCK:
                amount_out_min = int(
                    (
                        Decimal(swap.expected_stock_units) * (Decimal(1) - tolerance)
                    ).to_integral_value(rounding=ROUND_FLOOR)
                )
                if router_allowance_units < swap.usdc_in_units:
                    steps.append(
                        _LpStepSpec(
                            role=LpExecutionRole.ROUTER_ALLOWANCE,
                            to_address=BASE_USDC_ADDRESS,
                            inner_calldata=build_approval_calldata(
                                self._policy.router_address,
                                self._policy.router_allowance_standing_cap_units,
                            ),
                            description=(
                                f"set the {self._policy.router_allowance_standing_cap_usdc} USDC "
                                "bounded standing router allowance"
                            ),
                        )
                    )
                steps.append(
                    _LpStepSpec(
                        role=LpExecutionRole.BALANCING_SWAP,
                        to_address=self._policy.router_address,
                        inner_calldata=build_swap_calldata(
                            self._safe_address,
                            swap.usdc_in_units,
                            amount_out_min,
                            build_swap_path(
                                BASE_USDC_ADDRESS, stock_token, observation.tick_spacing
                            ),
                            deadline,
                        ),
                        description=(
                            f"swap {swap.usdc_in_units} raw USDC for at least "
                            f"{amount_out_min} raw {context.listing.symbol} covering the "
                            f"{swap.stock_shortfall_units}-unit stock shortfall"
                        ),
                    )
                )
            elif swap.direction is BalancingSwapDirection.STOCK_TO_USDC:
                amount_out_min = int(
                    (
                        Decimal(swap.expected_usdc_units) * (Decimal(1) - tolerance)
                    ).to_integral_value(rounding=ROUND_FLOOR)
                )
                if router_stock_allowance_units < swap.stock_in_units:
                    steps.append(
                        _LpStepSpec(
                            role=LpExecutionRole.STOCK_ROUTER_ALLOWANCE,
                            to_address=stock_token,
                            inner_calldata=build_approval_calldata(
                                self._policy.router_address, swap.stock_in_units
                            ),
                            description=(
                                f"approve exactly {swap.stock_in_units} raw "
                                f"{context.listing.symbol} to the whitelisted router for "
                                "the quote-side rebalance"
                            ),
                        )
                    )
                steps.append(
                    _LpStepSpec(
                        role=LpExecutionRole.BALANCING_SWAP,
                        to_address=self._policy.router_address,
                        inner_calldata=build_swap_calldata(
                            self._safe_address,
                            swap.stock_in_units,
                            amount_out_min,
                            build_swap_path(
                                stock_token, BASE_USDC_ADDRESS, observation.tick_spacing
                            ),
                            deadline,
                        ),
                        description=(
                            f"swap {swap.stock_in_units} raw {context.listing.symbol} for at "
                            f"least {amount_out_min} raw USDC covering the "
                            f"{swap.usdc_shortfall_units}-unit quote-side shortfall"
                        ),
                    )
                )
            else:
                raise ValueError("required balancing swap has no executable direction")
        if nfpm_usdc_allowance_units < usdc_desired:
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_USDC_ALLOWANCE,
                    to_address=BASE_USDC_ADDRESS,
                    inner_calldata=build_approval_calldata(observation.nfpm_address, usdc_desired),
                    description=(
                        f"approve exactly {usdc_desired} raw USDC to the NFPM for the mint pull"
                    ),
                )
            )
        if nfpm_stock_allowance_units < stock_desired:
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_STOCK_ALLOWANCE,
                    to_address=stock_token,
                    inner_calldata=build_approval_calldata(observation.nfpm_address, stock_desired),
                    description=(
                        f"approve exactly {stock_desired} raw {context.listing.symbol} to the "
                        "NFPM for the mint pull"
                    ),
                )
            )
        steps.append(
            _LpStepSpec(
                role=LpExecutionRole.MINT,
                to_address=observation.nfpm_address,
                inner_calldata=build_lp_mint_calldata(
                    LpMintParams(
                        token0_address=observation.token0_address,
                        token1_address=observation.token1_address,
                        tick_spacing=observation.tick_spacing,
                        tick_lower=plan.position_range.tick_lower,
                        tick_upper=plan.position_range.tick_upper,
                        amount0_desired_units=plan.amounts.amount0_desired_units,
                        amount1_desired_units=plan.amounts.amount1_desired_units,
                        amount0_min_units=plan.amounts.amount0_min_units,
                        amount1_min_units=plan.amounts.amount1_min_units,
                        recipient_address=self._safe_address,
                        deadline=deadline,
                        sqrt_price_x96=0,
                    )
                ),
                description=(
                    f"mint range [{plan.position_range.tick_lower}, "
                    f"{plan.position_range.tick_upper}) with "
                    f"{plan.amounts.amount0_desired_units} + "
                    f"{plan.amounts.amount1_desired_units} raw units"
                ),
            )
        )
        return steps

    def _build_stake_attempt(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
    ) -> tuple[LpStakeDryRunReport, tuple[_BuiltLpStep, ...]]:
        """Build, sign, validate, and estimate the complete stake sequence."""
        build_started = self._timer()
        listing, observation, caps = self._observe_pool(symbol)
        owner: str | None = None
        ownership_diagnostic = ""
        try:
            owner = normalize_evm_address(
                self._read_address(
                    observation.nfpm_address,
                    self._erc721_owner_of_calldata(token_id),
                    "ownerOf()",
                )
            )
            if owner != self._safe_address:
                ownership_diagnostic = (
                    f"ownerOf reports {owner}, not this Safe; an execute attempt will refuse"
                )
        except ExecutorRpcRevertError as error:
            ownership_diagnostic = (
                f"ownerOf reverted, so the token is not minted yet or the id is unknown "
                f"({error}); a pre-mint dry run proves machinery only, and an execute attempt "
                "will refuse until the mint confirms"
            )
        position: LpPositionView | None = None
        position_diagnostic = ""
        try:
            position = decode_lp_positions_view(
                self._rpc.eth_call(
                    observation.nfpm_address, build_lp_positions_read_calldata(token_id)
                )
            )
        except (ExecutorRpcRevertError, ValueError) as error:
            position_diagnostic = f"positions view unavailable: {error}"
        operator_approved = (
            self._read_word(
                observation.nfpm_address,
                self._erc721_is_approved_for_all_calldata(
                    self._safe_address, observation.gauge_address
                ),
                "isApprovedForAll()",
            )
            == 1
        )
        gas_price, safe_eth, live_nonce = self._preflight(caps)
        steps: list[_LpStepSpec] = []
        if not operator_approved:
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_GAUGE_APPROVAL,
                    to_address=observation.nfpm_address,
                    inner_calldata=build_set_approval_for_all_calldata(
                        observation.gauge_address, True
                    ),
                    description=(
                        f"approve gauge {observation.gauge_address} as NFPM operator so deposit "
                        "can pull the NFT"
                    ),
                )
            )
        steps.append(
            _LpStepSpec(
                role=LpExecutionRole.GAUGE_DEPOSIT,
                to_address=observation.gauge_address,
                inner_calldata=build_gauge_deposit_calldata(token_id),
                description=f"stake token {token_id} into the gauge",
            )
        )
        self._record_stake_plan(
            mode,
            listing.symbol,
            observation,
            token_id,
            owner,
            operator_approved,
        )
        built_steps = self._build_steps(steps, live_nonce, key_bytes, "stake", mode)
        report = LpStakeDryRunReport(
            symbol=listing.symbol,
            pool_address=observation.pool_address,
            nfpm_address=observation.nfpm_address,
            gauge_address=observation.gauge_address,
            token_id=token_id,
            token_owner_address=owner,
            ownership_diagnostic=ownership_diagnostic,
            position=position,
            position_diagnostic=position_diagnostic,
            gauge_operator_approved=operator_approved,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=tuple(step.report for step in built_steps),
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
        )
        return report, built_steps

    def _build_unstake_attempt(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
    ) -> tuple[LpUnstakeDryRunReport, tuple[_BuiltLpStep, ...]]:
        """Build, sign, validate, and estimate the complete unstake sequence."""
        build_started = self._timer()
        context = self._resolve_position(symbol, token_id)
        if not context.staked:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_NOT_STAKED,
                f"token {token_id} is held by {context.owner}, not the pool's gauge, so "
                "nothing is staked to unstake; verify the token id and pool symbol",
            )
        observation = context.observation
        accrued_earned = self._read_gauge_reward_word(
            observation.gauge_address,
            build_gauge_earned_read_calldata(self._safe_address, token_id),
            "earned(address,uint256)",
        )
        accrued_checkpoint = self._read_gauge_reward_word(
            observation.gauge_address,
            build_gauge_rewards_read_calldata(token_id),
            "rewards(uint256)",
        )
        penalty = self._read_penalty_window(observation, token_id)
        self._require_penalty_clear(penalty, accrued_earned)
        caps = list(context.caps)
        caps.append(
            f"penalty window clear with {penalty.remaining_seconds}s margin at "
            f"{penalty.penalty_rate_bps} bps"
        )
        gas_price, safe_eth, live_nonce = self._preflight(caps)
        steps = [
            _LpStepSpec(
                role=LpExecutionRole.GAUGE_WITHDRAW,
                to_address=observation.gauge_address,
                inner_calldata=build_gauge_withdraw_calldata(token_id),
                description=(
                    f"unstake token {token_id}; the withdraw also sweeps pending fees and "
                    "auto-claims the accrued emissions"
                ),
            )
        ]
        self._record_unstake_plan(
            mode,
            context.listing.symbol,
            observation,
            token_id,
            accrued_earned,
            accrued_checkpoint,
            penalty,
        )
        built_steps = self._build_steps(steps, live_nonce, key_bytes, "unstake", mode)
        report = LpUnstakeDryRunReport(
            symbol=context.listing.symbol,
            pool_address=observation.pool_address,
            nfpm_address=observation.nfpm_address,
            gauge_address=observation.gauge_address,
            token_id=token_id,
            position=context.position,
            accrued_aero_earned_units=accrued_earned,
            accrued_aero_checkpoint_units=accrued_checkpoint,
            penalty=penalty,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=tuple(step.report for step in built_steps),
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
            diagnostics=(
                (
                    f"earned reports {accrued_earned} raw AERO live and {accrued_checkpoint} "
                    "raw AERO checkpointed; the withdraw claims the live amount"
                ),
                (
                    f"the penalty window clears at "
                    f"{penalty.window_clears_at_timestamp} "
                    f"({penalty.remaining_seconds}s remaining at "
                    f"{penalty.penalty_rate_bps} bps)"
                ),
            ),
        )
        return report, built_steps

    def _build_exit_attempt(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
    ) -> tuple[LpExitDryRunReport, tuple[_BuiltLpStep, ...]]:
        """Build, sign, validate, and estimate the complete withdraw sequence."""
        build_started = self._timer()
        context = self._resolve_position(symbol, token_id)
        if context.staked:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_STAKED,
                f"token {token_id} is staked in the gauge, which holds the NFT and blocks "
                "every NFPM-side operation; unstake first, then withdraw",
            )
        position = context.position
        observation = context.observation
        amount0, amount1 = self._exit_amounts(observation, position)
        tolerance = DEFAULT_MINT_SLIPPAGE_TOLERANCE
        amount0_min = int((amount0 * (Decimal(1) - tolerance)).to_integral_value(ROUND_FLOOR))
        amount1_min = int((amount1 * (Decimal(1) - tolerance)).to_integral_value(ROUND_FLOOR))
        if position.liquidity == 0 and (
            position.tokens_owed0_units == 0 and position.tokens_owed1_units == 0
        ):
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_EMPTY,
                f"token {token_id} holds no liquidity and no checkpointed fees, so there is "
                "nothing to withdraw; recenter to burn and remint it, or burn it directly "
                "once an execute path exists",
            )
        range_state = position_range_state(
            position.tick_lower, position.tick_upper, observation.current_tick
        )
        caps = list(context.caps)
        caps.append(f"exit minima floored at the {tolerance} slippage tolerance")
        gas_price, safe_eth, live_nonce = self._preflight(caps)
        deadline = int(self._now().timestamp()) + LP_DEADLINE_SECONDS
        steps: list[_LpStepSpec] = []
        if position.liquidity > 0:
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_DECREASE,
                    to_address=observation.nfpm_address,
                    inner_calldata=build_lp_decrease_liquidity_calldata(
                        LpDecreaseLiquidityParams(
                            token_id=token_id,
                            liquidity=position.liquidity,
                            amount0_min_units=amount0_min,
                            amount1_min_units=amount1_min,
                            deadline=deadline,
                        )
                    ),
                    description=(
                        f"decrease token {token_id} by its full {position.liquidity} liquidity "
                        f"for at least {amount0_min} + {amount1_min} raw units"
                    ),
                )
            )
        steps.append(
            _LpStepSpec(
                role=LpExecutionRole.NFPM_COLLECT,
                to_address=observation.nfpm_address,
                inner_calldata=build_lp_collect_calldata(
                    LpCollectParams(
                        token_id=token_id,
                        recipient_address=self._safe_address,
                        amount0_max_units=MAX_UINT128,
                        amount1_max_units=MAX_UINT128,
                    )
                ),
                description=(
                    f"collect every fee and leftover on token {token_id} to the Safe "
                    f"(checkpointed {position.tokens_owed0_units} + "
                    f"{position.tokens_owed1_units} raw units)"
                ),
            )
        )
        self._record_exit_plan(
            mode,
            context.listing.symbol,
            observation,
            token_id,
            range_state,
            amount0,
            amount1,
            amount0_min,
            amount1_min,
            position.tokens_owed0_units,
            position.tokens_owed1_units,
        )
        built_steps = self._build_steps(steps, live_nonce, key_bytes, "withdraw", mode)
        diagnostics = [
            (
                f"the full decrease returns {amount0} + {amount1} raw units at the snapshot "
                f"price ({range_state.value})"
            ),
            (
                f"the collect sweeps the checkpointed fees "
                f"{position.tokens_owed0_units} + {position.tokens_owed1_units} raw units "
                "plus the decrease's outputs"
            ),
        ]
        if range_state is not PositionRangeState.IN_RANGE:
            side = "token zero" if range_state is PositionRangeState.BELOW_RANGE else "token one"
            diagnostics.append(
                f"the position is entirely {side} because the price has left the range"
            )
        report = LpExitDryRunReport(
            symbol=context.listing.symbol,
            pool_address=observation.pool_address,
            nfpm_address=observation.nfpm_address,
            gauge_address=observation.gauge_address,
            token_id=token_id,
            position=position,
            range_state=range_state,
            amount0_units=amount0,
            amount1_units=amount1,
            amount0_min_units=amount0_min,
            amount1_min_units=amount1_min,
            fees_owed0_units=position.tokens_owed0_units,
            fees_owed1_units=position.tokens_owed1_units,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=tuple(step.report for step in built_steps),
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
            diagnostics=tuple(diagnostics),
        )
        return report, built_steps

    def _build_collect_attempt(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        ephemeral_key: bool,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
    ) -> tuple[LpCollectDryRunReport, tuple[_BuiltLpStep, ...]]:
        """Build, sign, validate, and estimate the complete collect sequence."""
        build_started = self._timer()
        context = self._resolve_position(symbol, token_id)
        position = context.position
        observation = context.observation
        accrued_earned = 0
        accrued_checkpoint = 0
        penalty: LpPenaltyWindow | None = None
        caps = list(context.caps)
        if context.staked:
            accrued_earned = self._read_gauge_reward_word(
                observation.gauge_address,
                build_gauge_earned_read_calldata(self._safe_address, token_id),
                "earned(address,uint256)",
            )
            accrued_checkpoint = self._read_gauge_reward_word(
                observation.gauge_address,
                build_gauge_rewards_read_calldata(token_id),
                "rewards(uint256)",
            )
            penalty = self._read_penalty_window(observation, token_id)
            self._require_penalty_clear(penalty, accrued_earned)
            caps.append(
                f"penalty window clear with {penalty.remaining_seconds}s margin at "
                f"{penalty.penalty_rate_bps} bps"
            )
        gas_price, safe_eth, live_nonce = self._preflight(caps)
        if context.staked:
            steps = [
                _LpStepSpec(
                    role=LpExecutionRole.GAUGE_GET_REWARD,
                    to_address=observation.gauge_address,
                    inner_calldata=build_gauge_get_reward_calldata(token_id),
                    description=(
                        f"claim token {token_id}'s accrued emissions, "
                        f"{accrued_earned} raw AERO by the live earned view"
                    ),
                )
            ]
            diagnostics = [
                (
                    f"earned reports {accrued_earned} raw AERO live and {accrued_checkpoint} "
                    "raw AERO checkpointed; the claim pays the live amount"
                ),
                (
                    "checkpointed position fees do not flow through getReward; they sweep on "
                    "the gauge's next deposit or withdraw"
                ),
            ]
        else:
            steps = [
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_COLLECT,
                    to_address=observation.nfpm_address,
                    inner_calldata=build_lp_collect_calldata(
                        LpCollectParams(
                            token_id=token_id,
                            recipient_address=self._safe_address,
                            amount0_max_units=MAX_UINT128,
                            amount1_max_units=MAX_UINT128,
                        )
                    ),
                    description=(
                        f"collect every fee and leftover on token {token_id} to the Safe "
                        f"(checkpointed {position.tokens_owed0_units} + "
                        f"{position.tokens_owed1_units} raw units)"
                    ),
                )
            ]
            diagnostics = [
                (
                    f"the unstaked collect sweeps the checkpointed fees "
                    f"{position.tokens_owed0_units} + {position.tokens_owed1_units} raw units"
                )
            ]
        self._record_collect_plan(
            mode,
            context.listing.symbol,
            observation,
            token_id,
            context.staked,
            accrued_earned,
            accrued_checkpoint,
            position.tokens_owed0_units,
            position.tokens_owed1_units,
        )
        built_steps = self._build_steps(steps, live_nonce, key_bytes, "collect", mode)
        report = LpCollectDryRunReport(
            symbol=context.listing.symbol,
            pool_address=observation.pool_address,
            nfpm_address=observation.nfpm_address,
            gauge_address=observation.gauge_address,
            token_id=token_id,
            position=position,
            staked=context.staked,
            accrued_aero_earned_units=accrued_earned,
            accrued_aero_checkpoint_units=accrued_checkpoint,
            penalty=penalty,
            fees_owed0_units=position.tokens_owed0_units if not context.staked else 0,
            fees_owed1_units=position.tokens_owed1_units if not context.staked else 0,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=tuple(step.report for step in built_steps),
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
            diagnostics=tuple(diagnostics),
        )
        return report, built_steps

    def _build_exit_swap_attempt(
        self,
        symbol: str,
        key_bytes: bytes,
        ephemeral_key: bool,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
    ) -> tuple[LpExitSwapDryRunReport, tuple[_BuiltLpStep, ...]]:
        """Build, sign, validate, and estimate the complete exit-swap sequence."""
        build_started = self._timer()
        listing, observation, caps = self._observe_pool(symbol)
        stock_token = (
            observation.token0_address
            if observation.stock_is_token0
            else observation.token1_address
        )
        balance_units = self._rpc.fetch_token_balance(stock_token, self._safe_address)
        if balance_units <= 0:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.STOCK_BALANCE_ZERO,
                f"the Safe holds no {listing.symbol} balance to convert back to USDC; "
                "there is nothing to exit-swap",
            )
        allowance_units = self._rpc.fetch_erc20_allowance(
            stock_token, self._safe_address, self._policy.router_address
        )
        price = observation.price_usdc_per_stock
        with localcontext() as context:
            context.prec = 50
            expected_out_units = int(
                (
                    Decimal(balance_units).scaleb(-observation.stock_decimals)
                    * price
                    * Decimal(10) ** observation.quote_decimals
                ).to_integral_value(ROUND_FLOOR)
            )
        tolerance = DEFAULT_MINT_SLIPPAGE_TOLERANCE
        amount_out_min_units = int(
            (Decimal(expected_out_units) * (Decimal(1) - tolerance)).to_integral_value(ROUND_FLOOR)
        )
        per_pool_cap_units = usdc_units(MAX_POSITION_USDC_PER_POOL)
        if expected_out_units > per_pool_cap_units:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.EXIT_OUTPUT_ABOVE_POOL_CAP,
                f"the exit swap's quoted output {expected_out_units} raw USDC exceeds the "
                f"{MAX_POSITION_USDC_PER_POOL} USDC per-pool pilot cap ({per_pool_cap_units} "
                "raw units); an inventory this large is out of band for a capped pilot, so "
                "the swap refuses rather than moving it",
            )
        if amount_out_min_units <= 0:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.STOCK_BALANCE_ZERO,
                f"the Safe's {balance_units} raw {listing.symbol} balance quotes to a "
                "zero USDC minimum at the snapshot price; the dust is not worth a swap",
            )
        caps.append(
            f"exit output at or below the {MAX_POSITION_USDC_PER_POOL} USDC per-pool pilot cap"
        )
        caps.append(f"exit minimum floored at the {tolerance} slippage tolerance")
        gas_price, safe_eth, live_nonce = self._preflight(caps)
        deadline = int(self._now().timestamp()) + LP_DEADLINE_SECONDS
        steps: list[_LpStepSpec] = []
        if allowance_units < balance_units:
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.STOCK_ROUTER_ALLOWANCE,
                    to_address=stock_token,
                    inner_calldata=build_approval_calldata(
                        self._policy.router_address, balance_units
                    ),
                    description=(
                        f"approve exactly {balance_units} raw {listing.symbol} to the "
                        "whitelisted router for the exit pull"
                    ),
                )
            )
        steps.append(
            _LpStepSpec(
                role=LpExecutionRole.EXIT_SWAP,
                to_address=self._policy.router_address,
                inner_calldata=build_swap_calldata(
                    self._safe_address,
                    balance_units,
                    amount_out_min_units,
                    build_swap_path(stock_token, BASE_USDC_ADDRESS, observation.tick_spacing),
                    deadline,
                ),
                description=(
                    f"swap the entire {balance_units} raw {listing.symbol} balance for at "
                    f"least {amount_out_min_units} raw USDC (quoted {expected_out_units})"
                ),
            )
        )
        self._record_exit_swap_plan(
            mode,
            listing.symbol,
            observation,
            balance_units,
            price,
            expected_out_units,
            amount_out_min_units,
            allowance_units,
        )
        built_steps = self._build_steps(steps, live_nonce, key_bytes, "exit_swap", mode)
        report = LpExitSwapDryRunReport(
            symbol=listing.symbol,
            pool_address=observation.pool_address,
            gauge_address=observation.gauge_address,
            router_address=self._policy.router_address,
            stock_token_address=stock_token,
            snapshot_block=observation.snapshot_block,
            price_usdc_per_stock=price,
            stock_balance_units=balance_units,
            amount_in_units=balance_units,
            expected_out_units=expected_out_units,
            amount_out_min_units=amount_out_min_units,
            router_stock_allowance_units=allowance_units,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=tuple(step.report for step in built_steps),
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
            diagnostics=(
                (
                    f"selling the Safe's entire {balance_units} raw {listing.symbol} "
                    f"balance at the snapshot price {price} USDC per stock"
                ),
                (
                    f"quoted {expected_out_units} raw USDC with a {amount_out_min_units} "
                    f"raw minimum at the {tolerance} tolerance"
                ),
            ),
        )
        return report, built_steps

    def _dry_run_recenter(
        self,
        symbol: str,
        token_id: int,
        width_spacings: int | None,
        budget_usdc: Decimal | None,
        key_bytes: bytes,
        ephemeral_key: bool,
    ) -> LpRecenterDryRunReport:
        """Build, sign, validate, and estimate the complete recenter batch."""
        build_started = self._timer()
        context = self._resolve_position(symbol, token_id)
        if width_spacings is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.DERIVED_WIDTH_UNAVAILABLE,
                "no --width-ticks override was supplied and the solver-derived width path "
                "needs a reconstructed price path this manual surface does not yet carry "
                "(the corrected emissions-APR convention is live); pass an explicit half "
                "width in tick spacings per side",
            )
        position = context.position
        observation = context.observation
        held = self._enumerate_held_positions(observation)
        live_others = [
            held_position.token_id
            for held_position in held
            if held_position.live and held_position.token_id != token_id
        ]
        if live_others:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.UNTRACKED_EXISTING_POSITIONS,
                f"the Safe holds {len(live_others)} live untracked position NFT(s) "
                f"(token ids {', '.join(str(token) for token in live_others)}) on this NFPM "
                "beyond the one being recentered, so the total pilot exposure cap cannot "
                "be evaluated honestly; refuse until they are reconciled or exited",
            )
        accrued_earned = 0
        if context.staked:
            accrued_earned = self._read_gauge_reward_word(
                observation.gauge_address,
                build_gauge_earned_read_calldata(self._safe_address, token_id),
                "earned(address,uint256)",
            )
            penalty = self._read_penalty_window(observation, token_id)
            self._require_penalty_clear(penalty, accrued_earned)
        amount0, amount1 = self._exit_amounts(observation, position)
        range_state = position_range_state(
            position.tick_lower, position.tick_upper, observation.current_tick
        )
        projected_usdc, projected_stock = self._projected_inventory(
            observation, position, amount0, amount1
        )
        price = observation.price_usdc_per_stock
        # The recycled budget values the decrease outputs plus the checkpointed
        # fees, on each side of the pair, at the snapshot price.
        stock_units_out = (amount0 if observation.stock_is_token0 else amount1) + Decimal(
            position.tokens_owed0_units
            if observation.stock_is_token0
            else position.tokens_owed1_units
        )
        usdc_units_out = (amount1 if observation.stock_is_token0 else amount0) + Decimal(
            position.tokens_owed1_units
            if observation.stock_is_token0
            else position.tokens_owed0_units
        )
        recycled_budget = (
            budget_usdc
            if budget_usdc is not None
            else +(
                stock_units_out * Decimal(10) ** -observation.stock_decimals * price
                + usdc_units_out * Decimal(10) ** -observation.quote_decimals
            )
        )
        inventory = SafeInventory(
            usdc_units=projected_usdc,
            stock_units=projected_stock,
        )
        directive = MintDirective(
            budget_usdc=recycled_budget,
            half_width_spacings=width_spacings,
            width_source=WidthSource.EXPLICIT_OVERRIDE,
        )
        plan = plan_mint_entry(self._plan_policy, observation, directive, inventory)
        self._record_mint_plan(ExecutionMode.DRY_RUN, plan)
        if plan.balancing_swap.required and plan.balancing_swap.tranche_count > 1:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.MULTI_TRANCHE_SWAP_UNSUPPORTED,
                f"the recenter's balancing swap plans {plan.balancing_swap.tranche_count} "
                "tranches and this execution surface runs only a single tranche; lower the "
                "budget or wait for calmer conditions so the modeled impact stays under the "
                "tranche threshold",
            )
        caps = list(context.caps)
        caps.append(
            "recenter recycles only the exited position's inventory; any other held NFT refuses"
        )
        caps.extend(plan.caps_enforced)
        gas_price, safe_eth, live_nonce = self._preflight(caps)
        stock_token = (
            observation.token0_address
            if observation.stock_is_token0
            else observation.token1_address
        )
        router_allowance = self._rpc.fetch_erc20_allowance(
            BASE_USDC_ADDRESS, self._safe_address, self._policy.router_address
        )
        router_stock_allowance = self._rpc.fetch_erc20_allowance(
            stock_token, self._safe_address, self._policy.router_address
        )
        nfpm_usdc_allowance = self._rpc.fetch_erc20_allowance(
            BASE_USDC_ADDRESS, self._safe_address, observation.nfpm_address
        )
        nfpm_stock_allowance = self._rpc.fetch_erc20_allowance(
            stock_token, self._safe_address, observation.nfpm_address
        )
        operator_approved = (
            self._read_word(
                observation.nfpm_address,
                self._erc721_is_approved_for_all_calldata(
                    self._safe_address, observation.gauge_address
                ),
                "isApprovedForAll()",
            )
            == 1
        )
        deadline = int(self._now().timestamp()) + LP_DEADLINE_SECONDS
        steps: list[_LpStepSpec] = []
        if context.staked:
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.GAUGE_WITHDRAW,
                    to_address=observation.gauge_address,
                    inner_calldata=build_gauge_withdraw_calldata(token_id),
                    description=(
                        f"unstake token {token_id}; the withdraw sweeps its fees and claims "
                        f"its {accrued_earned} raw AERO of accrued emissions"
                    ),
                )
            )
        if position.liquidity > 0:
            tolerance = DEFAULT_MINT_SLIPPAGE_TOLERANCE
            amount0_min = int((amount0 * (Decimal(1) - tolerance)).to_integral_value(ROUND_FLOOR))
            amount1_min = int((amount1 * (Decimal(1) - tolerance)).to_integral_value(ROUND_FLOOR))
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_DECREASE,
                    to_address=observation.nfpm_address,
                    inner_calldata=build_lp_decrease_liquidity_calldata(
                        LpDecreaseLiquidityParams(
                            token_id=token_id,
                            liquidity=position.liquidity,
                            amount0_min_units=amount0_min,
                            amount1_min_units=amount1_min,
                            deadline=deadline,
                        )
                    ),
                    description=(
                        f"decrease token {token_id} by its full {position.liquidity} liquidity "
                        f"for at least {amount0_min} + {amount1_min} raw units"
                    ),
                )
            )
        if (
            position.liquidity > 0
            or position.tokens_owed0_units > 0
            or position.tokens_owed1_units > 0
        ):
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_COLLECT,
                    to_address=observation.nfpm_address,
                    inner_calldata=build_lp_collect_calldata(
                        LpCollectParams(
                            token_id=token_id,
                            recipient_address=self._safe_address,
                            amount0_max_units=MAX_UINT128,
                            amount1_max_units=MAX_UINT128,
                        )
                    ),
                    description=(
                        f"collect every fee and leftover on token {token_id} to the Safe "
                        f"(checkpointed {position.tokens_owed0_units} + "
                        f"{position.tokens_owed1_units} raw units)"
                    ),
                )
            )
        steps.append(
            _LpStepSpec(
                role=LpExecutionRole.NFPM_BURN,
                to_address=observation.nfpm_address,
                inner_calldata=build_lp_burn_calldata(token_id),
                description=f"burn the emptied position NFT {token_id}",
            )
        )
        mint_context = _LpMintContext(context.listing, observation, inventory, width_spacings, caps)
        steps.extend(
            self._compose_mint_steps(
                mint_context,
                plan,
                router_allowance,
                router_stock_allowance,
                nfpm_usdc_allowance,
                nfpm_stock_allowance,
                deadline,
            )
        )
        if not operator_approved:
            steps.append(
                _LpStepSpec(
                    role=LpExecutionRole.NFPM_GAUGE_APPROVAL,
                    to_address=observation.nfpm_address,
                    inner_calldata=build_set_approval_for_all_calldata(
                        observation.gauge_address, True
                    ),
                    description=(
                        f"approve gauge {observation.gauge_address} as NFPM operator for the "
                        "restake follow-up"
                    ),
                )
            )
        restake_followup = (
            "the restake completes by running the stake command with the fresh mint's "
            "confirmed token id; the NFPM exposes no next-id view (nextTokenId() reverts, "
            "verified live), so the deposit cannot be bound into this pre-execution batch"
        )
        self._record_recenter_plan(
            ExecutionMode.DRY_RUN,
            context.listing.symbol,
            observation,
            token_id,
            context.staked,
            recycled_budget,
            projected_usdc,
            projected_stock,
            plan,
            restake_followup,
        )
        built_steps = self._build_steps(steps, live_nonce, key_bytes, "recenter")
        return LpRecenterDryRunReport(
            symbol=context.listing.symbol,
            pool_address=observation.pool_address,
            nfpm_address=observation.nfpm_address,
            gauge_address=observation.gauge_address,
            token_id=token_id,
            position=position,
            staked=context.staked,
            range_state=range_state,
            amount0_units=amount0,
            amount1_units=amount1,
            fees_owed0_units=position.tokens_owed0_units,
            fees_owed1_units=position.tokens_owed1_units,
            projected_usdc_units=projected_usdc,
            projected_stock_units=projected_stock,
            plan=plan,
            restake_followup=restake_followup,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=tuple(step.report for step in built_steps),
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
        )

    def _position_status(
        self,
        symbol: str,
        token_id: int,
        aero_price_usdc: Decimal | None,
        entry_cost_usdc: Decimal | None,
    ) -> LpPositionStatusReport:
        """Observe one position read-only and quote its emissions APR."""
        context = self._resolve_position(symbol, token_id)
        position = context.position
        observation = context.observation
        if aero_price_usdc is None:
            aero_price_usdc = self._read_live_aero_price(observation)
        amount0, amount1 = self._exit_amounts(observation, position)
        range_state = position_range_state(
            position.tick_lower, position.tick_upper, observation.current_tick
        )
        price = observation.price_usdc_per_stock
        token0_scale = (
            Decimal(10) ** -observation.stock_decimals
            if observation.stock_is_token0
            else Decimal(10) ** -observation.quote_decimals
        )
        token1_scale = (
            Decimal(10) ** -observation.quote_decimals
            if observation.stock_is_token0
            else Decimal(10) ** -observation.stock_decimals
        )
        token0_price = price if observation.stock_is_token0 else Decimal(1)
        token1_price = Decimal(1) if observation.stock_is_token0 else price
        token0_value = +(amount0 * token0_scale * token0_price)
        token1_value = +(amount1 * token1_scale * token1_price)
        position_value = +(token0_value + token1_value)
        accrued_earned: int | None = None
        accrued_checkpoint: int | None = None
        penalty: LpPenaltyWindow | None = None
        diagnostics: list[str] = []
        if context.staked:
            accrued_earned = self._read_gauge_reward_word(
                observation.gauge_address,
                build_gauge_earned_read_calldata(self._safe_address, token_id),
                "earned(address,uint256)",
            )
            accrued_checkpoint = self._read_gauge_reward_word(
                observation.gauge_address,
                build_gauge_rewards_read_calldata(token_id),
                "rewards(uint256)",
            )
            penalty = self._read_penalty_window(observation, token_id)
            diagnostics.append(
                f"staked; earned reports {accrued_earned} raw AERO live and "
                f"{accrued_checkpoint} raw AERO checkpointed"
            )
            if penalty.remaining_seconds > 0 and penalty.penalty_rate_bps > 0:
                diagnostics.append(
                    f"inside the early-exit penalty window for {penalty.remaining_seconds}s "
                    f"more at {penalty.penalty_rate_bps} bps; claiming or unstaking now "
                    "forfeits that share of accrued emissions"
                )
        else:
            diagnostics.append("unstaked; the Safe itself holds the position NFT")
        quoted_apr, apr_diagnostic = self._quote_emissions_apr(observation, aero_price_usdc)
        unrealized_pnl: Decimal | None = None
        pnl_diagnostic = ""
        if entry_cost_usdc is not None:
            unrealized_pnl = +(position_value - entry_cost_usdc)
            pnl_diagnostic = (
                f"position value {position_value} USDC against the supplied entry cost "
                f"{entry_cost_usdc} USDC"
            )
        else:
            pnl_diagnostic = (
                "entry cost unknown: no --entry-cost was supplied and no executed-mint record "
                "links this token id to a cost basis yet"
            )
        diagnostics.append(
            f"composition {amount0} + {amount1} raw units worth {position_value} USDC at the "
            f"snapshot price {price} USDC per stock ({range_state.value})"
        )
        caps = list(context.caps)
        caps.append("read-only observation; nothing was built or signed")
        self._record_status(
            context.listing.symbol,
            observation,
            token_id,
            context.staked,
            position_value,
            quoted_apr,
            aero_price_usdc,
        )
        return LpPositionStatusReport(
            symbol=context.listing.symbol,
            pool_address=observation.pool_address,
            nfpm_address=observation.nfpm_address,
            gauge_address=observation.gauge_address,
            token_id=token_id,
            token_owner_address=context.owner,
            staked=context.staked,
            position=position,
            range_state=range_state,
            current_tick=observation.current_tick,
            amount0_units=amount0,
            amount1_units=amount1,
            token0_value_usdc=token0_value,
            token1_value_usdc=token1_value,
            position_value_usdc=position_value,
            fees_owed0_units=position.tokens_owed0_units,
            fees_owed1_units=position.tokens_owed1_units,
            accrued_aero_earned_units=accrued_earned,
            accrued_aero_checkpoint_units=accrued_checkpoint,
            penalty=penalty,
            quoted_emissions_apr=quoted_apr,
            apr_diagnostic=apr_diagnostic,
            aero_price_assumption_usdc=aero_price_usdc,
            entry_cost_usdc=entry_cost_usdc,
            unrealized_pnl_usdc=unrealized_pnl,
            pnl_diagnostic=pnl_diagnostic,
            snapshot_block=observation.snapshot_block,
            observed_at=observation.observed_at,
            caps_enforced=tuple(caps),
            diagnostics=tuple(diagnostics),
        )

    def _resolve_position(self, symbol: str, token_id: int) -> _LpPositionContext:
        """Resolve one position to its observation, view, and live ownership.

        Args:
            symbol: The registry-matched B20 stock symbol.
            token_id: The position NFT being resolved.

        Returns:
            The resolved position context with every gate label so far.

        Raises:
            LpExecutionRefusalError: If the token does not exist on the
                pool's NFPM or is owned outside the Safe-and-gauge pair.
        """
        listing, observation, caps = self._observe_pool(symbol)
        try:
            position = decode_lp_positions_view(
                self._rpc.eth_call(
                    observation.nfpm_address, build_lp_positions_read_calldata(token_id)
                )
            )
        except (ExecutorRpcRevertError, ValueError) as error:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_UNKNOWN,
                f"token {token_id} has no position on NFPM {observation.nfpm_address} "
                f"({error}); verify the token id and pool symbol",
            ) from error
        try:
            owner = normalize_evm_address(
                self._read_address(
                    observation.nfpm_address,
                    self._erc721_owner_of_calldata(token_id),
                    "ownerOf()",
                )
            )
        except ExecutorRpcRevertError as error:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_UNKNOWN,
                f"token {token_id} reverted ownerOf on NFPM {observation.nfpm_address} "
                f"({error}); the id is unknown or burned",
            ) from error
        if owner != self._safe_address and owner != observation.gauge_address:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_NOT_OWNED,
                f"token {token_id} is owned by {owner}, which is neither this Safe nor the "
                f"pool's gauge {observation.gauge_address}; this executor manages only its "
                "own positions",
            )
        caps.append(
            f"position {token_id} resolved "
            + ("staked in the gauge" if owner == observation.gauge_address else "in the Safe")
        )
        return _LpPositionContext(listing, observation, position, owner, self._safe_address, caps)

    def _read_gauge_reward_word(self, gauge_address: str, calldata: str, source: str) -> int:
        """Read one gauge reward word, refusing fail-closed when it reverts.

        Args:
            gauge_address: The CLGauge being read.
            calldata: Complete 0x-prefixed read payload.
            source: Human label naming the read in diagnostics.

        Returns:
            The decoded unsigned integer.

        Raises:
            LpExecutionRefusalError: If the read reverts, because the
                emissions at stake cannot be established honestly.
        """
        try:
            return self._read_word(gauge_address, calldata, source)
        except ExecutorRpcRevertError as error:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.PENALTY_STATE_UNREADABLE,
                f"the gauge read {source} reverted ({error}); the accrued emissions and "
                "penalty exposure cannot be established, so the attempt is refused rather "
                "than guessed at",
            ) from error

    def _read_penalty_window(
        self, observation: LpPoolObservation, token_id: int
    ) -> LpPenaltyWindow:
        """Resolve one staked position's exact early-exit penalty window.

        The gauge dates every deposit, and the factory owns both the penalty
        rate and the pool's minimum stake time, so three reads resolve the
        window exactly: ``depositTimestamp(tokenId) + minStakeTimes(pool)``
        against the injected clock.

        Args:
            observation: The pool observation naming the gauge and pool.
            token_id: The staked position NFT being dated.

        Returns:
            The resolved penalty window.

        Raises:
            LpExecutionRefusalError: If any penalty read reverts, because a
                claim's forfeiture exposure cannot be established.
        """
        try:
            gauge_factory = self._read_address(
                observation.gauge_address,
                build_gauge_gauge_factory_read_calldata(),
                "gaugeFactory()",
            )
            penalty_rate_bps = self._read_word(
                gauge_factory,
                build_gauge_penalty_rate_read_calldata(),
                "penaltyRate()",
            )
            min_stake_seconds = self._read_word(
                gauge_factory,
                build_gauge_min_stake_times_read_calldata(observation.pool_address),
                "minStakeTimes(address)",
            )
            deposit_timestamp = self._read_word(
                observation.gauge_address,
                build_gauge_deposit_timestamp_read_calldata(token_id),
                "depositTimestamp(uint256)",
            )
        except ExecutorRpcRevertError as error:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.PENALTY_STATE_UNREADABLE,
                f"a penalty-window read reverted ({error}); the early-exit forfeiture "
                "exposure cannot be established, so the claim or withdrawal is refused "
                "rather than guessed at",
            ) from error
        clears_at = deposit_timestamp + min_stake_seconds
        remaining = max(0, clears_at - int(self._now().timestamp()))
        return LpPenaltyWindow(
            penalty_rate_bps=penalty_rate_bps,
            min_stake_seconds=min_stake_seconds,
            deposit_timestamp=deposit_timestamp,
            window_clears_at_timestamp=clears_at,
            remaining_seconds=remaining,
        )

    def _require_penalty_clear(self, penalty: LpPenaltyWindow, accrued_earned: int) -> None:
        """Refuse any claim that would forfeit emissions inside the window.

        Args:
            penalty: The resolved early-exit penalty window.
            accrued_earned: The live accrued emissions at stake, raw units.

        Raises:
            LpExecutionRefusalError: When the window is open, the rate is
                positive, and accrued emissions would be forfeited.
        """
        if penalty.remaining_seconds > 0 and penalty.penalty_rate_bps > 0 and accrued_earned > 0:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.WITHIN_PENALTY_WINDOW,
                f"the early-exit penalty window is open for {penalty.remaining_seconds} more "
                f"seconds at {penalty.penalty_rate_bps} bps, and {accrued_earned} raw AERO "
                f"of accrued emissions would be forfeited; wait until unix "
                f"{penalty.window_clears_at_timestamp} and retry",
            )

    def _exit_amounts(
        self, observation: LpPoolObservation, position: LpPositionView
    ) -> tuple[Decimal, Decimal]:
        """Compute both sides' amounts a full decrease returns at the snapshot.

        Args:
            observation: The block-pinned pool observation.
            position: The live twelve-word position view.

        Returns:
            The raw token-zero and token-one amounts.
        """
        return position_amounts_at_sqrt_ratio(
            observation.sqrt_ratio,
            position.tick_lower,
            position.tick_upper,
            Decimal(position.liquidity),
        )

    def _projected_inventory(
        self,
        observation: LpPoolObservation,
        position: LpPositionView,
        amount0: Decimal,
        amount1: Decimal,
    ) -> tuple[int, int]:
        """Project the Safe's post-exit inventory for one recenter.

        The exit has not executed at build time, so the projection adds the
        full decrease outputs and checkpointed fees to the live balances the
        same sequence will have augmented when the mint lands.

        Args:
            observation: The block-pinned pool observation.
            position: The live twelve-word position view.
            amount0: The expected raw token-zero decrease output.
            amount1: The expected raw token-one decrease output.

        Returns:
            The projected raw USDC and stock balances.
        """
        usdc_side = amount1 if observation.stock_is_token0 else amount0
        stock_side = amount0 if observation.stock_is_token0 else amount1
        usdc_fees = Decimal(
            position.tokens_owed1_units
            if observation.stock_is_token0
            else position.tokens_owed0_units
        )
        stock_fees = Decimal(
            position.tokens_owed0_units
            if observation.stock_is_token0
            else position.tokens_owed1_units
        )
        live_usdc = self._rpc.fetch_token_balance(BASE_USDC_ADDRESS, self._safe_address)
        stock_token = (
            observation.token0_address
            if observation.stock_is_token0
            else observation.token1_address
        )
        live_stock = self._rpc.fetch_token_balance(stock_token, self._safe_address)
        projected_usdc = int(
            (Decimal(live_usdc) + usdc_side + usdc_fees).to_integral_value(ROUND_FLOOR)
        )
        projected_stock = int(
            (Decimal(live_stock) + stock_side + stock_fees).to_integral_value(ROUND_FLOOR)
        )
        return projected_usdc, projected_stock

    def _read_live_aero_price(self, observation: LpPoolObservation) -> Decimal:
        """Read the live AERO price at the observation's snapshot block.

        Args:
            observation: The block-pinned observation anchoring the read.

        Returns:
            The USDC price of one whole AERO token at that block.

        Raises:
            LpExecutionRefusalError: If the live read fails closed.
        """
        try:
            return self._rpc.fetch_aero_price_usdc(hex(observation.snapshot_block))
        except (ExecutorRpcRevertError, ExecutionUnavailableError, ValueError) as error:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.AERO_PRICE_UNREADABLE,
                f"the live AERO price read from Aerodrome's USDC/AERO pool failed at "
                f"block {observation.snapshot_block}: {error}; pass --aero-price to "
                "assume a price explicitly or retry",
            ) from error

    def _quote_emissions_apr(
        self, observation: LpPoolObservation, aero_price_usdc: Decimal
    ) -> tuple[Decimal | None, str]:
        """Quote the pool's emissions APR in Aerodrome's displayed convention.

        The conversion is the shared, source-cited convention in
        ``aero_bot.emissions_apr`` - the annualized gauge reward value over
        the Sugar snapshot's current-cell staked value - plus the width
        family the same convention generalizes to, so the report shows the
        concentration dependence explicitly.

        Args:
            observation: The block-pinned pool observation.
            aero_price_usdc: The AERO price in USDC (live read or override).

        Returns:
            The quoted APR as a decimal fraction, or None with its diagnostic
            when the inputs are absent.
        """
        staked0 = observation.staked_reserve0_units
        staked1 = observation.staked_reserve1_units
        if (
            observation.emissions_per_second_units <= 0
            or (staked0 <= 0 and staked1 <= 0)
            or aero_price_usdc <= 0
        ):
            return None, (
                "no quoted emissions APR: the snapshot carries no emissions rate or staked "
                "reserves to quote against"
            )
        apr = aerodrome_display_emissions_apr(
            observation.emissions_per_second_units,
            aero_price_usdc,
            staked0,
            staked1,
            observation.stock_is_token0,
            observation.stock_decimals,
            observation.quote_decimals,
            observation.price_usdc_per_stock,
        )
        staked_value = staked_value_usdc(
            staked0,
            staked1,
            observation.stock_is_token0,
            observation.stock_decimals,
            observation.quote_decimals,
            observation.price_usdc_per_stock,
        )
        width_notes: list[str] = []
        if observation.gauge_liquidity_units > 0:
            anchor = (
                observation.current_tick // observation.tick_spacing
            ) * observation.tick_spacing
            for half_width in (observation.tick_spacing, 3, 10):
                try:
                    width_apr = emissions_apr_at_tick_width(
                        observation.emissions_per_second_units,
                        aero_price_usdc,
                        observation.gauge_liquidity_units,
                        observation.sqrt_ratio,
                        anchor,
                        half_width,
                        observation.stock_is_token0,
                        observation.stock_decimals,
                        observation.quote_decimals,
                    )
                except ValueError:
                    continue
                width_notes.append(f"+/-{half_width} ticks {Decimal(100) * width_apr:.2f}%")
        width_line = (
            f"; the same stream at staked widths {' '.join(width_notes)}" if width_notes else ""
        )
        return apr, (
            f"annual reward value {Decimal(apr) * staked_value:.4f} USDC over the snapshot's "
            f"{staked_value:.2f} USDC current-cell staked value at the AERO price "
            f"{aero_price_usdc} - Aerodrome's displayed convention, a per-cell "
            "concentration number that inflates as staked value concentrates near "
            f"the current price{width_line}"
        )

    def safe_position_inventory(self, symbol: str) -> LpSafePositionsSnapshot:
        """Enumerate every position NFT the Safe holds on one pool's NFPM.

        The snapshot is read-only: registry and discovery gates run exactly as
        every other surface, then the Safe's NFPM balance is enumerated token
        by token with each position's live liquidity and owed fees. A
        mid-enumeration revert refuses fail-closed rather than guessing at
        the missing entries.

        Args:
            symbol: The registry-matched B20 stock symbol.

        Returns:
            The complete held-NFT snapshot; nothing was built or signed.

        Raises:
            LpExecutionRefusalError: If any registry, discovery, or
                enumeration gate refuses.
        """
        try:
            listing, observation, caps = self._observe_pool(symbol)
            held = self._enumerate_held_positions(observation)
            caps.append(
                f"enumerated {len(held)} Safe-held NFT(s) on the NFPM "
                f"({sum(1 for position in held if position.live)} live)"
            )
            return LpSafePositionsSnapshot(
                symbol=listing.symbol,
                pool_address=observation.pool_address,
                nfpm_address=observation.nfpm_address,
                positions=held,
                snapshot_block=observation.snapshot_block,
                observed_at=observation.observed_at,
                caps_enforced=tuple(caps),
                diagnostics=(
                    (
                        f"the Safe holds {len(held)} position NFT(s) on NFPM "
                        f"{observation.nfpm_address}"
                    ),
                    (
                        "live token ids: " + ", ".join(str(p.token_id) for p in held if p.live)
                        if any(p.live for p in held)
                        else "no live positions"
                    ),
                ),
            )
        except LpExecutionRefusalError as error:
            self._record_refusal("inventory", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def _enumerate_held_positions(
        self, observation: LpPoolObservation
    ) -> tuple[LpHeldPosition, ...]:
        """Enumerate the Safe's held NFTs with their live exposure fields.

        Args:
            observation: The block-pinned pool observation naming the NFPM.

        Returns:
            Every Safe-held position NFT in enumeration order.

        Raises:
            LpExecutionRefusalError: If any enumeration read reverts, because
                the held inventory cannot be established honestly.
        """
        try:
            held_count = self._read_word(
                observation.nfpm_address,
                self._erc20_balance_calldata(self._safe_address),
                "NFPM balanceOf()",
            )
            held: list[LpHeldPosition] = []
            for index in range(held_count):
                token_id = self._read_word(
                    observation.nfpm_address,
                    self._erc721_token_of_owner_by_index_calldata(self._safe_address, index),
                    "tokenOfOwnerByIndex(address,uint256)",
                )
                view = decode_lp_positions_view(
                    self._rpc.eth_call(
                        observation.nfpm_address, build_lp_positions_read_calldata(token_id)
                    )
                )
                held.append(
                    LpHeldPosition(
                        token_id=token_id,
                        liquidity=view.liquidity,
                        tokens_owed0_units=view.tokens_owed0_units,
                        tokens_owed1_units=view.tokens_owed1_units,
                    )
                )
        except (ExecutorRpcRevertError, ValueError) as error:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.ENUMERATION_UNREADABLE,
                f"the Safe's held-position enumeration on NFPM "
                f"{observation.nfpm_address} could not complete ({error}); the total "
                "pilot exposure cannot be evaluated honestly, so the attempt refuses",
            ) from error
        return tuple(held)

    def _resolve_mint_context(self, symbol: str, width_spacings: int | None) -> _LpMintContext:
        """Resolve one mint to its observation, inventory, and shared gates.

        Args:
            symbol: The registry-matched B20 stock symbol.
            width_spacings: The explicit half width in tick spacings per side.

        Returns:
            The resolved context carrying every gate label enforced so far.

        Raises:
            LpExecutionRefusalError: If any registry, discovery, snapshot,
                pool-shape, width-source, or untracked-positions gate refuses.
        """
        listing, observation, caps = self._observe_pool(symbol)
        if width_spacings is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.DERIVED_WIDTH_UNAVAILABLE,
                "no --width-ticks override was supplied and the solver-derived width path "
                "needs a reconstructed price path this manual surface does not yet carry "
                "(the corrected emissions-APR convention is live); pass an explicit half "
                "width in tick spacings per side",
            )
        usdc_balance = self._rpc.fetch_token_balance(BASE_USDC_ADDRESS, self._safe_address)
        stock_token = (
            observation.token0_address
            if observation.stock_is_token0
            else observation.token1_address
        )
        stock_balance = self._rpc.fetch_token_balance(stock_token, self._safe_address)
        # The total-exposure cap stays honest by refusing once the Safe holds
        # LIVE untracked positions this executor cannot value; empty residual
        # NFTs carry no exposure and no longer block entry.
        held = self._enumerate_held_positions(observation)
        live_untracked = [position.token_id for position in held if position.live]
        if live_untracked:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.UNTRACKED_EXISTING_POSITIONS,
                f"the Safe holds {len(live_untracked)} live untracked position NFT(s) "
                f"(token ids {', '.join(str(token) for token in live_untracked)}) on this "
                "NFPM, so the total pilot exposure cap cannot be evaluated honestly; "
                "refuse until they are reconciled or exited",
            )
        if held:
            caps.append(
                f"Safe holds {len(held)} empty residual NFT(s) on this NFPM carrying no exposure"
            )
        else:
            caps.append("Safe holds no untracked position NFTs on this NFPM")
        inventory = SafeInventory(usdc_units=usdc_balance, stock_units=stock_balance)
        return _LpMintContext(listing, observation, inventory, width_spacings, caps)

    def _plan_from_context(self, context: _LpMintContext, budget_usdc: Decimal) -> LpMintPlan:
        """Run the pure planner over one resolved mint context.

        Args:
            context: The resolved observation and inventory context.
            budget_usdc: The total USDC value the position commits.

        Returns:
            The complete capped mint plan.

        Raises:
            LpPlanRefusalError: If any planning cap refuses.
        """
        directive = MintDirective(
            budget_usdc=budget_usdc,
            half_width_spacings=context.width_spacings,
            width_source=WidthSource.EXPLICIT_OVERRIDE,
        )
        return plan_mint_entry(self._plan_policy, context.observation, directive, context.inventory)

    def _observe_pool(self, symbol: str) -> tuple[B20AssetListing, LpPoolObservation, list[str]]:
        """Resolve one symbol to its live pool observation with shared gates.

        Args:
            symbol: The registry-matched B20 stock symbol.

        Returns:
            The registry listing, the block-pinned observation, and the cap
            labels enforced so far.

        Raises:
            LpExecutionRefusalError: If any registry, discovery, snapshot, or
                pool-shape gate refuses.
        """
        caps: list[str] = []
        registry = self._sources.load_registry()
        if registry.status is not RegistryStatus.VERIFIED:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.REGISTRY_UNVERIFIED,
                f"the official Coinbase-issued B20 registry did not validate "
                f"({registry.status.value}); the token whitelist is USDC plus that registry "
                "only, so execution is refused until the registry validates",
            )
        listing = next(
            (asset for asset in registry.assets if asset.symbol.lower() == symbol.strip().lower()),
            None,
        )
        if listing is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.SYMBOL_NOT_IN_REGISTRY,
                f"symbol {symbol!r} is not in the official Coinbase-issued B20 registry; the "
                "token whitelist is USDC plus that registry only",
            )
        pin = (
            self._pool_pin_store.load().get(listing.symbol.strip().lower())
            if self._pool_pin_store is not None
            else None
        )
        if pin is not None:
            try:
                observation = self._known_pool_observation(listing, pin)
            except (ExecutionUnavailableError, ValueError):
                # Any unreadable view or identity mismatch falls back to the
                # full sweep below, which re-verifies everything the slow way
                # and refreshes the pin: the store is a cache, never a trust
                # root, so it can only ever cost speed.
                pass
            else:
                observed_at = observation.observed_at
                age_seconds = max(0, int((self._now() - observed_at).total_seconds()))
                if age_seconds > self._policy.snapshot_max_age_seconds:
                    raise LpExecutionRefusalError(
                        LpExecutionRefusalCode.SNAPSHOT_STALE,
                        f"the pool snapshot is {age_seconds} seconds old, above the "
                        f"{self._policy.snapshot_max_age_seconds}-second staleness bound; "
                        "re-run discovery for a fresh snapshot",
                    )
                caps.append("token within the USDC-plus-registry whitelist")
                caps.append(
                    f"pool {observation.pool_address} from the known-pool fast path "
                    f"(identity re-verified live, state at block {observation.snapshot_block})"
                )
                caps.append(
                    f"snapshot fresher than {self._policy.snapshot_max_age_seconds} seconds"
                )
                return listing, observation, caps
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
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POOL_NOT_DISCOVERED,
                f"no live Sugar-verified B20/USDC pool exists for {listing.symbol!r} "
                f"(discovery status {discovery.status.value}); execution requires a pool from "
                "live discovery",
            )
        if discovery.observed_at is None or discovery.snapshot_block is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.SNAPSHOT_EVIDENCE_MISSING,
                "the discovery snapshot carries no observation evidence; refusing to plan "
                "without a block-pinned snapshot",
            )
        if pool.nfpm_address is None or pool.gauge_address is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POOL_MISSING_NFPM_OR_GAUGE,
                f"pool {pool.pool_address} carries no NFPM or gauge in its Sugar record, so "
                "no Slipstream lifecycle exists for it",
            )
        stock_is_token0 = pool.token0_address == listing.address
        stock_token = pool.token0_address if stock_is_token0 else pool.token1_address
        stock_decimals = self._sources.read_token_decimals(stock_token)
        observed_at = discovery.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        age_seconds = max(0, int((self._now() - observed_at).total_seconds()))
        if age_seconds > self._policy.snapshot_max_age_seconds:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.SNAPSHOT_STALE,
                f"the pool snapshot is {age_seconds} seconds old, above the "
                f"{self._policy.snapshot_max_age_seconds}-second staleness bound; re-run "
                "discovery for a fresh snapshot",
            )
        caps.append("token within the USDC-plus-registry whitelist")
        caps.append(f"pool {pool.pool_address} from live Sugar discovery")
        caps.append(f"snapshot fresher than {self._policy.snapshot_max_age_seconds} seconds")
        observation = LpPoolObservation(
            symbol=listing.symbol,
            pool_address=pool.pool_address,
            nfpm_address=pool.nfpm_address,
            gauge_address=pool.gauge_address,
            token0_address=pool.token0_address,
            token1_address=pool.token1_address,
            stock_is_token0=stock_is_token0,
            stock_decimals=stock_decimals,
            quote_decimals=QUOTE_TOKEN_DECIMALS,
            tick_spacing=pool.tick_spacing,
            current_tick=pool.current_tick,
            sqrt_ratio=pool.sqrt_ratio,
            pool_active_liquidity=pool.pool_active_liquidity,
            usdc_reserve_units=pool.reserve1 if stock_is_token0 else pool.reserve0,
            emissions_per_second_units=pool.emissions_per_second,
            emissions_token_address=pool.emissions_token_address,
            gauge_liquidity_units=pool.gauge_liquidity,
            staked_reserve0_units=pool.staked0,
            staked_reserve1_units=pool.staked1,
            snapshot_block=discovery.snapshot_block,
            observed_at=observed_at,
        )
        self._persist_pool_pin(
            listing=listing,
            factory_address=pool.factory_address,
            observation=observation,
            snapshot_block=discovery.snapshot_block,
            observed_at=observed_at,
            discovery_source=discovery.source,
            existing_pin=pin,
        )
        return listing, observation, caps

    def _known_pool_observation(
        self, listing: B20AssetListing, pin: LpPoolPin
    ) -> LpPoolObservation:
        """Build one fresh live observation for a pinned known pool.

        The pinned identity is re-verified against the pool contract's own
        immutable views (token pair, tick spacing, factory, gauge binding,
        and the gauge factory's NFPM, the Sugar's own resolution path) and the
        pool's live state - price and tick through ``slot0()``, active and
        staked liquidity, the USDC reserve balance, and the gauge's emission
        token and per-second rate - is read at one freshly pinned block, so
        planning still re-prices against current on-chain state. Speed comes
        from skipping the full-pool enumeration, never from skipping
        verification.

        Args:
            listing: The registry listing the symbol resolved to.
            pin: The persisted Sugar-verified identity for the pool.

        Returns:
            The block-pinned live observation of the known pool.

        Raises:
            ValueError: If any pinned identity fact no longer matches the
                live pool, or any decoded view fails its coherence checks.
            ExecutionUnavailableError: If any read cannot complete.
        """
        rpc = self._rpc
        block_number = rpc.fetch_block_number()
        block_tag = hex(block_number)

        def read(contract_address: str, calldata: str) -> str:
            return rpc.eth_call_at(contract_address, calldata, block_tag)

        pool_address = pin.pool_address
        token0 = decode_address_view_result(read(pool_address, build_pool_token0_read_calldata()))
        token1 = decode_address_view_result(read(pool_address, build_pool_token1_read_calldata()))
        tick_spacing = decode_uint_view_result(
            read(pool_address, build_pool_tick_spacing_read_calldata())
        )
        gauge = decode_address_view_result(read(pool_address, build_pool_gauge_read_calldata()))
        factory = decode_address_view_result(read(pool_address, build_pool_factory_read_calldata()))
        gauge_factory = decode_address_view_result(
            read(pin.gauge_address, build_gauge_gauge_factory_read_calldata())
        )
        nfpm = decode_address_view_result(
            read(gauge_factory, build_gauge_factory_nft_read_calldata())
        )
        listing_stock = normalize_evm_address(listing.address)
        pinned_identity = (
            normalize_evm_address(pin.token0_address),
            normalize_evm_address(pin.token1_address),
            pin.tick_spacing,
            normalize_evm_address(pin.gauge_address),
            normalize_evm_address(pin.factory_address),
            normalize_evm_address(pin.nfpm_address),
        )
        live_identity = (token0, token1, tick_spacing, gauge, factory, nfpm)
        if live_identity != pinned_identity or listing_stock not in (token0, token1):
            raise ValueError(
                f"the pinned identity for {listing.symbol} no longer matches the live "
                f"pool {pool_address}: pinned {pinned_identity} versus live {live_identity} "
                f"with registry stock {listing_stock}; falling back to full discovery"
            )
        stock_is_token0 = token0 == listing_stock
        sqrt_ratio, current_tick = decode_pool_slot0_view(
            read(pool_address, build_pool_slot0_read_calldata())
        )
        pool_active_liquidity = decode_uint_view_result(
            read(pool_address, build_pool_liquidity_read_calldata())
        )
        gauge_liquidity = decode_uint_view_result(
            read(pool_address, build_pool_staked_liquidity_read_calldata())
        )
        usdc_reserve = decode_uint_view_result(
            read(BASE_USDC_ADDRESS, self._erc20_balance_calldata(pool_address))
        )
        emissions_token = decode_address_view_result(
            read(pin.gauge_address, build_gauge_reward_token_read_calldata())
        )
        emissions_per_second = decode_uint_view_result(
            read(pin.gauge_address, build_gauge_reward_rate_read_calldata())
        )
        return LpPoolObservation(
            symbol=listing.symbol,
            pool_address=pool_address,
            nfpm_address=nfpm,
            gauge_address=gauge,
            token0_address=token0,
            token1_address=token1,
            stock_is_token0=stock_is_token0,
            stock_decimals=pin.stock_decimals,
            quote_decimals=QUOTE_TOKEN_DECIMALS,
            tick_spacing=tick_spacing,
            current_tick=current_tick,
            sqrt_ratio=sqrt_ratio,
            pool_active_liquidity=pool_active_liquidity,
            usdc_reserve_units=usdc_reserve,
            emissions_per_second_units=emissions_per_second,
            emissions_token_address=emissions_token,
            gauge_liquidity_units=gauge_liquidity,
            staked_reserve0_units=0,
            staked_reserve1_units=0,
            snapshot_block=block_number,
            observed_at=self._now(),
        )

    def _persist_pool_pin(
        self,
        *,
        listing: B20AssetListing,
        factory_address: str,
        observation: LpPoolObservation,
        snapshot_block: int | None,
        observed_at: datetime,
        discovery_source: str,
        existing_pin: LpPoolPin | None,
    ) -> None:
        """Refresh one pool's pin after a successful full-sweep resolution.

        The pin store is a cache of verified facts, so persistence failures
        are ignored: the action already passed every gate, and the next run
        simply re-runs the sweep when the pin could not be written.

        Args:
            listing: The registry listing the symbol resolved to.
            factory_address: The pool's factory as the sweep validated it.
            observation: The sweep-built observation carrying the identity.
            snapshot_block: The sweep's pinned snapshot block.
            observed_at: The sweep's observation time.
            discovery_source: The sweep's source provenance string.
            existing_pin: The pin that failed the fast path, if any, kept for
                callers that want to compare before rewriting.
        """
        if self._pool_pin_store is None or snapshot_block is None:
            return
        try:
            pin = build_pool_pin_from_discovery(
                symbol=listing.symbol,
                pool_address=observation.pool_address,
                factory_address=factory_address,
                token0_address=observation.token0_address,
                token1_address=observation.token1_address,
                tick_spacing=observation.tick_spacing,
                gauge_address=observation.gauge_address,
                nfpm_address=observation.nfpm_address,
                stock_decimals=observation.stock_decimals,
                snapshot_block=snapshot_block,
                observed_at=observed_at,
                discovery_source=discovery_source,
            )
            if pin == existing_pin:
                return
            self._pool_pin_store.save_pin(pin)
        except (OSError, ValueError):
            # The cache never gates the action; a failed write only costs
            # the next run its fast path.
            return

    def _preflight(self, caps: list[str]) -> tuple[int, int, int]:
        """Run the pre-sign chain gates and return the live preflight state.

        Args:
            caps: The enforced-cap list extended with each passing gate.

        Returns:
            The gas price, Safe ETH balance, and live Safe nonce, all observed
            before anything was signed.

        Raises:
            LpExecutionRefusalError: If the gas cap or ETH floor refuses.
        """
        gas_price = self._rpc.fetch_gas_price()
        if gas_price > self._policy.gas_price_cap_wei:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.GAS_PRICE_ABOVE_CAP,
                f"the endpoint's gas price {gas_price} wei exceeds the "
                f"{self._policy.gas_price_cap_wei}-wei cap (1 gwei); wait for calmer network "
                "conditions or consciously raise the cap in configuration",
            )
        caps.append(f"effective gas price at or below {self._policy.gas_price_cap_wei} wei")
        safe_eth = self._rpc.fetch_eth_balance(self._safe_address)
        if safe_eth < self._policy.safe_eth_floor_wei:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.SAFE_ETH_BELOW_FLOOR,
                f"the Safe holds {safe_eth} wei, below the documented floor of "
                f"{self._policy.safe_eth_floor_wei} wei; top up the Safe's ETH balance before "
                "executing",
            )
        caps.append(f"Safe ETH balance at or above the {self._policy.safe_eth_floor_wei}-wei floor")
        live_nonce = self._safe_rpc.fetch_live_nonce()
        return gas_price, safe_eth, live_nonce

    def _build_steps(
        self,
        steps: Sequence[_LpStepSpec],
        live_nonce: int,
        key_bytes: bytes,
        action: str,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
    ) -> tuple[_BuiltLpStep, ...]:
        """Build, sign, validate, and estimate every step in sequence order.

        Args:
            steps: The composed inner calls in execution order.
            live_nonce: The Safe nonce the first transaction occupies.
            key_bytes: Exactly 32 raw signing-key bytes.
            action: The lifecycle action the sequence belongs to.
            mode: The attempt mode every build audit record carries.

        Returns:
            The fully built steps with their reports in execution order.
        """
        built: list[_BuiltLpStep] = []
        for index, step in enumerate(steps):
            built.append(
                self._build_step(
                    step,
                    nonce=live_nonce + index,
                    key_bytes=key_bytes,
                    action=action,
                    sequenced_behind_predecessors=index > 0,
                    mode=mode,
                )
            )
        return tuple(built)

    def _build_step(
        self,
        step: _LpStepSpec,
        nonce: int,
        key_bytes: bytes,
        action: str,
        sequenced_behind_predecessors: bool,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
    ) -> _BuiltLpStep:
        """Build, sign, validate, and estimate one LP Safe transaction.

        Args:
            step: The composed inner call being built.
            nonce: The Safe nonce this transaction occupies.
            key_bytes: Exactly 32 raw signing-key bytes.
            action: The lifecycle action this transaction belongs to.
            sequenced_behind_predecessors: Whether earlier transactions of the
                same sequence precede this one, so a reverting estimate is the
                expected pre-execution answer rather than an anomaly.
            mode: The attempt mode the build audit record carries.

        Returns:
            The fully built step carrying its report; nothing was broadcast.
        """
        transaction = SafeTransaction(
            to_address=step.to_address, data=step.inner_calldata, nonce=nonce
        )
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
            if sequenced_behind_predecessors:
                gas_diagnostic += (
                    " (expected while this transaction's predecessors in the sequence remain "
                    "unexecuted)"
                )
        self._record_build(
            mode,
            action,
            step.role,
            built,
            calldata_digest,
            step.description,
            validation,
            gas_estimate,
        )
        return _BuiltLpStep(
            report=BuiltLpTransaction(
                role=step.role,
                action=action,
                safe_tx_hash=built.safe_tx_hash,
                to_address=step.to_address,
                calldata_digest=calldata_digest,
                nonce=nonce,
                description=step.description,
                signature_verified=validation.verified,
                signature_diagnostic=validation.diagnostic,
                gas_estimate=gas_estimate,
                gas_estimate_diagnostic=gas_diagnostic,
            ),
            transaction=transaction,
            built=built,
            signature=signature,
            exec_calldata=calldata,
        )

    def _execute_steps(
        self, action: str, steps: Sequence[_BuiltLpStep], key_bytes: bytes
    ) -> tuple[tuple[LpStepExecutionReport, ...], str]:
        """Broadcast every built step in nonce order, one inclusion at a time.

        Per-nonce Safe sequencing requires each predecessor to be mined before
        the next transaction can estimate or execute, so the loop never
        proceeds past an unconfirmed or failed delivery.

        Args:
            action: The lifecycle action the sequence belongs to.
            steps: The built steps in execution order.
            key_bytes: Exactly 32 raw signing-key bytes signing every delivery.

        Returns:
            The per-step reports and the halt reason, empty when every step
            confirmed.

        Raises:
            LpExecutionRefusalError: If a pre-broadcast check refuses; the
                refusal names the step and the sequence stops at the completed
                prefix.
        """
        reports: list[LpStepExecutionReport] = []
        for step in steps:
            try:
                report = self._execute_step(action, step, key_bytes)
            except LpExecutionRefusalError as error:
                if reports:
                    previous = tuple(getattr(error, "completed_steps", ()))
                    error.completed_steps = tuple(reports) + previous
                raise
            reports.append(report)
            if report.status != "confirmed":
                reason = f"the {report.role.value} delivery is {report.status}"
                if report.diagnostic:
                    reason = f"{reason}: {report.diagnostic}"
                return tuple(reports), reason
        return tuple(reports), ""

    def _execute_step(
        self, action: str, step: _BuiltLpStep, key_bytes: bytes
    ) -> LpStepExecutionReport:
        """Rebuild, re-validate, estimate, deliver, and track one step.

        Args:
            action: The lifecycle action the step belongs to.
            step: The built step being broadcast.
            key_bytes: Exactly 32 raw signing-key bytes signing the delivery.

        Returns:
            The step's delivery report; a non-confirmed status halts the
            sequence without being a refusal.

        Raises:
            LpExecutionRefusalError: If the hash pin, live validation, fresh
            estimate, or relayer preflight refuses; nothing was broadcast for
            this step when that happens.
        """
        role = step.report.role

        # 1. Rebuild the SafeTx from its exact transaction and pin the hash.
        rebuild_started = self._timer()
        rebuilt = build_safe_transaction(step.transaction, self._safe_address)
        if rebuilt.safe_tx_hash != step.built.safe_tx_hash:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.REBUILD_HASH_MISMATCH,
                f"the rebuilt safe_tx_hash {rebuilt.safe_tx_hash} no longer equals the "
                f"validated {step.built.safe_tx_hash}; refusing to broadcast content "
                "that was never validated",
            )
        rebuild_ms = Decimal(self._milliseconds_since(rebuild_started))

        # 2. Prove the owner signature against the live contract again.
        validate_started = self._timer()
        validation = self._safe_rpc.validate_owner_signature(step.built, step.signature)
        if not validation.verified:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.SIGNATURE_REJECTED,
                f"the live Safe rejected the {role.value} signature at execute time: "
                f"{validation.diagnostic}",
            )
        validate_ms = Decimal(self._milliseconds_since(validate_started))

        # 3. Fresh estimate: predecessors are mined by now, so a revert is a
        #    genuine refusal to stop at; only a transient endpoint-lag GS026
        #    earns bounded fresh re-reads.
        estimate_started = self._timer()
        gas_estimate: int | None = None
        estimate_error = ExecutorRpcRevertError("the estimate never ran")
        for _ in range(EXECUTE_ESTIMATE_LAG_RETRIES):
            try:
                gas_estimate = self._rpc.estimate_gas(self._safe_address, step.exec_calldata)
                break
            except ExecutorRpcRevertError as error:
                estimate_error = error
                if "GS026" not in str(error):
                    break
                self._sleep(EXECUTE_ESTIMATE_LAG_RETRY_SECONDS)
        if gas_estimate is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.ESTIMATE_REVERTED,
                f"the fresh on-chain estimate for the {role.value} transaction reverted "
                f"with every predecessor mined: {estimate_error}; stopping honestly at "
                "the completed prefix",
            )
        estimate_ms = Decimal(self._milliseconds_since(estimate_started))

        # 4. Build the delivery transaction with the relayer preflights.
        delivery_started = self._timer()
        relayer = normalize_evm_address(Account.from_key(key_bytes).address)
        gas_price = min(self._rpc.fetch_gas_price(), self._policy.gas_price_cap_wei)
        buffered = Decimal(gas_estimate) * (Decimal(1) + GAS_LIMIT_BUFFER_FRACTION)
        gas_limit = int(buffered.to_integral_value(rounding=ROUND_CEILING))
        relayer_nonce = self._rpc.fetch_relayer_nonce(relayer)
        relayer_balance = self._rpc.fetch_eth_balance(relayer)
        required_wei = gas_limit * gas_price
        floor_wei = max(self._policy.relayer_eth_floor_wei, 2 * required_wei)
        if relayer_balance < floor_wei:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.RELAYER_ETH_INSUFFICIENT,
                f"the relaying EOA {relayer} holds {relayer_balance} wei, below the "
                f"{floor_wei}-wei floor (the policy floor and twice the "
                f"{required_wei}-wei bounded gas cost); fund the EOA before executing",
            )
        delivery: dict[str, Any] = {
            "to": to_checksum_address(self._safe_address),
            "data": step.exec_calldata,
            "nonce": relayer_nonce,
            "gas": gas_limit,
            "maxFeePerGas": gas_price,
            "maxPriorityFeePerGas": gas_price,
            "chainId": SAFE_CHAIN_ID,
            "type": 2,
        }
        signed = Account.sign_transaction(delivery, key_bytes)
        raw_transaction = "0x" + bytes(signed.raw_transaction).hex()
        delivery_ms = Decimal(self._milliseconds_since(delivery_started))

        # 5. Broadcast, but derive the transaction hash locally first. The
        # hash is deterministic from the signed bytes, so a transport/HTTP
        # failure after the node accepted the transaction can never erase the
        # identity of the possibly-landed delivery.
        send_started = self._timer()
        local_transaction_hash = "0x" + keccak(bytes(signed.raw_transaction)).hex()
        send_error = ""
        try:
            transaction_hash = self._rpc.send_raw_transaction(raw_transaction)
        except ExecutionUnavailableError as error:
            transaction_hash = local_transaction_hash
            send_error = str(error)
            print(
                f"[{action}/{role.value}] WARNING: broadcast response unavailable for "
                f"{transaction_hash}; submission outcome is unknown ({send_error})",
                file=sys.stderr,
                flush=True,
            )
            self._record_execute_broadcast_unknown(
                action, role, step.report, transaction_hash, relayer
            )
        else:
            print(
                f"[{action}/{role.value}] broadcast {transaction_hash} "
                f"(Safe nonce {step.report.nonce}, delivery gas {gas_limit} at "
                f"{gas_price} wei)",
                file=sys.stderr,
                flush=True,
            )
            self._record_execute_sent(action, role, step.report, transaction_hash, relayer)
        send_ms = Decimal(self._milliseconds_since(send_started))

        # 6. Bounded receipt wait across every configured backend. Even when
        # submission acknowledgement was lost, a secondary endpoint can prove
        # that the deterministic transaction hash landed successfully.
        receipt = self._await_receipt_multi(transaction_hash)
        inclusion_ms = Decimal(self._milliseconds_since(send_started))
        if receipt is None:
            diagnostic = (
                f"no receipt for {transaction_hash} within "
                f"{EXECUTE_RECEIPT_TOTAL_TIMEOUT_SECONDS:.0f}s across "
                f"{len(self._receipt_backends)} endpoint(s); "
                + (
                    f"submission acknowledgement was unavailable ({send_error}), so the "
                    "transaction may or may not have landed"
                    if send_error
                    else "the broadcast may still land and the audit chain records the send"
                )
            )
            print(
                f"[{action}/{role.value}] WARNING: {diagnostic}",
                file=sys.stderr,
                flush=True,
            )
            return LpStepExecutionReport(
                action=action,
                role=role,
                safe_tx_hash=step.built.safe_tx_hash,
                nonce=step.report.nonce,
                transaction_hash=transaction_hash,
                status="unconfirmed",
                block_number=None,
                gas_used=None,
                effective_gas_price_wei=None,
                fee_wei=None,
                delivery_gas_limit=gas_limit,
                delivery_max_fee_per_gas_wei=gas_price,
                relayer_nonce=relayer_nonce,
                inclusion_ms=inclusion_ms,
                rebuild_ms=rebuild_ms,
                validate_ms=validate_ms,
                estimate_ms=estimate_ms,
                delivery_ms=delivery_ms,
                send_ms=send_ms,
                diagnostic=diagnostic,
            )
        status = self._receipt_quantity(receipt, "status")
        block_number = self._receipt_quantity(receipt, "blockNumber")
        gas_used = self._receipt_quantity(receipt, "gasUsed")
        effective_gas_price = self._receipt_quantity(receipt, "effectiveGasPrice")
        fee_wei = gas_used * effective_gas_price
        if status == 1:
            outcome: Literal["confirmed", "failed"] = "confirmed"
            diagnostic = ""
            print(
                f"[{action}/{role.value}] included {transaction_hash} block "
                f"{block_number}, {gas_used} gas at {effective_gas_price} wei "
                f"({fee_wei} wei fee)",
                file=sys.stderr,
                flush=True,
            )
        else:
            outcome = "failed"
            diagnostic = (
                "the delivery transaction reverted on-chain; the Safe nonce was "
                "consumed and the inner call did not execute"
            )
            print(
                f"[{action}/{role.value}] FAILED {transaction_hash}: {diagnostic}",
                file=sys.stderr,
                flush=True,
            )
        self._record_execute_receipt(
            outcome,
            action,
            role,
            step.built.safe_tx_hash,
            transaction_hash,
            block_number,
            gas_used,
            effective_gas_price,
            int(inclusion_ms),
            diagnostic,
        )
        return LpStepExecutionReport(
            action=action,
            role=role,
            safe_tx_hash=step.built.safe_tx_hash,
            nonce=step.report.nonce,
            transaction_hash=transaction_hash,
            status=outcome,
            block_number=block_number,
            gas_used=gas_used,
            effective_gas_price_wei=effective_gas_price,
            fee_wei=fee_wei,
            delivery_gas_limit=gas_limit,
            delivery_max_fee_per_gas_wei=gas_price,
            relayer_nonce=relayer_nonce,
            inclusion_ms=inclusion_ms,
            rebuild_ms=rebuild_ms,
            validate_ms=validate_ms,
            estimate_ms=estimate_ms,
            delivery_ms=delivery_ms,
            send_ms=send_ms,
            diagnostic=diagnostic,
        )

    def _await_receipt_multi(self, transaction_hash: str) -> dict[str, object] | None:
        """Poll every receipt backend round-robin under one total bound.

        A landed broadcast is never relabeled a failure: exhaustion returns
        None and the caller reports an unconfirmed warning whose source of
        truth is the audit chain's send record.

        Args:
            transaction_hash: The broadcast transaction's hash.

        Returns:
            The receipt when included, None within the bounded wait.
        """
        started = self._timer()
        while True:
            endpoint_failures: list[str] = []
            for backend in self._receipt_backends:
                try:
                    receipt = backend.fetch_transaction_receipt(transaction_hash)
                except ExecutionUnavailableError as error:
                    # Receipt visibility is deliberately redundant. One public
                    # endpoint can rate-limit, reject, or lag after the exact
                    # transaction has already landed on Base; never relabel
                    # that landed broadcast as a failed action merely because
                    # one observer is unavailable.
                    endpoint_failures.append(str(error))
                    continue
                if receipt is not None:
                    return receipt
            if self._timer() - started >= EXECUTE_RECEIPT_TOTAL_TIMEOUT_SECONDS:
                if endpoint_failures:
                    print(
                        f"[receipt] all available endpoints were unreadable in the final "
                        f"poll for {transaction_hash}: " + "; ".join(endpoint_failures),
                        file=sys.stderr,
                        flush=True,
                    )
                return None
            self._sleep(EXECUTE_RECEIPT_POLL_SECONDS)

    @staticmethod
    def _receipt_quantity(receipt: dict[str, object], key: str) -> int:
        """Decode one hex-or-integer receipt field."""
        value = receipt.get(key)
        if isinstance(value, str):
            return int(value, 16)
        if isinstance(value, int):
            return value
        return 0

    def _record_execute_sent(
        self,
        action: str,
        role: LpExecutionRole,
        report: BuiltLpTransaction,
        transaction_hash: str,
        relayer_address: str,
    ) -> None:
        """Append the broadcast audit event before any receipt wait."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_EXECUTE_SENT,
            LpExecuteSentPayload(
                action=action,
                role=role,
                safe_tx_hash=report.safe_tx_hash,
                transaction_hash=transaction_hash,
                nonce=report.nonce,
                relayer_address=relayer_address,
                safe_address=self._safe_address,
            ),
            self._now(),
        )

    def _record_execute_broadcast_unknown(
        self,
        action: str,
        role: LpExecutionRole,
        report: BuiltLpTransaction,
        transaction_hash: str,
        relayer_address: str,
    ) -> None:
        """Audit a deterministic tx hash when submission acknowledgement is unavailable."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_EXECUTE_BROADCAST_UNKNOWN,
            LpExecuteSentPayload(
                action=action,
                role=role,
                safe_tx_hash=report.safe_tx_hash,
                transaction_hash=transaction_hash,
                nonce=report.nonce,
                relayer_address=relayer_address,
                safe_address=self._safe_address,
            ),
            self._now(),
        )

    def _record_execute_receipt(
        self,
        outcome: Literal["confirmed", "failed"],
        action: str,
        role: LpExecutionRole,
        safe_tx_hash: str,
        transaction_hash: str,
        block_number: int,
        gas_used: int,
        effective_gas_price_wei: int,
        inclusion_ms: int,
        diagnostic: str,
    ) -> None:
        """Append the inclusion audit event for one delivery."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_EXECUTE_CONFIRMED
            if outcome == "confirmed"
            else AuditEventType.LP_EXECUTE_FAILED,
            LpExecuteReceiptPayload(
                outcome=outcome,
                action=action,
                role=role,
                safe_tx_hash=safe_tx_hash,
                transaction_hash=transaction_hash,
                block_number=block_number,
                gas_used=gas_used,
                effective_gas_price_wei=effective_gas_price_wei,
                inclusion_ms=inclusion_ms,
                diagnostic=diagnostic,
            ),
            self._now(),
        )

    def _read_word(self, to_address: str, calldata: str, source: str) -> int:
        """Perform one word-returning read-only call and decode it.

        Args:
            to_address: The contract being called.
            calldata: Complete 0x-prefixed call payload.
            source: Human label naming the call in diagnostics.

        Returns:
            The decoded unsigned integer.

        Raises:
            ExecutionUnavailableError: If the return is not one full word.
        """
        result = self._rpc.eth_call(to_address, calldata)
        if not result.startswith("0x") or len(result) != 2 + 64:
            raise ExecutionUnavailableError(
                f"{source} returned {max(len(result) - 2, 0)} bytes instead of 32"
            )
        return int(result[2:], 16)

    def _read_address(self, to_address: str, calldata: str, source: str) -> str:
        """Perform one address-returning read-only call and decode it.

        Args:
            to_address: The contract being called.
            calldata: Complete 0x-prefixed call payload.
            source: Human label naming the call in diagnostics.

        Returns:
            The decoded normalized address.
        """
        word = self._read_word(to_address, calldata, source)
        return "0x" + format(word & ((1 << 160) - 1), "040x")

    @staticmethod
    def _erc20_balance_calldata(owner_address: str) -> str:
        """Encode one ERC20 or ERC721 balanceOf read.

        Args:
            owner_address: The normalized account whose balance is read.

        Returns:
            Complete 0x-prefixed balanceOf calldata.
        """
        return "0x" + ERC20_BALANCE_OF_SELECTOR + "0" * 24 + owner_address[2:]

    @staticmethod
    def _erc721_owner_of_calldata(token_id: int) -> str:
        """Encode one ERC721 ownerOf read.

        Args:
            token_id: The NFT whose owner is read.

        Returns:
            Complete 0x-prefixed ownerOf calldata.

        Raises:
            ValueError: If the token id is negative.
        """
        if token_id < 0:
            raise ValueError("token_id must be non-negative")
        return "0x" + ERC721_OWNER_OF_SELECTOR + token_id.to_bytes(32, "big").hex()

    @staticmethod
    def _erc721_token_of_owner_by_index_calldata(owner_address: str, index: int) -> str:
        """Encode one ERC721 tokenOfOwnerByIndex read.

        Args:
            owner_address: The normalized account whose NFTs are enumerated.
            index: The enumeration index being read.

        Returns:
            Complete 0x-prefixed tokenOfOwnerByIndex calldata.

        Raises:
            ValueError: If the index is negative.
        """
        if index < 0:
            raise ValueError("index must be non-negative")
        return (
            "0x"
            + ERC721_TOKEN_OF_OWNER_BY_INDEX_SELECTOR
            + "0" * 24
            + owner_address[2:]
            + index.to_bytes(32, "big").hex()
        )

    @staticmethod
    def _erc721_is_approved_for_all_calldata(owner_address: str, operator_address: str) -> str:
        """Encode one ERC721 isApprovedForAll read.

        Args:
            owner_address: The normalized account whose approval is read.
            operator_address: The normalized operator being queried.

        Returns:
            Complete 0x-prefixed isApprovedForAll calldata.
        """
        return (
            "0x"
            + ERC721_IS_APPROVED_FOR_ALL_SELECTOR
            + "0" * 24
            + owner_address[2:]
            + "0" * 24
            + operator_address[2:]
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

    def _record_mint_plan(self, mode: ExecutionMode, plan: LpMintPlan) -> None:
        """Append the mint plan audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_MINT_PLANNED,
            LpMintPlannedPayload(
                mode=mode,
                symbol=plan.symbol,
                pool_address=plan.pool_address,
                nfpm_address=plan.nfpm_address,
                gauge_address=plan.gauge_address,
                snapshot_block=plan.snapshot_block,
                budget_usdc=plan.budget_usdc,
                tick_lower=plan.position_range.tick_lower,
                tick_upper=plan.position_range.tick_upper,
                half_width_ticks=plan.position_range.half_width_ticks,
                width_source=plan.position_range.width_source,
                amount0_desired_units=plan.amounts.amount0_desired_units,
                amount1_desired_units=plan.amounts.amount1_desired_units,
                balancing_swap_required=plan.balancing_swap.required,
                balancing_swap_direction=plan.balancing_swap.direction,
                swap_usdc_in_units=plan.balancing_swap.usdc_in_units,
                swap_stock_in_units=plan.balancing_swap.stock_in_units,
                swap_expected_usdc_units=plan.balancing_swap.expected_usdc_units,
                swap_modeled_impact_fraction=plan.balancing_swap.modeled_impact_fraction,
                caps_enforced=plan.caps_enforced,
            ),
            self._now(),
        )

    def _record_exit_swap_plan(
        self,
        mode: ExecutionMode,
        symbol: str,
        observation: LpPoolObservation,
        balance_units: int,
        price: Decimal,
        expected_out_units: int,
        amount_out_min_units: int,
        allowance_units: int,
    ) -> None:
        """Append the exit-swap plan audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_EXIT_SWAP_PLANNED,
            LpExitSwapPlannedPayload(
                mode=mode,
                symbol=symbol,
                pool_address=observation.pool_address,
                router_address=self._policy.router_address,
                snapshot_block=observation.snapshot_block,
                stock_balance_units=balance_units,
                price_usdc_per_stock=str(price),
                expected_out_units=expected_out_units,
                amount_out_min_units=amount_out_min_units,
                router_stock_allowance_units=allowance_units,
            ),
            self._now(),
        )

    def _record_stake_plan(
        self,
        mode: ExecutionMode,
        symbol: str,
        observation: LpPoolObservation,
        token_id: int,
        token_owner: str | None,
        operator_approved: bool,
    ) -> None:
        """Append the stake plan audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_STAKE_PLANNED,
            LpStakePlannedPayload(
                mode=mode,
                symbol=symbol,
                pool_address=observation.pool_address,
                nfpm_address=observation.nfpm_address,
                gauge_address=observation.gauge_address,
                token_id=token_id,
                token_owner_address=token_owner,
                gauge_operator_approved=operator_approved,
            ),
            self._now(),
        )

    def _record_unstake_plan(
        self,
        mode: ExecutionMode,
        symbol: str,
        observation: LpPoolObservation,
        token_id: int,
        accrued_earned: int,
        accrued_checkpoint: int,
        penalty: LpPenaltyWindow,
    ) -> None:
        """Append the unstake plan audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_UNSTAKE_PLANNED,
            LpUnstakePlannedPayload(
                mode=mode,
                symbol=symbol,
                pool_address=observation.pool_address,
                nfpm_address=observation.nfpm_address,
                gauge_address=observation.gauge_address,
                token_id=token_id,
                accrued_aero_earned_units=accrued_earned,
                accrued_aero_checkpoint_units=accrued_checkpoint,
                penalty_rate_bps=penalty.penalty_rate_bps,
                penalty_remaining_seconds=penalty.remaining_seconds,
            ),
            self._now(),
        )

    def _record_exit_plan(
        self,
        mode: ExecutionMode,
        symbol: str,
        observation: LpPoolObservation,
        token_id: int,
        range_state: PositionRangeState,
        amount0: Decimal,
        amount1: Decimal,
        amount0_min: int,
        amount1_min: int,
        fees_owed0: int,
        fees_owed1: int,
    ) -> None:
        """Append the withdraw plan audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_EXIT_PLANNED,
            LpExitPlannedPayload(
                mode=mode,
                symbol=symbol,
                pool_address=observation.pool_address,
                nfpm_address=observation.nfpm_address,
                gauge_address=observation.gauge_address,
                token_id=token_id,
                range_state=range_state,
                amount0_units=amount0,
                amount1_units=amount1,
                amount0_min_units=amount0_min,
                amount1_min_units=amount1_min,
                fees_owed0_units=fees_owed0,
                fees_owed1_units=fees_owed1,
            ),
            self._now(),
        )

    def _record_collect_plan(
        self,
        mode: ExecutionMode,
        symbol: str,
        observation: LpPoolObservation,
        token_id: int,
        staked: bool,
        accrued_earned: int,
        accrued_checkpoint: int,
        fees_owed0: int,
        fees_owed1: int,
    ) -> None:
        """Append the collect plan audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_COLLECT_PLANNED,
            LpCollectPlannedPayload(
                mode=mode,
                symbol=symbol,
                pool_address=observation.pool_address,
                nfpm_address=observation.nfpm_address,
                gauge_address=observation.gauge_address,
                token_id=token_id,
                staked=staked,
                accrued_aero_earned_units=accrued_earned,
                accrued_aero_checkpoint_units=accrued_checkpoint,
                fees_owed0_units=fees_owed0 if not staked else 0,
                fees_owed1_units=fees_owed1 if not staked else 0,
            ),
            self._now(),
        )

    def _record_recenter_plan(
        self,
        mode: ExecutionMode,
        symbol: str,
        observation: LpPoolObservation,
        token_id: int,
        staked: bool,
        budget_usdc: Decimal,
        projected_usdc_units: int,
        projected_stock_units: int,
        plan: LpMintPlan,
        restake_followup: str,
    ) -> None:
        """Append the recenter plan audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_RECENTER_PLANNED,
            LpRecenterPlannedPayload(
                mode=mode,
                symbol=symbol,
                pool_address=observation.pool_address,
                nfpm_address=observation.nfpm_address,
                gauge_address=observation.gauge_address,
                token_id=token_id,
                staked=staked,
                budget_usdc=budget_usdc,
                projected_usdc_units=projected_usdc_units,
                projected_stock_units=projected_stock_units,
                tick_lower=plan.position_range.tick_lower,
                tick_upper=plan.position_range.tick_upper,
                restake_followup=restake_followup,
            ),
            self._now(),
        )

    def _record_status(
        self,
        symbol: str,
        observation: LpPoolObservation,
        token_id: int,
        staked: bool,
        position_value_usdc: Decimal,
        quoted_emissions_apr: Decimal | None,
        aero_price_usdc: Decimal,
    ) -> None:
        """Append the position status audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_STATUS_REPORTED,
            LpStatusReportedPayload(
                mode=ExecutionMode.DRY_RUN,
                symbol=symbol,
                pool_address=observation.pool_address,
                token_id=token_id,
                staked=staked,
                position_value_usdc=position_value_usdc,
                quoted_emissions_apr=quoted_emissions_apr,
                aero_price_assumption_usdc=aero_price_usdc,
            ),
            self._now(),
        )

    def _record_build(
        self,
        mode: ExecutionMode,
        action: str,
        role: LpExecutionRole,
        built: BuiltSafeTransaction,
        calldata_digest: str,
        description: str,
        validation: SafeSignatureValidation,
        gas_estimate: int | None,
    ) -> None:
        """Append the build audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        self._audit_sink.append(
            AuditEventType.LP_TRANSACTION_BUILT,
            LpTransactionBuiltPayload(
                mode=mode,
                action=action,
                role=role,
                safe_tx_hash=built.safe_tx_hash,
                to_address=built.transaction.to_address,
                calldata_digest=calldata_digest,
                nonce=built.transaction.nonce,
                description=description,
                signature_verified=validation.verified,
                gas_estimate=gas_estimate,
            ),
            self._now(),
        )

    def _record_refusal(
        self,
        action: str,
        mode: ExecutionMode,
        error: LpExecutionRefusalError | LpPlanRefusalError,
        symbol: str | None,
    ) -> None:
        """Append the refusal audit event when a sink is configured."""
        if self._audit_sink is None:
            return
        code = str(error.code)
        self._audit_sink.append(
            AuditEventType.LP_REFUSED,
            LpRefusedPayload(
                action=action,
                mode=mode,
                code=code,
                plan_code=code if isinstance(error, LpPlanRefusalError) else "",
                message=str(error),
                symbol=symbol,
            ),
            self._now(),
        )


def _add_lp_symbol_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the symbol and output-format arguments shared by every subcommand.

    Args:
        parser: The subcommand parser receiving the shared arguments.
    """
    parser.add_argument(
        "--symbol",
        required=True,
        help="Registry-matched B20 stock symbol, like AAPLc.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete report as JSON instead of a summary.",
    )


def build_lp_argument_parser() -> argparse.ArgumentParser:
    """Build the LP command's argument parser.

    Returns:
        The configured parser for the aero-bot-lp command.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-lp",
        description=(
            "Manually build, validate, and execute hard-capped Slipstream LP "
            "lifecycle transactions for the canary Safe on Base. Dry runs "
            "never broadcast; execute commands refuse without an explicit "
            "broadcast confirmation and then deliver one audited Safe nonce "
            "at a time."
        ),
    )
    parser.add_argument(
        "--full-discovery",
        action="store_true",
        help=(
            "Ignore persisted pool pins and resolve every pool through the "
            "full Sugar enumeration this run; the pin cache re-arms on the "
            "next normal run."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser(
        "plan",
        help="Plan one capped mint against live discovery; nothing is built or signed.",
    )
    plan_subparsers = plan_parser.add_subparsers(dest="lifecycle", required=True)
    plan_mint_parser = plan_subparsers.add_parser(
        "mint",
        help="Run the pure planner over live discovery and the Safe's inventory.",
    )
    _add_lp_symbol_arguments(plan_mint_parser)
    plan_mint_parser.add_argument(
        "--amount",
        type=Decimal,
        required=True,
        help="Total USDC value the position commits; refused above the caps.",
    )
    plan_mint_parser.add_argument(
        "--width-ticks",
        type=int,
        default=None,
        help=(
            "Half width in tick spacings per side; required until the "
            "solver-derived width path lands."
        ),
    )
    dry_run_parser = subparsers.add_parser(
        "dry-run",
        help=(
            "Build, sign, and validate the Safe transaction sequence without broadcasting anything."
        ),
    )
    dry_run_subparsers = dry_run_parser.add_subparsers(dest="lifecycle", required=True)
    dry_run_mint_parser = dry_run_subparsers.add_parser(
        "mint",
        help="Build and validate the complete mint sequence; nothing is broadcast.",
    )
    _add_lp_symbol_arguments(dry_run_mint_parser)
    dry_run_mint_parser.add_argument(
        "--amount",
        type=Decimal,
        required=True,
        help="Total USDC value the position commits; refused above the caps.",
    )
    dry_run_mint_parser.add_argument(
        "--width-ticks",
        type=int,
        default=None,
        help=(
            "Half width in tick spacings per side; required until the "
            "solver-derived width path lands."
        ),
    )
    dry_run_mint_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign the dry run with a freshly generated throwaway key instead of "
            "the configured signing-key source; the signature check will honestly "
            "report rejection."
        ),
    )
    dry_run_stake_parser = dry_run_subparsers.add_parser(
        "stake",
        help="Build and validate the stake sequence; nothing is broadcast.",
    )
    _add_lp_symbol_arguments(dry_run_stake_parser)
    dry_run_stake_parser.add_argument(
        "--token-id",
        type=int,
        required=True,
        help=("Position NFT to stake; may be a not-yet-minted id for a pre-mint machinery proof."),
    )
    dry_run_stake_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign the dry run with a freshly generated throwaway key instead of "
            "the configured signing-key source; the signature check will honestly "
            "report rejection."
        ),
    )
    dry_run_unstake_parser = dry_run_subparsers.add_parser(
        "unstake",
        help="Build and validate the unstake sequence; nothing is broadcast.",
    )
    _add_lp_symbol_arguments(dry_run_unstake_parser)
    dry_run_unstake_parser.add_argument(
        "--token-id",
        type=int,
        required=True,
        help="Staked position NFT to unstake; refused when the gauge does not hold it.",
    )
    dry_run_unstake_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign the dry run with a freshly generated throwaway key instead of "
            "the configured signing-key source; the signature check will honestly "
            "report rejection."
        ),
    )
    dry_run_withdraw_parser = dry_run_subparsers.add_parser(
        "withdraw",
        help="Build and validate the full withdraw sequence; nothing is broadcast.",
    )
    _add_lp_symbol_arguments(dry_run_withdraw_parser)
    dry_run_withdraw_parser.add_argument(
        "--token-id",
        type=int,
        required=True,
        help="Unstaked position NFT to decrease fully and collect out.",
    )
    dry_run_withdraw_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign the dry run with a freshly generated throwaway key instead of "
            "the configured signing-key source; the signature check will honestly "
            "report rejection."
        ),
    )
    dry_run_collect_parser = dry_run_subparsers.add_parser(
        "collect",
        help="Build and validate the claim sequence; nothing is broadcast.",
    )
    _add_lp_symbol_arguments(dry_run_collect_parser)
    dry_run_collect_parser.add_argument(
        "--token-id",
        type=int,
        required=True,
        help=("Position NFT to collect: gauge getReward when staked, NFPM collect when not."),
    )
    dry_run_collect_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign the dry run with a freshly generated throwaway key instead of "
            "the configured signing-key source; the signature check will honestly "
            "report rejection."
        ),
    )
    dry_run_recenter_parser = dry_run_subparsers.add_parser(
        "recenter",
        help="Build and validate the full recenter batch; nothing is broadcast.",
    )
    _add_lp_symbol_arguments(dry_run_recenter_parser)
    dry_run_recenter_parser.add_argument(
        "--token-id",
        type=int,
        required=True,
        help="Old position NFT to exit, burn, and recycle into the fresh mint.",
    )
    dry_run_recenter_parser.add_argument(
        "--width-ticks",
        type=int,
        default=None,
        help=(
            "Half width in tick spacings per side for the fresh range; required until "
            "the solver-derived width path lands."
        ),
    )
    dry_run_recenter_parser.add_argument(
        "--amount",
        type=Decimal,
        default=None,
        help=("USDC budget for the fresh mint; omit to recycle the old position's snapshot value."),
    )
    dry_run_recenter_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign the dry run with a freshly generated throwaway key instead of "
            "the configured signing-key source; the signature check will honestly "
            "report rejection."
        ),
    )
    dry_run_exit_swap_parser = dry_run_subparsers.add_parser(
        "exit-swap",
        help="Build and validate the stock-to-USDC exit swap; nothing is broadcast.",
    )
    _add_lp_symbol_arguments(dry_run_exit_swap_parser)
    dry_run_exit_swap_parser.add_argument(
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
            "Build, validate, and broadcast the Safe transaction sequence one "
            "nonce at a time; refuses without --confirm-broadcast."
        ),
    )
    execute_subparsers = execute_parser.add_subparsers(dest="lifecycle", required=True)
    execute_mint_parser = execute_subparsers.add_parser(
        "mint",
        help="Build and broadcast the complete mint sequence.",
    )
    _add_lp_symbol_arguments(execute_mint_parser)
    execute_mint_parser.add_argument(
        "--amount",
        type=Decimal,
        required=True,
        help="Total USDC value the position commits; refused above the caps.",
    )
    execute_mint_parser.add_argument(
        "--width-ticks",
        type=int,
        default=None,
        help="Half width in tick spacings per side; required for the mint.",
    )
    execute_exit_swap_parser = execute_subparsers.add_parser(
        "exit-swap", help="Build and broadcast the stock-to-USDC exit swap."
    )
    _add_lp_symbol_arguments(execute_exit_swap_parser)
    execute_exit_swap_parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help=(
            "Sign with a freshly generated throwaway key; the live signature "
            "check will honestly reject it before anything broadcasts."
        ),
    )
    execute_exit_swap_parser.add_argument(
        "--confirm-broadcast",
        action="store_true",
        help=(
            "Explicit operator confirmation to broadcast; without it the "
            "command refuses before building anything."
        ),
    )
    for lifecycle_parser in (
        execute_mint_parser,
        execute_subparsers.add_parser("stake", help="Build and broadcast the stake sequence."),
        execute_subparsers.add_parser("unstake", help="Build and broadcast the unstake sequence."),
        execute_subparsers.add_parser(
            "withdraw", help="Build and broadcast the decrease-and-collect exit."
        ),
        execute_subparsers.add_parser(
            "collect", help="Build and broadcast the fee or emissions claim."
        ),
    ):
        if lifecycle_parser is not execute_mint_parser:
            _add_lp_symbol_arguments(lifecycle_parser)
            lifecycle_parser.add_argument(
                "--token-id",
                type=int,
                required=True,
                help="Position NFT the action targets.",
            )
        lifecycle_parser.add_argument(
            "--ephemeral-key",
            action="store_true",
            help=(
                "Sign with a freshly generated throwaway key; the live signature "
                "check will honestly reject it before anything broadcasts."
            ),
        )
        lifecycle_parser.add_argument(
            "--confirm-broadcast",
            action="store_true",
            help=(
                "Explicit operator confirmation to broadcast; without it the "
                "command refuses before building anything."
            ),
        )
    status_parser = subparsers.add_parser(
        "status",
        help="Observe one position read-only; nothing is built or signed.",
    )
    _add_lp_symbol_arguments(status_parser)
    status_parser.add_argument(
        "--token-id",
        type=int,
        required=True,
        help="Position NFT to report on, wherever its current owner holds it.",
    )
    status_parser.add_argument(
        "--aero-price",
        type=Decimal,
        default=None,
        help=(
            "Optional AERO price override in USDC for the quoted emissions APR; "
            "absent means the price is read live from Aerodrome's own "
            "USDC/AERO pool at the snapshot block."
        ),
    )
    status_parser.add_argument(
        "--entry-cost",
        type=Decimal,
        default=None,
        help="Optional entry cost basis in USDC for the unrealized P&L.",
    )
    return parser


def _print_execution_report(report: LpActionExecutionReport) -> None:
    """Print one execution report: its build evidence then every delivery."""
    build = report.build
    if isinstance(build, LpMintDryRunReport):
        _print_mint_dry_run(build)
    elif isinstance(build, LpStakeDryRunReport):
        _print_stake_dry_run(build)
    elif isinstance(build, LpUnstakeDryRunReport):
        _print_unstake_dry_run(build)
    elif isinstance(build, LpExitDryRunReport):
        _print_exit_dry_run(build)
    elif isinstance(build, LpCollectDryRunReport):
        _print_collect_dry_run(build)
    elif isinstance(build, LpExitSwapDryRunReport):
        _print_exit_swap_dry_run(build)
    for step in report.steps:
        line = (
            f"[{step.action}/{step.role.value}] Safe nonce {step.nonce}: "
            f"{step.status} {step.transaction_hash}"
        )
        if step.gas_used is not None:
            line += (
                f", {step.gas_used} gas at {step.effective_gas_price_wei} wei "
                f"({step.fee_wei} wei fee)"
            )
        print(line)
        if step.diagnostic:
            print(f"  {step.diagnostic}")
    if not report.completed:
        print(f"halted: {report.halted_reason}")


def _lp_progress(line: str) -> None:
    """Print one operator progress line on stderr.

    Progress lines never touch stdout so the machine-readable JSON output
    stays clean for scripted consumers.

    Args:
        line: One human-readable progress line from a long-running phase.
    """
    print(f"[aero-bot-lp] {line}", file=sys.stderr)


def _print_lp_plan(plan: LpMintPlan) -> None:
    """Print one mint plan's human summary.

    Args:
        plan: The capped mint plan being reported.
    """
    swap = plan.balancing_swap
    swap_note = (
        f"balancing swap {swap.usdc_in_units} raw USDC in for {swap.expected_stock_units} raw "
        f"units expected (impact {swap.modeled_impact_fraction:.6f}, "
        f"{swap.tranche_count} tranche(s))"
        if swap.required
        else "no balancing swap needed"
    )
    print(
        f"{plan.symbol} pool {plan.pool_address} (snapshot block {plan.snapshot_block}), "
        f"budget {plan.budget_usdc} USDC"
    )
    print(
        f"range [{plan.position_range.tick_lower}, {plan.position_range.tick_upper}) "
        f"({plan.position_range.half_width_ticks} ticks per side, "
        f"{plan.position_range.width_source.value})"
    )
    print(
        f"mint {plan.amounts.amount0_desired_units} + {plan.amounts.amount1_desired_units} "
        f"raw units, minimums {plan.amounts.amount0_min_units} + "
        f"{plan.amounts.amount1_min_units}; {swap_note}"
    )


def _print_built_lp(line_prefix: str, built: BuiltLpTransaction) -> None:
    """Print one built LP transaction's human summary.

    Args:
        line_prefix: Role label starting each line.
        built: The built transaction being reported.
    """
    print(f"{line_prefix} {built.description}")
    print(f"{line_prefix} safeTxHash {built.safe_tx_hash} (nonce {built.nonce})")
    print(f"{line_prefix} target {built.to_address}, calldata digest {built.calldata_digest}")
    estimate = (
        f"{built.gas_estimate} gas estimated"
        if built.gas_estimate is not None
        else f"no estimate: {built.gas_estimate_diagnostic}"
    )
    verdict = "accepted" if built.signature_verified else "REJECTED"
    print(f"{line_prefix} signature {verdict} by live checkSignatures, {estimate}")


def _print_mint_dry_run(report: LpMintDryRunReport) -> None:
    """Print one mint dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    _print_lp_plan(report.plan)
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        "nothing broadcast)"
    )
    print(
        f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei, "
        f"router allowance {report.router_usdc_allowance_units} raw USDC, NFPM allowances "
        f"{report.nfpm_usdc_allowance_units} USDC / {report.nfpm_stock_allowance_units} stock"
    )
    for transaction in report.transactions:
        _print_built_lp(f"[{transaction.role.value}]", transaction)
    print(f"build took {report.build_duration_ms} ms")


def _print_stake_dry_run(report: LpStakeDryRunReport) -> None:
    """Print one stake dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    print(
        f"{report.symbol} pool {report.pool_address}, NFPM {report.nfpm_address}, "
        f"gauge {report.gauge_address}, token {report.token_id}"
    )
    if report.token_owner_address is not None:
        print(f"ownerOf reports {report.token_owner_address}")
    else:
        print(f"ownerOf unavailable: {report.ownership_diagnostic}")
    if report.ownership_diagnostic and report.token_owner_address is not None:
        print(f"ownership diagnostic: {report.ownership_diagnostic}")
    if report.position is not None:
        print(
            f"position liquidity {report.position.liquidity}, fees "
            f"{report.position.tokens_owed0_units} + {report.position.tokens_owed1_units}"
        )
    else:
        print(f"position view unavailable: {report.position_diagnostic}")
    approval_state = (
        "already operator-approved"
        if report.gauge_operator_approved
        else "operator approval required"
    )
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        f"nothing broadcast), gauge {approval_state}"
    )
    print(f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei")
    for transaction in report.transactions:
        _print_built_lp(f"[{transaction.role.value}]", transaction)
    print(f"build took {report.build_duration_ms} ms")


def _print_penalty(penalty: LpPenaltyWindow) -> None:
    """Print one resolved penalty window's human summary.

    Args:
        penalty: The penalty window being reported.
    """
    state = "OPEN" if penalty.remaining_seconds > 0 else "clear"
    print(
        f"penalty window {state}: rate {penalty.penalty_rate_bps} bps, min stake "
        f"{penalty.min_stake_seconds}s, deposited at unix {penalty.deposit_timestamp}, "
        f"clears at unix {penalty.window_clears_at_timestamp} "
        f"({penalty.remaining_seconds}s remaining)"
    )


def _print_unstake_dry_run(report: LpUnstakeDryRunReport) -> None:
    """Print one unstake dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    print(
        f"{report.symbol} pool {report.pool_address}, gauge {report.gauge_address}, "
        f"token {report.token_id}"
    )
    print(
        f"position liquidity {report.position.liquidity}, fees "
        f"{report.position.tokens_owed0_units} + {report.position.tokens_owed1_units} raw units"
    )
    print(
        f"accrued AERO: {report.accrued_aero_earned_units} raw live, "
        f"{report.accrued_aero_checkpoint_units} raw checkpointed (the withdraw claims the live)"
    )
    _print_penalty(report.penalty)
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        "nothing broadcast)"
    )
    print(f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei")
    for transaction in report.transactions:
        _print_built_lp(f"[{transaction.role.value}]", transaction)
    for line in report.diagnostics:
        print(f"note: {line}")
    print(f"build took {report.build_duration_ms} ms")


def _print_exit_dry_run(report: LpExitDryRunReport) -> None:
    """Print one withdraw dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    print(
        f"{report.symbol} pool {report.pool_address}, NFPM {report.nfpm_address}, "
        f"token {report.token_id} ({report.range_state.value})"
    )
    print(
        f"full decrease returns {report.amount0_units} + {report.amount1_units} raw units, "
        f"minima {report.amount0_min_units} + {report.amount1_min_units}"
    )
    print(
        f"checkpointed fees {report.fees_owed0_units} + {report.fees_owed1_units} raw units "
        "sweep on the collect"
    )
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        "nothing broadcast)"
    )
    print(f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei")
    for transaction in report.transactions:
        _print_built_lp(f"[{transaction.role.value}]", transaction)
    for line in report.diagnostics:
        print(f"note: {line}")
    print(f"build took {report.build_duration_ms} ms")


def _print_collect_dry_run(report: LpCollectDryRunReport) -> None:
    """Print one collect dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    path = "gauge getReward" if report.staked else "NFPM collect"
    print(f"{report.symbol} pool {report.pool_address}, token {report.token_id}, claim path {path}")
    if report.staked:
        print(
            f"accrued AERO: {report.accrued_aero_earned_units} raw live, "
            f"{report.accrued_aero_checkpoint_units} raw checkpointed"
        )
        penalty = report.penalty
        if penalty is not None:
            _print_penalty(penalty)
    else:
        print(f"checkpointed fees {report.fees_owed0_units} + {report.fees_owed1_units} raw units")
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        "nothing broadcast)"
    )
    print(f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei")
    for transaction in report.transactions:
        _print_built_lp(f"[{transaction.role.value}]", transaction)
    for line in report.diagnostics:
        print(f"note: {line}")
    print(f"build took {report.build_duration_ms} ms")


def _print_exit_swap_dry_run(report: LpExitSwapDryRunReport) -> None:
    """Print one exit-swap dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    print(
        f"{report.symbol} pool {report.pool_address} at block {report.snapshot_block}, "
        f"stock {report.stock_token_address}"
    )
    print(
        f"selling the entire {report.stock_balance_units} raw stock balance at "
        f"{report.price_usdc_per_stock} USDC per stock: quoted "
        f"{report.expected_out_units} raw USDC, minimum {report.amount_out_min_units}"
    )
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        "nothing broadcast)"
    )
    print(f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei")
    for transaction in report.transactions:
        _print_built_lp(f"[{transaction.role.value}]", transaction)
    for line in report.diagnostics:
        print(f"note: {line}")
    print(f"build took {report.build_duration_ms} ms")


def _print_recenter_dry_run(report: LpRecenterDryRunReport) -> None:
    """Print one recenter dry-run report's human summary.

    Args:
        report: The dry-run report being printed.
    """
    key_note = "ephemeral" if report.ephemeral_key else "configured source"
    print(
        f"{report.symbol} pool {report.pool_address}, token {report.token_id} "
        f"({'staked' if report.staked else 'unstaked'}, {report.range_state.value})"
    )
    print(
        f"full decrease returns {report.amount0_units} + {report.amount1_units} raw units; "
        f"checkpointed fees {report.fees_owed0_units} + {report.fees_owed1_units} raw units"
    )
    print(
        f"projected post-exit inventory: {report.projected_usdc_units} raw USDC + "
        f"{report.projected_stock_units} raw stock"
    )
    _print_lp_plan(report.plan)
    print(f"restake follow-up: {report.restake_followup}")
    print(
        f"safe {report.safe_address}, relayer {report.relayer_address} ({key_note} key, "
        "nothing broadcast)"
    )
    print(f"gas price {report.gas_price_wei} wei, Safe ETH {report.safe_eth_wei} wei")
    for transaction in report.transactions:
        _print_built_lp(f"[{transaction.role.value}]", transaction)
    print(f"build took {report.build_duration_ms} ms")


def _print_position_status(report: LpPositionStatusReport) -> None:
    """Print one read-only position report's human summary.

    Args:
        report: The position report being printed.
    """
    custody = "staked in the gauge" if report.staked else "held by the Safe"
    print(
        f"{report.symbol} pool {report.pool_address}, NFPM {report.nfpm_address}, "
        f"gauge {report.gauge_address}, token {report.token_id} ({custody})"
    )
    print(
        f"owner {report.token_owner_address}, snapshot block {report.snapshot_block}, "
        f"current tick {report.current_tick} ({report.range_state.value})"
    )
    print(
        f"composition {report.amount0_units} + {report.amount1_units} raw units valued "
        f"{report.token0_value_usdc} + {report.token1_value_usdc} = "
        f"{report.position_value_usdc} USDC"
    )
    print(f"checkpointed fees {report.fees_owed0_units} + {report.fees_owed1_units} raw units")
    if report.accrued_aero_earned_units is not None:
        print(
            f"accrued AERO: {report.accrued_aero_earned_units} raw live, "
            f"{report.accrued_aero_checkpoint_units} raw checkpointed"
        )
        penalty = report.penalty
        if penalty is not None:
            _print_penalty(penalty)
    if report.quoted_emissions_apr is not None:
        print(
            f"quoted emissions APR {report.quoted_emissions_apr:.6f} "
            f"({report.quoted_emissions_apr * Decimal(100):.4f}%) at assumed AERO "
            f"{report.aero_price_assumption_usdc} USDC"
        )
    else:
        print("no quoted emissions APR: the inputs are absent")
    if report.apr_diagnostic:
        print(f"apr note: {report.apr_diagnostic}")
    if report.unrealized_pnl_usdc is not None:
        print(
            f"unrealized P&L {report.unrealized_pnl_usdc} USDC against entry cost "
            f"{report.entry_cost_usdc} USDC"
        )
    else:
        print(f"no P&L: {report.pnl_diagnostic}")
    for line in report.diagnostics:
        print(f"note: {line}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one manual LP lifecycle command.

    The RPC endpoint, Sugar address, and audit database come from the
    application settings, the Safe address defaults to the canary deployment
    behind ``AERO_BOT_SAFE_ADDRESS``, and the signing key comes from the
    the platform key source (macOS Keychain, sealed environment variable,
    or owner-only key file - see ``aero_bot.signing_key``) or the
    command's explicit ephemeral flag. Dry-run
    subcommands stop at building and validation; execute subcommands refuse
    without ``--confirm-broadcast`` and then broadcast one audited Safe nonce
    at a time.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on success, one on failure, two on any
        refusal.
    """
    settings = Settings()
    parser = build_lp_argument_parser()
    arguments = parser.parse_args(argv)
    safe_address = os.environ.get(SAFE_ADDRESS_ENV, DEFAULT_CANARY_SAFE_ADDRESS)
    sources = LiveExecutionSources(
        rpc_url=settings.base_rpc_url,
        sugar_address=settings.lp_sugar_address,
        fallback_rpc_urls=EXECUTE_RECEIPT_ENDPOINT_URLS,
        progress=_lp_progress,
    )
    rpc = ExecutorRpcBackend(
        rpc_url=settings.base_rpc_url,
        fallback_rpc_urls=EXECUTE_RECEIPT_ENDPOINT_URLS,
        progress=_lp_progress,
    )
    safe_rpc = SafeTransactionRpcBackend(
        rpc_url=settings.base_rpc_url,
        safe_address=safe_address,
        fallback_rpc_urls=EXECUTE_RECEIPT_ENDPOINT_URLS,
    )
    # The pin store arms the known-pool fast path; --full-discovery bypasses
    # it for one run by constructing the executor without pins.
    pool_pin_store = (
        None if arguments.full_discovery else LpPoolPinStore(settings.lp_pool_pins_path)
    )
    try:
        audit_store = AuditStore(settings.audit_database_path)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"the audit store is unavailable: {error}", file=sys.stderr)
        return EXIT_FAILURE
    receipt_backends = [rpc] + [
        ExecutorRpcBackend(rpc_url=url)
        for url in EXECUTE_RECEIPT_ENDPOINT_URLS
        if url != settings.base_rpc_url
    ]
    executor = LpLifecycleExecutor(
        policy=LpSafeExecutionPolicy(),
        plan_policy=LpExecutionPolicy(),
        safe_address=safe_address,
        sources=sources,
        rpc=rpc,
        safe_rpc=safe_rpc,
        audit_sink=audit_store,
        receipt_backends=receipt_backends,
        pool_pin_store=pool_pin_store,
    )
    try:
        if arguments.command == "plan" and arguments.lifecycle == "mint":
            if arguments.amount <= 0:
                parser.error("--amount must be positive")
            plan = executor.plan_mint(arguments.symbol, arguments.amount, arguments.width_ticks)
            if arguments.json:
                print(plan.model_dump_json(indent=2))
            else:
                _print_lp_plan(plan)
            return EXIT_OK
        if arguments.command == "dry-run" and arguments.lifecycle == "mint":
            if arguments.amount <= 0:
                parser.error("--amount must be positive")
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            report = executor.dry_run_mint(
                arguments.symbol,
                arguments.amount,
                arguments.width_ticks,
                key_bytes,
                ephemeral_key=ephemeral,
            )
            if arguments.json:
                print(report.model_dump_json(indent=2))
            else:
                _print_mint_dry_run(report)
            return EXIT_OK
        if arguments.command == "dry-run" and arguments.lifecycle == "stake":
            if arguments.token_id < 0:
                parser.error("--token-id must be non-negative")
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            stake_report = executor.dry_run_stake(
                arguments.symbol, arguments.token_id, key_bytes, ephemeral_key=ephemeral
            )
            if arguments.json:
                print(stake_report.model_dump_json(indent=2))
            else:
                _print_stake_dry_run(stake_report)
            return EXIT_OK
        if arguments.command == "dry-run" and arguments.lifecycle == "unstake":
            if arguments.token_id < 0:
                parser.error("--token-id must be non-negative")
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            unstake_report = executor.dry_run_unstake(
                arguments.symbol, arguments.token_id, key_bytes, ephemeral_key=ephemeral
            )
            if arguments.json:
                print(unstake_report.model_dump_json(indent=2))
            else:
                _print_unstake_dry_run(unstake_report)
            return EXIT_OK
        if arguments.command == "dry-run" and arguments.lifecycle == "withdraw":
            if arguments.token_id < 0:
                parser.error("--token-id must be non-negative")
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            exit_report = executor.dry_run_exit(
                arguments.symbol, arguments.token_id, key_bytes, ephemeral_key=ephemeral
            )
            if arguments.json:
                print(exit_report.model_dump_json(indent=2))
            else:
                _print_exit_dry_run(exit_report)
            return EXIT_OK
        if arguments.command == "dry-run" and arguments.lifecycle == "collect":
            if arguments.token_id < 0:
                parser.error("--token-id must be non-negative")
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            collect_report = executor.dry_run_collect(
                arguments.symbol, arguments.token_id, key_bytes, ephemeral_key=ephemeral
            )
            if arguments.json:
                print(collect_report.model_dump_json(indent=2))
            else:
                _print_collect_dry_run(collect_report)
            return EXIT_OK
        if arguments.command == "dry-run" and arguments.lifecycle == "exit-swap":
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            exit_swap_report = executor.dry_run_exit_swap(
                arguments.symbol, key_bytes, ephemeral_key=ephemeral
            )
            if arguments.json:
                print(exit_swap_report.model_dump_json(indent=2))
            else:
                _print_exit_swap_dry_run(exit_swap_report)
            return EXIT_OK
        if arguments.command == "dry-run" and arguments.lifecycle == "recenter":
            if arguments.token_id < 0:
                parser.error("--token-id must be non-negative")
            if arguments.amount is not None and arguments.amount <= 0:
                parser.error("--amount must be positive")
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            recenter_report = executor.dry_run_recenter(
                arguments.symbol,
                arguments.token_id,
                arguments.width_ticks,
                arguments.amount,
                key_bytes,
                ephemeral_key=ephemeral,
            )
            if arguments.json:
                print(recenter_report.model_dump_json(indent=2))
            else:
                _print_recenter_dry_run(recenter_report)
            return EXIT_OK
        if arguments.command == "execute":
            if arguments.lifecycle == "mint":
                if arguments.amount <= 0:
                    parser.error("--amount must be positive")
            elif arguments.lifecycle == "exit-swap":
                pass
            else:
                if arguments.token_id < 0:
                    parser.error("--token-id must be non-negative")
            if arguments.ephemeral_key:
                key_bytes = bytes(Account.create().key)
                ephemeral = True
            else:
                key_bytes = load_signing_key_source().load_signing_key()
                ephemeral = False
            try:
                with exclusive_execution_lock(
                    settings.audit_database_path.parent / "execution.lock"
                ):
                    if arguments.lifecycle == "mint":
                        execution_report = executor.execute_mint(
                            arguments.symbol,
                            arguments.amount,
                            arguments.width_ticks,
                            key_bytes,
                            confirm_broadcast=arguments.confirm_broadcast,
                            ephemeral_key=ephemeral,
                        )
                    elif arguments.lifecycle == "stake":
                        execution_report = executor.execute_stake(
                            arguments.symbol,
                            arguments.token_id,
                            key_bytes,
                            confirm_broadcast=arguments.confirm_broadcast,
                            ephemeral_key=ephemeral,
                        )
                    elif arguments.lifecycle == "unstake":
                        execution_report = executor.execute_unstake(
                            arguments.symbol,
                            arguments.token_id,
                            key_bytes,
                            confirm_broadcast=arguments.confirm_broadcast,
                            ephemeral_key=ephemeral,
                        )
                    elif arguments.lifecycle == "withdraw":
                        execution_report = executor.execute_withdraw(
                            arguments.symbol,
                            arguments.token_id,
                            key_bytes,
                            confirm_broadcast=arguments.confirm_broadcast,
                            ephemeral_key=ephemeral,
                        )
                    elif arguments.lifecycle == "collect":
                        execution_report = executor.execute_collect(
                            arguments.symbol,
                            arguments.token_id,
                            key_bytes,
                            confirm_broadcast=arguments.confirm_broadcast,
                            ephemeral_key=ephemeral,
                        )
                    else:
                        execution_report = executor.execute_exit_swap(
                            arguments.symbol,
                            key_bytes,
                            confirm_broadcast=arguments.confirm_broadcast,
                            ephemeral_key=ephemeral,
                        )
            except ExecutionLockUnavailableError as error:
                print(f"execute refused: {error}", file=sys.stderr)
                return EXIT_REFUSED
            if arguments.json:
                print(execution_report.model_dump_json(indent=2))
            else:
                _print_execution_report(execution_report)
            if any(step.status == "failed" for step in execution_report.steps):
                return EXIT_FAILURE
            return EXIT_OK
        if arguments.command == "status":
            if arguments.token_id < 0:
                parser.error("--token-id must be non-negative")
            if arguments.aero_price is not None and arguments.aero_price <= 0:
                parser.error("--aero-price must be positive")
            if arguments.entry_cost is not None and arguments.entry_cost < 0:
                parser.error("--entry-cost must be non-negative")
            status_report = executor.position_status(
                arguments.symbol,
                arguments.token_id,
                arguments.aero_price,
                entry_cost_usdc=arguments.entry_cost,
            )
            if arguments.json:
                print(status_report.model_dump_json(indent=2))
            else:
                _print_position_status(status_report)
            return EXIT_OK
        parser.error(f"unknown command combination {arguments.command}/{arguments.lifecycle}")
        return EXIT_FAILURE
    except (LpExecutionRefusalError, LpPlanRefusalError) as error:
        code = str(getattr(error, "code", "unknown"))
        print(f"refused [{code}]: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
        print(f"failed: {error}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
