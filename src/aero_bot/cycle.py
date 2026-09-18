"""One scheduled decision cycle: reconcile, decide, act within the locked caps.

The ``aero-bot-cycle`` command runs exactly one decision cycle - for one
pinned registry symbol, or for the whole verified B20 board in selector
mode - and exits - the systemd timer (not an in-process scheduler)
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
   daily-loss anchor). Selector mode evaluates every verified B20 pool
   through the complete entry gate chain and selects the best-qualifying
   pool by qualifying emissions APR under the captain's 2026-09-09
   cross-board ruling; market windows no longer gate entries since the
   same date's twenty-four-seven ruling.
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
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from aero_bot.audit import AuditEventType, AuditRecord, AuditStore
from aero_bot.config import Settings
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.execution_lock import ExecutionLockUnavailableError, exclusive_execution_lock
from aero_bot.executor import (
    DEFAULT_CANARY_SAFE_ADDRESS,
    SAFE_ADDRESS_ENV,
    ExecutionUnavailableError,
)
from aero_bot.history import price_usdc_per_stock
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
from aero_bot.selector import (
    DEFAULT_SWITCH_MARGIN_FRACTION,
    BoardSelection,
    PoolBoardOption,
    SwitchDirective,
    closest_call_evaluation,
    select_board,
)
from aero_bot.strategy import (
    SELECTOR_SYMBOL,
    BoardListing,
    StrategyDecisionReport,
    StrategySources,
    assemble_board,
    assemble_observation,
    parse_reference_quotes,
)
from aero_bot.venues import BASE_USDC_ADDRESS, PoolCandidate

# Environment variable carrying an explicit cycle-state path override.
CYCLE_STATE_PATH_ENV = "AERO_BOT_CYCLE_STATE_PATH"
# Environment variable carrying an optional injected reference quote.
CYCLE_REFERENCE_PRICE_ENV = "AERO_BOT_CYCLE_REFERENCE_PRICE_USDC"
# Environment variable carrying the relayer's public address for dry runs.
RELAYER_ADDRESS_ENV = "AERO_BOT_RELAYER_ADDRESS"
# Environment variable optionally pinning one registry symbol for the cycle.
# Unset or "auto" runs the cross-board selector (the default); an explicit
# symbol pins one pool for operator runs.
CYCLE_SYMBOL_ENV = "AERO_BOT_CYCLE_SYMBOL"
# Environment variable carrying the cross-board switch margin as a fraction
# (default 0.30, the captain's 2026-09-09 trial ruling).
CYCLE_SWITCH_MARGIN_ENV = "AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION"
# After a confirmed live action, the primary read endpoint must catch up to
# the action's inclusion block before final reconciliation. This prevents a
# load-balanced or briefly lagging RPC from reporting the pre-action balance
# as if it were the final state.
POST_ACTION_VISIBILITY_ATTEMPTS = 6
POST_ACTION_VISIBILITY_BASE_BACKOFF_SECONDS = 0.5
POST_ACTION_VISIBILITY_MAX_BACKOFF_SECONDS = 4.0
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
    # When the position first moved outside either range edge. This must
    # survive scheduled cycles so the recenter wait cannot restart from zero.
    out_of_range_since: datetime | None = None
    # Which side owns the persisted wait anchor.
    out_of_range_side: Literal["above", "below"] | None = None


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
    # Why the stock is held, so failed entries can retry before sell-back.
    origin: Literal["stale_low_exit", "failed_entry", "adopted_balance"] = "adopted_balance"


class ReentryCooldown(BaseModel):
    """Carry one pool's re-entry cooldown from a stop or dilution exit."""

    # Frozen strict fields keep one cooldown record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the cooldown applies to.
    symbol: str
    # Re-entry into this pool stays blocked until this instant.
    blocked_until: datetime


class CycleStateBook(BaseModel):
    """Carry every engine-owned fact the next cycle must thread forward."""

    # The tracked open position, or None while flat.
    position: TrackedPosition | None = None
    # Stock held unsold after a stale-low burn, or None.
    held_inventory: HeldInventoryRecord | None = None
    # Re-entry cooldowns, one per pool: an exit from one pool never blocks
    # another pool's entry (the captain's 2026-09-09 cross-board ruling).
    reentry_cooldowns: tuple[ReentryCooldown, ...] = ()
    # The America/New_York day the day-start equity anchor belongs to.
    day: date | None = None
    # Day-start equity anchors the five-percent daily loss halt.
    day_start_equity_usd: Decimal | None = None
    # The day a daily loss halt tripped, if any.
    halted_day: date | None = None
    # When this book was last persisted, timezone-aware.
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="before")
    @classmethod
    def fold_legacy_cooldown(cls, data: object) -> object:
        """Fold a legacy single-pool cooldown field into the per-pool map.

        Books persisted before the cross-board ruling carried one global
        ``reentry_blocked_until``; the upgrade applies it to the tracked
        position's pool when one exists and drops it otherwise.

        Args:
            data: Raw model input mapping or value.

        Returns:
            Input with the legacy field folded into ``reentry_cooldowns``.
        """
        if not isinstance(data, dict) or "reentry_cooldowns" in data:
            return data
        legacy = data.get("reentry_blocked_until")
        tracked = data.get("position")
        symbol = tracked.get("symbol") if isinstance(tracked, dict) else None
        if legacy is not None and isinstance(symbol, str):
            data = {key: value for key, value in data.items() if key != "reentry_blocked_until"}
            data["reentry_cooldowns"] = [{"symbol": symbol, "blocked_until": legacy}]
        return data


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
    # Highest confirmed inclusion block among the action's deliveries. This
    # lets the cycle prove its final read endpoint has caught up before it
    # labels post-action balances as final.
    confirmed_block_number: Annotated[int, Field(ge=0)] | None = None
    # The refusal's catalog code when status is refused, else empty.
    refusal_code: str = ""
    # Actual mint budget executed after any post-swap inventory resize.
    executed_budget_usdc: Decimal | None = None
    # Human-readable evidence for the outcome.
    diagnostic: str = ""


