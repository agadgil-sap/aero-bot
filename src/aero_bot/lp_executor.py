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
composes the NFPM operator approval for the gauge and the gauge deposit. Each
transaction is signed over its EIP-712 SafeTx hash, proven read-only against
the live Safe with ``checkSignatures``, gas-estimated, and audited; nothing is
broadcast by anything in this module's current surface.

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
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Annotated, Literal

from eth_account import Account
from eth_utils.crypto import keccak
from pydantic import BaseModel, Field, field_validator, model_validator

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.config import Settings
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.executor import (
    AERODROME_ROUTER_ADDRESS,
    APPROVAL_CAP_CEILING_USDC,
    DEFAULT_APPROVAL_STANDING_CAP_USDC,
    DEFAULT_CANARY_SAFE_ADDRESS,
    DEFAULT_GAS_PRICE_CAP_WEI,
    DEFAULT_QUOTE_MAX_AGE_SECONDS,
    DEFAULT_SAFE_ETH_FLOOR_WEI,
    ERC20_BALANCE_OF_SELECTOR,
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
from aero_bot.keychain import KeychainKeySource
from aero_bot.lp_calldata import (
    LpMintParams,
    LpPositionView,
    build_gauge_deposit_calldata,
    build_lp_mint_calldata,
    build_lp_positions_read_calldata,
    build_set_approval_for_all_calldata,
    decode_lp_positions_view,
)
from aero_bot.lp_plan import (
    QUOTE_TOKEN_DECIMALS,
    LpExecutionPolicy,
    LpMintPlan,
    LpPlanRefusalError,
    LpPoolObservation,
    MintDirective,
    SafeInventory,
    WidthSource,
    plan_mint_entry,
)
from aero_bot.registry import B20AssetListing, RegistryStatus
from aero_bot.safe_tx import (
    BuiltSafeTransaction,
    SafeSignatureValidation,
    SafeTransaction,
    SafeTransactionRpcBackend,
    build_exec_transaction_calldata,
    build_safe_transaction,
    sign_safe_tx_hash,
)
from aero_bot.venues import BASE_USDC_ADDRESS, PoolDiscoveryStatus

# keccak256("ownerOf(uint256)")[0:4], the ERC721 ownership read.
ERC721_OWNER_OF_SELECTOR = "6352211e"
# keccak256("isApprovedForAll(address,address)")[0:4], the operator read.
ERC721_IS_APPROVED_FOR_ALL_SELECTOR = "e985e9c5"
# The LP deadline sits eight minutes past its build time, mirroring the swap.
LP_DEADLINE_SECONDS = 8 * 60
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
        DEFAULT_APPROVAL_STANDING_CAP_USDC
    )

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
        if self.router_allowance_standing_cap_usdc > APPROVAL_CAP_CEILING_USDC:
            raise ValueError(
                f"router_allowance_standing_cap_usdc {self.router_allowance_standing_cap_usdc}"
                f" exceeds the documented bound of {APPROVAL_CAP_CEILING_USDC} USDC; the "
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
    # The balancing swap's exact USDC input, zero when absent.
    swap_usdc_in_units: Annotated[int, Field(ge=0)]
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
            timer: Injected monotonic clock for duration metrics.
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
            return self._dry_run_mint(symbol, budget_usdc, width_spacings, key_bytes, ephemeral_key)
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
            return self._dry_run_stake(symbol, token_id, key_bytes, ephemeral_key)
        except LpExecutionRefusalError as error:
            self._record_refusal("stake", ExecutionMode.DRY_RUN, error, symbol)
            raise

    def _dry_run_mint(
        self,
        symbol: str,
        budget_usdc: Decimal,
        width_spacings: int | None,
        key_bytes: bytes,
        ephemeral_key: bool,
    ) -> LpMintDryRunReport:
        """Build, sign, validate, and estimate the complete mint sequence."""
        build_started = self._timer()
        context = self._resolve_mint_context(symbol, width_spacings)
        plan = self._plan_from_context(context, budget_usdc)
        self._record_mint_plan(ExecutionMode.DRY_RUN, plan)
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
            nfpm_usdc_allowance,
            nfpm_stock_allowance,
            deadline,
        )
        transactions = self._build_steps(steps, live_nonce, key_bytes, "mint")
        return LpMintDryRunReport(
            plan=plan,
            safe_address=self._safe_address,
            relayer_address=normalize_evm_address(Account.from_key(key_bytes).address),
            ephemeral_key=ephemeral_key,
            router_usdc_allowance_units=router_allowance,
            nfpm_usdc_allowance_units=nfpm_usdc_allowance,
            nfpm_stock_allowance_units=nfpm_stock_allowance,
            gas_price_wei=gas_price,
            safe_eth_wei=safe_eth,
            transactions=transactions,
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
        )

    def _compose_mint_steps(
        self,
        context: _LpMintContext,
        plan: LpMintPlan,
        router_allowance_units: int,
        nfpm_usdc_allowance_units: int,
        nfpm_stock_allowance_units: int,
        deadline: int,
    ) -> list[_LpStepSpec]:
        """Compose the mint sequence's inner calls in execution order.

        Args:
            context: The resolved observation and inventory context.
            plan: The capped mint plan being composed.
            router_allowance_units: The live USDC allowance to the router.
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
            amount_out_min = int(
                (Decimal(swap.expected_stock_units) * (Decimal(1) - tolerance)).to_integral_value(
                    rounding=ROUND_FLOOR
                )
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
                        build_swap_path(BASE_USDC_ADDRESS, stock_token, observation.tick_spacing),
                        deadline,
                    ),
                    description=(
                        f"swap {swap.usdc_in_units} raw USDC for at least {amount_out_min} raw "
                        f"{context.listing.symbol} covering the {swap.stock_shortfall_units}-unit "
                        "shortfall"
                    ),
                )
            )
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

    def _dry_run_stake(
        self, symbol: str, token_id: int, key_bytes: bytes, ephemeral_key: bool
    ) -> LpStakeDryRunReport:
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
            ExecutionMode.DRY_RUN,
            listing.symbol,
            observation,
            token_id,
            owner,
            operator_approved,
        )
        transactions = self._build_steps(steps, live_nonce, key_bytes, "stake")
        return LpStakeDryRunReport(
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
            transactions=transactions,
            caps_enforced=tuple(caps),
            build_duration_ms=self._milliseconds_since(build_started),
        )

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
                "no --width-ticks override was supplied and the solver-derived width path is "
                "not wired yet (its emissions-APR input is known understated until the APR "
                "convention fix lands); pass an explicit half width in tick spacings per side",
            )
        usdc_balance = self._rpc.fetch_token_balance(BASE_USDC_ADDRESS, self._safe_address)
        stock_token = (
            observation.token0_address
            if observation.stock_is_token0
            else observation.token1_address
        )
        stock_balance = self._rpc.fetch_token_balance(stock_token, self._safe_address)
        # The total-exposure cap stays honest by refusing once the Safe holds
        # position NFTs this executor cannot yet value live.
        held_positions = self._read_word(
            observation.nfpm_address,
            self._erc20_balance_calldata(self._safe_address),
            "NFPM balanceOf()",
        )
        if held_positions > 0:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.UNTRACKED_EXISTING_POSITIONS,
                f"the Safe already holds {held_positions} position NFT(s) on this NFPM and no "
                "live position-value read exists yet, so the total pilot exposure cap cannot "
                "be evaluated honestly; refuse until that read lands",
            )
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
            snapshot_block=discovery.snapshot_block,
            observed_at=observed_at,
        )
        return listing, observation, caps

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
        self, steps: Sequence[_LpStepSpec], live_nonce: int, key_bytes: bytes, action: str
    ) -> tuple[BuiltLpTransaction, ...]:
        """Build, sign, validate, and estimate every step in sequence order.

        Args:
            steps: The composed inner calls in execution order.
            live_nonce: The Safe nonce the first transaction occupies.
            key_bytes: Exactly 32 raw signing-key bytes.
            action: The lifecycle action the sequence belongs to.

        Returns:
            The fully built transaction reports in execution order.
        """
        built: list[BuiltLpTransaction] = []
        for index, step in enumerate(steps):
            built.append(
                self._build_step(
                    step,
                    nonce=live_nonce + index,
                    key_bytes=key_bytes,
                    action=action,
                    sequenced_behind_predecessors=index > 0,
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
    ) -> BuiltLpTransaction:
        """Build, sign, validate, and estimate one LP Safe transaction.

        Args:
            step: The composed inner call being built.
            nonce: The Safe nonce this transaction occupies.
            key_bytes: Exactly 32 raw signing-key bytes.
            action: The lifecycle action this transaction belongs to.
            sequenced_behind_predecessors: Whether earlier transactions of the
                same sequence precede this one, so a reverting estimate is the
                expected pre-execution answer rather than an anomaly.

        Returns:
            The fully built transaction report; nothing was broadcast.
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
            ExecutionMode.DRY_RUN,
            action,
            step.role,
            built,
            calldata_digest,
            step.description,
            validation,
            gas_estimate,
        )
        return BuiltLpTransaction(
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
                swap_usdc_in_units=plan.balancing_swap.usdc_in_units,
                swap_modeled_impact_fraction=plan.balancing_swap.modeled_impact_fraction,
                caps_enforced=plan.caps_enforced,
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
            "Manually build, validate, and plan hard-capped Slipstream LP "
            "lifecycle transactions for the canary Safe on Base. This release "
            "builds and validates only; nothing is ever broadcast."
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
            "the Keychain key; the signature check will honestly report rejection."
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
            "the Keychain key; the signature check will honestly report rejection."
        ),
    )
    return parser


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
    key_note = "ephemeral" if report.ephemeral_key else "Keychain"
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
    key_note = "ephemeral" if report.ephemeral_key else "Keychain"
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run one manual LP lifecycle command.

    The RPC endpoint, Sugar address, and audit database come from the
    application settings, the Safe address defaults to the canary deployment
    behind ``AERO_BOT_SAFE_ADDRESS``, and the signing key comes from the
    macOS Keychain or the dry run's explicit ephemeral flag. This release has
    no broadcast path: every subcommand stops at building and validation.

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
    )
    rpc = ExecutorRpcBackend(rpc_url=settings.base_rpc_url)
    safe_rpc = SafeTransactionRpcBackend(rpc_url=settings.base_rpc_url, safe_address=safe_address)
    try:
        audit_store = AuditStore(settings.audit_database_path)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"the audit store is unavailable: {error}", file=sys.stderr)
        return EXIT_FAILURE
    executor = LpLifecycleExecutor(
        policy=LpSafeExecutionPolicy(),
        plan_policy=LpExecutionPolicy(),
        safe_address=safe_address,
        sources=sources,
        rpc=rpc,
        safe_rpc=safe_rpc,
        audit_sink=audit_store,
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
                key_bytes = KeychainKeySource.from_environment().load_signing_key()
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
                key_bytes = KeychainKeySource.from_environment().load_signing_key()
                ephemeral = False
            stake_report = executor.dry_run_stake(
                arguments.symbol, arguments.token_id, key_bytes, ephemeral_key=ephemeral
            )
            if arguments.json:
                print(stake_report.model_dump_json(indent=2))
            else:
                _print_stake_dry_run(stake_report)
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
