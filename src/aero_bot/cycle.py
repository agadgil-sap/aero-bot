"""One scheduled decision cycle: reconcile, decide, act within the locked caps.

The ``aero-bot-cycle`` command runs exactly one decision cycle for one
registry symbol and exits - the systemd timer (not an in-process scheduler)
decides when cycles happen. One cycle, in fixed order:

1. **Reconcile first.** The Safe's live USDC, stock, and relayer-ETH
   balances, the complete held-NFT inventory on the pool's NFPM, and - when a
   position is tracked - its live custody, value, and P&L against entry. The
   chain, never memory, is the source of truth: a crashed cycle reconciles
   toward on-chain reality and never double-acts, because every broadcast
   still flows through the audited per-nonce executor sequences, and a
   crashed entry is adopted back only through the audit chain's own
   confirmed-mint evidence.
2. **Decide.** The complete locked policy engine runs over one live
   observation assembled exactly like ``aero-bot-decide``, threading the
   reconciled policy state (open position, held inventory, cooldowns, the
   daily-loss anchor). Flat verdicts inside closed-market windows are the
   doctrine working, not failures.
3. **Act.** Only when the policy authorizes an action does the cycle execute
   it, and only through the proven audited executor surfaces (mint, stake,
   unstake, withdraw, exit swap) inside the existing caps and refusal
   catalog. Any refusal or failed delivery halts the cycle; an out-of-band
   condition refuses, records, and stops the cycle without acting.

The cycle's own memory is one self-healing JSON book beside the audit store;
the append-only audit chain stays the authoritative record of everything
broadcast. A dry run reconciles and decides but never loads the signing key
and never builds anything.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from aero_bot.audit import AuditEventType, AuditRecord, AuditStore
from aero_bot.config import Settings
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.executor import (
    DEFAULT_CANARY_SAFE_ADDRESS,
    SAFE_ADDRESS_ENV,
    ExecutionUnavailableError,
)
from aero_bot.lp_executor import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REFUSED,
    NFPM_INCREASE_LIQUIDITY_TOPIC0,
    LpActionExecutionReport,
    LpExecutionRefusalError,
    LpPositionStatusReport,
    LpSafePositionsSnapshot,
)
from aero_bot.lp_plan import LpPlanRefusalError
from aero_bot.policy import (
    LOCKED_POLICY_PARAMETERS,
    MATH_PRECISION,
    TICK_PRICE_RATIO,
    AlignedPriceRange,
    HeldInventory,
    PolicyActionKind,
    PolicyEngine,
    PolicyOutcome,
    PolicyPosition,
    PolicyState,
    evaluate_event_window,
    load_event_calendar,
)
from aero_bot.strategy import StrategyDecisionReport, StrategySources, assemble_observation
from aero_bot.venues import BASE_USDC_ADDRESS, PoolCandidate

# Environment variable carrying an explicit cycle-state path override.
CYCLE_STATE_PATH_ENV = "AERO_BOT_CYCLE_STATE_PATH"
# Environment variable carrying an optional injected reference quote.
CYCLE_REFERENCE_PRICE_ENV = "AERO_BOT_CYCLE_REFERENCE_PRICE_USDC"
# Environment variable carrying the relayer's public address for dry runs.
RELAYER_ADDRESS_ENV = "AERO_BOT_RELAYER_ADDRESS"
# The policy day boundary follows the engine's America/New_York convention.
POLICY_TIMEZONE = ZoneInfo("America/New_York")


def _cycle_progress(line: str) -> None:
    """Print one operator progress line on stderr.

    Progress lines never touch stdout so the machine-readable JSON report
    stays clean, and they land in the systemd journal beside it.

    Args:
        line: One human-readable progress line from a long-running phase.
    """
    print(f"[aero-bot-cycle] {line}", file=sys.stderr)


class CycleMode(StrEnum):
    """Identify whether one cycle may broadcast."""

    # Reconcile and decide only; no key is ever loaded.
    DRY_RUN = "dry_run"
    # Execute policy-authorized actions through the audited surfaces.
    LIVE = "live"


class TrackedPosition(BaseModel):
    """Carry the cycle's record of the one position it manages."""

    # Frozen strict fields keep one tracked position coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the position lives in.
    symbol: str
    # The position NFT id on the pool's NFPM.
    token_id: Annotated[int, Field(ge=0)]
    # The pool the position was minted in.
    pool_address: EvmAddress
    # The committed USDC value at entry, the P&L basis.
    committed_usd: Annotated[Decimal, Field(gt=0)]
    # When the position was entered, timezone-aware.
    entered_at: datetime


class HeldInventoryRecord(BaseModel):
    """Carry the cycle's record of stock held unsold after a stale-low burn."""

    # Frozen strict fields keep one held-inventory record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol being held.
    symbol: str
    # The stock token contract held.
    token_address: EvmAddress
    # The stock quantity held, in whole tokens.
    stock_quantity: Annotated[Decimal, Field(gt=0)]
    # When the inventory was taken on, timezone-aware.
    held_since: datetime