class CycleReconciliation(BaseModel):
    """Carry one coherent on-chain reconciliation snapshot."""

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
    # The symbol whose stock balance held_stock_quantity measures; None
    # when no stock is held or the balance is not one pool's inventory.
    held_symbol: str | None = None
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
    # The final reconciled on-chain state after any live action.
    reconciliation: CycleReconciliation
    # The pre-action snapshot the policy actually decided on. It equals the
    # final reconciliation for dry runs and no-action cycles.
    decision_reconciliation: CycleReconciliation | None = None
    # False only when a live action confirmed but the primary read endpoint
    # failed to reach its inclusion block within the bounded visibility wait.
    final_reconciliation_verified: bool = True
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

    def dry_run_recenter(
        self,
        symbol: str,
        token_id: int,
        width_spacings: int | None,
        budget_usdc: Decimal | None,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> object:
        """Preflight one same-pool recenter without broadcasting."""
        ...

    def dry_run_switch(
        self,
        from_symbol: str,
        token_id: int,
        to_symbol: str,
        width_spacings: int | None,
        budget_usdc: Decimal,
        key_bytes: bytes,
        ephemeral_key: bool = False,
    ) -> object:
        """Preflight a cross-pool replacement without broadcasting."""
        ...

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


def _action_hashes_fees_and_block(
    report: LpActionExecutionReport,
) -> tuple[tuple[str, ...], int, int | None]:
    """Collect one action's delivery hashes, total fees, and latest confirmed block."""
    hashes = tuple(step.transaction_hash for step in report.steps)
    fees = sum(step.fee_wei or 0 for step in report.steps)
    blocks: list[int] = []
    for step in report.steps:
        if step.status != "confirmed":
            continue
        block_number = getattr(step, "block_number", None)
        if isinstance(block_number, int):
            blocks.append(block_number)
    return hashes, fees, max(blocks) if blocks else None


def _action_hashes_and_fees(report: LpActionExecutionReport) -> tuple[tuple[str, ...], int]:
    """Backward-compatible action summary used by the monitor-only watchtower."""
    hashes, fees, _ = _action_hashes_fees_and_block(report)
    return hashes, fees


def _price_at_tick(
    tick: int,
    *,
    stock_is_token0: bool,
    stock_decimals: int,
) -> Decimal:
    """Return one Slipstream tick boundary in human USDC per stock.

    Args:
        tick: The pool tick boundary.
        stock_is_token0: Whether the stock is token0 rather than token1.
        stock_decimals: Decimal count of the stock token.

    Returns:
        The boundary price in USDC per whole stock token.
    """
    with localcontext() as context:
        context.prec = MATH_PRECISION
        sqrt_ratio = int((TICK_PRICE_RATIO**tick).sqrt() * Decimal(1 << 96))
    return price_usdc_per_stock(
        sqrt_ratio,
        stock_is_token0,
        stock_decimals,
        6,
    )


def _cooldown_until(book: CycleStateBook, symbol: str) -> datetime | None:
    """Read one pool's re-entry cooldown from the book.

    Args:
        book: The persisted cycle book.
        symbol: The registry-matched stock symbol being looked up.

    Returns:
        The pool's blocked-until instant, or None when clear.
    """
    for cooldown in book.reentry_cooldowns:
        if cooldown.symbol.lower() == symbol.lower():
            return cooldown.blocked_until
    return None


def _book_with_cooldowns(
    book: CycleStateBook, updates: Mapping[str, datetime | None]
) -> CycleStateBook:
    """Merge per-pool cooldown updates into the book.

    A None update clears its pool's cooldown; pools not named keep theirs.

    Args:
        book: The book being updated.
        updates: The per-symbol blocked-until instants to merge.

    Returns:
        The book carrying the merged cooldown map.
    """
    lowered = {symbol.lower(): until for symbol, until in updates.items()}
    kept = tuple(
        cooldown for cooldown in book.reentry_cooldowns if cooldown.symbol.lower() not in lowered
    )
    fresh = tuple(
        ReentryCooldown(symbol=symbol, blocked_until=until)
        for symbol, until in lowered.items()
        if until is not None
    )
    return book.model_copy(update={"reentry_cooldowns": kept + fresh})


class CycleRunner:
    """Run one complete reconcile-decide-act cycle."""

    def __init__(
        self,
        symbol: str | None,
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
        sleep: Callable[[float], None] = time.sleep,
        switch_margin_fraction: Decimal = DEFAULT_SWITCH_MARGIN_FRACTION,
    ) -> None:
        """Configure one cycle runner over every injectable boundary.

        Args:
            symbol: The registry-matched symbol this cycle manages, or None
                to run the cross-board selector over every verified pool.
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
            sleep: Injected delay used only for bounded post-action RPC catch-up.
            switch_margin_fraction: The relative APR margin another pool
                must beat the held pool by before a switch fires.
        """
        self._symbol = symbol.strip() if symbol is not None else None
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
        self._sleep = sleep
        self._switch_margin_fraction = switch_margin_fraction
        self._last_reconciliation: CycleReconciliation | None = None
        # One cycle process enumerates the board at most once; reconcile and
        # decide share the cached listing and its snapshot block.
        self._board: tuple[BoardListing, ...] | None = None
        self._board_block: int | None = None

    @property
    def selector_mode(self) -> bool:
        """Return whether this runner selects across the whole B20 board."""
        return self._symbol is None

    def _ensure_board(self) -> None:
        """Enumerate the verified board once per runner unless cached.

        Raises:
            ExecutionUnavailableError: If discovery cannot complete or does
                not verify.
        """
        if self._board is None or self._board_block is None:
            self._board, self._board_block = self._sources.enumerate_pools()

    def _board_listings(self) -> tuple[BoardListing, ...]:
        """Enumerate the verified board once per runner, cached.

        Returns:
            Every registry-symbolled verified pool, ordered by symbol.

        Raises:
            ExecutionUnavailableError: If discovery cannot complete or does
                not verify.
        """
        self._ensure_board()
        assert self._board is not None  # noqa: S101 - populated by _ensure_board
        return self._board

    def run(
        self,
        mode: CycleMode,
        key_bytes: bytes | None = None,
        reference_price_usdc: Decimal | None = None,
        reference_age_seconds: int | None = None,
        reference_prices_by_symbol: Mapping[str, Decimal] | None = None,
    ) -> CycleReport:
        """Run one complete cycle.

        Args:
            mode: Dry runs decide without acting; live cycles may broadcast.
            key_bytes: The signing key for live cycles; loaded by the caller
                from the sealed source and never persisted here.
            reference_price_usdc: Optional injected real-market quote for the
                pinned symbol.
            reference_age_seconds: Age of the injected quote.
            reference_prices_by_symbol: Optional per-symbol injected quotes;
                selector mode consumes these and ignores the single quote.

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
        decision_reconciliation = reconciliation
        final_reconciliation_verified = True
        self._last_reconciliation = reconciliation
        # A present quote with an absent age counts as fresh (age zero);
        # only an absent quote leaves the reference unset.
        if reference_price_usdc is not None and reference_age_seconds is None:
            reference_age_seconds = 0
        if reference_prices_by_symbol and reference_age_seconds is None:
            reference_age_seconds = 0
        decision_report: StrategyDecisionReport | None = None
        actions: list[CycleActionRecord] = []
        halted_reason = ""
        if reconciliation.out_of_band:
            halted_reason = reconciliation.out_of_band
        else:
            book = self._adopt_into_book(book, reconciliation)
            decision_report = self._decide(
                book,
                reference_price_usdc,
                reference_age_seconds,
                reference_prices_by_symbol or {},
            )
            outcome = decision_report.outcome
            if mode is CycleMode.LIVE and outcome.decision.action is not PolicyActionKind.HOLD:
                if self._executor is None or key_bytes is None:
                    raise ValueError("a live cycle requires its signing key and executor")
                actions, halted_reason, book = self._act(
                    book, outcome, key_bytes, decision_report.symbol, decision_report.switch
                )
                final_reconciliation_verified = self._await_post_action_visibility(tuple(actions))
                if not final_reconciliation_verified and not halted_reason:
                    target = max(
                        (action.confirmed_block_number or 0 for action in actions), default=0
                    )
                    halted_reason = (
                        "post-action reconciliation is unverified because the primary RPC "
                        f"did not reach confirmed block {target} within the bounded wait"
                    )
            final_reconciliation = self._reconcile(book)
            if not final_reconciliation_verified:
                final_reconciliation = final_reconciliation.model_copy(
                    update={
                        "diagnostics": final_reconciliation.diagnostics
                        + (
                            "WARNING: final balances may lag a confirmed action because the "
                            "primary RPC did not prove visibility of its inclusion block.",
                        )
                    }
                )
            self._last_reconciliation = final_reconciliation
            if not final_reconciliation.out_of_band:
                book = self._rebuild_book(
                    book,
                    final_reconciliation,
                    decision_report.outcome.next_state,
                    decision_report.symbol,
                    decision_report.outcome.decision.action,
                )
            elif not halted_reason:
                halted_reason = final_reconciliation.out_of_band
            reconciliation = final_reconciliation
        self._state_store.save(book)
        report = self._assemble_report(
            started_at,
            mode,
            reconciliation,
            decision_reconciliation,
            final_reconciliation_verified,
            decision_report,
            tuple(actions),
            halted_reason,
        )
        self._record(report)
        return report

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _reconcile_symbol(self, book: CycleStateBook) -> str:
        """Resolve the symbol this reconciliation anchors its reads on.

        Pinned cycles always anchor on the pinned symbol. Selector cycles
        anchor on the tracked position's symbol, then the held inventory's,
        and finally on the deterministic first board listing so a flat cycle
        still enumerates the Safe's NFPM inventory through a concrete pool.

        Args:
            book: The persisted book whose tracked state names the anchor.

        Returns:
            The anchor symbol, or the selector's reserved name when the
            board enumerated nothing.
        """
        if self._symbol is not None:
            return self._symbol
        if book.position is not None:
            return book.position.symbol
        if book.held_inventory is not None:
            return book.held_inventory.symbol
        listings = self._board_listings()
        return listings[0].symbol if listings else SELECTOR_SYMBOL

    def _reconcile(self, book: CycleStateBook) -> CycleReconciliation:
        """Read the complete on-chain state one cycle acts on.

        Args:
            book: The persisted book whose tracked position reconciles.

        Returns:
            The complete reconciliation with any out-of-band condition.

        Raises:
            ExecutionUnavailableError: If a live read cannot complete.
            ValueError: If the anchor symbol resolves to nothing.
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
        anchor = self._reconcile_symbol(book)
        empty_count = 0
        if anchor == SELECTOR_SYMBOL:
            live_ids: tuple[int, ...] = ()
            diagnostics.append("the board enumerated no verified pools; no inventory read ran")
        else:
            inventory = self._reads.safe_position_inventory(anchor)
            live_ids = tuple(position.token_id for position in inventory.live_positions)
            empty_count = inventory.empty_count
        tracked_status: LpPositionStatusReport | None = None
        tracked_token_id: int | None = None
        tracked_staked = False
        out_of_band = ""
        tracked = book.position
        if tracked is not None:
            tracked_status = self._reads.position_status(
                tracked.symbol, tracked.token_id, entry_cost_usdc=tracked.committed_usd
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
        # The stock sweep finds stray stock the book does not record: pinned
        # cycles read the one anchor token, selector cycles sweep every board
        # token so unsold stock in any pool is adopted with its own symbol.
        stock_units = 0
        held_quantity = Decimal("0")
        held_symbol: str | None = None
        stray_stocks: list[tuple[str, str, int]] = []
        if book.held_inventory is not None:
            held_token = book.held_inventory.token_address
            held_units = self._balances.fetch_token_balance(held_token, self._safe_address)
            held_quantity = Decimal(held_units).scaleb(-self._sources.token_decimals(held_token))
            stock_units = held_units
            held_symbol = book.held_inventory.symbol
            if held_quantity == 0 and tracked_token_id is None:
                diagnostics.append("recorded held inventory no longer exists on-chain; clearing")
        elif self.selector_mode and book.position is None:
            for listing in self._board_listings():
                token = self._stock_token_of_pool(listing.pool)
                units = self._balances.fetch_token_balance(token, self._safe_address)
                if units > 0:
                    stray_stocks.append((listing.symbol, token, units))
        elif anchor != SELECTOR_SYMBOL:
            stock_address = self._stock_token_address_for(anchor)
            stock_units = self._balances.fetch_token_balance(stock_address, self._safe_address)
            if stock_units > 0:
                stray_stocks.append((anchor, stock_address, stock_units))
        stray = stray_stocks[0] if len(stray_stocks) == 1 else None
        if len(stray_stocks) > 1:
            if not out_of_band:
                out_of_band = (
                    "the Safe holds unrecorded stock in multiple pools ("
                    + ", ".join(symbol for symbol, _, _ in stray_stocks)
                    + "); refusing the cycle until reconciled"
                )
        elif stray is not None and not out_of_band:
            stray_symbol, stray_token, stray_units = stray
            stray_quantity = Decimal(stray_units).scaleb(-self._sources.token_decimals(stray_token))
            if tracked_token_id is None and book.held_inventory is None:
                held_quantity = stray_quantity
                held_symbol = stray_symbol
                stock_units = stray_units
                diagnostics.append(
                    f"adopting unrecorded {stray_symbol} stock balance "
                    f"{stray_quantity} as held inventory; the policy's convergence "
                    "machinery will unwind it"
                )
            else:
                stock_units = stray_units
                diagnostics.append(
                    f"Safe holds {stray_quantity} {stray_symbol} stock beside the "
                    "tracked position; the exit swap will convert it"
                )
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
            symbol=anchor,
            safe_usdc_units=safe_usdc,
            relayer_eth_wei=relayer_eth,
            safe_stock_units=stock_units,
            inventory_live_token_ids=live_ids,
            inventory_empty_count=empty_count,
            tracked_status=tracked_status,
            tracked_token_id=tracked_token_id,
            tracked_staked=tracked_staked,
            held_stock_quantity=held_quantity,
            held_symbol=held_symbol,
            out_of_band=out_of_band,
            diagnostics=tuple(diagnostics),
        )

    def _await_post_action_visibility(self, actions: tuple[CycleActionRecord, ...]) -> bool:
        """Wait until the primary RPC reaches every confirmed action's inclusion block.

        The execution layer can confirm a delivery through a secondary receipt
        endpoint while the primary read endpoint is temporarily behind. Final
        reconciliation must not label pre-action balances as post-action truth.
        """
        target_block = max((action.confirmed_block_number or 0 for action in actions), default=0)
        if target_block == 0:
            return True
        fetch_block_number = getattr(self._balances, "fetch_block_number", None)
        if fetch_block_number is None:
            # Test doubles and legacy boundaries without block reads cannot
            # prove visibility; production ExecutorRpcBackend always can.
            return False
        failure = ""
        for attempt in range(POST_ACTION_VISIBILITY_ATTEMPTS):
            try:
                visible_block = int(fetch_block_number())
            except (ExecutionUnavailableError, ValueError, TypeError) as error:
                failure = str(error)
            else:
                if visible_block >= target_block:
                    return True
                failure = f"latest block {visible_block} is behind target {target_block}"
            if attempt + 1 < POST_ACTION_VISIBILITY_ATTEMPTS:
                backoff = min(
                    POST_ACTION_VISIBILITY_BASE_BACKOFF_SECONDS * (2**attempt),
                    POST_ACTION_VISIBILITY_MAX_BACKOFF_SECONDS,
                )
                _cycle_progress(
                    f"post-action RPC visibility attempt {attempt + 1} of "
                    f"{POST_ACTION_VISIBILITY_ATTEMPTS} failed ({failure}); "
                    f"backing off {backoff:.1f}s"
                )
                self._sleep(backoff)
        _cycle_progress(
            f"post-action RPC visibility remained behind confirmed block {target_block}: {failure}"
        )
        return False

    def _stock_token_address_for(self, symbol: str) -> str:
        """Resolve one symbol's stock token address from the registry.

        Args:
            symbol: The registry-matched stock symbol.

        Returns:
            The B20 contract address for that symbol.

        Raises:
            ValueError: If the symbol is outside the official registry.
        """
        token = self._sources.symbol_address(symbol)
        if token is None:
            raise ValueError(f"symbol {symbol!r} is not in the official B20 registry")
        return token

    def _stock_token_of_pool(self, pool: PoolCandidate) -> str:
        """Read one pool's non-USDC side, the B20 stock token.

        Args:
            pool: The verified pool whose stock side is needed.

        Returns:
            The stock token contract address.
        """
        normalized_usdc = BASE_USDC_ADDRESS.lower()
        return (
            pool.token1_address
            if pool.token0_address.lower() == normalized_usdc
            else pool.token0_address
        )

    def _pool_for_symbol(self, symbol: str) -> PoolCandidate:
        """Resolve one symbol's live pool through the cached board or sources.

        Selector cycles serve the decision symbol straight from the cached
        board listing (the same snapshot the decision ran over); pinned
        cycles resolve through the known-pool fast path as before.

        Args:
            symbol: The registry-matched stock symbol.

        Returns:
            The symbol's verified pool candidate.
        """
        if self._board is not None:
            for listing in self._board:
                if listing.symbol.lower() == symbol.lower():
                    return listing.pool
        pool, _ = self._sources.resolve_pool(symbol)
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
            committed, planned_symbol = self._last_mint_plan()
            adopted_symbol = planned_symbol or (
                self._symbol if self._symbol is not None else reconciliation.symbol
            )
            if adopted_symbol == SELECTOR_SYMBOL:
                adopted_symbol = reconciliation.symbol
            pool = (
                status.pool_address
                if status is not None
                else self._pool_for_symbol(adopted_symbol).pool_address
            )
            if committed is not None:
                updates["position"] = TrackedPosition(
                    symbol=adopted_symbol,
                    token_id=reconciliation.tracked_token_id,
                    pool_address=pool,
                    committed_usd=committed,
                    entered_at=self._now(),
                )
        if (
            book.held_inventory is None
            and reconciliation.held_stock_quantity > 0
            and reconciliation.held_symbol is not None
            and reconciliation.tracked_token_id is None
            and not reconciliation.out_of_band
        ):
            updates["held_inventory"] = HeldInventoryRecord(
                symbol=reconciliation.held_symbol,
                token_address=self._stock_token_address_for(reconciliation.held_symbol),
                stock_quantity=reconciliation.held_stock_quantity,
                held_since=self._now(),
            )
        if not updates:
            return book
        return book.model_copy(update=updates)

    def _last_mint_plan(self) -> tuple[Decimal | None, str | None]:
        """Read the newest audited mint plan, the adoption cost basis.

        Returns:
            The newest mint budget (or None) and the symbol that plan named
            (or None when the record carries no symbol).
        """
        if self._audit_reader is None:
            return None, None
        for record in reversed(self._audit_reader.recent_records()):
            if record.event_type is not AuditEventType.LP_MINT_PLANNED:
                continue
            payload = json.loads(record.payload_json)
            budget = payload.get("budget_usdc")
            symbol = payload.get("symbol")
            if isinstance(budget, str) and Decimal(budget) > 0:
                return Decimal(budget), symbol if isinstance(symbol, str) else None
        return None, None

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
            stock_address = self._stock_token_address_for(book.position.symbol)
            pool = self._pool_for_symbol(book.position.symbol)
            stock_decimals = self._sources.token_decimals(stock_address)
            stock_is_token0 = normalize_evm_address(pool.token0_address) == normalize_evm_address(
                stock_address
            )
            first_edge_price = _price_at_tick(
                status.position.tick_lower,
                stock_is_token0=stock_is_token0,
                stock_decimals=stock_decimals,
            )
            second_edge_price = _price_at_tick(
                status.position.tick_upper,
                stock_is_token0=stock_is_token0,
                stock_decimals=stock_decimals,
            )
            position = PolicyPosition(
                pool_address=status.pool_address,
                token_address=stock_address,
                price_range=AlignedPriceRange(
                    lower_tick=status.position.tick_lower,
                    upper_tick=status.position.tick_upper,
                    lower_price=min(first_edge_price, second_edge_price),
                    upper_price=max(first_edge_price, second_edge_price),
                ),
                committed_usd=book.position.committed_usd,
                entered_at=book.position.entered_at,
                out_of_range_since=book.position.out_of_range_since,
                out_of_range_side=book.position.out_of_range_side,
            )
        held: HeldInventory | None = None
        if book.held_inventory is not None:
            held = HeldInventory(
                pool_address=self._pool_for_symbol(book.held_inventory.symbol).pool_address,
                token_address=book.held_inventory.token_address,
                stock_quantity=book.held_inventory.stock_quantity,
                held_since=book.held_inventory.held_since,
                origin=book.held_inventory.origin,
            )
        day = self._now().astimezone(POLICY_TIMEZONE).date()
        same_day = book.day == day and book.day_start_equity_usd is not None
        day_start = book.day_start_equity_usd if same_day else None
        if day_start is None:
            day_start = Decimal(reconciliation.safe_usdc_units).scaleb(-6)
        return PolicyState(
            day=day if same_day else None,
            day_start_equity_usd=day_start,
            halted_day=book.halted_day if same_day else None,
            position=position,
            held_inventory=held,
            reentry_blocked_until=None,
        )

    def _cooldown_map(self, book: CycleStateBook) -> dict[str, datetime]:
        """Read the book's per-pool re-entry cooldowns as a mapping.

        Args:
            book: The persisted cycle book.

        Returns:
            Every pool's blocked-until instant keyed by symbol.
        """
        return {cooldown.symbol: cooldown.blocked_until for cooldown in book.reentry_cooldowns}

    def _decide(
        self,
        book: CycleStateBook,
        reference_price_usdc: Decimal | None,
        reference_age_seconds: int | None,
        reference_prices_by_symbol: Mapping[str, Decimal],
    ) -> StrategyDecisionReport:
        """Run the complete locked engine over one live observation.

        Pinned cycles decide one symbol's observation exactly as before;
        selector cycles hand the whole enumerated board to the cross-board
        selector, whose verdict this same report shape carries.

        Args:
            book: The reconciled book threading the engine state.
            reference_price_usdc: Optional injected real-market quote for
                the pinned symbol.
            reference_age_seconds: Age of the injected quote.
            reference_prices_by_symbol: Per-symbol injected quotes for
                selector mode; the pinned quote is ignored there.

        Returns:
            The complete decision report with its typed outcome.

        Raises:
            ExecutionUnavailableError: If discovery or a read fails.
            ValueError: If the symbol resolves to no pool, or the selector's
                board enumerates nothing.
        """
        assert self._last_reconciliation is not None  # noqa: S101 - set by run()
        if self._symbol is None:
            return self._decide_selector(book, reference_prices_by_symbol, reference_age_seconds)
        pool, snapshot_block = self._sources.resolve_pool(self._symbol)
        state = self._policy_state(book, self._last_reconciliation).model_copy(
            update={"reentry_blocked_until": _cooldown_until(book, self._symbol)}
        )
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
        # Production actions are authoritative to the exact resolved Aerodrome
        # pool. External equity references remain report-only diagnostics and
        # can never directly trigger a buy, sell, mint, burn, or defensive exit.
        observation = observation.model_copy(update={"reference_enforcement_enabled": False})
        notes = notes + (
            "external reference is diagnostic-only; scheduled actions use the "
            "resolved Aerodrome pool's on-chain price and state",
        )
        # Include the tracked LP mark in portfolio equity.
        tracked_status = self._last_reconciliation.tracked_status
        if tracked_status is not None and tracked_status.position_value_usdc is not None:
            lp_value = tracked_status.position_value_usdc
            observation = observation.model_copy(
                update={"equity_usd": observation.equity_usd + lp_value}
            )
            notes = notes + (
                f"equity includes tracked LP marked value {lp_value} USDC; "
                f"portfolio equity is {observation.equity_usd} USDC",
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

    def _decide_selector(
        self,
        book: CycleStateBook,
        reference_prices_by_symbol: Mapping[str, Decimal],
        reference_age_seconds: int | None,
    ) -> StrategyDecisionReport:
        """Run the cross-board selector over every verified B20 pool.

        Args:
            book: The reconciled book threading the engine state.
            reference_prices_by_symbol: Per-symbol injected real-market
                quotes; pools without one block fail-closed as usual.
            reference_age_seconds: Age of the injected quotes.

        Returns:
            The complete decision report for the selected or held pool.

        Raises:
            ExecutionUnavailableError: If discovery or a read fails.
            ValueError: If the board enumerates nothing.
        """
        assert self._last_reconciliation is not None  # noqa: S101 - set by run()
        self._ensure_board()
        listings = self._board or ()
        snapshot_block = self._board_block
        if not listings or snapshot_block is None:
            raise ValueError("no verified B20 pools were enumerated for the board")
        options, aero_price, gas_price, notes = assemble_board(
            self._sources,
            listings,
            snapshot_block,
            self._now(),
            None,
            reference_prices_by_symbol,
            reference_age_seconds,
            self._safe_address,
        )
        # Apply the same pool-authoritative doctrine to every selector option.
        options = tuple(
            option.model_copy(
                update={
                    "observation": option.observation.model_copy(
                        update={"reference_enforcement_enabled": False}
                    )
                }
            )
            for option in options
        )
        # Selector observations start from loose Safe balances. When an LP is
        # already tracked, add its live marked value to every board option
        # before policy evaluation so the daily-loss guard sees total managed
        # portfolio equity rather than falsely treating deployed LP capital as
        # a drawdown.
        tracked_status = self._last_reconciliation.tracked_status
        if tracked_status is not None and tracked_status.position_value_usdc is not None:
            lp_value = tracked_status.position_value_usdc
            options = tuple(
                option.model_copy(
                    update={
                        "observation": option.observation.model_copy(
                            update={"equity_usd": option.observation.equity_usd + lp_value}
                        )
                    }
                )
                for option in options
            )
            notes = notes + (f"selector equity includes tracked LP marked value {lp_value} USDC",)
        notes = notes + (
            "external references are diagnostic-only; selector actions use each "
            "resolved Aerodrome pool's on-chain price and state",
        )
        engine = PolicyEngine(LOCKED_POLICY_PARAMETERS, load_event_calendar())
        selection = select_board(
            engine,
            self._policy_state(book, self._last_reconciliation),
            options,
            self._cooldown_map(book),
            self._switch_margin_fraction,
        )
        decision_option = self._option_for_selection(selection, options)
        window = evaluate_event_window(self._now(), decision_option.token_address, engine.calendar)
        switch = selection.switch
        return StrategyDecisionReport(
            symbol=decision_option.symbol,
            pool_address=decision_option.pool_address,
            snapshot_block=snapshot_block,
            observed_at=self._now(),
            amm_price_usdc=decision_option.observation.amm_price_usdc,
            emissions_apr=decision_option.observation.emissions_apr,
            aero_price_usdc=aero_price,
            pool_depth_usd=decision_option.observation.pool_depth_usd,
            equity_usd=decision_option.observation.equity_usd,
            gas_price_gwei=gas_price,
            reference_price_usdc=reference_prices_by_symbol.get(decision_option.symbol),
            event_window=window,
            outcome=selection.outcome,
            input_notes=notes + (selection.summary,),
            selector_mode=True,
            board=selection.evaluations,
            switch=switch,
            board_summary=selection.summary,
        )

    def _option_for_selection(
        self, selection: "BoardSelection", options: tuple[PoolBoardOption, ...]
    ) -> PoolBoardOption:
        """Resolve the board option the selection's decision covers.

        Args:
            selection: The selector's complete verdict.
            options: The assembled board options in listing order.

        Returns:
            The held, selected, or closest-call option; the report names its
            pool, mirroring the decide surface's fallback.
        """
        symbol = selection.selected_symbol
        if symbol is not None:
            for option in options:
                if option.symbol == symbol:
                    return option
        if selection.switch is not None:
            for option in options:
                if option.pool_address == selection.switch.to_pool_address:
                    return option
        # Nothing qualified: name the closest-call pool - the highest
        # emissions APR on the board, ties lexicographic - so both surfaces
        # report the same pool for the same board.
        closest = closest_call_evaluation(selection.evaluations)
        if closest is not None:
            for option in options:
                if option.symbol == closest.symbol:
                    return option
        return options[0]

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------

    def _act(  # noqa: PLR0915, PLR0912 - one fixed policy mapping, explicit branches
        self,
        book: CycleStateBook,
        outcome: PolicyOutcome,
        key_bytes: bytes,
        decision_symbol: str,
        switch: SwitchDirective | None = None,
    ) -> tuple[list[CycleActionRecord], str, CycleStateBook]:
        """Execute the policy-authorized action through audited surfaces."""
        executor = self._executor
        assert executor is not None  # noqa: S101 - the caller verified liveness
        decision = outcome.decision
        action = decision.action
        records: list[CycleActionRecord] = []
        halted = ""
        tracked_symbol = book.position.symbol if book.position is not None else None

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
                completed_steps = tuple(getattr(error, "completed_steps", ()))
                hashes = tuple(step.transaction_hash for step in completed_steps)
                fees = sum(step.fee_wei or 0 for step in completed_steps)
                blocks = tuple(
                    int(step.block_number)
                    for step in completed_steps
                    if step.status == "confirmed"
                    and getattr(step, "block_number", None) is not None
                )
                records.append(
                    CycleActionRecord(
                        action=name,
                        status="refused",
                        transaction_hashes=hashes,
                        fee_wei=fees,
                        confirmed_block_number=max(blocks) if blocks else None,
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
            hashes, fees, confirmed_block = _action_hashes_fees_and_block(report)
            mint_plan = getattr(report.build, "plan", None) if name == "mint" else None
            executed_budget = (
                getattr(mint_plan, "budget_usdc", None) if mint_plan is not None else None
            )
            records.append(
                CycleActionRecord(
                    action=name,
                    status="completed" if _action_completed(report) else "failed",
                    transaction_hashes=hashes,
                    fee_wei=fees,
                    confirmed_block_number=confirmed_block,
                    executed_budget_usdc=executed_budget,
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
            width = self._width_from_range(decision.price_range, decision_symbol)
            if size is None or size <= 0 or width is None:
                halted = "the enter decision carried no positive size or tick-aligned width"
                return records, halted, book
            budget: Decimal = size
            mint_width: int = width
            if not run(
                "mint",
                lambda: executor.execute_mint(
                    decision_symbol, budget, mint_width, key_bytes, confirm_broadcast=True
                ),
            ):
                held_quantity = self._live_stock_quantity(decision_symbol)
                failed_book = book
                if held_quantity > 0:
                    failed_book = book.model_copy(
                        update={
                            "held_inventory": HeldInventoryRecord(
                                symbol=decision_symbol,
                                token_address=self._stock_token_address_for(decision_symbol),
                                stock_quantity=held_quantity,
                                held_since=self._now(),
                                origin="failed_entry",
                            )
                        }
                    )
                return records, halted, failed_book
            token_id = self._decode_mint_token_id(records[-1])
            if token_id is None:
                halted = "the minted position id could not be decoded from the receipt"
                return records, halted, book
            minted_id: int = token_id
            if not run(
                "stake",
                lambda: executor.execute_stake(
                    decision_symbol, minted_id, key_bytes, confirm_broadcast=True
                ),
            ):
                return records, halted, book
            return (
                records,
                halted,
                self._book_with_position(
                    book,
                    decision_symbol,
                    minted_id,
                    records[-2].executed_budget_usdc or budget,
                ),
            )

        if action is PolicyActionKind.POOL_SWITCH:
            # A qualified cross-pool switch: exit the tracked position first,
            # then mint and stake the better pool - one position at every
            # intermediate instant, and a failed exit halts before any entry.
            if switch is None:
                halted = "the pool switch carried no qualified directive"
                return records, halted, book
            if book.position is None or tracked_symbol is None:
                halted = "the selector authorized a switch while flat"
                return records, halted, book
            size = decision.size_usd
            width = self._width_from_range(decision.price_range, switch.to_symbol)
            if size is None or size <= 0 or width is None:
                halted = "the switch decision carried no positive size or tick-aligned width"
                return records, halted, book
            switch_budget: Decimal = size
            switch_width: int = width
            # Prove the target can be funded from a conservative projection of
            # the complete source exit before unstaking the currently earning
            # LP. A refusal leaves the source NFT untouched and staked.
            try:
                executor.dry_run_switch(
                    tracked_symbol,
                    book.position.token_id,
                    switch.to_symbol,
                    switch_width,
                    switch_budget,
                    key_bytes,
                )
            except (LpExecutionRefusalError, LpPlanRefusalError) as error:
                code = str(getattr(error, "code", "plan_refused"))
                records.append(
                    CycleActionRecord(
                        action="switch_preflight",
                        status="refused",
                        refusal_code=code,
                        diagnostic=str(error),
                    )
                )
                halted = f"the switch preflight refused [{code}]"
                return records, halted, book
            except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
                records.append(
                    CycleActionRecord(
                        action="switch_preflight", status="failed", diagnostic=str(error)
                    )
                )
                halted = f"the switch preflight failed: {error}"
                return records, halted, book
            if not self._exit_position(executor, book, key_bytes, run):
                return records, halted, book
            switched_book = book.model_copy(update={"position": None, "held_inventory": None})
            if not run(
                "mint",
                lambda: executor.execute_mint(
                    switch.to_symbol, switch_budget, switch_width, key_bytes, confirm_broadcast=True
                ),
            ):
                return records, halted, switched_book
            token_id = self._decode_mint_token_id(records[-1])
            if token_id is None:
                halted = "the switched position id could not be decoded from the receipt"
                return records, halted, switched_book
            switched_id: int = token_id
            if not run(
                "stake",
                lambda: executor.execute_stake(
                    switch.to_symbol, switched_id, key_bytes, confirm_broadcast=True
                ),
            ):
                return records, halted, switched_book
            return (
                records,
                halted,
                self._book_with_position(
                    switched_book,
                    switch.to_symbol,
                    switched_id,
                    records[-2].executed_budget_usdc or switch_budget,
                ),
            )

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
            # Preflight the complete replacement before touching the live LP.
            if action is PolicyActionKind.RECENTER:
                if (
                    tracked_symbol is None
                    or decision.size_usd is None
                    or decision.size_usd <= 0
                    or decision.price_range is None
                ):
                    halted = "the recenter decision carried no complete fresh entry"
                    return records, halted, book
                preflight_width = self._width_from_range(decision.price_range, tracked_symbol)
                if preflight_width is None:
                    halted = "the recenter decision carried no complete fresh entry"
                    return records, halted, book
                try:
                    executor.dry_run_recenter(
                        tracked_symbol,
                        book.position.token_id,
                        preflight_width,
                        decision.size_usd,
                        key_bytes,
                    )
                except (LpExecutionRefusalError, LpPlanRefusalError) as error:
                    code = str(getattr(error, "code", "plan_refused"))
                    records.append(
                        CycleActionRecord(
                            action="recenter_preflight",
                            status="refused",
                            refusal_code=code,
                            diagnostic=str(error),
                        )
                    )
                    halted = f"the recenter preflight refused [{code}]"
                    return records, halted, book
                except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
                    records.append(
                        CycleActionRecord(
                            action="recenter_preflight", status="failed", diagnostic=str(error)
                        )
                    )
                    halted = f"the recenter preflight failed: {error}"
                    return records, halted, book

            exit_ok = (
                self._burn_without_swap(executor, book, key_bytes, run)
                if action is PolicyActionKind.RECENTER
                else self._exit_position(executor, book, key_bytes, run)
            )
            if not exit_ok:
                return records, halted, book
            if action is PolicyActionKind.RECENTER:
                size = decision.size_usd
                width = self._width_from_range(decision.price_range, tracked_symbol)
                if size is None or size <= 0 or width is None or tracked_symbol is None:
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
                        tracked_symbol,
                        recenter_budget,
                        recenter_width,
                        key_bytes,
                        confirm_broadcast=True,
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
                        tracked_symbol, token_id, key_bytes, confirm_broadcast=True
                    ),
                ):
                    return (
                        records,
                        halted,
                        book.model_copy(update={"position": None, "held_inventory": None}),
                    )
                return (
                    records,
                    halted,
                    self._book_with_position(
                        book,
                        tracked_symbol,
                        token_id,
                        records[-2].executed_budget_usdc or size,
                    ),
                )
            return (
                records,
                halted,
                book.model_copy(update={"position": None, "held_inventory": None}),
            )

        if action is PolicyActionKind.STALE_LOW_BURN:
            if book.position is None or tracked_symbol is None:
                halted = "the engine authorized a stale-low burn while flat"
                return records, halted, book
            if not self._burn_without_swap(executor, book, key_bytes, run):
                return records, halted, book
            held_quantity = self._live_stock_quantity(tracked_symbol)
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
                            symbol=tracked_symbol,
                            token_address=self._stock_token_address_for(tracked_symbol),
                            stock_quantity=held_quantity,
                            held_since=self._now(),
                            origin="stale_low_exit",
                        ),
                    }
                ),
            )

        if action is PolicyActionKind.SELL_INVENTORY:
            if book.held_inventory is None:
                halted = "the engine authorized an inventory sale with nothing held"
                return records, halted, book
            held_symbol = book.held_inventory.symbol
            if not run(
                "exit_swap",
                lambda: executor.execute_exit_swap(held_symbol, key_bytes, confirm_broadcast=True),
            ):
                return records, halted, book
            return records, halted, book.model_copy(update={"held_inventory": None})

        halted = f"the cycle has no live mapping for action {action.value}"
        return records, halted, book

    def _book_with_position(
        self, book: CycleStateBook, symbol: str, token_id: int, committed: Decimal
    ) -> CycleStateBook:
        """Return the book carrying one freshly entered tracked position."""
        return book.model_copy(
            update={
                "position": TrackedPosition(
                    symbol=symbol,
                    token_id=token_id,
                    pool_address=self._pool_for_symbol(symbol).pool_address,
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
        symbol = tracked.symbol
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
        symbol = tracked.symbol
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

    def _live_stock_quantity(self, symbol: str) -> Decimal:
        """Read the Safe's whole-token stock balance right now."""
        stock_address = self._stock_token_address_for(symbol)
        units = self._balances.fetch_token_balance(stock_address, self._safe_address)
        return Decimal(units).scaleb(-self._sources.token_decimals(stock_address))

    def _width_from_range(
        self, price_range: AlignedPriceRange | None, symbol: str | None
    ) -> int | None:
        """Derive the explicit half width in tick spacings from a policy range.

        The engine's grid alignment can leave a non-integral spacing half
        width (a 70-tick span on a spacing-10 pool), so the mapping rounds
        up: the executed range always carries at least the policy's width,
        and the planner's own ceiling clamps anything wider.
        """
        if price_range is None or symbol is None:
            return None
        pool = self._pool_for_symbol(symbol)
        half_ticks = (price_range.upper_tick - price_range.lower_tick) // 2
        if half_ticks < 1:
            return None
        return -(-half_ticks // pool.tick_spacing)

    def _rebuild_book(
        self,
        book: CycleStateBook,
        reconciliation: CycleReconciliation,
        next_state: PolicyState,
        decision_symbol: str | None = None,
        decision_action: PolicyActionKind = PolicyActionKind.HOLD,
    ) -> CycleStateBook:
        """Rebuild the book from post-action chain truth plus engine state."""
        position = book.position
        if (
            position is not None
            and reconciliation.tracked_token_id is not None
            and reconciliation.tracked_token_id != position.token_id
        ):
            position = position.model_copy(update={"token_id": reconciliation.tracked_token_id})
        if (
            position is not None
            and next_state.position is not None
            and decision_action is PolicyActionKind.HOLD
        ):
            position = position.model_copy(
                update={
                    "out_of_range_since": next_state.position.out_of_range_since,
                    "out_of_range_side": next_state.position.out_of_range_side,
                }
            )
        held = book.held_inventory
        if held is not None and reconciliation.held_stock_quantity == 0:
            held = None
        elif (
            held is None
            and reconciliation.held_stock_quantity > 0
            and reconciliation.held_symbol is not None
            and reconciliation.tracked_token_id is None
        ):
            held = HeldInventoryRecord(
                symbol=reconciliation.held_symbol,
                token_address=self._stock_token_address_for(reconciliation.held_symbol),
                stock_quantity=reconciliation.held_stock_quantity,
                held_since=self._now(),
            )
        # The engine's successor state names at most one pool's cooldown -
        # the pool the decision ran over - so only that pool's entry merges.
        cooldown_symbol = decision_symbol or (position.symbol if position is not None else None)
        rebuilt = _book_with_cooldowns(
            book,
            {cooldown_symbol: next_state.reentry_blocked_until}
            if cooldown_symbol is not None
            else {},
        )
        return rebuilt.model_copy(
            update={
                "position": position,
                "held_inventory": held,
                "day": next_state.day,
                "day_start_equity_usd": next_state.day_start_equity_usd,
                "halted_day": next_state.halted_day,
                "updated_at": self._now(),
            }
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _assemble_report(
        self,
        started_at: datetime,
        mode: CycleMode,
        reconciliation: CycleReconciliation,
        decision_reconciliation: CycleReconciliation,
        final_reconciliation_verified: bool,
        decision_report: StrategyDecisionReport | None,
        actions: tuple[CycleActionRecord, ...],
        halted_reason: str,
    ) -> CycleReport:
        """Assemble the structured cycle report from its complete evidence."""
        report_symbol = (
            decision_report.symbol if decision_report is not None else reconciliation.symbol
        )
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
                started_at,
                self._stock_token_address_for(report_symbol)
                if report_symbol != SELECTOR_SYMBOL
                else BASE_USDC_ADDRESS,
                load_event_calendar(),
            ).description
            input_notes = ("the cycle refused out-of-band before deciding",)
        status = reconciliation.tracked_status
        pnl = status.unrealized_pnl_usdc if status is not None else None
        pnl_diagnostic = status.pnl_diagnostic if status is not None else "no tracked position"
        return CycleReport(
            started_at=started_at,
            mode=mode,
            symbol=report_symbol,
            reconciliation=reconciliation,
            decision_reconciliation=decision_reconciliation,
            final_reconciliation_verified=final_reconciliation_verified,
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
        record_cycle_report(self._audit_sink, report, self._now())


def record_cycle_report(audit_sink: AuditStore, report: CycleReport, created_at: datetime) -> None:
    """Append one cycle-summary audit record to the chain.

    The payload never carries credential fields; the audited summary is the
    shared shape every cycle-shaped surface records - the scheduled cycle
    and the range watchtower alike.

    Args:
        audit_sink: The store receiving the cycle-summary record.
        report: The complete cycle report being recorded.
        created_at: The record's timestamp, timezone-aware.
    """
    status = report.reconciliation.tracked_status
    audit_sink.append(
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
        created_at,
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


def _reference_price_from_environment(
    environ: Mapping[str, str],
) -> Decimal | dict[str, Decimal] | None:
    """Read the optional injected reference quote(s) from the environment.

    The single form (``318.5``) quotes the pinned symbol; the map form
    (``AAPLc=318.5,FIXc=100``) carries one quote per symbol for the
    cross-board selector.

    Args:
        environ: The environment mapping carrying the optional quote.

    Returns:
        One Decimal, the per-symbol mapping, or None when unset.

    Raises:
        ValueError: If any configured value is not a positive number.
    """
    raw = environ.get(CYCLE_REFERENCE_PRICE_ENV, "").strip()
    if not raw:
        return None
    return parse_reference_quotes(raw)


def _symbol_from_arguments_and_environment(
    argument_symbol: str | None, environ: Mapping[str, str]
) -> str | None:
    """Resolve the cycle's symbol scope from the flag and sealed environment.

    The CLI flag wins; the sealed ``AERO_BOT_CYCLE_SYMBOL`` variable is the
    deployment's pin. Unset, empty, or ``auto`` runs the cross-board
    selector (the default); any other value pins one pool.

    Args:
        argument_symbol: The optional --symbol flag value.
        environ: The environment mapping carrying the optional pin.

    Returns:
        The pinned symbol, or None for selector mode.
    """
    raw = argument_symbol if argument_symbol is not None else environ.get(CYCLE_SYMBOL_ENV, "")
    symbol = raw.strip()
    if not symbol or symbol.lower() == SELECTOR_SYMBOL:
        return None
    return symbol


def _switch_margin_from_environment(environ: Mapping[str, str]) -> Decimal:
    """Read the cross-board switch margin from the sealed environment.

    Args:
        environ: The environment mapping carrying the optional margin.

    Returns:
        The configured fraction, or the locked default.

    Raises:
        ValueError: If the configured margin is negative or not a number.
    """
    raw = environ.get(CYCLE_SWITCH_MARGIN_ENV, "").strip()
    if not raw:
        return DEFAULT_SWITCH_MARGIN_FRACTION
    value = Decimal(raw)
    if value < 0:
        raise ValueError(f"{CYCLE_SWITCH_MARGIN_ENV} must be non-negative, not {raw!r}")
    return value


def build_cycle_runner(
    settings: Settings,
    symbol: str | None,
    safe_address: str,
    relayer_address: str | None,
    switch_margin_fraction: Decimal = DEFAULT_SWITCH_MARGIN_FRACTION,
) -> CycleRunner:
    """Assemble the live cycle runner from the application settings.

    Args:
        settings: The application settings backing every boundary.
        symbol: The registry symbol this cycle manages, or None for the
            cross-board selector.
        safe_address: The Safe whose positions and balances reconcile.
        relayer_address: The relayer's public address, or None.
        switch_margin_fraction: The relative APR margin another pool must
            beat the held pool by before a switch fires.

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

    rpc = ExecutorRpcBackend(
        rpc_url=settings.base_rpc_url,
        fallback_rpc_urls=EXECUTE_RECEIPT_ENDPOINT_URLS,
        progress=_cycle_progress,
    )
    audit_store = AuditStore(settings.audit_database_path)
    pin_store = LpPoolPinStore(settings.lp_pool_pins_path)
    # Every long-running phase (the first full Sugar sweep above all) reports
    # honest progress on stderr so a slow cycle never looks like a stall; the
    # JSON report on stdout stays machine-clean.
    progress = _cycle_progress
    sources = LiveStrategySources(
        rpc_url=settings.base_rpc_url,
        sugar_address=settings.lp_sugar_address,
        fallback_rpc_urls=EXECUTE_RECEIPT_ENDPOINT_URLS,
        pool_pin_store=pin_store,
        progress=progress,
    )
    safe_rpc = SafeTransactionRpcBackend(
        rpc_url=settings.base_rpc_url,
        safe_address=safe_address,
        fallback_rpc_urls=EXECUTE_RECEIPT_ENDPOINT_URLS,
    )
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
            fallback_rpc_urls=EXECUTE_RECEIPT_ENDPOINT_URLS,
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
        switch_margin_fraction=switch_margin_fraction,
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
    parser.add_argument(
        "--symbol",
        default=None,
        help=(
            "Registry symbol like AAPLc, auto, or unset: unset or auto (also "
            "the sealed AERO_BOT_CYCLE_SYMBOL default) runs the cross-board "
            "selector over every verified B20 pool; an explicit symbol pins "
            "one pool for operator runs."
        ),
    )
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
        default=None,
        help=(
            "Optional injected real-market quote in USDC per stock - either "
            "one price for the pinned symbol or per-symbol SYMBOL=PRICE "
            "pairs (AAPLc=318.5,FIXc=100) for selector mode; the "
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
        "--switch-margin",
        type=Decimal,
        default=None,
        help=(
            "The relative emissions-APR margin another pool must beat the "
            "held pool by before a switch fires, as a fraction (default "
            "0.30; the sealed AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION "
            "variable supplies the same value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete report as JSON instead of a summary.",
    )
    arguments = parser.parse_args(argv)
    if arguments.reference_age_seconds < 0:
        parser.error("--reference-age-seconds must be non-negative")
    if arguments.switch_margin is not None and arguments.switch_margin < 0:
        parser.error("--switch-margin must be non-negative")
    symbol = _symbol_from_arguments_and_environment(arguments.symbol, os.environ)
    try:
        switch_margin = (
            arguments.switch_margin
            if arguments.switch_margin is not None
            else _switch_margin_from_environment(os.environ)
        )
        configured_reference = (
            parse_reference_quotes(arguments.reference_price)
            if arguments.reference_price is not None
            else _reference_price_from_environment(os.environ)
        )
    except (ValueError, ArithmeticError) as error:
        print(f"invalid configuration: {error}", file=sys.stderr)
        return EXIT_FAILURE
    single_reference: Decimal | None = None
    reference_map: dict[str, Decimal] = {}
    if isinstance(configured_reference, Decimal):
        # A legacy single-symbol quote belongs to pinned mode. Selector mode is
        # Aerodrome-authoritative and references are diagnostic-only, so an
        # AAPL-only sealed quote must not block cross-board operation or be
        # misapplied to every B20 pool. Per-symbol maps remain available when
        # the operator wants complete external diagnostics.
        if symbol is not None:
            single_reference = configured_reference
    elif configured_reference is not None:
        reference_map = configured_reference
    safe_address = os.environ.get(SAFE_ADDRESS_ENV, DEFAULT_CANARY_SAFE_ADDRESS)
    raw_relayer = os.environ.get(RELAYER_ADDRESS_ENV, "").strip()
    # Both sides normalize before comparison: a checksummed environment
    # value and the lowercase derived address are the same relayer, and a
    # case-sensitive compare refused the correct key on the first armed
    # scheduled cycle (2026-09-09).
    configured_relayer = normalize_evm_address(raw_relayer) if raw_relayer else None
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
        runner = build_cycle_runner(
            settings, symbol, safe_address, configured_relayer, switch_margin
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"the cycle runner is unavailable: {error}", file=sys.stderr)
        return EXIT_FAILURE
    try:
        if mode is CycleMode.LIVE:
            lock_path = settings.audit_database_path.parent / "execution.lock"
            with exclusive_execution_lock(lock_path):
                report = runner.run(
                    mode,
                    key_bytes=key_bytes,
                    reference_price_usdc=single_reference,
                    reference_age_seconds=arguments.reference_age_seconds,
                    reference_prices_by_symbol=reference_map or None,
                )
        else:
            report = runner.run(
                mode,
                key_bytes=key_bytes,
                reference_price_usdc=single_reference,
                reference_age_seconds=arguments.reference_age_seconds,
                reference_prices_by_symbol=reference_map or None,
            )
    except ExecutionLockUnavailableError as error:
        print(f"cycle refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
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