class CycleStateBook(BaseModel):
    """Carry every engine-owned fact the next cycle must thread forward."""

    # The tracked open position, or None while flat.
    position: TrackedPosition | None = None
    # Stock held unsold after a stale-low burn, or None.
    held_inventory: HeldInventoryRecord | None = None
    # Re-entry stays blocked until this instant after stop or dilution exits.
    reentry_blocked_until: datetime | None = None
    # The America/New_York day the day-start equity anchor belongs to.
    day: date | None = None
    # Day-start equity anchors the five-percent daily loss halt.
    day_start_equity_usd: Decimal | None = None
    # The day a daily loss halt tripped, if any.
    halted_day: date | None = None
    # When this book was last persisted, timezone-aware.
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CycleStateStore:
    """Persist the cycle book as one self-healing JSON file.

    The store mirrors the pool-pin store's discipline: any load failure
    yields an empty book (the chain reconciliation rebuilds truth), and every
    save is an atomic rewrite so a crash can never leave a torn file.
    """

    def __init__(self, path: Path) -> None:
        """Point the store at one JSON path.

        Args:
            path: Absolute path of the cycle book file.
        """
        self._path = path.expanduser()

    @property
    def path(self) -> Path:
        """Return the book path; paths are not secrets."""
        return self._path

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] = os.environ, settings: Settings | None = None
    ) -> "CycleStateStore":
        """Build the store from the environment and application settings.

        Args:
            environ: Environment mapping carrying the optional path override.
            settings: Application settings; None constructs fresh ones.

        Returns:
            The store rooted at the override or beside the audit store.
        """
        resolved = settings if settings is not None else Settings()
        override = environ.get(CYCLE_STATE_PATH_ENV, "").strip()
        base = (
            Path(override) if override else resolved.audit_database_path.parent / "cycle_state.json"
        )
        return cls(base)

    def load(self) -> CycleStateBook:
        """Load the book, yielding an empty book on any failure.

        Returns:
            The persisted book, or a fresh empty book when nothing readable
            exists - the reconciliation rebuilds everything else.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return CycleStateBook()
        try:
            return CycleStateBook.model_validate(raw)
        except ValueError:
            return CycleStateBook()

    def save(self, book: CycleStateBook) -> None:
        """Atomically persist one book.

        Args:
            book: The complete book to persist.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_text(
            json.dumps(json.loads(book.model_dump_json()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)


class CycleActionRecord(BaseModel):
    """Record one executed (or refused) action inside one cycle."""

    # Frozen strict fields keep one action record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The mapped lifecycle action that ran, like mint or exit_swap.
    action: str
    # completed, refused, or failed.
    status: str
    # Every delivery transaction hash the action broadcast, in order.
    transaction_hashes: Annotated[tuple[str, ...], Field(min_length=0)] = ()
    # Total delivery fees paid, in wei.
    fee_wei: Annotated[int, Field(ge=0)] = 0
    # The refusal's catalog code when status is refused, else empty.
    refusal_code: str = ""
    # Human-readable evidence for the outcome.
    diagnostic: str = ""


class CycleReconciliation(BaseModel):
    """Carry the complete pre-decision on-chain state one cycle acts on."""

    # Frozen strict fields keep one reconciliation coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched symbol the cycle manages.
    symbol: str
    # The Safe's live USDC balance in raw units.
    safe_usdc_units: Annotated[int, Field(ge=0)]
    # The relaying EOA's live ETH balance in wei; zero reads as unknown when
    # no relayer address is configured for this run.
    relayer_eth_wei: Annotated[int, Field(ge=0)]
    # The Safe's live stock balance in raw units.
    safe_stock_units: Annotated[int, Field(ge=0)]
    # Every live Safe-held NFT id on the pool's NFPM.
    inventory_live_token_ids: Annotated[tuple[int, ...], Field(min_length=0)]
    # How many held NFTs are empty residuals carrying no exposure.
    inventory_empty_count: Annotated[int, Field(ge=0)]
    # The tracked position's live status, None while flat.
    tracked_status: LpPositionStatusReport | None = None
    # The tracked position id after reconciliation (adoptions included).
    tracked_token_id: Annotated[int, Field(ge=0)] | None = None
    # Whether the tracked position is staked in the gauge.
    tracked_staked: bool = False
    # The held-inventory quantity in whole stock tokens, zero when none.
    held_stock_quantity: Annotated[Decimal, Field(ge=0)] = Decimal("0")
    # Nonempty when an out-of-band condition refuses the whole cycle.
    out_of_band: str = ""
    # Human-readable evidence lines covering the reconciliation.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class CycleReport(BaseModel):
    """Carry one complete cycle's structured evidence and outcome."""

    # Frozen strict fields keep one cycle's story coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # When the cycle started, timezone-aware.
    started_at: datetime
    # dry_run or live.
    mode: CycleMode
    # The registry-matched symbol the cycle managed.
    symbol: str
    # The reconciled on-chain state the decision was made on.
    reconciliation: CycleReconciliation
    # The engine's chosen action, or hold when the cycle refused out-of-band.
    decision_action: str
    # The engine's stable primary reason, or the out-of-band label.
    decision_reason: str
    # The engine's numeric evidence lines.
    decision_diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]
    # The active event-window view at the decision instant.
    event_window: str
    # Every executed or refused action, in order.
    actions: Annotated[tuple[CycleActionRecord, ...], Field(min_length=0)] = ()
    # The tracked position's unrealized P&L vs entry when computable.
    pnl_vs_entry_usdc: Decimal | None = None
    # Why P&L is absent, empty when computed.
    pnl_diagnostic: str = ""
    # Total delivery fees paid this cycle, in wei.
    fee_wei: Annotated[int, Field(ge=0)] = 0
    # Empty when the cycle ran to completion; otherwise why it halted.
    halted_reason: str = ""
    # The honest input notes the decision carried.
    input_notes: Annotated[tuple[str, ...], Field(min_length=1)] = ()


class CycleReportPayload(BaseModel):
    """Persist one cycle summary on the audit chain; no credential fields."""

    # Frozen strict fields keep the audited summary immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # dry_run or live.
    mode: str
    # The registry-matched symbol the cycle managed.
    symbol: str
    # The engine's chosen action.
    action: str
    # The engine's stable primary reason.
    reason: str
    # The tracked position id when one is live, else None.
    tracked_token_id: Annotated[int, Field(ge=0)] | None = None
    # The tracked position's USDC value when live, else None.
    position_value_usdc: str | None = None
    # The unrealized P&L vs entry when computable, else None.
    pnl_vs_entry_usdc: str | None = None
    # Total delivery fees paid, in wei.
    fee_wei: Annotated[int, Field(ge=0)] = 0
    # The number of actions attempted this cycle.
    action_count: Annotated[int, Field(ge=0)] = 0
    # Empty when the cycle completed; otherwise why it halted.
    halted_reason: str = ""


class CycleExecutorBoundary(Protocol):
    """Define the audited executor surface one live cycle may drive."""

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
        """Broadcast one capped mint and stake sequence through the executor."""
        ...

    def execute_stake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Broadcast one gauge deposit staking one freshly minted position."""
        ...

    def execute_unstake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Broadcast one gauge withdrawal unstaking one position."""
        ...

    def execute_withdraw(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Broadcast the full decrease-and-collect exit of one position."""
        ...

    def execute_exit_swap(
        self,
        symbol: str,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Broadcast the stock-to-USDC swap converting all inventory."""
        ...


class CycleReadBoundary(Protocol):
    """Define the read-only position surface one cycle reconciles through."""

    def safe_position_inventory(self, symbol: str) -> LpSafePositionsSnapshot:
        """Enumerate every Safe-held position NFT with live classification."""
        ...

    def position_status(
        self,
        symbol: str,
        token_id: int,
        aero_price_usdc: Decimal | None = None,
        entry_cost_usdc: Decimal | None = None,
    ) -> LpPositionStatusReport:
        """Observe one position read-only with custody, value, and P&L."""
        ...


class CycleBalanceBoundary(Protocol):
    """Define the balance reads one cycle reconciles through."""

    def fetch_token_balance(self, token_address: str, owner_address: str) -> int:
        """Read one ERC20 balance for one owner."""
        ...

    def fetch_eth_balance(self, account_address: str) -> int:
        """Read one account's ETH balance in wei."""
        ...

    def fetch_transaction_receipt(self, transaction_hash: str) -> dict[str, object] | None:
        """Fetch one transaction receipt for mint-event decoding."""
        ...


class CycleAuditReader(Protocol):
    """Define the audit-chain reads backing crash-recovery adoption."""

    def recent_records(self) -> tuple[AuditRecord, ...]:
        """Read the complete audit chain as one ascending tuple."""
        ...


class AuditStoreReader:
    """Read the complete audit chain through the store's bounded pages."""

    def __init__(self, store: AuditStore) -> None:
        """Wrap one audit store.

        Args:
            store: The append-only store being read.
        """
        self._store = store

    def recent_records(self) -> tuple[AuditRecord, ...]:
        """Read every record as one ascending tuple.

        Returns:
            The complete chain; the caller keeps only what it needs.
        """
        records: list[AuditRecord] = []
        while True:
            page = self._store.read_records(1_000, offset=len(records))
            records.extend(page)
            if len(page) < 1_000:
                return tuple(records)


def decode_minted_token_id(receipt: Mapping[str, object] | None) -> int:
    """Extract the minted position id from one mint delivery receipt.

    Args:
        receipt: The JSON-RPC transaction receipt of the delivered mint, or
            None when the receipt is not yet available.

    Returns:
        The fresh position's token id from the IncreaseLiquidity event.

    Raises:
        ValueError: If the receipt is absent or carries no IncreaseLiquidity
            log.
    """
    if receipt is None:
        raise ValueError("the mint receipt is not yet available")
    logs = receipt.get("logs", [])
    if not isinstance(logs, list):
        raise ValueError("the mint receipt carries no logs")
    for log in logs:
        if not isinstance(log, dict):
            continue
        topics = log.get("topics", [])
        if (
            isinstance(topics, list)
            and topics
            and str(topics[0]).lower() == NFPM_INCREASE_LIQUIDITY_TOPIC0
            and len(topics) > 1
        ):
            return int(str(topics[1]), 16)
    raise ValueError("the mint receipt carries no IncreaseLiquidity event")


def _action_completed(report: LpActionExecutionReport) -> bool:
    """Return whether one executed action confirmed every delivery."""
    return report.completed and all(step.status == "confirmed" for step in report.steps)


def _action_hashes_and_fees(report: LpActionExecutionReport) -> tuple[tuple[str, ...], int]:
    """Collect one action's delivery hashes and total fees."""
    hashes = tuple(step.transaction_hash for step in report.steps)
    fees = sum(step.fee_wei or 0 for step in report.steps)
    return hashes, fees


def _price_at_tick(tick: int) -> Decimal:
    """Return the exact pool price at one tick-grid boundary."""
    with localcontext() as context:
        context.prec = MATH_PRECISION
        return +(TICK_PRICE_RATIO**tick)


class CycleRunner:
    """Run one complete reconcile-decide-act cycle."""

    def __init__(
        self,
        symbol: str,
        safe_address: str,
        relayer_address: str | None,
        reads: CycleReadBoundary,
        balances: CycleBalanceBoundary,
        sources: StrategySources,
        executor: CycleExecutorBoundary | None,
        audit_reader: CycleAuditReader | None,
        audit_sink: AuditStore | None,
        state_store: CycleStateStore,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Configure one cycle runner over every injectable boundary.

        Args:
            symbol: The registry-matched symbol this cycle manages.
            safe_address: The Safe whose positions and balances reconcile.
            relayer_address: The relaying EOA's public address for balance
                reads; None leaves the relayer ETH read unreported.
            reads: The read-only position surface (the LP executor).
            balances: The RPC balance and receipt surface.
            sources: The live strategy sources backing the observation.
            executor: The audited execution surface; None forces dry-run
                semantics regardless of the requested mode.
            audit_reader: The audit-chain reader for crash-recovery adoption.
            audit_sink: The store receiving the cycle-summary audit record.
            state_store: The self-healing cycle-book store.
            now: Injected clock producing timezone-aware instants.
        """
        self._symbol = symbol
        self._safe_address = normalize_evm_address(safe_address)
        self._relayer_address = normalize_evm_address(relayer_address) if relayer_address else None
        self._reads = reads
        self._balances = balances
        self._sources = sources
        self._executor = executor
        self._audit_reader = audit_reader
        self._audit_sink = audit_sink
        self._state_store = state_store
        self._now = now
        self._last_reconciliation: CycleReconciliation | None = None

    def run(
        self,
        mode: CycleMode,
        key_bytes: bytes | None = None,
        reference_price_usdc: Decimal | None = None,
        reference_age_seconds: int | None = None,
    ) -> CycleReport:
        """Run one complete cycle.

        Args:
            mode: Dry runs decide without acting; live cycles may broadcast.
            key_bytes: The signing key for live cycles; loaded by the caller
                from the sealed source and never persisted here.
            reference_price_usdc: Optional injected real-market quote.
            reference_age_seconds: Age of the injected quote.

        Returns:
            The complete structured cycle report.

        Raises:
            ValueError: If a live cycle lacks its key or executor.
        """
        started_at = self._now()
        if mode is CycleMode.LIVE and (key_bytes is None or self._executor is None):
            raise ValueError("a live cycle requires its signing key and executor")
        book = self._state_store.load()
        reconciliation = self._reconcile(book)
        self._last_reconciliation = reconciliation
        # A present quote with an absent age counts as fresh (age zero);
        # only an absent quote leaves the reference unset.
        if reference_price_usdc is not None and reference_age_seconds is None:
            reference_age_seconds = 0
        decision_report: StrategyDecisionReport | None = None
        actions: list[CycleActionRecord] = []
        halted_reason = ""
        if reconciliation.out_of_band:
            halted_reason = reconciliation.out_of_band
        else:
            book = self._adopt_into_book(book, reconciliation)
            decision_report = self._decide(book, reference_price_usdc, reference_age_seconds)
            outcome = decision_report.outcome
            if mode is CycleMode.LIVE and outcome.decision.action is not PolicyActionKind.HOLD:
                if self._executor is None or key_bytes is None:
                    raise ValueError("a live cycle requires its signing key and executor")
                actions, halted_reason, book = self._act(book, outcome, key_bytes)
            final_reconciliation = self._reconcile(book)
            self._last_reconciliation = final_reconciliation
            if not final_reconciliation.out_of_band:
                book = self._rebuild_book(
                    book, final_reconciliation, decision_report.outcome.next_state
                )
            elif not halted_reason:
                halted_reason = final_reconciliation.out_of_band
            reconciliation = final_reconciliation
        self._state_store.save(book)
        report = self._assemble_report(
            started_at, mode, reconciliation, decision_report, tuple(actions), halted_reason
        )
        self._record(report)
        return report

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _reconcile(self, book: CycleStateBook) -> CycleReconciliation:
        """Read the complete on-chain state one cycle acts on.

        Args:
            book: The persisted book whose tracked position reconciles.

        Returns:
            The complete reconciliation with any out-of-band condition.

        Raises:
            ExecutionUnavailableError: If a live read cannot complete.
            ValueError: If the symbol resolves to nothing.
        """
        diagnostics: list[str] = []
        safe_usdc = self._balances.fetch_token_balance(BASE_USDC_ADDRESS, self._safe_address)
        relayer_eth = (
            self._balances.fetch_eth_balance(self._relayer_address)
            if self._relayer_address is not None
            else 0
        )
        if self._relayer_address is None:
            diagnostics.append("relayer ETH unread: no relayer address configured for this run")
        inventory = self._reads.safe_position_inventory(self._symbol)
        live_ids = tuple(position.token_id for position in inventory.live_positions)
        tracked_status: LpPositionStatusReport | None = None
        tracked_token_id: int | None = None
        tracked_staked = False
        out_of_band = ""
        tracked = book.position
        if tracked is not None:
            tracked_status = self._reads.position_status(
                self._symbol, tracked.token_id, entry_cost_usdc=tracked.committed_usd
            )
            owner = normalize_evm_address(tracked_status.token_owner_address)
            gauge = normalize_evm_address(tracked_status.gauge_address)
            if owner != self._safe_address and owner != gauge:
                out_of_band = (
                    f"tracked position {tracked.token_id} is owned by {owner}, which is "
                    "neither the Safe nor the pool's gauge; refusing the cycle"
                )
            else:
                tracked_token_id = tracked.token_id
                tracked_staked = owner == gauge
                diagnostics.append(
                    f"tracked position {tracked.token_id} "
                    f"({'staked' if tracked_staked else 'in the Safe'}) valued "
                    f"{tracked_status.position_value_usdc} USDC against "
                    f"{tracked.committed_usd} committed"
                )
            untracked_live = tuple(token for token in live_ids if token != tracked.token_id)
            if untracked_live and not out_of_band:
                out_of_band = (
                    f"live untracked position NFT(s) {untracked_live} sit beside the "
                    "tracked position; refusing the cycle until reconciled"
                )
        elif live_ids:
            adopted = self._adoption_evidence(live_ids)
            if adopted is None:
                out_of_band = (
                    f"the Safe holds live position NFT(s) {live_ids} the cycle does not "
                    "track and no audit evidence proves they are ours; refusing the cycle"
                )
            else:
                tracked_token_id = adopted
                diagnostics.append(
                    f"adopting live position {adopted} proven ours by the audit chain's "
                    "confirmed mint evidence"
                )
        stock_address = self._stock_token_address()
        stock_units = self._balances.fetch_token_balance(stock_address, self._safe_address)
        stock_decimals = self._sources.token_decimals(stock_address)
        held_quantity = Decimal(stock_units).scaleb(-stock_decimals)
        if (
            book.held_inventory is None
            and held_quantity > 0
            and tracked_token_id is None
            and not out_of_band
        ):
            diagnostics.append(
                f"adopting unrecorded stock balance {held_quantity} as held inventory; "
                "the policy's convergence machinery will unwind it"
            )
        if book.held_inventory is not None and held_quantity == 0 and tracked_token_id is None:
            diagnostics.append("recorded held inventory no longer exists on-chain; clearing")
        diagnostics.append(
            f"Safe holds {Decimal(safe_usdc).scaleb(-6)} USDC"
            + (
                f", relayer holds {Decimal(relayer_eth).scaleb(-18)} ETH"
                if self._relayer_address is not None
                else ""
            )
            + f", Safe holds {held_quantity} stock"
        )
        return CycleReconciliation(
            symbol=self._symbol,
            safe_usdc_units=safe_usdc,
            relayer_eth_wei=relayer_eth,
            safe_stock_units=stock_units,
            inventory_live_token_ids=live_ids,
            inventory_empty_count=inventory.empty_count,
            tracked_status=tracked_status,
            tracked_token_id=tracked_token_id,
            tracked_staked=tracked_staked,
            held_stock_quantity=held_quantity,
            out_of_band=out_of_band,
            diagnostics=tuple(diagnostics),
        )

    def _stock_token_address(self) -> str:
        """Resolve the cycle symbol's stock token address from the registry."""
        token = self._sources.symbol_address(self._symbol)
        if token is None:
            raise ValueError(f"symbol {self._symbol!r} is not in the official B20 registry")
        return token

    def _resolved_pool(self) -> PoolCandidate:
        """Resolve the cycle symbol's live pool through the decision sources."""
        pool, _ = self._sources.resolve_pool(self._symbol)
        return pool

    def _adoption_evidence(self, live_ids: tuple[int, ...]) -> int | None:
        """Prove one live untracked position is ours from the audit chain.

        Adoption requires the chain's own evidence: the newest confirmed mint
        delivery whose IncreaseLiquidity receipt names exactly one of the
        live untracked ids. Anything else refuses.

        Args:
            live_ids: The live untracked position ids on the NFPM.

        Returns:
            The proven token id, or None when no evidence adopts anything.
        """
        if self._audit_reader is None or len(live_ids) != 1:
            return None
        for record in reversed(self._audit_reader.recent_records()):
            if record.event_type is not AuditEventType.LP_EXECUTE_CONFIRMED:
                continue
            payload = json.loads(record.payload_json)
            if (
                payload.get("action") != "mint"
                or payload.get("role") != "mint"
                or payload.get("outcome") != "confirmed"
            ):
                continue
            transaction_hash = str(payload.get("transaction_hash", ""))
            try:
                receipt = self._balances.fetch_transaction_receipt(transaction_hash)
                token_id = decode_minted_token_id(receipt)
            except (ValueError, ExecutionUnavailableError):
                continue
            return token_id if token_id in live_ids else None
        return None

    def _adopt_into_book(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> CycleStateBook:
        """Fold reconciliation adoptions into the book before deciding."""
        updates: dict[str, object] = {}
        if (
            book.position is None
            and reconciliation.tracked_token_id is not None
            and not reconciliation.out_of_band
        ):
            status = reconciliation.tracked_status
            pool = status.pool_address if status is not None else self._resolved_pool().pool_address
            committed = self._last_mint_budget()
            if committed is not None:
                updates["position"] = TrackedPosition(
                    symbol=self._symbol,
                    token_id=reconciliation.tracked_token_id,
                    pool_address=pool,
                    committed_usd=committed,
                    entered_at=self._now(),
                )
        if (
            book.held_inventory is None
            and reconciliation.held_stock_quantity > 0
            and reconciliation.tracked_token_id is None
            and not reconciliation.out_of_band
        ):
            updates["held_inventory"] = HeldInventoryRecord(
                symbol=self._symbol,
                token_address=self._stock_token_address(),
                stock_quantity=reconciliation.held_stock_quantity,
                held_since=self._now(),
            )
        if not updates:
            return book
        return book.model_copy(update=updates)

    def _last_mint_budget(self) -> Decimal | None:
        """Read the newest audited mint budget, the adoption cost basis."""
        if self._audit_reader is None:
            return None
        for record in reversed(self._audit_reader.recent_records()):
            if record.event_type is not AuditEventType.LP_MINT_PLANNED:
                continue
            payload = json.loads(record.payload_json)
            budget = payload.get("budget_usdc")
            if isinstance(budget, str) and Decimal(budget) > 0:
                return Decimal(budget)
        return None

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def _policy_state(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> PolicyState:
        """Reconstruct the engine state from the book and live custody."""
        position: PolicyPosition | None = None
        if book.position is not None and reconciliation.tracked_status is not None:
            status = reconciliation.tracked_status
            position = PolicyPosition(
                pool_address=status.pool_address,
                token_address=self._stock_token_address(),
                price_range=AlignedPriceRange(
                    lower_tick=status.position.tick_lower,
                    upper_tick=status.position.tick_upper,
                    lower_price=_price_at_tick(status.position.tick_lower),
                    upper_price=_price_at_tick(status.position.tick_upper),
                ),
                committed_usd=book.position.committed_usd,
                entered_at=book.position.entered_at,
            )
        held: HeldInventory | None = None
        if book.held_inventory is not None:
            held = HeldInventory(
                pool_address=self._resolved_pool().pool_address,
                token_address=book.held_inventory.token_address,
                stock_quantity=book.held_inventory.stock_quantity,
                held_since=book.held_inventory.held_since,
            )
        day = self._now().astimezone(POLICY_TIMEZONE).date()
        day_start = book.day_start_equity_usd if book.day == day else None
        if day_start is None:
            day_start = Decimal(reconciliation.safe_usdc_units).scaleb(-6)
        return PolicyState(
            day=day,
            day_start_equity_usd=day_start,
            halted_day=book.halted_day,
            position=position,
            held_inventory=held,
            reentry_blocked_until=book.reentry_blocked_until,
        )

    def _decide(
        self,
        book: CycleStateBook,
        reference_price_usdc: Decimal | None,
        reference_age_seconds: int | None,
    ) -> StrategyDecisionReport:
        """Run the complete locked engine over one live observation.

        Args:
            book: The reconciled book threading the engine state.
            reference_price_usdc: Optional injected real-market quote.
            reference_age_seconds: Age of the injected quote.

        Returns:
            The complete decision report with its typed outcome.

        Raises:
            ExecutionUnavailableError: If discovery or a read fails.
            ValueError: If the symbol resolves to no pool.
        """
        pool, snapshot_block = self._sources.resolve_pool(self._symbol)
        assert self._last_reconciliation is not None  # noqa: S101 - set by run()
        state = self._policy_state(book, self._last_reconciliation)
        observation, _, aero_price, notes = assemble_observation(
            self._sources,
            self._symbol,
            pool,
            snapshot_block,
            self._now(),
            None,
            reference_price_usdc,
            reference_age_seconds,
            self._safe_address,
        )
        engine = PolicyEngine(LOCKED_POLICY_PARAMETERS, load_event_calendar())
        outcome = engine.decide(state, observation)
        window = evaluate_event_window(
            observation.observed_at, observation.token_address, engine.calendar
        )
        return StrategyDecisionReport(
            symbol=self._symbol,
            pool_address=pool.pool_address,
            snapshot_block=snapshot_block,
            observed_at=observation.observed_at,
            amm_price_usdc=observation.amm_price_usdc,
            emissions_apr=observation.emissions_apr,
            aero_price_usdc=aero_price,
            pool_depth_usd=observation.pool_depth_usd,
            equity_usd=observation.equity_usd,
            gas_price_gwei=observation.gas_price_gwei,
            reference_price_usdc=reference_price_usdc,
            event_window=window,
            outcome=outcome,
            input_notes=notes,
        )

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------

    def _act(  # noqa: PLR0915 - one fixed policy mapping, explicit branches
        self, book: CycleStateBook, outcome: PolicyOutcome, key_bytes: bytes
    ) -> tuple[list[CycleActionRecord], str, CycleStateBook]:
        """Execute the policy-authorized action through audited surfaces."""
        executor = self._executor
        assert executor is not None  # noqa: S101 - the caller verified liveness
        decision = outcome.decision
        action = decision.action
        records: list[CycleActionRecord] = []
        halted = ""
        symbol = self._symbol

        def run(name: str, call: Callable[[], LpActionExecutionReport]) -> bool:
            """Run one audited action, recording its outcome.

            Returns:
                True when the action completed and the cycle may continue.
            """
            nonlocal halted
            try:
                report = call()
            except (LpExecutionRefusalError, LpPlanRefusalError) as error:
                code = str(getattr(error, "code", "plan_refused"))
                records.append(
                    CycleActionRecord(
                        action=name,
                        status="refused",
                        refusal_code=code,
                        diagnostic=str(error),
                    )
                )
                halted = f"the {name} action refused [{code}]"
                return False
            except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
                records.append(
                    CycleActionRecord(action=name, status="failed", diagnostic=str(error))
                )
                halted = f"the {name} action failed: {error}"
                return False
            hashes, fees = _action_hashes_and_fees(report)
            records.append(
                CycleActionRecord(
                    action=name,
                    status="completed" if _action_completed(report) else "failed",
                    transaction_hashes=hashes,
                    fee_wei=fees,
                    diagnostic=report.halted_reason,
                )
            )
            if not _action_completed(report):
                halted = f"the {name} action halted: {report.halted_reason}"
                return False
            return True

        if action is PolicyActionKind.ENTER:
            if book.position is not None:
                halted = "the engine authorized an entry while a position is tracked"
                return records, halted, book
            size = decision.size_usd
            width = self._width_from_range(decision.price_range)
            if size is None or size <= 0 or width is None:
                halted = "the enter decision carried no positive size or tick-aligned width"
                return records, halted, book
            budget: Decimal = size
            mint_width: int = width
            if not run(
                "mint",
                lambda: executor.execute_mint(
                    symbol, budget, mint_width, key_bytes, confirm_broadcast=True
                ),
            ):
                return records, halted, book
            token_id = self._decode_mint_token_id(records[-1])
            if token_id is None:
                halted = "the minted position id could not be decoded from the receipt"
                return records, halted, book
            minted_id: int = token_id
            if not run(
                "stake",
                lambda: executor.execute_stake(
                    symbol, minted_id, key_bytes, confirm_broadcast=True
                ),
            ):
                return records, halted, book
            return records, halted, self._book_with_position(book, minted_id, budget)

        if action in (
            PolicyActionKind.STOP_OUT,
            PolicyActionKind.DILUTION_EXIT,
            PolicyActionKind.EVENT_EXIT,
            PolicyActionKind.DISLOCATION_EXIT,
            PolicyActionKind.DEFENSIVE_EXIT,
            PolicyActionKind.RECENTER,
        ):
            if book.position is None:
                halted = f"the engine authorized {action.value} while flat"
                return records, halted, book
            if not self._exit_position(executor, book, key_bytes, run):
                return records, halted, book
            if action is PolicyActionKind.RECENTER:
                size = decision.size_usd
                width = self._width_from_range(decision.price_range)
                if size is None or size <= 0 or width is None:
                    halted = "the recenter decision carried no complete fresh entry"
                    return (
                        records,
                        halted,
                        book.model_copy(update={"position": None, "held_inventory": None}),
                    )
                recenter_budget: Decimal = size
                recenter_width: int = width
                if not run(
                    "mint",
                    lambda: executor.execute_mint(
                        symbol, recenter_budget, recenter_width, key_bytes, confirm_broadcast=True
                    ),
                ):
                    return (
                        records,
                        halted,
                        book.model_copy(update={"position": None, "held_inventory": None}),
                    )
                token_id = self._decode_mint_token_id(records[-1])
                if token_id is None:
                    halted = "the recentered position id could not be decoded"
                    return (
                        records,
                        halted,
                        book.model_copy(update={"position": None, "held_inventory": None}),
                    )
                if not run(
                    "stake",
                    lambda: executor.execute_stake(
                        symbol, token_id, key_bytes, confirm_broadcast=True
                    ),
                ):
                    return (
                        records,
                        halted,
                        book.model_copy(update={"position": None, "held_inventory": None}),
                    )
                return records, halted, self._book_with_position(book, token_id, size)
            return (
                records,
                halted,
                book.model_copy(update={"position": None, "held_inventory": None}),
            )

        if action is PolicyActionKind.STALE_LOW_BURN:
            if book.position is None:
                halted = "the engine authorized a stale-low burn while flat"
                return records, halted, book
            if not self._burn_without_swap(executor, book, key_bytes, run):
                return records, halted, book
            held_quantity = self._live_stock_quantity()
            if held_quantity <= 0:
                halted = "the stale-low burn left no stock balance to hold"
                return records, halted, book.model_copy(update={"position": None})
            return (
                records,
                halted,
                book.model_copy(
                    update={
                        "position": None,
                        "held_inventory": HeldInventoryRecord(
                            symbol=symbol,
                            token_address=self._stock_token_address(),
                            stock_quantity=held_quantity,
                            held_since=self._now(),
                        ),
                    }
                ),
            )

        if action is PolicyActionKind.SELL_INVENTORY:
            if book.held_inventory is None:
                halted = "the engine authorized an inventory sale with nothing held"
                return records, halted, book
            if not run(
                "exit_swap",
                lambda: executor.execute_exit_swap(symbol, key_bytes, confirm_broadcast=True),
            ):
                return records, halted, book
            return records, halted, book.model_copy(update={"held_inventory": None})

        halted = f"the cycle has no live mapping for action {action.value}"
        return records, halted, book

    def _book_with_position(
        self, book: CycleStateBook, token_id: int, committed: Decimal
    ) -> CycleStateBook:
        """Return the book carrying one freshly entered tracked position."""
        return book.model_copy(
            update={
                "position": TrackedPosition(
                    symbol=self._symbol,
                    token_id=token_id,
                    pool_address=self._resolved_pool().pool_address,
                    committed_usd=committed,
                    entered_at=self._now(),
                ),
                "held_inventory": None,
            }
        )

    def _exit_position(
        self,
        executor: CycleExecutorBoundary,
        book: CycleStateBook,
        key_bytes: bytes,
        run: Callable[[str, Callable[[], LpActionExecutionReport]], bool],
    ) -> bool:
        """Unstake, withdraw, and swap one tracked position fully to USDC."""
        tracked = book.position
        assert tracked is not None  # noqa: S101 - the caller verified a tracked position
        symbol = self._symbol
        assert self._last_reconciliation is not None  # noqa: S101 - set by run()
        if self._last_reconciliation.tracked_staked and not run(
            "unstake",
            lambda: executor.execute_unstake(
                symbol, tracked.token_id, key_bytes, confirm_broadcast=True
            ),
        ):
            return False
        if not run(
            "withdraw",
            lambda: executor.execute_withdraw(
                symbol, tracked.token_id, key_bytes, confirm_broadcast=True
            ),
        ):
            return False
        return run(
            "exit_swap",
            lambda: executor.execute_exit_swap(symbol, key_bytes, confirm_broadcast=True),
        )

    def _burn_without_swap(
        self,
        executor: CycleExecutorBoundary,
        book: CycleStateBook,
        key_bytes: bytes,
        run: Callable[[str, Callable[[], LpActionExecutionReport]], bool],
    ) -> bool:
        """Unstake and withdraw while holding the stock unsold."""
        tracked = book.position
        assert tracked is not None  # noqa: S101 - the caller verified a tracked position
        symbol = self._symbol
        assert self._last_reconciliation is not None  # noqa: S101 - set by run()
        if self._last_reconciliation.tracked_staked and not run(
            "unstake",
            lambda: executor.execute_unstake(
                symbol, tracked.token_id, key_bytes, confirm_broadcast=True
            ),
        ):
            return False
        return run(
            "withdraw",
            lambda: executor.execute_withdraw(
                symbol, tracked.token_id, key_bytes, confirm_broadcast=True
            ),
        )

    def _decode_mint_token_id(self, record: CycleActionRecord) -> int | None:
        """Decode the fresh position id from one completed mint's receipt."""
        if not record.transaction_hashes:
            return None
        try:
            receipt = self._balances.fetch_transaction_receipt(record.transaction_hashes[-1])
            return decode_minted_token_id(receipt)
        except (ValueError, ExecutionUnavailableError):
            return None

    def _live_stock_quantity(self) -> Decimal:
        """Read the Safe's whole-token stock balance right now."""
        stock_address = self._stock_token_address()
        units = self._balances.fetch_token_balance(stock_address, self._safe_address)
        return Decimal(units).scaleb(-self._sources.token_decimals(stock_address))

    def _width_from_range(self, price_range: AlignedPriceRange | None) -> int | None:
        """Derive the explicit half width in tick spacings from a policy range.

        The engine's grid alignment can leave a non-integral spacing half
        width (a 70-tick span on a spacing-10 pool), so the mapping rounds
        up: the executed range always carries at least the policy's width,
        and the planner's own ceiling clamps anything wider.
        """
        if price_range is None:
            return None
        pool = self._resolved_pool()
        half_ticks = (price_range.upper_tick - price_range.lower_tick) // 2
        if half_ticks < 1:
            return None
        return -(-half_ticks // pool.tick_spacing)

    def _rebuild_book(
        self,
        book: CycleStateBook,
        reconciliation: CycleReconciliation,
        next_state: PolicyState,
    ) -> CycleStateBook:
        """Rebuild the book from post-action chain truth plus engine state."""
        position = book.position
        if (
            position is not None
            and reconciliation.tracked_token_id is not None
            and reconciliation.tracked_token_id != position.token_id
        ):
            position = position.model_copy(update={"token_id": reconciliation.tracked_token_id})
        held = book.held_inventory
        if held is not None and reconciliation.held_stock_quantity == 0:
            held = None
        return CycleStateBook(
            position=position,
            held_inventory=held,
            reentry_blocked_until=next_state.reentry_blocked_until,
            day=next_state.day,
            day_start_equity_usd=next_state.day_start_equity_usd,
            halted_day=next_state.halted_day,
            updated_at=self._now(),
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _assemble_report(
        self,
        started_at: datetime,
        mode: CycleMode,
        reconciliation: CycleReconciliation,
        decision_report: StrategyDecisionReport | None,
        actions: tuple[CycleActionRecord, ...],
        halted_reason: str,
    ) -> CycleReport:
        """Assemble the structured cycle report from its complete evidence."""
        if decision_report is not None:
            decision_action = decision_report.outcome.decision.action.value
            decision_reason = decision_report.outcome.decision.reason.value
            decision_diagnostics = decision_report.outcome.decision.diagnostics
            event_window = decision_report.event_window.description
            input_notes = decision_report.input_notes
        else:
            decision_action = PolicyActionKind.HOLD.value
            decision_reason = "out_of_band"
            decision_diagnostics = (reconciliation.out_of_band,)
            event_window = evaluate_event_window(
                started_at, self._stock_token_address(), load_event_calendar()
            ).description
            input_notes = ("the cycle refused out-of-band before deciding",)
        status = reconciliation.tracked_status
        pnl = status.unrealized_pnl_usdc if status is not None else None
        pnl_diagnostic = status.pnl_diagnostic if status is not None else "no tracked position"
        return CycleReport(
            started_at=started_at,
            mode=mode,
            symbol=self._symbol,
            reconciliation=reconciliation,
            decision_action=decision_action,
            decision_reason=decision_reason,
            decision_diagnostics=decision_diagnostics,
            event_window=event_window,
            actions=actions,
            pnl_vs_entry_usdc=pnl,
            pnl_diagnostic=pnl_diagnostic if pnl is None else "",
            fee_wei=sum(action.fee_wei for action in actions),
            halted_reason=halted_reason,
            input_notes=input_notes,
        )

    def _record(self, report: CycleReport) -> None:
        """Append the cycle-summary audit record when a sink is configured."""
        if self._audit_sink is None:
            return
        status = report.reconciliation.tracked_status
        self._audit_sink.append(
            AuditEventType.CYCLE_REPORTED,
            CycleReportPayload(
                mode=report.mode.value,
                symbol=report.symbol,
                action=report.decision_action,
                reason=report.decision_reason,
                tracked_token_id=report.reconciliation.tracked_token_id,
                position_value_usdc=str(status.position_value_usdc) if status is not None else None,
                pnl_vs_entry_usdc=str(report.pnl_vs_entry_usdc)
                if report.pnl_vs_entry_usdc is not None
                else None,
                fee_wei=report.fee_wei,
                action_count=len(report.actions),
                halted_reason=report.halted_reason,
            ),
            self._now(),
        )


def _print_report(report: CycleReport) -> None:
    """Print one cycle report's human summary.

    Args:
        report: The cycle report being printed.
    """
    recon = report.reconciliation
    print(f"cycle {report.mode.value} for {report.symbol} started {report.started_at.isoformat()}")
    for line in recon.diagnostics:
        print(f"  state: {line}")
    if recon.out_of_band:
        print(f"  OUT OF BAND: {recon.out_of_band}")
    print(
        f"decision: {report.decision_action} ({report.decision_reason}), "
        f"window: {report.event_window}"
    )
    for diagnostic in report.decision_diagnostics:
        print(f"  - {diagnostic}")
    for action in report.actions:
        suffix = f" [{action.refusal_code}]" if action.refusal_code else ""
        hashes = ", ".join(action.transaction_hashes) or "no broadcasts"
        print(f"  action {action.action}: {action.status}{suffix} ({action.fee_wei} wei, {hashes})")
        if action.diagnostic:
            print(f"    - {action.diagnostic}")
    if report.pnl_vs_entry_usdc is not None:
        print(f"pnl vs entry: {report.pnl_vs_entry_usdc} USDC")
    elif report.pnl_diagnostic:
        print(f"pnl vs entry: unavailable ({report.pnl_diagnostic})")
    print(f"gas spent: {report.fee_wei} wei")
    if report.halted_reason:
        print(f"halted: {report.halted_reason}")
    for note in report.input_notes:
        print(f"  note: {note}")


def _reference_price_from_environment(environ: Mapping[str, str]) -> Decimal | None:
    """Read the optional injected reference quote from the environment."""
    raw = environ.get(CYCLE_REFERENCE_PRICE_ENV, "").strip()
    if not raw:
        return None
    value = Decimal(raw)
    if value <= 0:
        raise ValueError(f"{CYCLE_REFERENCE_PRICE_ENV} must be positive, not {raw!r}")
    return value


def build_cycle_runner(
    settings: Settings,
    symbol: str,
    safe_address: str,
    relayer_address: str | None,
) -> CycleRunner:
    """Assemble the live cycle runner from the application settings.

    Args:
        settings: The application settings backing every boundary.
        symbol: The registry symbol this cycle manages.
        safe_address: The Safe whose positions and balances reconcile.
        relayer_address: The relayer's public address, or None.

    Returns:
        The fully wired runner; nothing has been read yet.
    """
    from aero_bot.executor import ExecutorRpcBackend, LiveExecutionSources
    from aero_bot.lp_executor import (
        EXECUTE_RECEIPT_ENDPOINT_URLS,
        LpLifecycleExecutor,
        LpSafeExecutionPolicy,
    )
    from aero_bot.lp_pins import LpPoolPinStore
    from aero_bot.lp_plan import LpExecutionPolicy
    from aero_bot.safe_tx import SafeTransactionRpcBackend
    from aero_bot.strategy import LiveStrategySources

    rpc = ExecutorRpcBackend(rpc_url=settings.base_rpc_url, progress=_cycle_progress)
    audit_store = AuditStore(settings.audit_database_path)
    pin_store = LpPoolPinStore(settings.lp_pool_pins_path)
    # Every long-running phase (the first full Sugar sweep above all) reports
    # honest progress on stderr so a slow cycle never looks like a stall; the
    # JSON report on stdout stays machine-clean.
    progress = _cycle_progress
    sources = LiveStrategySources(
        rpc_url=settings.base_rpc_url,
        sugar_address=settings.lp_sugar_address,
        pool_pin_store=pin_store,
        progress=progress,
    )
    safe_rpc = SafeTransactionRpcBackend(rpc_url=settings.base_rpc_url, safe_address=safe_address)
    receipt_backends = [rpc] + [
        ExecutorRpcBackend(rpc_url=url)
        for url in EXECUTE_RECEIPT_ENDPOINT_URLS
        if url != settings.base_rpc_url
    ]
    executor = LpLifecycleExecutor(
        policy=LpSafeExecutionPolicy(),
        plan_policy=LpExecutionPolicy(),
        safe_address=safe_address,
        sources=LiveExecutionSources(
            rpc_url=settings.base_rpc_url,
            sugar_address=settings.lp_sugar_address,
            progress=progress,
        ),
        rpc=rpc,
        safe_rpc=safe_rpc,
        audit_sink=audit_store,
        receipt_backends=receipt_backends,
        pool_pin_store=pin_store,
    )
    return CycleRunner(
        symbol=symbol,
        safe_address=safe_address,
        relayer_address=relayer_address,
        reads=executor,
        balances=rpc,
        sources=sources,
        executor=executor,
        audit_reader=AuditStoreReader(audit_store),
        audit_sink=audit_store,
        state_store=CycleStateStore.from_environment(settings=settings),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one decision cycle.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on any completed cycle (a hold is a
        decision, not a failure), one on failures, two when any refusal or
        out-of-band condition halted the cycle.
    """
    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="aero-bot-cycle",
        description=(
            "Run one scheduled decision cycle: reconcile on-chain state, run "
            "the locked policy engine, and execute the authorized action "
            "through the audited capped surfaces. The systemd timer - never "
            "an in-process scheduler - decides when cycles run."
        ),
    )
    parser.add_argument("--symbol", required=True, help="Registry symbol like AAPLc.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Reconcile and decide without loading the signing key or building "
            "anything; the report still carries the complete verdict."
        ),
    )
    parser.add_argument(
        "--reference-price",
        type=Decimal,
        default=None,
        help=(
            "Optional injected real-market quote in USDC per stock; the "
            "AERO_BOT_CYCLE_REFERENCE_PRICE_USDC variable supplies the same "
            "value when the flag is absent."
        ),
    )
    parser.add_argument(
        "--reference-age-seconds",
        type=int,
        default=0,
        help="Age of the injected reference quote in seconds (default: 0).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete report as JSON instead of a summary.",
    )
    arguments = parser.parse_args(argv)
    if arguments.reference_price is not None and arguments.reference_price <= 0:
        parser.error("--reference-price must be positive")
    if arguments.reference_age_seconds < 0:
        parser.error("--reference-age-seconds must be non-negative")
    try:
        reference = (
            arguments.reference_price
            if arguments.reference_price is not None
            else _reference_price_from_environment(os.environ)
        )
    except ValueError as error:
        print(f"invalid reference price: {error}", file=sys.stderr)
        return EXIT_FAILURE
    safe_address = os.environ.get(SAFE_ADDRESS_ENV, DEFAULT_CANARY_SAFE_ADDRESS)
    configured_relayer = os.environ.get(RELAYER_ADDRESS_ENV, "").strip() or None
    mode = CycleMode.DRY_RUN if arguments.dry_run else CycleMode.LIVE
    key_bytes: bytes | None = None
    if mode is CycleMode.LIVE:
        from eth_account import Account

        from aero_bot.signing_key import load_signing_key_source

        try:
            key_bytes = load_signing_key_source().load_signing_key()
        except (RuntimeError, ValueError) as error:
            print(f"the signing key is unavailable: {error}", file=sys.stderr)
            return EXIT_FAILURE
        derived_relayer = normalize_evm_address(Account.from_key(key_bytes).address)
        if configured_relayer and configured_relayer != derived_relayer:
            print(
                f"the configured relayer {configured_relayer} does not match the signing "
                f"key's address {derived_relayer}; refusing the cycle",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        configured_relayer = derived_relayer
    try:
        runner = build_cycle_runner(settings, arguments.symbol, safe_address, configured_relayer)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"the cycle runner is unavailable: {error}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        report = runner.run(
            mode,
            key_bytes=key_bytes,
            reference_price_usdc=reference,
            reference_age_seconds=arguments.reference_age_seconds,
        )
    except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
        print(f"cycle failed: {error}", file=sys.stderr)
        return EXIT_FAILURE
    if arguments.json:
        print(report.model_dump_json(indent=2))
    else:
        _print_report(report)
    # The email hook never raises: a failed delivery warns on stderr and the
    # cycle's report and exit code stand on their own.
    from aero_bot.alerts import deliver_cycle_alerts

    deliver_cycle_alerts(report)
    if report.halted_reason:
        if report.reconciliation.out_of_band or any(
            action.status == "refused" for action in report.actions
        ):
            return EXIT_REFUSED
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
