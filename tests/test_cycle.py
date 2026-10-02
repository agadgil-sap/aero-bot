"""Pin the scheduled decision cycle's reconcile-decide-act behavior."""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import BaseModel
from test_lp_executor import (
    B20_ADDRESS,
    GAUGE_ADDRESS,
    LP_RANGE_LOWER,
    LP_RANGE_UPPER,
    LP_SQRT_RATIO,
    NFPM_ADDRESS,
    POOL_ADDRESS,
    SAFE_ADDRESS,
    STOCK_DECIMALS,
    make_candidate,
)

from aero_bot.allocator import PortfolioParameters
from aero_bot.audit import AuditEventType, AuditRecord, AuditStore
from aero_bot.cycle import (
    CYCLE_AERO_CONVERSION_MIN_ENV,
    CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC_ENV,
    CYCLE_CONCENTRATION_CAP_ENV,
    CYCLE_INCOME_HISTORY_CYCLES_ENV,
    CYCLE_MAX_POSITIONS_ENV,
    CYCLE_MIN_POSITION_FLOOR_USDC_ENV,
    CYCLE_MIN_POSITION_USDC_ENV,
    CYCLE_OUT_OF_RANGE_GRACE_ENV,
    CYCLE_REFERENCE_PRICE_ENV,
    CYCLE_SWITCH_MARGIN_ENV,
    CYCLE_SYMBOL_ENV,
    CYCLE_TIER_BAND_ENV,
    DEFAULT_AERO_CONVERSION_MIN_USDC,
    CycleMode,
    CycleRunner,
    CycleStateBook,
    CycleStateStore,
    HeldInventoryRecord,
    ReentryCooldown,
    TrackedPosition,
    _aero_conversion_min_from_environment,
    _income_history_cycles_from_environment,
    _out_of_range_grace_from_environment,
    _portfolio_parameters_from_environment,
    _reference_price_from_environment,
    _switch_margin_from_environment,
    _symbol_from_arguments_and_environment,
    _trim_apr_history,
    decode_minted_token_id,
)
from aero_bot.emissions_apr import AprReadingSample
from aero_bot.history import price_usdc_per_stock
from aero_bot.lp_executor import (
    NFPM_INCREASE_LIQUIDITY_TOPIC0,
    LpActionExecutionReport,
    LpExecutionRefusalCode,
    LpExecutionRefusalError,
    LpExecutionRole,
    LpPositionStatusReport,
    LpSafePositionsSnapshot,
)
from aero_bot.policy import (
    LOCKED_POLICY_PARAMETERS,
    AlignedPriceRange,
    PolicyActionKind,
    PolicyDecision,
    PolicyOutcome,
    PolicyParameters,
    PolicyReason,
    PolicyState,
)
from aero_bot.strategy import BoardListing
from aero_bot.venues import AERO_TOKEN_ADDRESS, BASE_USDC_ADDRESS, PoolCandidate

# The fixture stock token contract, aliased for the status view's sides.
STOCK_TOKEN_ADDRESS = B20_ADDRESS

# 2026-09-08 is a Tuesday; 20:30 UTC is 16:30 New York, past every session
# window, so decisions run the full gate chain.
QUIET_INSTANT = datetime(2026, 9, 8, 20, 30, tzinfo=UTC)
# The tracked fixture position's token id, mirroring the canary shape.
TRACKED_TOKEN_ID = 5_703_026
# The cycle's relayer is a public address fixture.
RELAYER_ADDRESS = "0x0c49cc4d53423ccd6be2bcf115a25f418649c5c9"
# A mint delivery hash the fake receipt store resolves.
MINT_TX_HASH = "0x" + "ab" * 32
# A stand-in gauge/staked custody shape.
FIXTURE_RANGE_LOWER = LP_RANGE_LOWER
FIXTURE_RANGE_UPPER = LP_RANGE_UPPER
# The entry size and width the locked engine derives at a ten-USDC
# equity: the 80-percent equity cap (the captain's 2026-09-09 sizing
# ruling) and the ceiling-rounded spacing width.
EXPECTED_ENTER_SIZE = Decimal("8")
EXPECTED_ENTER_WIDTH = 4
# The fixture AMM price doubles as a neutral reference quote: equal to the
# pool price, no dislocation can trigger in either direction.
with localcontext():
    FIXTURE_AMM_PRICE = price_usdc_per_stock(LP_SQRT_RATIO, False, STOCK_DECIMALS, 6)


class FakeConfirmPayload(BaseModel):
    """The confirmed-delivery audit shape the real executor writes."""

    outcome: str
    action: str
    role: str
    transaction_hash: str


class FakePlanPayload(BaseModel):
    """The mint-plan audit shape the real executor writes."""

    mode: str = "execute"
    budget_usdc: str
    symbol: str | None = None


class FakeStakePlanPayload(BaseModel):
    """The stake-plan audit shape the real executor writes."""

    mode: str
    symbol: str
    token_id: int
    token_owner_address: str = SAFE_ADDRESS


class FakeCycleSources:
    """Serve deterministic discovery and reads without any network."""

    def __init__(
        self,
        *,
        usdc_units: int = 10_000_000,
        stock_units: int = 0,
        listings: tuple[BoardListing, ...] | None = None,
    ) -> None:
        """Configure the fixture pool and the Safe's live balances."""
        self._usdc_units = usdc_units
        self._stock_units = stock_units
        self._listings = listings

    def resolve_pool(self, symbol: str) -> tuple[PoolCandidate, int]:
        """Return the verified fixture pool and its snapshot block."""
        return make_candidate(), 123

    def enumerate_pools(self) -> tuple[tuple[BoardListing, ...], int]:
        """Return the scripted board and its snapshot block."""
        if self._listings is not None:
            return self._listings, 123
        return (BoardListing(symbol="FIXc", pool=make_candidate()),), 123

    def registry_paused(self) -> bool:
        """The fixture registry is verified."""
        return False

    def symbol_address(self, symbol: str) -> str | None:
        """Resolve the fixture FIXc symbol."""
        return B20_ADDRESS if symbol.lower() == "fixc" else None

    def token_decimals(self, token_address: str) -> int:
        """The fixture stock carries eight decimals."""
        return 8

    def aero_price(self, block_number: int) -> Decimal:
        """Return the fixture AERO price."""
        return Decimal("0.6")

    def gas_price_gwei(self) -> Decimal | None:
        """Return the fixture gas price."""
        return Decimal("0.001")

    def token_balance(self, token_address: str, owner_address: str) -> int:
        """Serve the Safe's configured token balances."""
        if token_address.lower() == BASE_USDC_ADDRESS.lower():
            return self._usdc_units
        return self._stock_units


class FakeReads:
    """Serve the position inventory and status surface statefully."""

    def __init__(self, snapshot: LpSafePositionsSnapshot | None = None) -> None:
        """Start from one scripted inventory snapshot."""
        self._snapshot = snapshot if snapshot is not None else empty_inventory()
        self._statuses: dict[int, LpPositionStatusReport] = {}

    def set_inventory(self, snapshot: LpSafePositionsSnapshot) -> None:
        """Swap the served inventory (the fake executor mutates state)."""
        self._snapshot = snapshot

    def set_status(self, token_id: int, status: LpPositionStatusReport) -> None:
        """Serve one scripted position status per token id."""
        self._statuses[token_id] = status

    def safe_position_inventory(self, symbol: str) -> LpSafePositionsSnapshot:
        """Return the current scripted inventory."""
        return self._snapshot

    def position_status(
        self,
        symbol: str,
        token_id: int,
        aero_price_usdc: Decimal | None = None,
        entry_cost_usdc: Decimal | None = None,
    ) -> LpPositionStatusReport:
        """Return the scripted status for one tracked token."""
        status = self._statuses.get(token_id)
        if status is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_UNKNOWN, f"no scripted status for {token_id}"
            )
        return status


class FakeBalances:
    """Serve token balances, ETH balances, and mint receipts."""

    def __init__(
        self,
        *,
        usdc_units: int = 10_000_000,
        stock_units: int = 0,
        aero_units: int = 0,
        relayer_eth_wei: int = 10**15,
        block_number: int = 99_999_999,
    ) -> None:
        """Configure every served balance."""
        self.usdc_units = usdc_units
        self.stock_units = stock_units
        self.aero_units = aero_units
        self.relayer_eth_wei = relayer_eth_wei
        self.block_number = block_number
        self.receipts: dict[str, dict[str, object]] = {}

    def fetch_token_balance(self, token_address: str, owner_address: str) -> int:
        """Serve the configured per-token balance."""
        if token_address.lower() == BASE_USDC_ADDRESS.lower():
            return self.usdc_units
        if token_address.lower() == AERO_TOKEN_ADDRESS.lower():
            return self.aero_units
        return self.stock_units

    def fetch_eth_balance(self, account_address: str) -> int:
        """Serve the relayer's configured ETH balance."""
        return self.relayer_eth_wei

    def fetch_transaction_receipt(self, transaction_hash: str) -> dict[str, object] | None:
        """Serve one scripted receipt when present."""
        return self.receipts.get(transaction_hash)

    def fetch_block_number(self) -> int:
        """Serve the primary RPC block used by the post-action visibility gate."""
        return self.block_number


class FakeExecutor:
    """Serve scripted action outcomes while mutating the shared fake state."""

    def __init__(
        self,
        reads: FakeReads,
        balances: FakeBalances,
        audit: AuditStore,
        *,
        now: datetime = QUIET_INSTANT,
    ) -> None:
        """Wire the fake executor to the state it mutates on success."""
        self._reads = reads
        self._balances = balances
        self._audit = audit
        self._now = now
        self.calls: list[tuple[object, ...]] = []
        self.refuse_next: str | None = None
        self.mint_receipt_token_id: int | None = TRACKED_TOKEN_ID
        # Successive mints pop distinct token ids so a portfolio's positions
        # never alias onto one NFT; None falls back to the single default.
        self.mint_token_ids: list[int] | None = None
        self.mint_executed_budget: Decimal | None = None
        self.fee_wei_per_step = 90_000
        self.confirmed_block_number = 51_000_000
        self.collect_aero_units = 0
        # Raise KeyboardInterrupt on the Nth mint call (one-indexed) to
        # simulate the service timeout killing the process mid-act.
        self.interrupt_on_mint: int | None = None
        self._mint_calls = 0

    def _complete(self, action: str, hashes: tuple[str, ...]) -> LpActionExecutionReport:
        """Build one completed execution report over scripted steps."""
        steps = tuple(
            cast(
                object,
                SimpleNamespace(
                    transaction_hash=h,
                    fee_wei=self.fee_wei_per_step,
                    status="confirmed",
                    block_number=self.confirmed_block_number,
                ),
            )
            for h in hashes
        )
        return LpActionExecutionReport.model_construct(
            action=action, build=None, steps=steps, completed=True, halted_reason=""
        )

    def _audit_confirm(self, action: str, role: LpExecutionRole, tx_hash: str) -> None:
        """Append the same confirmed-delivery record the real executor does."""
        self._audit.append(
            AuditEventType.LP_EXECUTE_CONFIRMED,
            FakeConfirmPayload(
                outcome="confirmed",
                action=action,
                role=role.value,
                transaction_hash=tx_hash,
            ),
            self._now,
        )

    def dry_run_recenter(
        self,
        symbol: str,
        token_id: int,
        width_spacings: int | None,
        budget_usdc: Decimal | None,
        key_bytes: bytes,
        ephemeral_key: bool = False,
        portfolio_live_positions: Sequence[tuple[int, Decimal]] | None = None,
    ) -> object:
        """Preflight one replacement without mutating the scripted chain state."""
        self.calls.append(("recenter_preflight", symbol, token_id, width_spacings, budget_usdc))
        if self.refuse_next == "recenter_preflight":
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "scripted recenter preflight refusal",
            )
        return SimpleNamespace()

    def dry_run_switch(
        self,
        from_symbol: str,
        token_id: int,
        to_symbol: str,
        width_spacings: int | None,
        budget_usdc: Decimal,
        key_bytes: bytes,
        ephemeral_key: bool = False,
        portfolio_live_positions: Sequence[tuple[int, Decimal]] | None = None,
    ) -> object:
        """Preflight one cross-pool replacement without mutating chain state."""
        self.calls.append(
            ("switch_preflight", from_symbol, token_id, to_symbol, width_spacings, budget_usdc)
        )
        if self.refuse_next == "switch_preflight":
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "scripted switch preflight refusal",
            )
        return SimpleNamespace()

    def execute_mint(
        self,
        symbol: str,
        budget_usdc: Decimal,
        width_spacings: int | None,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
        portfolio_live_positions: Sequence[tuple[int, Decimal]] | None = None,
    ) -> LpActionExecutionReport:
        """Complete one mint, minting the scripted token id on-chain."""
        self.calls.append(("mint", symbol, budget_usdc, width_spacings))
        self._mint_calls += 1
        if self.interrupt_on_mint is not None and self._mint_calls >= self.interrupt_on_mint:
            raise KeyboardInterrupt("simulated service timeout")
        if self.refuse_next == "mint":
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING, "scripted refusal"
            )
        if self.mint_token_ids:
            self.mint_receipt_token_id = self.mint_token_ids.pop(0)
        if self.mint_receipt_token_id is not None:
            self._balances.receipts[MINT_TX_HASH] = mint_receipt(self.mint_receipt_token_id)
        else:
            self._balances.receipts[MINT_TX_HASH] = {"logs": []}
        self._audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(mode="execute", budget_usdc=str(budget_usdc), symbol=symbol),
            self._now,
        )
        self._audit_confirm("mint", LpExecutionRole.MINT, MINT_TX_HASH)
        self._reads.set_inventory(inventory_with(TRACKED_TOKEN_ID))
        report = self._complete("mint", (MINT_TX_HASH,))
        if self.mint_executed_budget is not None:
            report = report.model_copy(
                update={
                    "build": SimpleNamespace(
                        plan=SimpleNamespace(budget_usdc=self.mint_executed_budget)
                    )
                }
            )
        return report

    def execute_stake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Complete one stake, placing the NFT in the gauge's custody."""
        self.calls.append(("stake", symbol, token_id))
        if self.refuse_next == "stake":
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING, "scripted refusal"
            )
        self._audit.append(
            AuditEventType.LP_STAKE_PLANNED,
            FakeStakePlanPayload(mode="execute", symbol=symbol, token_id=token_id),
            self._now,
        )
        self._reads.set_status(
            token_id,
            tracked_status(owner=GAUGE_ADDRESS).model_copy(update={"token_id": token_id}),
        )
        return self._complete("stake", ("0x" + "cd" * 32,))

    def execute_unstake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Complete one unstake, returning the NFT to the Safe."""
        self.calls.append(("unstake", symbol, token_id))
        return self._complete("unstake", ("0x" + "ce" * 32,))

    def execute_withdraw(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Complete one withdraw, emptying the tracked position."""
        self.calls.append(("withdraw", symbol, token_id))
        self._reads.set_inventory(empty_inventory())
        return self._complete("withdraw", ("0x" + "cf" * 32,))

    def execute_exit_swap(
        self,
        symbol: str,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Complete one exit swap, converting all stock to USDC."""
        self.calls.append(("exit_swap", symbol))
        self._balances.stock_units = 0
        return self._complete("exit_swap", ("0x" + "d0" * 32,))

    def execute_collect(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Complete one claim, sweeping the scripted emissions to the Safe."""
        self.calls.append(("collect_rewards", symbol, token_id))
        if self.refuse_next == "collect_rewards":
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING, "scripted refusal"
            )
        self._balances.aero_units += self.collect_aero_units
        return self._complete("collect", ("0x" + "d1" * 32,))

    def execute_aero_swap(
        self,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Complete one reward conversion, emptying the Safe's AERO."""
        self.calls.append(("aero_swap",))
        if self.refuse_next == "aero_swap":
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING, "scripted refusal"
            )
        swapped = self._balances.aero_units
        self._balances.aero_units = 0
        self._balances.usdc_units += int(Decimal(swapped).scaleb(-18) * Decimal("0.6") * 10**6)
        report = self._complete("aero_swap", ("0x" + "d2" * 32,))
        return report.model_copy(update={"build": SimpleNamespace(aero_balance_units=swapped)})


def _empty_book_fields(book: CycleStateBook) -> bool:
    """Return whether one book carries no tracked state (time aside)."""
    fresh = CycleStateBook().model_dump(exclude={"updated_at"})
    return book.model_dump(exclude={"updated_at"}) == fresh


def empty_inventory() -> LpSafePositionsSnapshot:
    """Build one inventory snapshot with nothing held."""
    return LpSafePositionsSnapshot(
        symbol="FIXc",
        pool_address=POOL_ADDRESS,
        nfpm_address=NFPM_ADDRESS,
        positions=(),
        snapshot_block=123,
        observed_at=QUIET_INSTANT,
        caps_enforced=("fixture",),
        diagnostics=("nothing held",),
    )


def inventory_with(token_id: int) -> LpSafePositionsSnapshot:
    """Build one inventory snapshot holding a single live position."""
    from aero_bot.lp_executor import LpHeldPosition

    return LpSafePositionsSnapshot(
        symbol="FIXc",
        pool_address=POOL_ADDRESS,
        nfpm_address=NFPM_ADDRESS,
        positions=(
            LpHeldPosition(
                token_id=token_id, liquidity=10**10, tokens_owed0_units=0, tokens_owed1_units=0
            ),
        ),
        snapshot_block=123,
        observed_at=QUIET_INSTANT,
        caps_enforced=("fixture",),
        diagnostics=(f"one live position {token_id}",),
    )


def inventory_with_ids(token_ids: tuple[int, ...]) -> LpSafePositionsSnapshot:
    """Build one inventory snapshot holding every given live position."""
    from aero_bot.lp_executor import LpHeldPosition

    return LpSafePositionsSnapshot(
        symbol="FIXc",
        pool_address=POOL_ADDRESS,
        nfpm_address=NFPM_ADDRESS,
        positions=tuple(
            LpHeldPosition(
                token_id=token_id, liquidity=10**10, tokens_owed0_units=0, tokens_owed1_units=0
            )
            for token_id in token_ids
        ),
        snapshot_block=123,
        observed_at=QUIET_INSTANT,
        caps_enforced=("fixture",),
        diagnostics=(f"{len(token_ids)} live positions",),
    )


def portfolio_book() -> CycleStateBook:
    """Build one book tracking the fixture's two-position portfolio."""
    second_id = TRACKED_TOKEN_ID + 1
    return CycleStateBook(
        positions=(
            TrackedPosition(
                symbol="BBBc",
                token_id=TRACKED_TOKEN_ID,
                pool_address=SELECTOR_BBB_POOL,
                committed_usd=Decimal("175"),
                entered_at=QUIET_INSTANT - timedelta(hours=2),
            ),
            TrackedPosition(
                symbol="AAAc",
                token_id=second_id,
                pool_address=POOL_ADDRESS,
                committed_usd=Decimal("120"),
                entered_at=QUIET_INSTANT - timedelta(hours=2),
            ),
        ),
        updated_at=QUIET_INSTANT,
    )


def portfolio_reads(
    *,
    fee_growth: int | None = None,
    earned_aero: int | None = None,
    liquidity: int = 12_345,
) -> FakeReads:
    """Serve both portfolio positions as staked with matching statuses."""
    reads = FakeReads(inventory_with_ids((TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1)))
    for token_id in (TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1):
        reads.set_status(
            token_id,
            tracked_status(
                owner=GAUGE_ADDRESS,
                accrued_aero_units=earned_aero,
                fee_growth_inside0_x128=fee_growth,
                fee_growth_inside1_x128=fee_growth,
                liquidity=liquidity,
            ).model_copy(update={"token_id": token_id}),
        )
    return reads


def tracked_status(
    *,
    owner: str = SAFE_ADDRESS,
    value: Decimal = Decimal("8"),
    pnl: Decimal | None = Decimal("1"),
    fees_usdc: Decimal | None = None,
    observed_at: datetime = QUIET_INSTANT,
    accrued_aero_units: int | None = None,
    fee_growth_inside0_x128: int | None = None,
    fee_growth_inside1_x128: int | None = None,
    liquidity: int = 12_345,
    fees_owed0_units: int = 0,
    fees_owed1_units: int = 0,
) -> LpPositionStatusReport:
    """Build one minimal tracked-position status via unchecked construction."""
    view = SimpleNamespace(
        tick_lower=FIXTURE_RANGE_LOWER,
        tick_upper=FIXTURE_RANGE_UPPER,
        token0_address=BASE_USDC_ADDRESS,
        token1_address=STOCK_TOKEN_ADDRESS,
        liquidity=liquidity,
    )
    return LpPositionStatusReport.model_construct(
        symbol="FIXc",
        pool_address=POOL_ADDRESS,
        token_id=TRACKED_TOKEN_ID,
        token_owner_address=owner,
        gauge_address=GAUGE_ADDRESS,
        position=view,
        position_value_usdc=value,
        token0_value_usdc=+(value / 2),
        token1_value_usdc=+(value / 2),
        unrealized_pnl_usdc=pnl,
        pnl_diagnostic="" if pnl is not None else "no entry cost",
        fees_owed_usdc=fees_usdc,
        fees_owed0_units=fees_owed0_units,
        fees_owed1_units=fees_owed1_units,
        accrued_aero_earned_units=accrued_aero_units,
        fee_growth_inside0_x128=fee_growth_inside0_x128,
        fee_growth_inside1_x128=fee_growth_inside1_x128,
        observed_at=observed_at,
    )


def mint_receipt(token_id: int) -> dict[str, object]:
    """Build one mint receipt whose IncreaseLiquidity topic names the id."""
    return {
        "logs": [
            {
                "topics": [
                    NFPM_INCREASE_LIQUIDITY_TOPIC0,
                    "0x" + token_id.to_bytes(32, "big").hex(),
                ]
            }
        ]
    }


def tracked_book(
    *,
    owner: str = SAFE_ADDRESS,
    committed: Decimal = Decimal("7"),
    symbol: str = "FIXc",
    entered_at: datetime = QUIET_INSTANT,
) -> CycleStateBook:
    """Build one book tracking the fixture position."""
    return CycleStateBook(
        positions=(
            TrackedPosition(
                symbol=symbol,
                token_id=TRACKED_TOKEN_ID,
                pool_address=POOL_ADDRESS,
                committed_usd=committed,
                entered_at=entered_at,
            ),
        ),
        updated_at=QUIET_INSTANT,
    )


def make_runner(
    tmp_path: Path,
    *,
    book: CycleStateBook | None = None,
    reads: FakeReads | None = None,
    balances: FakeBalances | None = None,
    sources: FakeCycleSources | None = None,
    executor: object | None = None,
    audit_seed: bool = False,
    now: datetime = QUIET_INSTANT,
    symbol: str | None = "FIXc",
    sleep: Callable[[float], None] | None = None,
    parameters: PolicyParameters | None = None,
    aero_conversion_min_usdc: Decimal | None = None,
    income_history_cycles: int | None = None,
) -> tuple[CycleRunner, FakeExecutor | None, AuditStore, CycleStateStore]:
    """Assemble one cycle runner over fully scripted boundaries."""
    store_path = tmp_path / "cycle_state.json"
    state_store = CycleStateStore(store_path)
    if book is not None:
        state_store.save(book)
    audit_path = tmp_path / "audit.sqlite3"
    audit = AuditStore(audit_path)
    if audit_seed:
        audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(mode="execute", budget_usdc="7", symbol="FIXc"),
            QUIET_INSTANT,
        )
        audit.append(
            AuditEventType.LP_EXECUTE_CONFIRMED,
            FakeConfirmPayload(
                outcome="confirmed",
                action="mint",
                role="mint",
                transaction_hash=MINT_TX_HASH,
            ),
            QUIET_INSTANT,
        )
    fake_reads = reads if reads is not None else FakeReads()
    fake_balances = balances if balances is not None else FakeBalances()
    fake_sources = sources if sources is not None else FakeCycleSources()
    fake_executor: FakeExecutor | None = None
    if executor is not None:
        fake_executor = cast(FakeExecutor, executor)
    elif executor is not False:
        fake_executor = FakeExecutor(fake_reads, fake_balances, audit, now=now)

    class Reader:
        """Read the seeded audit chain through the store."""

        def __init__(self, store: AuditStore) -> None:
            self._store = store

        def recent_records(self) -> tuple[AuditRecord, ...]:
            records: list[AuditRecord] = []
            while True:
                page = self._store.read_records(1_000, offset=len(records))
                records.extend(page)
                if len(page) < 1_000:
                    return tuple(records)

    runner = CycleRunner(
        symbol=symbol,
        safe_address=SAFE_ADDRESS,
        relayer_address=RELAYER_ADDRESS,
        reads=fake_reads,
        balances=fake_balances,
        sources=fake_sources,
        executor=fake_executor,
        audit_reader=Reader(audit),
        audit_sink=audit,
        state_store=state_store,
        now=lambda: now,
        sleep=sleep if sleep is not None else (lambda _seconds: None),
        parameters=parameters if parameters is not None else LOCKED_POLICY_PARAMETERS,
        aero_conversion_min_usdc=(
            aero_conversion_min_usdc
            if aero_conversion_min_usdc is not None
            else DEFAULT_AERO_CONVERSION_MIN_USDC
        ),
        income_history_cycles=income_history_cycles,
    )
    return runner, fake_executor, audit, state_store


class TestCycleStateStore:
    """The self-healing book store."""

    def test_round_trip_preserves_every_field(self, tmp_path: Path) -> None:
        """A saved book loads back byte-equivalent through validation."""
        store = CycleStateStore(tmp_path / "book.json")
        book = tracked_book()
        store.save(book)
        assert store.load() == book

    def test_missing_or_corrupt_file_yields_an_empty_book(self, tmp_path: Path) -> None:
        """Any unreadable book loads empty; reconciliation rebuilds truth."""
        store = CycleStateStore(tmp_path / "book.json")
        assert _empty_book_fields(store.load())
        (tmp_path / "book.json").write_text("{not json", encoding="utf-8")
        assert _empty_book_fields(store.load())

    def test_environment_default_sits_beside_the_audit_store(self, tmp_path: Path) -> None:
        """Without an override the book lives beside the audit database."""
        from aero_bot.config import Settings

        settings = Settings.model_construct(audit_database_path=tmp_path / "audit.sqlite3")
        store = CycleStateStore.from_environment(environ={}, settings=settings)
        assert store.path == tmp_path / "cycle_state.json"


class TestMintReceiptDecoding:
    """The IncreaseLiquidity decode backing stake follow-ups."""

    def test_the_topic_names_the_minted_token_id(self) -> None:
        """The first indexed topic after the signature is the token id."""
        assert decode_minted_token_id(mint_receipt(TRACKED_TOKEN_ID)) == TRACKED_TOKEN_ID

    def test_a_receipt_without_the_event_refuses(self) -> None:
        """No IncreaseLiquidity log means no id; the caller halts honestly."""
        with pytest.raises(ValueError, match="IncreaseLiquidity"):
            decode_minted_token_id({"logs": [{"topics": ["0x" + "11" * 32]}]})
        with pytest.raises(ValueError, match="IncreaseLiquidity"):
            decode_minted_token_id({"logs": []})
        with pytest.raises(ValueError, match="no logs"):
            decode_minted_token_id({"logs": "not a list"})

    def test_an_absent_receipt_refuses(self) -> None:
        """A None receipt (not yet mined) is a decode refusal, not a guess."""
        with pytest.raises(ValueError, match="not yet available"):
            decode_minted_token_id(None)


class TestDryRunCycles:
    """Dry runs reconcile and decide without any key or build."""

    def test_flat_cycle_without_a_reference_uses_pool_authority(self, tmp_path: Path) -> None:
        """Scheduled cycles use the resolved Aerodrome pool without an external quote."""
        runner, _, audit, state_store = make_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert report.decision_action == "enter"
        assert report.decision_reason == "entry_threshold_met"
        assert any("diagnostic-only" in note for note in report.input_notes)
        assert report.actions == ()
        assert report.halted_reason == ""
        book = state_store.load()
        assert book.position is None
        assert (
            book.day
            == QUIET_INSTANT.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
        )
        records = audit.read_records(10)
        assert [record.event_type for record in records] == [AuditEventType.CYCLE_REPORTED]

    def test_day_rollover_anchor_prices_the_whole_book(self, tmp_path: Path) -> None:
        """The day-start equity anchor carries the tracked LP mark, not cash alone.

        The production book on 2026-09-23 showed an 80.73 anchor beside a
        ~99 book and made daily P&L unreadable; the anchor must use the
        same composition the engine acted on (Safe USDC, held stock, and
        the tracked position's marked value) so a deployed position never
        reads as a drawdown against a cash-only anchor.
        """
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS))
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(CycleMode.DRY_RUN)

        policy_day = QUIET_INSTANT.astimezone(
            __import__("zoneinfo").ZoneInfo("America/New_York")
        ).date()
        loaded = state_store.load()
        assert loaded.day == policy_day
        assert loaded.day_start_equity_usd == Decimal("18")
        assert report.equity_usd == Decimal("18")
        assert report.day_start_equity_usd == Decimal("18")
        assert report.day_pnl_usdc == Decimal("0")
        assert report.day_diagnostic == ""

    def test_day_economics_absent_when_the_cycle_refuses_out_of_band(self, tmp_path: Path) -> None:
        """An out-of-band refusal carries no day economics rather than a lie."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        runner, _, _, _ = make_runner(tmp_path, reads=reads)
        report = runner.run(CycleMode.DRY_RUN)
        assert "no audit evidence" in report.reconciliation.out_of_band
        assert report.decision_reason == "out_of_band"
        assert report.equity_usd is None
        assert report.day_start_equity_usd is None
        assert report.day_pnl_usdc is None
        assert report.day_diagnostic == "out-of-band cycle carried no day economics"

    def test_cycle_reports_first_claimable_fee_sample_without_a_window(
        self, tmp_path: Path
    ) -> None:
        """The first claimable reading reports evidence and opens the window."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(owner=GAUGE_ADDRESS, fees_usdc=Decimal("0.25")),
        )
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(CycleMode.DRY_RUN)

        evidence = report.fee_evidence
        assert evidence is not None
        assert evidence.token_id == TRACKED_TOKEN_ID
        assert evidence.claimable_pool_fees_usdc == Decimal("0.25")
        assert evidence.measured_fee_usdc_per_day is None
        assert evidence.measured_fee_apr is None
        assert "first claimable sample recorded" in evidence.diagnostic
        assert "lower bound" in evidence.diagnostic
        book = state_store.load()
        assert [sample.token_id for sample in book.fee_samples] == [TRACKED_TOKEN_ID]
        assert book.fee_samples[0].claimable_pool_fees_usdc == Decimal("0.25")

    def test_cycle_measures_the_fee_accrual_window_across_samples(self, tmp_path: Path) -> None:
        """Two samples price the checkpointed accrual rate against the mark."""
        day_one = QUIET_INSTANT
        day_two = QUIET_INSTANT + timedelta(hours=24)
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(owner=GAUGE_ADDRESS, fees_usdc=Decimal("0.25"), observed_at=day_one),
        )
        runner, _, _, state_store = make_runner(
            tmp_path, book=tracked_book(), reads=reads, now=day_one
        )
        runner.run(CycleMode.DRY_RUN)

        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(
                owner=GAUGE_ADDRESS,
                fees_usdc=Decimal("1.25"),
                observed_at=day_two,
            ),
        )
        second = runner.run(CycleMode.DRY_RUN)

        evidence = second.fee_evidence
        assert evidence is not None
        assert evidence.measured_fee_usdc_per_day == Decimal("1")
        assert evidence.measured_fee_apr == Decimal("365") / Decimal("8")
        assert "lower bound" in evidence.diagnostic
        assert [sample.token_id for sample in state_store.load().fee_samples] == [TRACKED_TOKEN_ID]

    def test_cycle_fee_evidence_stays_absent_without_a_tracked_position(
        self, tmp_path: Path
    ) -> None:
        """A flat cycle reports no fee evidence rather than a zero reading."""
        runner, _, audit, _ = make_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert report.fee_evidence is not None
        assert report.fee_evidence.token_id is None
        assert report.fee_evidence.claimable_pool_fees_usdc is None
        assert "no tracked position" in report.fee_evidence.diagnostic
        payload = json.loads(audit.read_records(1)[0].payload_json)
        assert payload["claimable_pool_fees_usdc"] is None
        assert payload["measured_fee_apr"] is None

    def test_audit_record_carries_the_day_and_fee_economics(self, tmp_path: Path) -> None:
        """The audited cycle summary persists equity, anchor, P&L, and fees."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(owner=GAUGE_ADDRESS, fees_usdc=Decimal("0.25")),
        )
        runner, _, audit, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)

        runner.run(CycleMode.DRY_RUN)

        payload = json.loads(audit.read_records(1)[0].payload_json)
        assert Decimal(payload["equity_usdc"]) == Decimal("18")
        assert Decimal(payload["day_start_equity_usdc"]) == Decimal("18")
        assert Decimal(payload["day_pnl_usdc"]) == Decimal("0")
        assert Decimal(payload["claimable_pool_fees_usdc"]) == Decimal("0.25")

    def test_market_window_no_longer_flats_the_cycle_since_the_ruling(self, tmp_path: Path) -> None:
        """A session window inside the cycle no longer gates the verdict.

        The captain's 2026-09-09 twenty-four-seven ruling (the B20 pools
        are continuous DeFi markets; nights and weekends are in scope)
        removed the flat-window doctrine: this Tuesday market-open fixture
        previously held as event_window_flat and now produces the entry
        verdict, with the window reported informationally.
        """
        market_open = datetime(2026, 9, 8, 13, 40, tzinfo=UTC)
        runner, _, _, _ = make_runner(tmp_path, now=market_open)
        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert report.decision_action == "enter"
        assert report.decision_reason == "entry_threshold_met"
        assert "market open" in report.event_window


class TestLiveCycles:
    """Live cycles act only through the audited fake executor."""

    def test_enter_mints_stakes_and_tracks_the_position(self, tmp_path: Path) -> None:
        """A quiet-window enter runs mint then stake and records the book."""
        runner, executor, _, state_store = make_runner(tmp_path)
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=Decimal("100"),
        )
        assert [call[0] for call in executor.calls] == ["mint", "stake"]
        budget = cast(Decimal, executor.calls[0][2])
        assert budget == EXPECTED_ENTER_SIZE
        assert cast(int, executor.calls[0][3]) == EXPECTED_ENTER_WIDTH
        assert cast(int, executor.calls[1][2]) == TRACKED_TOKEN_ID
        assert [action.status for action in report.actions] == ["completed", "completed"]
        assert report.fee_wei == 2 * 90_000
        assert report.halted_reason == ""
        book = state_store.load()
        assert book.position is not None
        assert book.position.token_id == TRACKED_TOKEN_ID
        assert book.position.committed_usd == EXPECTED_ENTER_SIZE

    def test_enter_records_the_actual_resized_mint_budget(self, tmp_path: Path) -> None:
        """A post-swap resized mint persists its actual deployed cost basis."""
        runner, executor, _, state_store = make_runner(tmp_path)
        assert executor is not None
        executor.mint_executed_budget = Decimal("63.25")
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=Decimal("100"),
        )
        assert [action.status for action in report.actions] == ["completed", "completed"]
        book = state_store.load()
        assert book.position is not None
        assert book.position.committed_usd == Decimal("63.25")

    def test_a_refused_mint_halts_the_cycle_before_any_stake(self, tmp_path: Path) -> None:
        """A refused mint records the catalog code and stops the cycle."""
        runner, executor, _, state_store = make_runner(tmp_path)
        assert executor is not None
        executor.refuse_next = "mint"
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=Decimal("100"),
        )
        assert [action.status for action in report.actions] == ["refused"]
        assert report.actions[0].refusal_code == "broadcast_confirmation_missing"
        assert "refused" in report.halted_reason
        assert [call[0] for call in executor.calls] == ["mint"]
        assert state_store.load().position is None

    def test_an_undecodable_mint_receipt_halts_before_staking(self, tmp_path: Path) -> None:
        """A mint receipt without the event cannot name the id; no stake."""
        runner, executor, _, _ = make_runner(tmp_path)
        assert executor is not None
        executor.mint_receipt_token_id = None
        executor._balances.receipts[MINT_TX_HASH] = {"logs": []}
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=Decimal("100"),
        )
        assert [call[0] for call in executor.calls] == ["mint"]
        assert "could not be decoded" in report.halted_reason

    def test_exit_actions_unstake_withdraw_and_swap_to_usdc(self, tmp_path: Path) -> None:
        """A stop-out decision maps to the full exit sequence."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS))
        runner, executor, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)
        assert executor is not None
        runner._last_reconciliation = runner._reconcile(tracked_book())
        outcome = PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.STOP_OUT,
                reason=PolicyReason.DOWNSIDE_STOP_TRIGGERED,
                diagnostics=("fixture stop",),
            ),
            next_state=PolicyState(),
        )
        actions, halted, book = runner._act(tracked_book(), outcome, b"\x01" * 32, "FIXc")
        assert halted == ""
        assert [record.action for record in actions] == [
            "unstake",
            "withdraw",
            "exit_swap",
        ]
        assert all(record.status == "completed" for record in actions)
        assert book.position is None and book.held_inventory is None

    def test_recenter_preserves_withdrawn_inventory_instead_of_round_tripping_usdc(
        self, tmp_path: Path
    ) -> None:
        """A recenter unstakes/withdraws then lets mint rebalance without a full exit swap."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS))
        runner, executor, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)
        assert executor is not None
        runner._last_reconciliation = runner._reconcile(tracked_book())
        outcome = PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.RECENTER,
                reason=PolicyReason.DOWNSIDE_RECENTER_ECONOMIC,
                diagnostics=("fixture economic recenter",),
                price_range=AlignedPriceRange(
                    lower_tick=-10,
                    upper_tick=10,
                    lower_price=Decimal("99"),
                    upper_price=Decimal("101"),
                ),
                size_usd=Decimal("7"),
            ),
            next_state=PolicyState(),
        )

        actions, halted, book = runner._act(tracked_book(), outcome, b"\x01" * 32, "FIXc")

        assert halted == ""
        assert [record.action for record in actions] == [
            "unstake",
            "withdraw",
            "mint",
            "stake",
        ]
        assert "exit_swap" not in [call[0] for call in executor.calls]
        assert book.position is not None

    def test_recenter_preflight_refusal_keeps_the_live_position_untouched(
        self, tmp_path: Path
    ) -> None:
        """A replacement that cannot be built refuses before unstake/withdraw."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS))
        runner, executor, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)
        assert executor is not None
        executor.refuse_next = "recenter_preflight"
        runner._last_reconciliation = runner._reconcile(tracked_book())
        outcome = PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.RECENTER,
                reason=PolicyReason.DOWNSIDE_RECENTER_ECONOMIC,
                diagnostics=("fixture economic recenter",),
                price_range=AlignedPriceRange(
                    lower_tick=-10,
                    upper_tick=10,
                    lower_price=Decimal("99"),
                    upper_price=Decimal("101"),
                ),
                size_usd=Decimal("7"),
            ),
            next_state=PolicyState(),
        )

        actions, halted, book = runner._act(tracked_book(), outcome, b"\x01" * 32, "FIXc")

        assert [record.action for record in actions] == ["recenter_preflight"]
        assert actions[0].status == "refused"
        assert "recenter preflight refused" in halted
        assert book.position is not None
        assert reads._statuses[TRACKED_TOKEN_ID].token_owner_address == GAUGE_ADDRESS
        assert [call[0] for call in executor.calls] == ["recenter_preflight"]

    def test_stale_low_burn_holds_the_returned_stock(self, tmp_path: Path) -> None:
        """A stale-low burn unstakes and withdraws but never swaps."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS))
        balances = FakeBalances(stock_units=2_100_000)
        runner, executor, _, _ = make_runner(
            tmp_path, book=tracked_book(), reads=reads, balances=balances
        )
        assert executor is not None
        runner._last_reconciliation = runner._reconcile(tracked_book())
        outcome = PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.STALE_LOW_BURN,
                reason=PolicyReason.DISLOCATION_STALE_LOW_TRIGGERED,
                diagnostics=("fixture dislocation",),
            ),
            next_state=PolicyState(),
        )
        actions, halted, book = runner._act(tracked_book(), outcome, b"\x01" * 32, "FIXc")
        assert [record.action for record in actions] == ["unstake", "withdraw"]
        assert book.position is None
        assert book.held_inventory is not None
        assert book.held_inventory.stock_quantity == Decimal("0.021")

    def test_sell_inventory_runs_the_exit_swap(self, tmp_path: Path) -> None:
        """A held-inventory sale maps to the exit swap alone."""
        runner, executor, _, _ = make_runner(tmp_path)
        assert executor is not None
        runner._last_reconciliation = runner._reconcile(CycleStateBook())
        book = CycleStateBook(
            held_inventory=HeldInventoryRecord(
                symbol="FIXc",
                token_address=B20_ADDRESS,
                stock_quantity=Decimal("0.021"),
                held_since=QUIET_INSTANT,
            ),
            updated_at=QUIET_INSTANT,
        )
        outcome = PolicyOutcome(
            decision=PolicyDecision(
                action=PolicyActionKind.SELL_INVENTORY,
                reason=PolicyReason.INVENTORY_CONVERGENCE_REACHED,
                diagnostics=("fixture convergence",),
            ),
            next_state=PolicyState(),
        )
        actions, halted, new_book = runner._act(book, outcome, b"\x01" * 32, "FIXc")
        assert [record.action for record in actions] == ["exit_swap"]
        assert halted == ""
        assert new_book.held_inventory is None


class TestPostActionVisibility:
    """Final reconciliation waits for the primary RPC to observe confirmed actions."""

    def test_live_cycle_waits_for_confirmed_block_before_final_reconcile(
        self, tmp_path: Path
    ) -> None:
        """A live action waits until the primary RPC reaches its confirmed block."""
        sleeps: list[float] = []

        class LaggingBalances(FakeBalances):
            def __init__(self) -> None:
                super().__init__(block_number=100)
                self.blocks = iter((100, 120, 51_000_000))

            def fetch_block_number(self) -> int:
                self.block_number = next(self.blocks)
                return self.block_number

        balances = LaggingBalances()
        runner, executor, _, _ = make_runner(tmp_path, balances=balances, sleep=sleeps.append)
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=Decimal("100"),
        )

        assert report.final_reconciliation_verified is True
        assert report.decision_reconciliation is not None
        assert sleeps == [0.5, 1.0]

    def test_visibility_timeout_marks_final_reconciliation_unverified(self, tmp_path: Path) -> None:
        """A bounded catch-up failure is explicit instead of silently reporting stale state."""
        balances = FakeBalances(block_number=100)
        runner, executor, _, _ = make_runner(
            tmp_path, balances=balances, sleep=lambda _seconds: None
        )
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=Decimal("100"),
        )

        assert report.final_reconciliation_verified is False
        assert "post-action reconciliation is unverified" in report.halted_reason
        assert any(
            "WARNING: final balances may lag" in line for line in report.reconciliation.diagnostics
        )


class TestReconciliation:
    """The reconcile-first discipline and its out-of-band refusals."""

    def test_above_range_wait_survives_across_scheduled_cycles(self, tmp_path: Path) -> None:
        """The fifteen-minute recenter clock never restarts on each cycle."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        # Explicitly put the token1-stock NFT below the fixture's
        # USDC/stock price so this test exercises the upside wait path.
        above_range = tracked_status().model_copy(
            update={
                "position": SimpleNamespace(
                    tick_lower=-10,
                    tick_upper=10,
                    token0_address=BASE_USDC_ADDRESS,
                    token1_address=STOCK_TOKEN_ADDRESS,
                    liquidity=12_345,
                )
            }
        )
        reads.set_status(TRACKED_TOKEN_ID, above_range)
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        first = runner.run(
            CycleMode.DRY_RUN,
            reference_price_usdc=FIXTURE_AMM_PRICE,
        )
        assert first.decision_reason == "open_above_range_waiting"

        position = state_store.load().position
        assert position is not None
        assert position.out_of_range_since == QUIET_INSTANT

        runner._now = lambda: QUIET_INSTANT + timedelta(minutes=5)
        second = runner.run(
            CycleMode.DRY_RUN,
            reference_price_usdc=FIXTURE_AMM_PRICE,
        )
        assert second.decision_reason == "open_above_range_waiting"
        assert any("0:05:00" in line for line in second.decision_diagnostics)

        position = state_store.load().position
        assert position is not None
        assert position.out_of_range_since == QUIET_INSTANT

    def test_tracked_position_reports_custody_value_and_pnl(self, tmp_path: Path) -> None:
        """An open tracked position carries its P&L into the report."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status())
        runner, _, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)
        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert report.reconciliation.tracked_staked is False
        assert report.pnl_vs_entry_usdc == Decimal("1")
        assert report.decision_action == "hold"

    def test_live_hold_recovers_an_unstaked_tracked_position(self, tmp_path: Path) -> None:
        """A partial prior cycle is healed by restaking the valid Safe-held NFT."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=SAFE_ADDRESS))
        runner, executor, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)
        assert executor is not None

        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=FIXTURE_AMM_PRICE,
        )

        assert report.decision_action == "hold"
        assert [call[0] for call in executor.calls] == ["stake"]
        assert len(report.actions) == 1
        assert report.actions[0].action == "stake_recovery"
        assert report.actions[0].status == "completed"
        assert report.reconciliation.tracked_staked is True
        assert report.halted_reason == ""

    def test_external_reference_cannot_force_scheduled_position_exit(self, tmp_path: Path) -> None:
        """A divergent external quote is advisory and cannot order an Aerodrome exit."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status())
        runner, _, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)
        report = runner.run(
            CycleMode.DRY_RUN,
            reference_price_usdc=Decimal("1"),
            reference_age_seconds=999999,
        )
        assert report.reconciliation.tracked_token_id == TRACKED_TOKEN_ID
        assert report.decision_action == "hold"
        assert report.decision_reason in {
            "open_in_range",
            "open_above_range_waiting",
            "open_below_edge_holding",
        }
        assert "dislocation" not in report.decision_reason
        assert "reference_stale" not in report.decision_reason
        assert any("diagnostic-only" in note for note in report.input_notes)

    def test_a_crashed_entry_is_adopted_from_audit_evidence(self, tmp_path: Path) -> None:
        """One live untracked position proven by the audit chain adopts."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status())
        balances = FakeBalances()
        balances.receipts[MINT_TX_HASH] = mint_receipt(TRACKED_TOKEN_ID)
        runner, _, _, state_store = make_runner(
            tmp_path, reads=reads, balances=balances, audit_seed=True
        )
        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert report.reconciliation.out_of_band == ""
        assert report.reconciliation.tracked_token_id == TRACKED_TOKEN_ID
        book = state_store.load()
        assert book.position is not None
        assert book.position.token_id == TRACKED_TOKEN_ID
        assert book.position.committed_usd == Decimal("7")

    def test_a_newer_foreign_mint_plan_never_labels_the_adopted_position(
        self, tmp_path: Path
    ) -> None:
        """The adoption's label is proven for the NFT, never the newest plan.

        A manual execute-mode mint can leave one live Safe-held NFT whose
        stake plan was never recorded; a later refused entry for a
        different symbol still writes its own mint plan during the build,
        crowning the chain's newest plan. Labeling the adopted NFT from
        that plan pairs another symbol's label with this pool's NFT and
        wedges every later cycle on the wrong pool's position read.
        """
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status())
        balances = FakeBalances()
        balances.receipts[MINT_TX_HASH] = mint_receipt(TRACKED_TOKEN_ID)
        runner, _, audit, state_store = make_runner(
            tmp_path, reads=reads, balances=balances, audit_seed=True
        )
        audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(mode="execute", budget_usdc="9", symbol="OTHRc"),
            QUIET_INSTANT,
        )

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert report.reconciliation.out_of_band == ""
        position = state_store.load().position
        assert position is not None
        assert position.symbol == "FIXc"
        assert position.pool_address == POOL_ADDRESS
        assert position.committed_usd == Decimal("7")
        second = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert second.halted_reason == ""
        assert second.decision_action == "hold"

    def test_an_adoption_without_a_receipt_linked_plan_skips_visibly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No proven committed basis means no adoption, and a visible reason.

        The confirmed delivery can prove the NFT ours while the plan that
        priced its build never reached the chain (a lost audit append).
        The book then never guesses a basis: the adoption skips with an
        operator-visible line rather than adopting silently mislabeled.
        """
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        balances = FakeBalances()
        balances.receipts[MINT_TX_HASH] = mint_receipt(TRACKED_TOKEN_ID)
        runner, _, audit, state_store = make_runner(
            tmp_path, reads=reads, balances=balances, audit_seed=False
        )
        audit.append(
            AuditEventType.LP_EXECUTE_CONFIRMED,
            FakeConfirmPayload(
                outcome="confirmed",
                action="mint",
                role="mint",
                transaction_hash=MINT_TX_HASH,
            ),
            QUIET_INSTANT,
        )

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert report.reconciliation.out_of_band == ""
        assert report.reconciliation.tracked_token_id == TRACKED_TOKEN_ID
        assert state_store.load().positions == ()
        assert "skipping the adoption of live position NFT" in capsys.readouterr().err

    def test_the_adopted_row_carries_the_linked_plans_symbol_not_the_anchor(
        self, tmp_path: Path
    ) -> None:
        """The row's pool label is the NFT's own, never the reconcile anchor.

        Slipstream shares one NFPM per generation across every pool, so an
        empty selector book's anchor (the board's first listing) enumerates
        every pool's Safe-held NFT: a first entry minted on the allocator's
        top rank, killed before its stake plan, must adopt with its own
        plan's symbol - labeling it with the anchor would price and manage
        the position against the wrong pool with no error.
        """
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status().model_copy(
                update={"symbol": "BBBc", "pool_address": SELECTOR_BBB_POOL}
            ),
        )
        balances = FakeBalances(usdc_units=SELECTOR_BOOK_USDC_UNITS)
        balances.receipts[MINT_TX_HASH] = mint_receipt(TRACKED_TOKEN_ID)
        runner, _, audit, state_store = make_runner(
            tmp_path,
            reads=reads,
            balances=balances,
            sources=SelectorCycleSources(usdc_units=SELECTOR_BOOK_USDC_UNITS),
            symbol=None,
        )
        audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(mode="execute", budget_usdc="12", symbol="BBBc"),
            QUIET_INSTANT,
        )
        audit.append(
            AuditEventType.LP_EXECUTE_CONFIRMED,
            FakeConfirmPayload(
                outcome="confirmed",
                action="mint",
                role="mint",
                transaction_hash=MINT_TX_HASH,
            ),
            QUIET_INSTANT,
        )

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=SELECTOR_REFERENCES)

        assert report.reconciliation.out_of_band == ""
        decision_recon = report.decision_reconciliation
        assert decision_recon is not None
        assert decision_recon.symbol == "AAAc"
        position = state_store.load().position
        assert position is not None
        assert position.token_id == TRACKED_TOKEN_ID
        assert position.symbol == "BBBc"
        assert position.pool_address == SELECTOR_BBB_POOL
        assert position.committed_usd == Decimal("12")

    def test_unproven_live_positions_refuse_out_of_band(self, tmp_path: Path) -> None:
        """A live position with no audit evidence stops the cycle."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        runner, _, _, state_store = make_runner(tmp_path, reads=reads)
        report = runner.run(CycleMode.DRY_RUN)
        assert "no audit evidence" in report.reconciliation.out_of_band
        assert report.halted_reason == report.reconciliation.out_of_band
        assert report.decision_reason == "out_of_band"
        assert state_store.load().position is None

    def test_custody_violation_refuses_out_of_band(self, tmp_path: Path) -> None:
        """A tracked position owned elsewhere stops the cycle."""
        stranger = "0x" + "77" * 20
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=stranger, pnl=None))
        runner, _, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)
        report = runner.run(CycleMode.DRY_RUN)
        assert "owned by" in report.reconciliation.out_of_band
        assert report.halted_reason != ""

    def test_an_unrecorded_stock_balance_becomes_held_inventory(self, tmp_path: Path) -> None:
        """A flat Safe holding stock adopts it as held inventory."""
        balances = FakeBalances(stock_units=2_100_000)
        runner, _, _, state_store = make_runner(tmp_path, balances=balances)
        report = runner.run(CycleMode.DRY_RUN)
        assert report.reconciliation.held_stock_quantity == Decimal("0.021")
        book = state_store.load()
        assert book.held_inventory is not None
        assert book.held_inventory.stock_quantity == Decimal("0.021")

    def test_rebuild_persists_post_action_unrecorded_stock(self, tmp_path: Path) -> None:
        """Post-action reconciliation persists stock acquired before a failed mint."""
        runner, _, _, _ = make_runner(tmp_path)
        reconciliation = runner._reconcile(CycleStateBook()).model_copy(
            update={
                "held_stock_quantity": Decimal("0.14296689"),
                "held_symbol": "FIXc",
                "safe_stock_units": 14_296_689,
            }
        )
        rebuilt = runner._rebuild_book(
            CycleStateBook(), reconciliation, PolicyState(), "FIXc", PolicyActionKind.RECENTER
        )
        assert rebuilt.position is None
        assert rebuilt.held_inventory is not None
        assert rebuilt.held_inventory.symbol == "FIXc"
        assert rebuilt.held_inventory.stock_quantity == Decimal("0.14296689")


class TestCrashedExitHeal:
    """The 2026-09-28 live wedge: an empty tracked NFT heals out of the book.

    Production evidence: the 11:24 UTC cycle withdrew METAc token 7149956
    on-chain (decrease_liquidity and collect included), then the re-entry
    mint died at 11:32 on an audit-store ``database is locked`` crash before
    the book updated. Every later cycle re-commanded the dead NFT into a
    withdraw ``position_empty`` refusal and halted - fifteen-plus identical
    crash-loops while the capital sat safe in the Safe. The reconcile now
    drops a verifiably empty tracked position so decide never re-commands
    it, and the crash recovery is idempotent.
    """

    def test_empty_tracked_position_drops_with_evidence(self, tmp_path: Path) -> None:
        """A zero-liquidity unstaked NFT leaves the book with a loud diagnostic."""
        reads = FakeReads(empty_inventory())
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(liquidity=0, value=Decimal("0"), pnl=None),
        )
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        decision_recon = report.decision_reconciliation
        assert decision_recon is not None
        assert decision_recon.empty_tracked_token_ids == (TRACKED_TOKEN_ID,)
        assert decision_recon.tracked_token_id is None
        assert decision_recon.position_statuses == ()
        assert any("reconciles EMPTY on-chain" in line for line in decision_recon.diagnostics)
        assert any(
            "dropping the stale tracking from the book" in line
            for line in decision_recon.diagnostics
        )
        # The final reconciliation is clean and the book no longer tracks the dead NFT.
        assert report.reconciliation.empty_tracked_token_ids == ()
        assert report.reconciliation.tracked_token_id is None
        assert state_store.load().positions == ()

    def test_live_heal_never_attempts_the_dead_withdraw_and_redeploys(self, tmp_path: Path) -> None:
        """The wedged book heals, no withdraw is commanded, capital redeploys."""
        reads = FakeReads(empty_inventory())
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(liquidity=0, value=Decimal("0"), pnl=None),
        )
        runner, executor, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)
        assert executor is not None

        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_price_usdc=FIXTURE_AMM_PRICE,
        )

        assert report.halted_reason == ""
        assert "position_empty" not in report.halted_reason
        assert all(call[0] not in ("withdraw", "unstake") for call in executor.calls)
        assert [call[0] for call in executor.calls] == ["mint", "stake"]
        book = state_store.load()
        assert book.position is not None
        assert book.position.token_id == TRACKED_TOKEN_ID

    def test_healed_book_reruns_clean_and_idempotent(self, tmp_path: Path) -> None:
        """The cycle after the heal carries no empty residual and no error."""
        reads = FakeReads(empty_inventory())
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(liquidity=0, value=Decimal("0"), pnl=None),
        )
        runner, _, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)

        first = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert first.decision_reconciliation is not None
        assert first.decision_reconciliation.empty_tracked_token_ids == (TRACKED_TOKEN_ID,)

        second = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert second.decision_reconciliation is not None
        assert second.decision_reconciliation.empty_tracked_token_ids == ()
        assert second.halted_reason == ""

    def test_stray_stock_from_the_crashed_reentry_is_adopted(self, tmp_path: Path) -> None:
        """The balancing swap's leftover stock is adopted once the book is flat."""
        reads = FakeReads(empty_inventory())
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(liquidity=0, value=Decimal("0"), pnl=None),
        )
        # The 11:27 balancing swap bought stock for the mint that never ran.
        balances = FakeBalances(stock_units=2_100_000)
        runner, _, _, state_store = make_runner(
            tmp_path,
            book=tracked_book(),
            reads=reads,
            balances=balances,
        )

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        decision_recon = report.decision_reconciliation
        assert decision_recon is not None
        assert decision_recon.empty_tracked_token_ids == (TRACKED_TOKEN_ID,)
        assert any(
            "adopting unrecorded FIXc stock balance" in line for line in decision_recon.diagnostics
        )
        book = state_store.load()
        assert book.positions == ()
        assert book.held_inventory is not None
        assert book.held_inventory.symbol == "FIXc"
        assert book.held_inventory.stock_quantity == Decimal("0.021")

    def test_live_liquidity_is_never_dropped(self, tmp_path: Path) -> None:
        """A position carrying liquidity stays tracked whatever its range."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status())
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert report.reconciliation.empty_tracked_token_ids == ()
        assert state_store.load().position is not None

    def test_owed_fees_are_never_dropped(self, tmp_path: Path) -> None:
        """A zero-liquidity NFT still owed fees stays tracked for the collect."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(liquidity=0, value=Decimal("0"), pnl=None, fees_owed0_units=500),
        )
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert report.reconciliation.empty_tracked_token_ids == ()
        assert report.reconciliation.tracked_token_id == TRACKED_TOKEN_ID
        assert state_store.load().position is not None

    def test_staked_position_is_never_dropped(self, tmp_path: Path) -> None:
        """A gauge-held NFT stays governed by the unstake path even when empty."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(owner=GAUGE_ADDRESS, liquidity=0, value=Decimal("0"), pnl=None),
        )
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert report.reconciliation.empty_tracked_token_ids == ()
        assert state_store.load().position is not None


class TestReferenceEnvironment:
    """The optional injected reference quote from the environment."""

    def test_a_positive_value_parses(self) -> None:
        """A valid quote parses into a Decimal."""
        assert _reference_price_from_environment({CYCLE_REFERENCE_PRICE_ENV: "318.5"}) == Decimal(
            "318.5"
        )

    def test_absent_or_invalid_values_fail_closed(self) -> None:
        """No variable means no quote; garbage refuses."""
        assert _reference_price_from_environment({}) is None
        with pytest.raises(ValueError, match="positive"):
            _reference_price_from_environment({CYCLE_REFERENCE_PRICE_ENV: "-1"})


class TestSystemdUnits:
    """The deployment contract the timer and service units pin."""

    def test_the_timer_defaults_five_minutes_with_persistence_and_jitter(self) -> None:
        """Five-minute policy cycles honor fifteen-minute persistence rules."""
        timer = Path("deploy/systemd/aero-bot-cycle@.timer").read_text(encoding="utf-8")
        assert "OnCalendar=*:0/5" in timer
        assert "Persistent=true" in timer
        assert "AccuracySec=15s" in timer
        assert "RandomizedDelaySec=15" in timer
        assert "Unit=aero-bot-cycle@%i.service" in timer

    def test_the_service_is_a_hardened_oneshot(self) -> None:
        """The cycle is a oneshot under a dedicated user with sealed env."""
        service = Path("deploy/systemd/aero-bot-cycle@.service").read_text(encoding="utf-8")
        assert "Type=oneshot" in service
        assert "User=aero-bot" in service
        assert "EnvironmentFile=/etc/aero-bot/cycle.env" in service
        assert "NoNewPrivileges=true" in service
        assert "Restart=no" in service
        assert "aero-bot-cycle --symbol %i --json" in service


class TestLiveRelayerGuard:
    """The live-mode relayer guard compares addresses, not casing."""

    def test_checksummed_relayer_matches_the_derived_address(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checksummed environment relayer is the same relayer.

        The first armed scheduled cycle refused the correct signing source
        because the raw environment string (checksummed) compared unequal to
        the lowercase derived address; both sides now normalize first.
        """
        from eth_account import Account

        from aero_bot import cycle as cycle_module

        account = Account.from_key("0x" + "22" * 32)
        checksummed = account.address
        assert checksummed != checksummed.lower()

        class _FakeKeySource:
            def load_signing_key(self) -> bytes:
                return bytes(account.key)

        class _FakeRunner:
            def run(
                self,
                mode: object,
                key_bytes: bytes | None = None,
                reference_price_usdc: object = None,
                reference_age_seconds: object = None,
                reference_prices_by_symbol: object = None,
            ) -> object:
                class _Report:
                    mode = "live"
                    symbol = "AAPLc"
                    decision_action = "hold"
                    decision_reason = "reference_stale"
                    decision_diagnostics: tuple[str, ...] = ()
                    event_window = "none"
                    actions: tuple[object, ...] = ()
                    pnl_vs_entry_usdc = None
                    pnl_diagnostic = "no tracked position"
                    fee_wei = 0
                    halted_reason = ""
                    input_notes: tuple[str, ...] = ()
                    reconciliation = None

                    def model_dump_json(self, indent: int = 2) -> str:
                        return "{}"

                return _Report()

        environment = {
            "AERO_BOT_SAFE_ADDRESS": "0xB69ab6C7E73F711D5f2d10feD8f0d09B1D028C28",
            "AERO_BOT_RELAYER_ADDRESS": checksummed,
            "AERO_BOT_AUDIT_DATABASE_PATH": str(tmp_path / "audit.sqlite3"),
            "AERO_BOT_LP_POOL_PINS_PATH": str(tmp_path / "pins.json"),
            "AERO_BOT_CYCLE_STATE_PATH": str(tmp_path / "state.json"),
        }
        import os as _os

        monkeypatch.setattr(_os, "environ", environment)
        monkeypatch.setattr(
            "aero_bot.signing_key.load_signing_key_source", lambda: _FakeKeySource()
        )
        monkeypatch.setattr(cycle_module, "build_cycle_runner", lambda *a, **k: _FakeRunner())
        exit_code = cycle_module.main(["--symbol", "AAPLc", "--json"])
        assert exit_code == 0, "the guard must accept the case-insensitive match"


# A second stock token and pool for the cross-board selector fixtures.
SELECTOR_BBB_TOKEN = "0xbb0000000000000000000078ee7ce2fe4908108c"  # noqa: S105
SELECTOR_BBB_POOL = "0x2222222222222222222222222222222222222222"
# The selector fixtures run an allocator-era book: five hundred USDC of
# cash, so the eighty-USDC minimum position and the thirty-five percent
# concentration bound bind exactly as the captain's ruling intends.
SELECTOR_BOOK_USDC_UNITS = 500_000_000
# Both pools quote the same fixture price, so one reference map serves both.
SELECTOR_REFERENCES = {"AAAc": FIXTURE_AMM_PRICE, "BBBc": FIXTURE_AMM_PRICE}


def selector_listings(bbb_emissions_multiplier: int = 2) -> tuple[BoardListing, ...]:
    """Build the two-pool scripted board: AAAc plain, BBBc scaled emissions.

    The staked liquidity is sized so both pools read qualifying APRs of
    about 250 and 500 percent at the fixture AERO price - a moderate
    regime where the income-basis flooring rarely changes an assertion,
    so the ordinary portfolio tests exercise the allocator (the
    thin-staked high-reading shapes live in their own tests).
    """
    return (
        BoardListing(
            symbol="AAAc",
            pool=make_candidate(
                staked0=15_600 * 10**6,
                staked1=100 * 10**STOCK_DECIMALS,
            ),
        ),
        BoardListing(
            symbol="BBBc",
            pool=make_candidate(
                pool_address=SELECTOR_BBB_POOL,
                token1_address=SELECTOR_BBB_TOKEN,
                emissions_per_second=bbb_emissions_multiplier * 4_494_371_922_759_724,
                # One USDC more staked than AAAc: the doubled emissions
                # then rank BBBc clear of the band floor by a real margin
                # instead of an exact 2:1 ratio that one ulp of division
                # rounding can flip.
                staked0=15_601 * 10**6,
                staked1=100 * 10**STOCK_DECIMALS,
            ),
        ),
    )


class SelectorCycleSources(FakeCycleSources):
    """Serve the two-pool board with per-symbol registry resolution."""

    def __init__(self, **kwargs: object) -> None:
        """Configure the scripted board and balances."""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._selector_listings = selector_listings()

    def with_listings(self, listings: tuple[BoardListing, ...]) -> "SelectorCycleSources":
        """Return a copy of these sources serving a different board."""
        clone = SelectorCycleSources()
        clone._usdc_units = self._usdc_units
        clone._stock_units = self._stock_units
        clone._selector_listings = listings
        return clone

    def resolve_pool(self, symbol: str) -> tuple[PoolCandidate, int]:
        """Return the named board pool's candidate."""
        for listing in self._selector_listings:
            if listing.symbol.lower() == symbol.strip().lower():
                return listing.pool, 123
        raise ValueError(f"symbol {symbol!r} is not on the scripted board")

    def enumerate_pools(self) -> tuple[tuple[BoardListing, ...], int]:
        """Return the scripted two-pool board and its snapshot block."""
        return self._selector_listings, 123

    def symbol_address(self, symbol: str) -> str | None:
        """Resolve the two board symbols to their stock tokens."""
        if symbol.lower() == "aaac":
            return B20_ADDRESS
        if symbol.lower() == "bbbc":
            return SELECTOR_BBB_TOKEN
        return None

    def token_balance(self, token_address: str, owner_address: str) -> int:
        """Serve the Safe's USDC; board stocks carry no stray balance."""
        if token_address.lower() == BASE_USDC_ADDRESS.lower():
            return self._usdc_units
        return 0


def single_name_sources(**kwargs: object) -> SelectorCycleSources:
    """Serve the board trimmed to the held BBBc name alone.

    The post-minimum-removal allocator deploys any idle cash into a fresh
    qualifying name, so tests that need a funded book to hold - the latch
    and day-anchor arcs - serve the held name only, leaving the cash with
    no second pool to enter.
    """
    sources = SelectorCycleSources(**kwargs)
    held = tuple(item for item in selector_listings() if item.symbol.lower() == "bbbc")
    return sources.with_listings(held)


def selector_runner(
    tmp_path: Path,
    *,
    book: CycleStateBook | None = None,
    reads: FakeReads | None = None,
    executor: object | None = None,
    sources: SelectorCycleSources | None = None,
    balances: FakeBalances | None = None,
    aero_conversion_min_usdc: Decimal | None = None,
) -> tuple[CycleRunner, FakeExecutor | None, CycleStateStore]:
    """Assemble one selector-mode cycle runner over the scripted board."""
    runner, fake_executor, _, state_store = make_runner(
        tmp_path,
        book=book,
        reads=reads,
        sources=sources
        if sources is not None
        else SelectorCycleSources(usdc_units=SELECTOR_BOOK_USDC_UNITS),
        executor=executor,
        symbol=None,
        balances=balances
        if balances is not None
        else FakeBalances(usdc_units=SELECTOR_BOOK_USDC_UNITS),
        aero_conversion_min_usdc=aero_conversion_min_usdc,
    )
    return runner, fake_executor, state_store


# ---------------------------------------------------------------------------
# The 2026-09-30 timeout incident fixtures: the four live names, pools, and
# the scout report's exact NFT ids and liquidity values
# (data/aero-bot-cycle-timeout/report.md).
# ---------------------------------------------------------------------------

# The surviving sibling the book kept tracking through the timeout.
TIMEOUT_SIBLING_TOKEN_ID = 7_310_376
# The killed cycle's three confirmed entries: NFT id, symbol, audited mint
# budget (the newest execute-mode plan before each stake plan), and the
# scout's live NFPM liquidity read (the third NFT's independent read hit an
# RPC reset, so its value mirrors the confirmed receipt's deployment).
TIMEOUT_ENTRIES: tuple[tuple[int, str, Decimal, int, Decimal], ...] = (
    (7_311_805, "TSLAc", Decimal("13.64859652562234191269156890"), 3_610_260_828, Decimal("12")),
    (7_312_392, "METAc", Decimal("15.08330000"), 2_790_693_399, Decimal("12.2")),
    (7_312_807, "SNDKc", Decimal("6.923894099820873036278282779"), 2_410_317_416, Decimal("11.5")),
)
# The book's day facts at the timeout, from the live persisted book: the
# day-start anchor the 11:46 cycle seeded and the running peak.
TIMEOUT_DAY_START = Decimal("104.2348945939082132986662799")
TIMEOUT_PEAK = Decimal("105.7353328824604165332248358")
# The equity the post-timeout cycles marked: sibling plus cash, omitting the
# three staked positions (the phantom drawdown's reading, mirrored at the
# fixture's digit width so every sum stays exact in the ambient context).
TIMEOUT_OMITTED_EQUITY = Decimal("68.528421317312414697474")
# Distinct synthetic pool identities per name so custody verification and
# board resolution never alias across pools.
TIMEOUT_POOL_ADDRESSES = {
    "MSTRc": "0x3100000000000000000000000000000000001001",
    "TSLAc": "0x3100000000000000000000000000000000001002",
    "METAc": "0x3100000000000000000000000000000000001003",
    "SNDKc": "0x3100000000000000000000000000000000001004",
}
TIMEOUT_STOCK_TOKENS = {
    "MSTRc": "0x4100000000000000000000000000000000001001",
    "TSLAc": "0x4100000000000000000000000000000000001002",
    "METAc": "0x4100000000000000000000000000000000001003",
    "SNDKc": "0x4100000000000000000000000000000000001004",
}
TIMEOUT_GAUGE_ADDRESSES = {
    "MSTRc": "0x5100000000000000000000000000000000001001",
    "TSLAc": "0x5100000000000000000000000000000000001002",
    "METAc": "0x5100000000000000000000000000000000001003",
    "SNDKc": "0x5100000000000000000000000000000000001004",
}
TIMEOUT_NFPM_ADDRESSES = {
    "MSTRc": "0x6100000000000000000000000000000000001001",
    "TSLAc": "0x6100000000000000000000000000000000001002",
    "METAc": "0x6100000000000000000000000000000000001003",
    "SNDKc": "0x6100000000000000000000000000000000001004",
}
# Both fixtures share one price, so one reference map serves every name.
TIMEOUT_REFERENCES = dict.fromkeys(TIMEOUT_POOL_ADDRESSES, FIXTURE_AMM_PRICE)


def timeout_listings() -> tuple[BoardListing, ...]:
    """Build the four-name board the timed-out cycle traded across."""
    return tuple(
        BoardListing(
            symbol=symbol,
            pool=make_candidate(
                pool_address=TIMEOUT_POOL_ADDRESSES[symbol],
                token1_address=TIMEOUT_STOCK_TOKENS[symbol],
                gauge_address=TIMEOUT_GAUGE_ADDRESSES[symbol],
                nfpm_address=TIMEOUT_NFPM_ADDRESSES[symbol],
                staked0=15_600 * 10**6,
                staked1=100 * 10**STOCK_DECIMALS,
            ),
        )
        for symbol in ("MSTRc", "TSLAc", "METAc", "SNDKc")
    )


class TimeoutCycleSources(SelectorCycleSources):
    """Serve the four-name incident board with per-symbol resolution."""

    def __init__(self, **kwargs: object) -> None:
        """Configure the incident board and balances."""
        super().__init__(**kwargs)
        self._selector_listings = timeout_listings()

    def symbol_address(self, symbol: str) -> str | None:
        """Resolve the four incident names to their stock tokens."""
        return TIMEOUT_STOCK_TOKENS.get(symbol)


def timeout_status(
    symbol: str,
    token_id: int,
    *,
    value: Decimal,
    liquidity: int = 12_345,
    owner: str | None = None,
) -> LpPositionStatusReport:
    """Build one incident position's status in its own pool's custody."""
    gauge = TIMEOUT_GAUGE_ADDRESSES[symbol]
    return tracked_status(
        owner=owner if owner is not None else gauge,
        value=value,
        pnl=None,
        liquidity=liquidity,
    ).model_copy(
        update={
            "symbol": symbol,
            "token_id": token_id,
            "pool_address": TIMEOUT_POOL_ADDRESSES[symbol],
            "gauge_address": gauge,
        }
    )


def timeout_empty_inventory() -> LpSafePositionsSnapshot:
    """Build the MSTRc-anchor inventory the Safe-held enumeration returned."""
    return LpSafePositionsSnapshot(
        symbol="MSTRc",
        pool_address=TIMEOUT_POOL_ADDRESSES["MSTRc"],
        nfpm_address=TIMEOUT_NFPM_ADDRESSES["MSTRc"],
        positions=(),
        snapshot_block=123,
        observed_at=QUIET_INSTANT,
        caps_enforced=("fixture",),
        diagnostics=("nothing held; the staked NFTs are gauge-owned",),
    )


def timeout_book(*, halted: date | None = None) -> CycleStateBook:
    """Build the persisted book exactly as the timeout left it.

    The 11:46 cycle's day facts ride beside the one surviving sibling; the
    three staked entries never reached the aggregate save.
    """
    return CycleStateBook(
        positions=(
            TrackedPosition(
                symbol="MSTRc",
                token_id=TIMEOUT_SIBLING_TOKEN_ID,
                pool_address=TIMEOUT_POOL_ADDRESSES["MSTRc"],
                committed_usd=Decimal("60.29119370327215717546763625"),
                entered_at=QUIET_INSTANT - timedelta(hours=2),
            ),
        ),
        day=QUIET_INSTANT.date(),
        day_start_equity_usd=TIMEOUT_DAY_START,
        peak_equity_usdc=TIMEOUT_PEAK,
        halted_day=halted,
        updated_at=QUIET_INSTANT,
    )


def timeout_reads(
    *,
    recovered_values: tuple[Decimal, ...] | None = None,
    sibling_value: Decimal = Decimal("36.198421317312414697474"),
) -> FakeReads:
    """Serve the incident's live custody: sibling staked, entries staked.

    The recovered values default to the honest marks (about 35.7 USDC); a
    test can pass smaller values to simulate a real drawdown instead.
    """
    values = recovered_values
    if values is None:
        values = tuple(entry[4] for entry in TIMEOUT_ENTRIES)
    reads = FakeReads(timeout_empty_inventory())
    reads.set_status(
        TIMEOUT_SIBLING_TOKEN_ID,
        timeout_status("MSTRc", TIMEOUT_SIBLING_TOKEN_ID, value=sibling_value),
    )
    for (token_id, symbol, _budget, liquidity, _mark), value in zip(
        TIMEOUT_ENTRIES, values, strict=True
    ):
        reads.set_status(
            token_id, timeout_status(symbol, token_id, value=value, liquidity=liquidity)
        )
    return reads


def seed_timeout_entries(
    audit: AuditStore,
    *,
    mode: str = "execute",
    entries: tuple[tuple[int, str, Decimal, int, Decimal], ...] | None = None,
) -> None:
    """Append the killed cycle's per-sibling audit evidence in chain order.

    Each sibling leaves one execute-mode mint plan (the committed basis),
    one stake plan naming the NFT, and the confirmed stake delivery - the
    same records the production chain carries for the three live entries.
    """
    for token_id, symbol, budget, _liquidity, _mark in entries or TIMEOUT_ENTRIES:
        audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(mode="execute", budget_usdc=str(budget), symbol=symbol),
            QUIET_INSTANT,
        )
        audit.append(
            AuditEventType.LP_STAKE_PLANNED,
            FakeStakePlanPayload(mode=mode, symbol=symbol, token_id=token_id),
            QUIET_INSTANT,
        )
        audit.append(
            AuditEventType.LP_EXECUTE_CONFIRMED,
            FakeConfirmPayload(
                outcome="confirmed",
                action="stake",
                role="gauge_deposit",
                transaction_hash="0x" + "ee" * 32,
            ),
            QUIET_INSTANT,
        )


def timeout_runner(
    tmp_path: Path,
    *,
    book: CycleStateBook | None = None,
    reads: FakeReads | None = None,
    seed: bool = True,
    symbol: str | None = None,
) -> tuple[CycleRunner, FakeExecutor | None, AuditStore, CycleStateStore]:
    """Assemble one runner over the incident board and balances."""
    assembled = make_runner(
        tmp_path,
        book=book if book is not None else timeout_book(),
        reads=reads if reads is not None else timeout_reads(),
        sources=TimeoutCycleSources(usdc_units=32_330_000),
        balances=FakeBalances(usdc_units=32_330_000),
        symbol=symbol,
    )
    if seed:
        seed_timeout_entries(assembled[2])
    return assembled


class TestTimeoutMultiEntryRecovery:
    """The 2026-09-30 timeout incident, pinned end to end.

    Production evidence (data/aero-bot-cycle-timeout/report.md): the 11:46 UTC
    cycle minted and gauge-staked TSLAc 7311805, METAc 7312392, and SNDKc
    7312807 beside the surviving MSTRc 7310376; systemd's thirty-minute
    timeout terminated the service before the aggregate book save, and the
    next cycles enumerated a Safe-owned inventory that cannot see gauge
    custody - equity marked 68.53 against the 104.23 day anchor and the
    daily-loss halt latched on the phantom 36-USDC drawdown. The cycle now
    checkpoints the book after every completed mint-and-stake pair and
    recovers audit-proven gauge custody in the reconcile, so the killed
    cycle's completed siblings are priced into equity before the latch
    observes anything.
    """

    def test_the_next_cycle_recovers_the_staked_siblings_and_equity(self, tmp_path: Path) -> None:
        """Equity reads the whole book (about 104.23), not the phantom 68.53."""
        runner, _, _, state_store = timeout_runner(tmp_path)

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        assert report.halted_reason == ""
        assert report.reconciliation.out_of_band == ""
        assert report.equity_usd == Decimal("104.228421317312414697474")
        assert any("gate daily_loss_halt: PASS" in line for line in report.decision_diagnostics)
        book = state_store.load()
        assert {position.token_id for position in book.positions} == {
            TIMEOUT_SIBLING_TOKEN_ID,
            7_311_805,
            7_312_392,
            7_312_807,
        }
        assert book.halted_day is None

    def test_recovery_diagnostics_name_the_killeds_cycles_evidence(self, tmp_path: Path) -> None:
        """The reconcile says which NFTs it recovered and why it can prove them."""
        runner, _, _, _ = timeout_runner(tmp_path)

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        decision_recon = report.decision_reconciliation
        assert decision_recon is not None
        diagnostics = decision_recon.diagnostics
        for token_id, symbol, _budget, _liquidity, _mark in TIMEOUT_ENTRIES:
            assert any(
                f"recovering audit-proven position NFT {token_id} on {symbol}" in line
                for line in diagnostics
            ), (token_id, diagnostics)
        assert any("stake plan" in line and "prove" in line for line in diagnostics), diagnostics

    def test_the_committed_basis_comes_from_the_audited_mint_plan(self, tmp_path: Path) -> None:
        """Each recovered position carries its own period's mint budget."""
        runner, _, _, state_store = timeout_runner(tmp_path)

        runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        committed = {
            position.token_id: position.committed_usd for position in state_store.load().positions
        }
        for token_id, _symbol, budget, _liquidity, _mark in TIMEOUT_ENTRIES:
            assert committed[token_id] == budget

    def test_recovery_is_idempotent_across_cycles(self, tmp_path: Path) -> None:
        """The cycle after the recovery tracks the same four, no duplicates."""
        runner, _, _, state_store = timeout_runner(tmp_path)

        first = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)
        first_recon = first.decision_reconciliation
        assert first_recon is not None
        assert len(first_recon.recovered_positions) == 3

        second = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)
        second_recon = second.decision_reconciliation
        assert second_recon is not None
        assert second_recon.recovered_positions == ()
        assert second.halted_reason == ""
        book = state_store.load()
        assert len(book.positions) == 4
        assert len({position.token_id for position in book.positions}) == 4

    def test_the_phantom_latch_clears_through_verified_recovery(self, tmp_path: Path) -> None:
        """The already-latched book (the live 12:28 shape) unlatches honestly."""
        runner, _, _, state_store = timeout_runner(
            tmp_path, book=timeout_book(halted=QUIET_INSTANT.date())
        )

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        assert any("gate daily_loss_halt: PASS" in line for line in report.decision_diagnostics)
        assert state_store.load().halted_day is None
        assert len(state_store.load().positions) == 4

    def test_a_real_loss_still_latches_after_recovery(self, tmp_path: Path) -> None:
        """Recovered positions marked genuinely down still trip the halt."""
        runner, _, _, state_store = timeout_runner(
            tmp_path,
            reads=timeout_reads(recovered_values=(Decimal("4"), Decimal("4.2"), Decimal("3.5"))),
        )

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        # The siblings still adopt (honest equity over the real marks)...
        decision_recon = report.decision_reconciliation
        assert decision_recon is not None
        assert len(decision_recon.recovered_positions) == 3
        assert report.equity_usd == Decimal("80.228421317312414697474")
        # ...and the 23-percent real drawdown from the 104.23 anchor latches.
        assert any("gate daily_loss_halt: FAIL" in line for line in report.decision_diagnostics)
        assert state_store.load().halted_day == QUIET_INSTANT.date()

    def test_dry_run_stake_plans_never_adopt(self, tmp_path: Path) -> None:
        """Rehearsal evidence cannot prove custody; nothing adopts from it."""
        runner, _, audit, state_store = timeout_runner(tmp_path, seed=False)
        seed_timeout_entries(audit, mode="dry_run")

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        decision_recon = report.decision_reconciliation
        assert decision_recon is not None
        assert decision_recon.recovered_positions == ()
        assert len(state_store.load().positions) == 1

    def test_stranger_custody_on_a_proven_position_refuses(self, tmp_path: Path) -> None:
        """A stake-plan NFT held by a stranger refuses the cycle out-of-band."""
        stranger = "0x" + "77" * 20
        reads = timeout_reads()
        reads.set_status(
            7_311_805,
            timeout_status("TSLAc", 7_311_805, value=Decimal("12"), owner=stranger),
        )
        runner, _, _, _ = timeout_runner(tmp_path, reads=reads)

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        assert "owned by" in report.reconciliation.out_of_band
        assert report.halted_reason == report.reconciliation.out_of_band

    def test_a_position_not_owned_refusal_on_a_candidate_refuses(self, tmp_path: Path) -> None:
        """The production custody read refuses strangers instead of returning.

        The real executor's status read raises position_not_owned inside its
        own ownership gate, never returning a stranger-owned status, so the
        recovery must distinguish that code from burned history: a
        Safe-proven NFT held by a stranger is equity the observation must
        never silently omit.
        """

        class StrangerCustodyReads(FakeReads):
            """Mirror the executor's ownership gate on the candidate ids."""

            def __init__(self, base: FakeReads, refuse_ids: frozenset[int]) -> None:
                super().__init__(base._snapshot)
                self._statuses = dict(base._statuses)
                self._refuse_ids = refuse_ids

            def position_status(
                self,
                symbol: str,
                token_id: int,
                aero_price_usdc: Decimal | None = None,
                entry_cost_usdc: Decimal | None = None,
            ) -> LpPositionStatusReport:
                if token_id in self._refuse_ids:
                    raise LpExecutionRefusalError(
                        LpExecutionRefusalCode.POSITION_NOT_OWNED,
                        f"token {token_id} is owned by a stranger, which is neither "
                        "this Safe nor the pool's gauge",
                    )
                return super().position_status(symbol, token_id, aero_price_usdc, entry_cost_usdc)

        candidates = frozenset(token_id for token_id, *_ in TIMEOUT_ENTRIES)
        runner, _, _, _ = timeout_runner(
            tmp_path, reads=StrangerCustodyReads(timeout_reads(), candidates)
        )

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        assert "owned by a stranger" in report.reconciliation.out_of_band
        assert "refusing the cycle" in report.reconciliation.out_of_band
        assert report.halted_reason == report.reconciliation.out_of_band

    def test_a_foreign_safes_stake_plan_never_adopts(self, tmp_path: Path) -> None:
        """A canary-flavored plan cannot leak its NFT into this book.

        The audit store is shared by every Safe that executes through it
        and a gauge is a shared custodian, so a foreign Safe's staked NFT
        passes the live custody read; only the plan's own recorded owner
        proves the candidate was minted for this runner's Safe.
        """
        foreign_safe = "0x" + "88" * 20
        reads = timeout_reads()
        reads.set_status(9_999_999, timeout_status("TSLAc", 9_999_999, value=Decimal("5")))
        runner, _, audit, state_store = timeout_runner(tmp_path, reads=reads)
        audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(mode="execute", budget_usdc="5", symbol="TSLAc"),
            QUIET_INSTANT,
        )
        audit.append(
            AuditEventType.LP_STAKE_PLANNED,
            FakeStakePlanPayload(
                mode="execute",
                symbol="TSLAc",
                token_id=9_999_999,
                token_owner_address=foreign_safe,
            ),
            QUIET_INSTANT,
        )

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        assert report.reconciliation.out_of_band == ""
        book = state_store.load()
        assert {position.token_id for position in book.positions} == {
            TIMEOUT_SIBLING_TOKEN_ID,
            7_311_805,
            7_312_392,
            7_312_807,
        }

    def test_a_pinned_cycle_skips_recovery_with_a_visible_reason(self, tmp_path: Path) -> None:
        """A pinned book cannot represent cross-symbol rows, so it never folds.

        The pinned surfaces price one position and the pinned rebuild writes
        a single-position book, so folding recovered siblings in a pinned
        cycle would wipe the very book it healed; the pinned cycle instead
        skips the recovery and names the skipped candidates and their
        committed value, leaving the heal to the next selector cycle.
        """
        runner, _, _, state_store = timeout_runner(tmp_path, symbol="MSTRc")

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert report.reconciliation.out_of_band == ""
        assert report.reconciliation.recovered_positions == ()
        skip_lines = [
            line for line in report.reconciliation.diagnostics if "pinned MSTRc cycle skips" in line
        ]
        assert skip_lines, report.reconciliation.diagnostics
        skip_line = skip_lines[0]
        assert "3 audit-proven" in skip_line
        assert "7311805" in skip_line and "7312392" in skip_line and "7312807" in skip_line
        assert "USDC committed" in skip_line
        book = state_store.load()
        assert [position.token_id for position in book.positions] == [TIMEOUT_SIBLING_TOKEN_ID]

    def test_an_empty_book_folds_each_recovered_siblings_own_labels(self, tmp_path: Path) -> None:
        """The fold is the authoritative adoption for a recovered NFT.

        A newer mint plan for a different symbol (every refused execute
        mint records its plan) must never label a recovered NFT: pairing
        that symbol with this pool's NFT would wedge every later cycle on
        the wrong pool's position read.
        """
        runner, _, audit, state_store = timeout_runner(
            tmp_path, book=CycleStateBook(day=QUIET_INSTANT.date())
        )
        audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(mode="execute", budget_usdc="9", symbol="MSTRc"),
            QUIET_INSTANT,
        )

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        assert report.reconciliation.out_of_band == ""
        rows = {
            position.token_id: (position.symbol, position.committed_usd)
            for position in state_store.load().positions
        }
        assert rows == {
            7_311_805: ("TSLAc", Decimal("13.64859652562234191269156890")),
            7_312_392: ("METAc", Decimal("15.08330000")),
            7_312_807: ("SNDKc", Decimal("6.923894099820873036278282779")),
        }

    def test_burned_history_candidates_skip_quietly(self, tmp_path: Path) -> None:
        """An exited position's stale stake plan adopts nothing and refuses nothing."""
        runner, _, audit, _ = timeout_runner(tmp_path, seed=False)
        seed_timeout_entries(audit)
        # A long-gone exited position: its NFT no longer resolves on-chain
        # (the fake reads refuse any unscripted status read).
        audit.append(
            AuditEventType.LP_STAKE_PLANNED,
            FakeStakePlanPayload(mode="execute", symbol="TSLAc", token_id=111),
            QUIET_INSTANT,
        )

        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=TIMEOUT_REFERENCES)

        assert report.reconciliation.out_of_band == ""
        decision_recon = report.decision_reconciliation
        assert decision_recon is not None
        assert len(decision_recon.recovered_positions) == 3

    def test_a_stake_plan_left_unstaked_adopts_and_the_live_cycle_restakes(
        self, tmp_path: Path
    ) -> None:
        """The sibling crash-entry gap: mint staked nothing, NFT still in the Safe.

        The NFT sits in the anchor pool's own inventory beside the tracked
        sibling - the exact shape the old reconcile refused out-of-band
        (untracked beside tracked) and the old empty-book-only adoption
        could never reach.
        """
        reads = FakeReads(inventory_with_ids((7_311_805,)))
        reads.set_status(
            TIMEOUT_SIBLING_TOKEN_ID,
            timeout_status("MSTRc", TIMEOUT_SIBLING_TOKEN_ID, value=Decimal("36.2")),
        )
        reads.set_status(
            7_311_805,
            timeout_status("TSLAc", 7_311_805, value=Decimal("12"), owner=SAFE_ADDRESS),
        )
        runner, executor, audit, state_store = timeout_runner(tmp_path, reads=reads, seed=False)
        seed_timeout_entries(audit, entries=(TIMEOUT_ENTRIES[0],))
        assert executor is not None

        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=TIMEOUT_REFERENCES,
        )

        assert report.reconciliation.out_of_band == ""
        assert executor.calls == [("stake", "TSLAc", 7_311_805)]
        book = state_store.load()
        assert {position.token_id for position in book.positions} == {
            TIMEOUT_SIBLING_TOKEN_ID,
            7_311_805,
        }

    def test_a_hard_interruption_mid_act_leaves_the_completed_siblings_saved(
        self, tmp_path: Path
    ) -> None:
        """The timeout itself: three entries planned, killed on the third mint."""
        runner, executor, _, state_store = timeout_runner(
            tmp_path,
            book=CycleStateBook(day=QUIET_INSTANT.date()),
            reads=FakeReads(timeout_empty_inventory()),
            seed=False,
        )
        assert executor is not None
        executor.mint_token_ids = [7_311_805, 7_312_392, 7_312_807]
        executor.interrupt_on_mint = 3

        with pytest.raises(KeyboardInterrupt):
            runner.run(
                CycleMode.LIVE,
                key_bytes=b"\x01" * 32,
                reference_prices_by_symbol=TIMEOUT_REFERENCES,
            )

        # The two completed mint-and-stake pairs already persisted: the
        # aggregate save never ran, but the per-sibling checkpoints did.
        assert {position.token_id for position in state_store.load().positions} == {
            7_311_805,
            7_312_392,
        }

    def test_no_checkpoint_lands_before_the_stake_confirms(self, tmp_path: Path) -> None:
        """A refused stake leaves the store flat: no half-saved position."""
        runner, executor, _, state_store = timeout_runner(
            tmp_path,
            book=CycleStateBook(day=QUIET_INSTANT.date()),
            reads=FakeReads(timeout_empty_inventory()),
            seed=False,
        )
        assert executor is not None
        executor.refuse_next = "stake"

        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=TIMEOUT_REFERENCES,
        )

        assert "refused" in report.halted_reason
        assert state_store.load().positions == ()


class TestSelectorCycles:
    """Cross-board portfolio cycles: tiers, hysteresis, and rebalancing."""

    def test_flat_selector_cycle_uses_pool_authority_without_references(
        self, tmp_path: Path
    ) -> None:
        """External references are not required for Aerodrome pool selection."""
        runner, _, _ = selector_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert report.decision_action == "enter"
        assert report.decision_reason == "entry_threshold_met"
        assert report.symbol == "BBBc"
        assert any("diagnostic-only" in note for note in report.input_notes)
        assert any("10% headroom" in note for note in report.input_notes)
        assert any("board [" in note for note in report.input_notes)
        assert any("funds 2 tranche(s)" in note for note in report.input_notes)

    def test_a_thin_sub_activation_book_deploys_its_available_funds(self, tmp_path: Path) -> None:
        """A ten-USDC trial book deploys below the activation equity.

        The captain's 2026-09-28 sub-1000 correction: no minimum and no
        dry-powder reserve bind until the book reaches the 1000-USDC
        activation equity, so the ten-USDC book funds its top-ranked
        pool at the engine-sized share of the whole budget instead of
        refusing forever behind a floored eighty-USDC target - the exact
        posture the live 105.73-USDC book sat in on 78.03 USDC of free
        cash (the allocator pins carry the verbatim live numbers).
        """
        runner, executor, state_store = selector_runner(
            tmp_path, sources=SelectorCycleSources(), balances=FakeBalances()
        )
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "enter"
        assert executor.calls, "the thin book deploys its available funds"
        assert state_store.load().positions != ()
        assert not any("insufficient_cash" in note for note in report.input_notes)

    def test_selector_counts_tracked_lp_in_daily_loss_equity(self, tmp_path: Path) -> None:
        """Deployed LP capital cannot masquerade as a selector-mode daily loss."""
        policy_day = QUIET_INSTANT.astimezone(
            __import__("zoneinfo").ZoneInfo("America/New_York")
        ).date()
        book = tracked_book(symbol="AAAc").model_copy(
            update={
                "day": policy_day,
                "day_start_equity_usd": Decimal("18"),
                "halted_day": None,
            }
        )
        sources = SelectorCycleSources().with_listings(selector_listings(1))
        runner, _, state_store = selector_runner(
            tmp_path,
            book=book,
            reads=_tracked_reads(),
            sources=sources,
        )

        report = runner.run(CycleMode.DRY_RUN)

        assert "daily_loss_halt_active" not in " ".join(report.input_notes)
        assert any(
            "selector equity includes 8 USDC of tracked LP marked value across 1 position(s)"
            in note
            for note in report.input_notes
        )
        assert state_store.load().halted_day is None

    def test_selector_funds_the_tiered_portfolio_in_rank_order(self, tmp_path: Path) -> None:
        """The live cycle mints and stakes every funded tier, best pool first."""
        runner, executor, state_store = selector_runner(tmp_path)
        assert executor is not None
        executor.mint_token_ids = [TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1]
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "enter"
        assert report.decision_reason == "entry_threshold_met"
        assert report.symbol == "BBBc"
        assert [call[0] for call in executor.calls] == [
            "mint",
            "stake",
            "mint",
            "stake",
        ]
        assert executor.calls[0][1] == "BBBc"
        assert executor.calls[2][1] == "AAAc"
        book = state_store.load()
        assert [position.symbol for position in book.positions] == ["BBBc", "AAAc"]
        # Below the 1000-USDC activation equity the concentration cap does
        # not bind (the captain's 2026-09-28 ruling): each tier takes its
        # full weight share of the 500 USDC book - two-thirds to the top
        # name, one-third to the second, quantized down to USDC's grid.
        assert book.positions[0].committed_usd == Decimal("333.328996")
        assert book.positions[1].committed_usd == Decimal("166.671003")

    def test_selector_holds_a_funded_pool_inside_the_margin_while_cash_deploys(
        self, tmp_path: Path
    ) -> None:
        """No churn inside the margin, but dry powder still funds the tier."""
        sources = SelectorCycleSources(usdc_units=SELECTOR_BOOK_USDC_UNITS).with_listings(
            selector_listings(1)
        )
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=tracked_book(symbol="AAAc", entered_at=QUIET_INSTANT - timedelta(hours=2)),
            reads=_tracked_reads(staked=True),
            sources=sources,
        )
        assert executor is not None
        # BBBc at ten percent over AAAc sits inside the thirty percent
        # margin, so the funded AAAc position stays; the cash still deploys
        # into the qualifying BBBc tier as a second position.
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "enter"
        assert executor.calls[0][0] == "mint"
        assert executor.calls[0][1] == "BBBc"
        assert "unstake" not in [call[0] for call in executor.calls]
        book = state_store.load()
        assert [position.symbol for position in book.positions] == ["AAAc", "BBBc"]

    def test_selector_switches_above_the_margin(self, tmp_path: Path) -> None:
        """A wide-enough margin breach exits the held pool and enters the winner."""
        # BBBc at double AAAc's APR clears the default thirty percent margin.
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=tracked_book(symbol="AAAc", entered_at=QUIET_INSTANT - timedelta(hours=2)),
            reads=_tracked_reads(staked=True),
        )
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "pool_switch"
        assert report.decision_reason == "pool_switch_triggered"
        assert [call[0] for call in executor.calls] == [
            "switch_preflight",
            "unstake",
            "withdraw",
            "exit_swap",
            "mint",
            "stake",
        ]
        assert executor.calls[4][1] == "BBBc"
        book = state_store.load()
        assert book.position is not None
        assert book.position.symbol == "BBBc"
        # Exactly one position is funded after the switch.
        assert book.position.token_id == TRACKED_TOKEN_ID

    def test_a_failed_switch_exit_halts_before_any_entry(self, tmp_path: Path) -> None:
        """A refused exit stops the switch with no second position minted."""
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=tracked_book(symbol="AAAc", entered_at=QUIET_INSTANT - timedelta(hours=2)),
            reads=_tracked_reads(staked=True),
        )
        assert executor is not None

        def refusing_unstake(
            symbol: str,
            token_id: int,
            key_bytes: bytes,
            *,
            confirm_broadcast: bool,
            ephemeral_key: bool = False,
        ) -> LpActionExecutionReport:
            """Refuse the unstake so the switch halts on its first step."""
            executor.calls.append(("unstake", symbol, token_id))
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "scripted unstake refusal",
            )

        executor.execute_unstake = refusing_unstake  # type: ignore[method-assign]
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert [call[0] for call in executor.calls] == ["switch_preflight", "unstake"]
        assert "refused" in report.halted_reason
        book = state_store.load()
        assert book.position is not None
        assert book.position.symbol == "AAAc"

    def test_failed_switch_mint_preserves_target_inventory_for_retry(self, tmp_path: Path) -> None:
        """A partial target mint keeps acquired stock tagged for direct retry."""
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=tracked_book(symbol="AAAc", entered_at=QUIET_INSTANT - timedelta(hours=2)),
            reads=_tracked_reads(staked=True),
        )
        assert executor is not None

        def refusing_mint(
            symbol: str,
            budget_usdc: Decimal,
            width_spacings: int | None,
            key_bytes: bytes,
            *,
            confirm_broadcast: bool,
            ephemeral_key: bool = False,
            portfolio_live_positions: Sequence[tuple[int, Decimal]] | None = None,
        ) -> LpActionExecutionReport:
            executor.calls.append(("mint", symbol, budget_usdc, width_spacings))
            cast(FakeBalances, runner._balances).stock_units = 3_000_000
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING,
                "scripted post-swap mint refusal",
            )

        executor.execute_mint = refusing_mint  # type: ignore[method-assign]
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )

        assert "mint action refused" in report.halted_reason
        book = state_store.load()
        assert book.position is None
        assert book.held_inventory is not None
        assert book.held_inventory.symbol == "BBBc"
        assert book.held_inventory.stock_quantity == Decimal("0.03")
        assert book.held_inventory.origin == "failed_entry"

    def test_switch_preflight_refusal_keeps_the_source_position_untouched(
        self, tmp_path: Path
    ) -> None:
        """A target-plan refusal never unstake/withdraws the earning source LP."""
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=tracked_book(symbol="AAAc", entered_at=QUIET_INSTANT - timedelta(hours=2)),
            reads=_tracked_reads(staked=True),
        )
        assert executor is not None
        executor.refuse_next = "switch_preflight"
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert [call[0] for call in executor.calls] == ["switch_preflight"]
        assert len(report.actions) == 1
        assert report.actions[0].action == "switch_preflight"
        assert report.actions[0].status == "refused"
        assert "switch preflight refused" in report.halted_reason
        book = state_store.load()
        assert book.position is not None
        assert book.position.symbol == "AAAc"
        assert book.position.token_id == TRACKED_TOKEN_ID

    def test_per_pool_cooldown_blocks_only_its_own_pool(self, tmp_path: Path) -> None:
        """A BBBc cooldown skips BBBc and lets the cycle enter AAAc."""
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=CycleStateBook(
                reentry_cooldowns=(
                    ReentryCooldown(
                        symbol="BBBc", blocked_until=QUIET_INSTANT + timedelta(hours=1)
                    ),
                ),
                updated_at=QUIET_INSTANT,
            ),
        )
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "enter"
        assert report.symbol == "AAAc"
        assert executor.calls[0][1] == "AAAc"
        assert state_store.load().position is not None


def _tracked_reads(*, staked: bool = False) -> FakeReads:
    """Serve one live in-range tracked position for the switch fixtures."""
    reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
    reads.set_status(
        TRACKED_TOKEN_ID,
        tracked_status(owner=GAUGE_ADDRESS if staked else SAFE_ADDRESS),
    )
    return reads


class TestCycleConfiguration:
    """The sealed-environment symbol, margin, and reference configuration."""

    def test_symbol_resolves_auto_versus_pinned(self) -> None:
        """Unset, empty, or auto means the selector; anything else pins."""
        assert _symbol_from_arguments_and_environment(None, {}) is None
        assert _symbol_from_arguments_and_environment(None, {CYCLE_SYMBOL_ENV: ""}) is None
        assert _symbol_from_arguments_and_environment(None, {CYCLE_SYMBOL_ENV: "auto"}) is None
        assert _symbol_from_arguments_and_environment(None, {CYCLE_SYMBOL_ENV: "AUTO"}) is None
        assert (
            _symbol_from_arguments_and_environment(None, {CYCLE_SYMBOL_ENV: " AAPLc "}) == "AAPLc"
        )
        assert _symbol_from_arguments_and_environment("auto", {CYCLE_SYMBOL_ENV: "AAPLc"}) is None
        assert (
            _symbol_from_arguments_and_environment("AAPLc", {CYCLE_SYMBOL_ENV: "FIXc"}) == "AAPLc"
        )

    def test_selector_ignores_a_legacy_single_reference_instead_of_refusing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An AAPL-only sealed quote cannot block Aerodrome-authoritative auto mode."""
        from aero_bot import cycle as cycle_module

        captured: dict[str, object] = {}

        class FakeRunner:
            def run(self, mode: CycleMode, **kwargs: object) -> object:
                captured.update(kwargs)
                raise RuntimeError("selector reached runner")

        monkeypatch.setenv(CYCLE_REFERENCE_PRICE_ENV, "317.10")
        monkeypatch.setattr(
            cycle_module, "build_cycle_runner", lambda *args, **kwargs: FakeRunner()
        )
        exit_code = cycle_module.main(["--symbol", "auto", "--dry-run", "--json"])
        assert exit_code == 1
        assert captured.get("reference_price_usdc") is None
        assert captured.get("reference_prices_by_symbol") is None

    def test_the_allocator_flags_parse_and_reach_the_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The portfolio flags override the sealed defaults end to end."""
        from aero_bot import cycle as cycle_module

        built: dict[str, object] = {}

        class FakeRunner:
            def run(self, mode: CycleMode, **kwargs: object) -> object:
                raise RuntimeError("selector reached runner")

        def fake_build(
            settings: object,
            symbol: object,
            safe_address: object,
            relayer: object,
            switch_margin: object,
            parameters: object,
            aero_min: object,
            portfolio: PortfolioParameters | None = None,
            income_history_cycles: object = None,
        ) -> object:
            built["portfolio"] = portfolio
            built["income_history_cycles"] = income_history_cycles
            return FakeRunner()

        monkeypatch.setattr(cycle_module, "build_cycle_runner", fake_build)
        monkeypatch.setenv(CYCLE_TIER_BAND_ENV, "0.6")
        exit_code = cycle_module.main(
            [
                "--symbol",
                "auto",
                "--dry-run",
                "--tier-band",
                "0.7",
                "--max-positions",
                "6",
                "--min-position-usdc",
                "95",
                "--min-position-floor-usdc",
                "25",
                "--concentration-cap",
                "0.3",
            ]
        )
        assert exit_code == 1
        portfolio = cast(PortfolioParameters, built["portfolio"])
        assert portfolio.tier_band_fraction == Decimal("0.7")  # the flag wins
        assert portfolio.max_concurrent_positions == 6
        assert portfolio.min_position_usdc == Decimal("95")
        assert portfolio.min_position_floor_usdc == Decimal("25")
        assert portfolio.concentration_cap_fraction == Decimal("0.3")

    def test_the_floor_flag_never_exceeds_the_minimum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A floor above the minimum refuses at the CLI boundary."""
        from aero_bot import cycle as cycle_module

        monkeypatch.setattr(
            cycle_module,
            "build_cycle_runner",
            lambda *args, **kwargs: pytest.fail("the runner must not build"),
        )
        for argv in (
            ["--dry-run", "--min-position-floor-usdc", "95"],  # above the default min
            [
                "--dry-run",
                "--min-position-usdc",
                "60",
                "--min-position-floor-usdc",
                "70",
            ],  # the pair checked against each other
            ["--dry-run", "--min-position-floor-usdc", "-5"],
        ):
            with pytest.raises(SystemExit) as raised:
                cycle_module.main(argv)
            assert raised.value.code == 2

        # A floor at the minimum stays coherent (the rule degenerates to
        # the old bound) and parses cleanly past the boundary check.
        class StubRunner:
            def run(self, mode: CycleMode, **kwargs: object) -> object:
                raise RuntimeError("selector reached runner")

        monkeypatch.setattr(
            cycle_module,
            "build_cycle_runner",
            lambda *args, **kwargs: StubRunner(),
        )
        assert cycle_module.main(["--dry-run", "--min-position-floor-usdc", "80"]) == 1

    def test_the_floor_env_pair_refuses_incoherent_values(self) -> None:
        """The env floor above the env minimum refuses at construction."""
        with pytest.raises(ValueError, match="never raises the minimum"):
            _portfolio_parameters_from_environment(
                {
                    CYCLE_MIN_POSITION_FLOOR_USDC_ENV: "95",
                    CYCLE_MIN_POSITION_USDC_ENV: "90",
                },
                Decimal("0.3"),
            )

    def test_the_allocator_flags_refuse_nonpositive_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each bound rejects zero, negatives, and sub-one counts."""
        from aero_bot import cycle as cycle_module

        monkeypatch.setattr(
            cycle_module,
            "build_cycle_runner",
            lambda *args, **kwargs: pytest.fail("the runner must not build"),
        )
        for argv in (
            ["--dry-run", "--tier-band", "0"],
            ["--dry-run", "--min-position-usdc", "-5"],
            ["--dry-run", "--concentration-cap", "0"],
            ["--dry-run", "--max-positions", "0"],
        ):
            with pytest.raises(SystemExit) as raised:
                cycle_module.main(argv)
            assert raised.value.code == 2

    def test_switch_margin_defaults_and_overrides(self) -> None:
        """The margin defaults to the ruling's thirty percent."""
        assert _switch_margin_from_environment({}) == Decimal("0.30")
        assert _switch_margin_from_environment({CYCLE_SWITCH_MARGIN_ENV: "0.5"}) == Decimal("0.5")
        with pytest.raises(ValueError, match="non-negative"):
            _switch_margin_from_environment({CYCLE_SWITCH_MARGIN_ENV: "-0.1"})

    def test_reference_environment_serves_single_and_map_forms(self) -> None:
        """A bare number quotes the pinned symbol; pairs map per symbol."""
        assert _reference_price_from_environment({}) is None
        assert _reference_price_from_environment({CYCLE_REFERENCE_PRICE_ENV: "318.5"}) == Decimal(
            "318.5"
        )
        quoted = _reference_price_from_environment(
            {CYCLE_REFERENCE_PRICE_ENV: "AAPLc=318.5, FIXc=100"}
        )
        assert quoted == {"AAPLc": Decimal("318.5"), "FIXc": Decimal("100")}
        with pytest.raises(ValueError, match="positive"):
            _reference_price_from_environment({CYCLE_REFERENCE_PRICE_ENV: "-1"})
        with pytest.raises(ValueError, match="SYMBOL=PRICE"):
            _reference_price_from_environment({CYCLE_REFERENCE_PRICE_ENV: "AAPLc="})
        with pytest.raises(ValueError, match="more than once"):
            _reference_price_from_environment({CYCLE_REFERENCE_PRICE_ENV: "A=1,A=2"})

    def test_out_of_range_grace_defaults_and_overrides(self) -> None:
        """The grace window defaults to ten minutes and refuses bad values."""
        assert _out_of_range_grace_from_environment({}) == timedelta(minutes=10)
        assert _out_of_range_grace_from_environment(
            {CYCLE_OUT_OF_RANGE_GRACE_ENV: "30"}
        ) == timedelta(minutes=30)
        with pytest.raises(ValueError, match="positive"):
            _out_of_range_grace_from_environment({CYCLE_OUT_OF_RANGE_GRACE_ENV: "0"})
        with pytest.raises(ValueError, match="positive"):
            _out_of_range_grace_from_environment({CYCLE_OUT_OF_RANGE_GRACE_ENV: "-5"})

    def test_portfolio_bounds_default_and_override(self) -> None:
        """The allocator bounds default locked and refuse loose overrides."""
        defaults = _portfolio_parameters_from_environment({}, Decimal("0.30"))
        assert defaults.tier_band_fraction == Decimal("0.50")
        assert defaults.max_concurrent_positions == 10
        assert defaults.min_position_usdc == Decimal("80")
        assert defaults.min_position_floor_usdc == Decimal("30")
        assert defaults.concentration_cap_fraction == Decimal("0.35")
        assert defaults.concentration_cap_activation_equity_usdc == Decimal("1000")
        assert defaults.switch_margin_fraction == Decimal("0.30")
        # Below the activation equity neither the cap nor ANY minimum binds
        # (the captain's 2026-09-28 sub-1000 correction): the unfunded book
        # deploys its available funds.
        assert defaults.effective_minimum_position_usdc(Decimal("105")) == Decimal("0")
        assert defaults.concentration_bound_usdc(Decimal("105")) is None
        assert defaults.concentration_bound_usdc(Decimal("1000")) == Decimal("350.00")
        tuned = _portfolio_parameters_from_environment(
            {
                CYCLE_TIER_BAND_ENV: "0.6",
                CYCLE_MAX_POSITIONS_ENV: "5",
                CYCLE_MIN_POSITION_USDC_ENV: "90",
                CYCLE_CONCENTRATION_CAP_ENV: "0.25",
                CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC_ENV: "500",
            },
            Decimal("0.5"),
        )
        assert tuned.tier_band_fraction == Decimal("0.6")
        assert tuned.max_concurrent_positions == 5
        assert tuned.min_position_usdc == Decimal("90")
        assert tuned.concentration_cap_fraction == Decimal("0.25")
        assert tuned.concentration_cap_activation_equity_usdc == Decimal("500")
        assert tuned.switch_margin_fraction == Decimal("0.5")
        floored = _portfolio_parameters_from_environment(
            {
                CYCLE_MIN_POSITION_FLOOR_USDC_ENV: "10",
                CYCLE_MIN_POSITION_USDC_ENV: "90",
            },
            Decimal("0.3"),
        )
        assert floored.min_position_floor_usdc == Decimal("10")
        # The floor is sealed-env configurable under the hard rule: a
        # floor above the configured minimum raises rather than bounds.
        with pytest.raises(ValueError, match="never raises the minimum"):
            _portfolio_parameters_from_environment(
                {
                    CYCLE_MIN_POSITION_FLOOR_USDC_ENV: "95",
                    CYCLE_MIN_POSITION_USDC_ENV: "90",
                },
                Decimal("0.3"),
            )
        with pytest.raises(ValueError, match="positive"):
            _portfolio_parameters_from_environment({CYCLE_TIER_BAND_ENV: "0"}, Decimal("0.3"))
        with pytest.raises(ValueError, match="at least one"):
            _portfolio_parameters_from_environment({CYCLE_MAX_POSITIONS_ENV: "0"}, Decimal("0.3"))
        with pytest.raises(ValueError, match="hard ceiling"):
            _portfolio_parameters_from_environment({CYCLE_MAX_POSITIONS_ENV: "11"}, Decimal("0.3"))

    def test_aero_conversion_min_defaults_and_overrides(self) -> None:
        """The conversion threshold defaults to five USDC and refuses negatives."""
        assert _aero_conversion_min_from_environment({}) == Decimal("5")
        assert _aero_conversion_min_from_environment(
            {CYCLE_AERO_CONVERSION_MIN_ENV: "12.5"}
        ) == Decimal("12.5")
        with pytest.raises(ValueError, match="non-negative"):
            _aero_conversion_min_from_environment({CYCLE_AERO_CONVERSION_MIN_ENV: "-1"})


class TestRewardConversion:
    """The capped AERO-to-USDC conversion inside the act step."""

    def test_live_cycle_converts_when_unclaimed_aero_exceeds_the_threshold(
        self, tmp_path: Path
    ) -> None:
        """The cadence fires: claim then swap, counted in the book's reconcile."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        staked = tracked_status(owner=GAUGE_ADDRESS, accrued_aero_units=10 * 10**18)
        reads.set_status(TRACKED_TOKEN_ID, staked)
        runner, executor, audit, state_store = make_runner(
            tmp_path, book=tracked_book(), reads=reads
        )
        assert executor is not None
        executor.collect_aero_units = 10 * 10**18

        report = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_price_usdc=FIXTURE_AMM_PRICE
        )

        assert [action.action for action in report.actions] == ["collect_rewards", "aero_swap"]
        assert all(action.status == "completed" for action in report.actions)
        # Ten AERO at the observed 0.6 price converts to six USDC.
        assert report.unclaimed_aero_units == 10 * 10**18
        assert report.unclaimed_aero_value_usdc == Decimal("6")
        assert report.reconciliation.safe_aero_units == 0
        assert ("aero_swap",) in executor.calls
        saved = state_store.load()
        assert saved.day_baseline is not None
        assert saved.day_baseline.aero_converted_units == 10 * 10**18

    def test_live_cycle_holds_the_rewards_below_the_threshold(self, tmp_path: Path) -> None:
        """Unclaimed AERO under the threshold never touches the executor."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS, accrued_aero_units=3 * 10**18)
        )
        runner, executor, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_price_usdc=FIXTURE_AMM_PRICE
        )

        assert report.actions == ()
        assert executor is not None
        assert not any(call[0] == "aero_swap" for call in executor.calls)
        # The measurement still reports the unclaimed pile honestly.
        assert report.unclaimed_aero_value_usdc == Decimal("1.8")

    def test_dry_run_never_converts(self, tmp_path: Path) -> None:
        """A dry cycle measures the pile but never claims or swaps."""
        balances = FakeBalances(aero_units=20 * 10**18)
        runner, executor, _, _ = make_runner(tmp_path, balances=balances)

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert executor is None or not any(
            call[0] in ("aero_swap", "collect_rewards")
            for call in (executor.calls if executor else [])
        )
        assert report.actions == ()
        assert report.unclaimed_aero_value_usdc == Decimal("12")

    def test_penalty_window_defers_the_claim(self, tmp_path: Path) -> None:
        """An open penalty window never claims; only claimed balances convert."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        inside_penalty = tracked_status(
            owner=GAUGE_ADDRESS, accrued_aero_units=10 * 10**18
        ).model_copy(
            update={"penalty": SimpleNamespace(remaining_seconds=120, penalty_rate_bps=10_000)}
        )
        reads.set_status(TRACKED_TOKEN_ID, inside_penalty)
        runner, executor, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)

        report = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_price_usdc=FIXTURE_AMM_PRICE
        )

        assert executor is not None
        assert not any(call[0] == "collect_rewards" for call in executor.calls)
        assert not any(call[0] == "aero_swap" for call in executor.calls)
        # The unearned pile is still measured honestly.
        assert report.unclaimed_aero_value_usdc == Decimal("6")

    def test_a_refused_swap_halts_the_cycle(self, tmp_path: Path) -> None:
        """A conversion refusal records its code and halts the cycle."""
        balances = FakeBalances(aero_units=20 * 10**18)
        runner, executor, _, _ = make_runner(tmp_path, balances=balances)
        assert executor is not None
        executor.refuse_next = "aero_swap"

        report = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_price_usdc=FIXTURE_AMM_PRICE
        )

        assert report.halted_reason.startswith("the aero_swap action refused")
        assert report.actions[-1].status == "refused"
        assert report.actions[-1].refusal_code == "broadcast_confirmation_missing"

    def test_idle_aero_counts_in_the_decision_equity(self, tmp_path: Path) -> None:
        """The equity the halt measures prices the idle AERO pile."""
        balances = FakeBalances(aero_units=10 * 10**18)
        runner, _, _, _ = make_runner(tmp_path, balances=balances)

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        # Ten USDC of Safe USDC plus six USDC of AERO at the observed price.
        assert report.equity_usd == Decimal("16")
        assert any("unclaimed AERO" in note for note in report.input_notes)


class TestPortfolioCycles:
    """The tiered book: per-position attribution, payloads, and safety."""

    def test_the_human_print_names_the_portfolio(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The human summary lists every position and its attribution slice."""
        from aero_bot.cycle import _print_report

        runner, _, _ = selector_runner(
            tmp_path,
            book=portfolio_book(),
            reads=portfolio_reads(fee_growth=2 << 128, earned_aero=2 * 10**18),
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=SELECTOR_REFERENCES)
        _print_report(report)
        printed = capsys.readouterr().out
        assert "portfolio: 2 tracked position(s)" in printed
        assert "BBBc #" in printed and "AAAc #" in printed
        assert "attribution: aero" in printed
        assert "yield attribution (day pnl decomposition):" in printed

    def test_per_position_attribution_rolls_up_across_positions(self, tmp_path: Path) -> None:
        """Each position's AERO, fees, and MTM measure at its own price."""
        runner, _, state_store = selector_runner(
            tmp_path,
            book=portfolio_book(),
            reads=portfolio_reads(fee_growth=2 << 128, earned_aero=2 * 10**18),
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        first = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=SELECTOR_REFERENCES)
        assert first.yield_attribution is not None
        assert first.yield_attribution.aero_rewards_usdc == Decimal("0")

        # Both positions advance their fee words and AERO, and the shared
        # fixture price moves one percent.
        reads = portfolio_reads(fee_growth=3 << 128, earned_aero=7 * 10**18)
        runner, _, _ = selector_runner(
            tmp_path,
            book=state_store.load(),
            reads=reads,
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        later = runner.run(
            CycleMode.DRY_RUN,
            reference_prices_by_symbol={
                symbol: FIXTURE_AMM_PRICE * Decimal("1.01") for symbol in ("AAAc", "BBBc")
            },
        )
        attribution = later.yield_attribution
        assert attribution is not None
        rows = {row.symbol: row for row in attribution.positions}
        assert set(rows) == {"AAAc", "BBBc"}
        for row in rows.values():
            assert row.fees_earned_usdc is not None and row.fees_earned_usdc > 0
            # Five more AERO at the observed 0.6 price per position.
            assert row.aero_rewards_usdc == Decimal("3")
            assert row.stock_mark_to_market_usdc is not None
        # The rollup sums the rows: ten AERO total, both fee streams, both
        # mark-to-markets.
        assert attribution.aero_rewards_usdc == Decimal("6")
        assert attribution.fees_earned_usdc == sum(
            row.fees_earned_usdc or Decimal("0") for row in rows.values()
        )
        assert attribution.stock_mark_to_market_usdc == sum(
            row.stock_mark_to_market_usdc or Decimal("0") for row in rows.values()
        )
        assert attribution.unattributed_usdc is not None
        assert attribution.day_pnl_usdc == (
            attribution.aero_rewards_usdc
            + attribution.fees_earned_usdc
            + attribution.stock_mark_to_market_usdc
            + attribution.unattributed_usdc
        )

    def test_attribution_reports_unmeasured_fees_when_liquidity_changed(
        self, tmp_path: Path
    ) -> None:
        """A mid-day liquidity change leaves the day's fee growth unmeasured.

        The day-baseline liquidity multiplied by the day's fee-growth delta
        would attribute growth to liquidity that no longer backs it - the
        day's fee component must read unmeasured instead, naming the change.
        """
        runner, _, state_store = selector_runner(
            tmp_path,
            book=portfolio_book(),
            reads=portfolio_reads(fee_growth=2 << 128, earned_aero=2 * 10**18),
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        first = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=SELECTOR_REFERENCES)
        assert first.yield_attribution is not None

        # The next cycle's positions carry less liquidity (a partial burn)
        # while the fee words still advance.
        reads = portfolio_reads(
            fee_growth=3 << 128,
            earned_aero=7 * 10**18,
            liquidity=9_000,
        )
        runner, _, _ = selector_runner(
            tmp_path,
            book=state_store.load(),
            reads=reads,
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        later = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=SELECTOR_REFERENCES)
        attribution = later.yield_attribution
        assert attribution is not None
        rows = {row.symbol: row for row in attribution.positions}
        for row in rows.values():
            assert row.fees_earned_usdc is None
            assert "liquidity changed" in row.diagnostic
            assert "12345" in row.diagnostic
            assert "9000" in row.diagnostic
        assert attribution.fees_earned_usdc is None
        assert "fees earned unmeasured" in attribution.diagnostic

    def test_report_and_payload_carry_the_portfolio_fields(self, tmp_path: Path) -> None:
        """Positions ride the report and the audited cycle summary."""
        runner, _, _ = selector_runner(
            tmp_path,
            book=portfolio_book(),
            reads=portfolio_reads(),
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        report = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=SELECTOR_REFERENCES)
        assert [row.symbol for row in report.positions] == ["BBBc", "AAAc"]
        assert all(row.staked for row in report.positions)
        assert [row.committed_usd for row in report.positions] == [
            Decimal("175"),
            Decimal("120"),
        ]
        audit = AuditStore(tmp_path / "audit.sqlite3")
        latest = audit.read_records(1)[-1]
        assert latest.event_type is AuditEventType.CYCLE_REPORTED
        payload = json.loads(latest.payload_json)
        assert payload["position_count"] == 2
        assert payload["total_committed_usdc"] == "295"
        assert payload["largest_position_share"] is not None

    def test_a_safety_exit_spares_the_other_position(self, tmp_path: Path) -> None:
        """A stop-out on one name never touches its sibling."""
        stopped = tracked_status(owner=GAUGE_ADDRESS).model_copy(
            update={
                "position": SimpleNamespace(
                    tick_lower=-90,
                    tick_upper=-75,
                    token0_address=BASE_USDC_ADDRESS,
                    token1_address=STOCK_TOKEN_ADDRESS,
                    liquidity=12_345,
                )
            }
        )
        reads = FakeReads(inventory_with_ids((TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1)))
        reads.set_status(TRACKED_TOKEN_ID, stopped)
        reads.set_status(TRACKED_TOKEN_ID + 1, tracked_status(owner=GAUGE_ADDRESS))
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=portfolio_book(),
            reads=reads,
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "stop_out"
        # Only the stopped position unwinds: three calls, one symbol.
        assert [call[0] for call in executor.calls] == [
            "unstake",
            "withdraw",
            "exit_swap",
        ]
        assert all(call[1] == "BBBc" for call in executor.calls)
        book = state_store.load()
        assert [position.symbol for position in book.positions] == ["AAAc"]

    def test_a_recenter_step_replaces_only_its_position(self, tmp_path: Path) -> None:
        """A per-position recenter prefights, burns, and re-mints in place."""
        above_range = tracked_status(owner=GAUGE_ADDRESS).model_copy(
            update={
                "position": SimpleNamespace(
                    tick_lower=-15,
                    tick_upper=-5,
                    token0_address=BASE_USDC_ADDRESS,
                    token1_address=STOCK_TOKEN_ADDRESS,
                    liquidity=12_345,
                )
            }
        )
        reads = FakeReads(inventory_with_ids((TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1)))
        reads.set_status(TRACKED_TOKEN_ID, above_range)
        reads.set_status(TRACKED_TOKEN_ID + 1, tracked_status(owner=GAUGE_ADDRESS))
        book = portfolio_book().model_copy(
            update={
                "positions": (
                    portfolio_book()
                    .positions[0]
                    .model_copy(
                        update={
                            "out_of_range_since": QUIET_INSTANT - timedelta(minutes=30),
                            "out_of_range_side": "above",
                        }
                    ),
                    portfolio_book().positions[1],
                )
            }
        )
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=book,
            reads=reads,
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "recenter"
        assert [call[0] for call in executor.calls] == [
            "recenter_preflight",
            "unstake",
            "withdraw",
            "mint",
            "stake",
        ]
        assert all(call[1] == "BBBc" for call in executor.calls if len(call) > 1)
        # The recentered slot replaces in place; the sibling never moves.
        book_after = state_store.load()
        assert sorted(position.symbol for position in book_after.positions) == ["AAAc", "BBBc"]

    def test_held_inventory_resolves_through_its_own_step(self, tmp_path: Path) -> None:
        """The convergence timeout sells the held stock without touching LPs."""
        reads = FakeReads(inventory_with_ids((TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1)))
        for token_id in (TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1):
            reads.set_status(token_id, tracked_status(owner=GAUGE_ADDRESS))
        book = portfolio_book().model_copy(
            update={
                "held_inventory": HeldInventoryRecord(
                    symbol="AAAc",
                    token_address=B20_ADDRESS,
                    stock_quantity=Decimal("0.5"),
                    held_since=QUIET_INSTANT - timedelta(minutes=10),
                    origin="stale_low_exit",
                )
            }
        )
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=book,
            reads=reads,
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "sell_inventory"
        assert [call[0] for call in executor.calls] == ["exit_swap"]
        assert executor.calls[0][1] == "AAAc"
        saved = state_store.load()
        assert saved.held_inventory is None
        assert len(saved.positions) == 2

    def test_a_grace_exit_routes_the_withdrawn_stock_to_convergence(self, tmp_path: Path) -> None:
        """A below-range grace exit holds the stock and arms the cooldown."""
        below_edge = tracked_status(owner=GAUGE_ADDRESS).model_copy(
            update={
                "position": SimpleNamespace(
                    tick_lower=-60,
                    tick_upper=-50,
                    token0_address=BASE_USDC_ADDRESS,
                    token1_address=STOCK_TOKEN_ADDRESS,
                    liquidity=12_345,
                )
            }
        )
        reads = FakeReads(inventory_with_ids((TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1)))
        reads.set_status(TRACKED_TOKEN_ID, below_edge)
        reads.set_status(TRACKED_TOKEN_ID + 1, tracked_status(owner=GAUGE_ADDRESS))
        book = portfolio_book().model_copy(
            update={
                "positions": (
                    portfolio_book()
                    .positions[0]
                    .model_copy(
                        update={
                            "out_of_range_since": QUIET_INSTANT - timedelta(minutes=30),
                            "out_of_range_side": "below",
                        }
                    ),
                    portfolio_book().positions[1],
                )
            }
        )
        parameters = LOCKED_POLICY_PARAMETERS.model_copy(
            update={"downside_recenter_max_payback_days": Decimal("0.000001")}
        )
        balances = FakeBalances(stock_units=5 * 10**8)
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=book,
            reads=reads,
            sources=SelectorCycleSources(),
            balances=balances,
        )
        # The runner needs the tightened parameters for the failing payback.
        runner._parameters = parameters
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "range_grace_exit"
        assert [call[0] for call in executor.calls] == ["unstake", "withdraw"]
        saved = state_store.load()
        assert [position.symbol for position in saved.positions] == ["AAAc"]
        assert saved.held_inventory is not None
        assert saved.held_inventory.symbol == "BBBc"
        assert saved.held_inventory.origin == "out_of_range_exit"
        assert any(cooldown.symbol.lower() == "bbbc" for cooldown in saved.reentry_cooldowns)

    def test_a_refused_recenter_preflight_halts_the_portfolio(self, tmp_path: Path) -> None:
        """A recenter preflight refusal leaves the position untouched."""
        above_range = tracked_status(owner=GAUGE_ADDRESS).model_copy(
            update={
                "position": SimpleNamespace(
                    tick_lower=-15,
                    tick_upper=-5,
                    token0_address=BASE_USDC_ADDRESS,
                    token1_address=STOCK_TOKEN_ADDRESS,
                    liquidity=12_345,
                )
            }
        )
        reads = FakeReads(inventory_with_ids((TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1)))
        reads.set_status(TRACKED_TOKEN_ID, above_range)
        reads.set_status(TRACKED_TOKEN_ID + 1, tracked_status(owner=GAUGE_ADDRESS))
        book = portfolio_book().model_copy(
            update={
                "positions": (
                    portfolio_book()
                    .positions[0]
                    .model_copy(
                        update={
                            "out_of_range_since": QUIET_INSTANT - timedelta(minutes=30),
                            "out_of_range_side": "above",
                        }
                    ),
                    portfolio_book().positions[1],
                )
            }
        )
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=book,
            reads=reads,
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        assert executor is not None
        executor.refuse_next = "recenter_preflight"
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert [call[0] for call in executor.calls] == ["recenter_preflight"]
        assert "recenter preflight refused" in report.halted_reason
        assert len(state_store.load().positions) == 2

    def test_an_undecodable_portfolio_mint_halts_the_cycle(self, tmp_path: Path) -> None:
        """A mint whose receipt carries no position id halts honestly."""
        runner, executor, state_store = selector_runner(
            tmp_path,
            sources=SelectorCycleSources(usdc_units=SELECTOR_BOOK_USDC_UNITS),
            balances=FakeBalances(usdc_units=SELECTOR_BOOK_USDC_UNITS),
        )
        assert executor is not None
        executor.mint_receipt_token_id = None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert "could not be decoded" in report.halted_reason
        assert state_store.load().positions == ()

    def test_the_conversion_collects_from_every_staked_position(self, tmp_path: Path) -> None:
        """Each position's earned counts and each penalty window binds its own."""
        reads = FakeReads(inventory_with_ids((TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1)))
        for token_id in (TRACKED_TOKEN_ID, TRACKED_TOKEN_ID + 1):
            reads.set_status(
                token_id,
                tracked_status(owner=GAUGE_ADDRESS, accrued_aero_units=5 * 10**18),
            )
        runner, executor, _ = selector_runner(
            tmp_path,
            book=portfolio_book(),
            reads=reads,
            sources=SelectorCycleSources(),
            balances=FakeBalances(),
        )
        assert executor is not None
        executor.collect_aero_units = 5 * 10**18
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        actions = [action.action for action in report.actions]
        assert actions == ["collect_rewards", "collect_rewards", "aero_swap"]
        assert report.unclaimed_aero_units == 10 * 10**18
        assert report.unclaimed_aero_value_usdc == Decimal("6")


class TestYieldAttribution:
    """The day P&L decomposition into the yield-duration components."""

    def test_two_cycles_decompose_the_day_pnl(self, tmp_path: Path) -> None:
        """AERO accrual, computed fees, and stock MTM each measure their stream."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        baseline_status = tracked_status(
            owner=GAUGE_ADDRESS,
            accrued_aero_units=2 * 10**18,
            fee_growth_inside0_x128=2 << 128,
            fee_growth_inside1_x128=2 << 128,
        )
        reads.set_status(TRACKED_TOKEN_ID, baseline_status)
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)

        first = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert first.yield_attribution is not None
        assert first.yield_attribution.aero_rewards_usdc == Decimal("0")

        # One more whole growth unit accrued on each side, five more AERO
        # earned, and a one-percent stock price move.
        later_status = tracked_status(
            owner=GAUGE_ADDRESS,
            accrued_aero_units=7 * 10**18,
            fee_growth_inside0_x128=3 << 128,
            fee_growth_inside1_x128=3 << 128,
        )
        reads.set_status(TRACKED_TOKEN_ID, later_status)
        later = runner.run(
            CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE * Decimal("1.01")
        )

        attribution = later.yield_attribution
        assert attribution is not None
        # Five more AERO at the observed 0.6 price.
        assert attribution.aero_rewards_usdc == Decimal("3")
        # 12345 raw USDC-side units plus the stock side at the new price.
        assert attribution.fees_earned_usdc is not None
        assert attribution.fees_earned_usdc > 0
        assert "feeGrowthInside" in " ".join(attribution.method)
        # Day P&L and the residual reconcile with the components.
        assert attribution.unattributed_usdc is not None
        aero_rewards = attribution.aero_rewards_usdc
        fees_earned = attribution.fees_earned_usdc
        stock_mtm = attribution.stock_mark_to_market_usdc
        assert aero_rewards is not None
        assert fees_earned is not None
        assert stock_mtm is not None
        assert attribution.day_pnl_usdc == (
            aero_rewards + fees_earned + stock_mtm + attribution.unattributed_usdc
        )
        saved = state_store.load()
        assert saved.day_baseline is not None
        assert saved.day_baseline.aero_units == 2 * 10**18

    def test_position_change_rebaselines_the_fee_words_only(self, tmp_path: Path) -> None:
        """A recentered token re-snapshots the fee baseline, not the day's AERO."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(
                owner=GAUGE_ADDRESS,
                accrued_aero_units=2 * 10**18,
                fee_growth_inside0_x128=2 << 128,
                fee_growth_inside1_x128=2 << 128,
            ),
        )
        runner, _, _, state_store = make_runner(tmp_path, book=tracked_book(), reads=reads)
        runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        # The next cycle tracks a fresh token whose words differ; the day's
        # AERO baseline must survive the position-scoped reset.
        fresh_id = TRACKED_TOKEN_ID + 1
        book = state_store.load()
        assert book.position is not None
        state_store.save(
            book.model_copy(
                update={"positions": (book.positions[0].model_copy(update={"token_id": fresh_id}),)}
            )
        )
        reads.set_inventory(inventory_with(fresh_id))
        reads.set_status(
            fresh_id,
            tracked_status(
                owner=GAUGE_ADDRESS,
                accrued_aero_units=4 * 10**18,
                fee_growth_inside0_x128=5 << 128,
                fee_growth_inside1_x128=5 << 128,
            ).model_copy(update={"token_id": fresh_id}),
        )
        runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        saved = state_store.load()
        assert saved.day_baseline is not None
        assert saved.day_baseline.token_id == fresh_id
        assert saved.day_baseline.fee_growth_inside0_x128 == 5 << 128
        assert saved.day_baseline.aero_units == 2 * 10**18

    def test_flat_first_cycle_carries_a_baseline_without_components(self, tmp_path: Path) -> None:
        """A flat cycle still opens the day's baseline honestly."""
        runner, _, _, state_store = make_runner(tmp_path)

        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        assert report.yield_attribution is not None
        assert report.yield_attribution.aero_rewards_usdc == Decimal("0")
        assert report.yield_attribution.fees_earned_usdc is None
        saved = state_store.load()
        assert saved.day_baseline is not None
        assert saved.day_baseline.token_id is None

    def test_audit_record_carries_the_attribution(self, tmp_path: Path) -> None:
        """The cycle_reported payload persists the decomposition fields."""
        runner, _, audit, _ = make_runner(tmp_path)

        runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)

        records = audit.read_records(10)
        payload = json.loads(records[-1].payload_json)
        assert payload["yield_aero_rewards_usdc"] is not None
        assert payload["peak_equity_usdc"] is not None


class TestGraceExitMapping:
    """The range-grace-exit action mapping and the grace configuration."""

    def test_grace_exit_routes_stock_through_inventory_convergence(self, tmp_path: Path) -> None:
        """A below-range grace exit burns without swapping and holds the stock."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        # Ticks -60..-50 price their lower edge about 0.4 percent above the
        # fixture price - below the range edge, above the stop, past the
        # minimum recenter distance.
        below_edge = tracked_status(owner=GAUGE_ADDRESS, accrued_aero_units=0).model_copy(
            update={
                "position": SimpleNamespace(
                    tick_lower=-60,
                    tick_upper=-50,
                    token0_address=BASE_USDC_ADDRESS,
                    token1_address=STOCK_TOKEN_ADDRESS,
                    liquidity=12_345,
                )
            }
        )
        reads.set_status(TRACKED_TOKEN_ID, below_edge)
        balances = FakeBalances(stock_units=5 * 10**8)
        expired_book = tracked_book().model_copy(
            update={
                "positions": (
                    TrackedPosition(
                        symbol="FIXc",
                        token_id=TRACKED_TOKEN_ID,
                        pool_address=POOL_ADDRESS,
                        committed_usd=Decimal("7"),
                        entered_at=QUIET_INSTANT - timedelta(minutes=30),
                        out_of_range_since=QUIET_INSTANT - timedelta(minutes=15),
                        out_of_range_side="below",
                    ),
                )
            }
        )
        # An unreachable payback bound forces the recenter economics to fail
        # at the elapsed grace, so income protection exits instead.
        parameters = LOCKED_POLICY_PARAMETERS.model_copy(
            update={"downside_recenter_max_payback_days": Decimal("0.000001")}
        )
        runner, executor, _, state_store = make_runner(
            tmp_path,
            book=expired_book,
            reads=reads,
            balances=balances,
            parameters=parameters,
        )
        assert executor is not None

        report = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_price_usdc=FIXTURE_AMM_PRICE
        )

        # The policy authorized the grace exit; the cycle unstaked, withdrew,
        # and held the stock as convergence inventory - never an exit swap.
        assert report.decision_action == "range_grace_exit"
        assert [action.action for action in report.actions] == ["unstake", "withdraw"]
        assert not any(call[0] == "exit_swap" for call in executor.calls)
        saved = state_store.load()
        assert saved.position is None
        assert saved.held_inventory is not None
        assert saved.held_inventory.origin == "out_of_range_exit"

    def test_configured_grace_window_threads_into_decisions(self, tmp_path: Path) -> None:
        """The runner's parameter override reaches the engine's decisions."""
        parameters = LOCKED_POLICY_PARAMETERS.model_copy(
            update={"out_of_range_grace": timedelta(minutes=45)}
        )
        runner, _, _, _ = make_runner(tmp_path, parameters=parameters)

        assert runner._parameters.out_of_range_grace == timedelta(minutes=45)


class TestContinuousLatchBook:
    """The running-peak latch persisted across cycles in the book."""

    def test_portfolio_loss_latch_survives_exit_and_blocks_reentry(self, tmp_path: Path) -> None:
        """A 110-to-104 book still protects entries after its held position exits.

        The live 2026-09-28 book kept day=None and a stale peak despite
        funded cycles. Replay the same shape through three persisted selector
        cycles: a 110 high-water hold, a 104 downside stop that must execute,
        and a flat 104 posture after the stop's cooldown has expired.
        """
        now = QUIET_INSTANT
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS, value=Decimal("80")))
        sources = single_name_sources(usdc_units=30_000_000)
        balances = FakeBalances(usdc_units=30_000_000)
        book = tracked_book(
            symbol="BBBc", committed=Decimal("80"), entered_at=now - timedelta(hours=2)
        )
        runner, executor, state_store = selector_runner(
            tmp_path, book=book, reads=reads, sources=sources, balances=balances
        )
        assert executor is not None
        clock = [now]
        runner._now = lambda: clock[0]

        high = runner.run(CycleMode.DRY_RUN, reference_prices_by_symbol=SELECTOR_REFERENCES)
        high_book = state_store.load()
        assert high.decision_action == "hold"
        assert high.equity_usd == Decimal("110")

        clock[0] += timedelta(minutes=5)
        reads.set_status(
            TRACKED_TOKEN_ID,
            tracked_status(owner=GAUGE_ADDRESS, value=Decimal("74")).model_copy(
                update={
                    "position": SimpleNamespace(
                        tick_lower=-90,
                        tick_upper=-75,
                        token0_address=BASE_USDC_ADDRESS,
                        token1_address=STOCK_TOKEN_ADDRESS,
                        liquidity=12_345,
                    )
                }
            ),
        )
        stopped = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_prices_by_symbol=SELECTOR_REFERENCES
        )
        stopped_book = state_store.load()
        assert stopped.equity_usd == Decimal("104")
        assert stopped.decision_action == "stop_out"
        assert [call[0] for call in executor.calls] == ["unstake", "withdraw", "exit_swap"]
        assert stopped_book.positions == ()

        # The scripted exit returns the marked position value to cash.
        # No new external deposit or state-file edit is introduced.
        clock[0] += timedelta(minutes=20)
        sources._usdc_units = 104_000_000
        balances.usdc_units = 104_000_000
        flat = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_prices_by_symbol=SELECTOR_REFERENCES
        )
        assert flat.decision_action == "hold"
        assert flat.decision_reason == "no_qualifying_pool"
        assert any("gate daily_loss_halt: FAIL" in line for line in flat.decision_diagnostics)
        assert [call[0] for call in executor.calls] == ["unstake", "withdraw", "exit_swap"]
        assert high_book.day == now.date()
        assert high_book.day_start_equity_usd == Decimal("110")
        assert high_book.peak_equity_usdc == Decimal("110")
        assert stopped_book.day == now.date()
        assert stopped_book.halted_day == now.date()
        assert stopped_book.peak_equity_usdc == Decimal("110")
        assert state_store.load().halted_day == now.date()

        # New York midnight resets the day-start anchor to 104, but the
        # cross-day peak still forbids fresh exposure at the 110-to-104 loss.
        clock[0] += timedelta(days=1)
        next_day = runner.run(
            CycleMode.LIVE, key_bytes=b"\x01" * 32, reference_prices_by_symbol=SELECTOR_REFERENCES
        )
        rolled = state_store.load()
        assert next_day.decision_action == "hold"
        assert any("gate daily_loss_halt: FAIL" in line for line in next_day.decision_diagnostics)
        assert rolled.day == clock[0].date()
        assert rolled.day_start_equity_usd == Decimal("104")
        assert rolled.peak_equity_usdc == Decimal("110")
        assert rolled.halted_day == clock[0].date()
        assert [call[0] for call in executor.calls] == ["unstake", "withdraw", "exit_swap"]

    def test_portfolio_day_anchor_includes_unclaimed_aero(self, tmp_path: Path) -> None:
        """The day-start mark and current equity use identical portfolio assets."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS, value=Decimal("80")))
        runner, _, state_store = selector_runner(
            tmp_path,
            book=tracked_book(symbol="BBBc", committed=Decimal("80")),
            reads=reads,
            sources=single_name_sources(usdc_units=30_000_000),
            balances=FakeBalances(usdc_units=30_000_000, aero_units=10**18),
        )
        first = runner.run(CycleMode.DRY_RUN)
        assert first.decision_action == "hold"
        assert first.equity_usd == Decimal("110.6")
        assert first.day_start_equity_usd == first.equity_usd
        assert first.day_pnl_usdc == Decimal("0")
        assert state_store.load().day_start_equity_usd == first.equity_usd

        second = runner.run(CycleMode.DRY_RUN)
        assert second.day_start_equity_usd == first.equity_usd
        assert second.day_pnl_usdc == Decimal("0")

    def test_portfolio_session_marks_all_held_names(self, tmp_path: Path) -> None:
        """The shared day transition counts every sibling position once."""
        runner, _, state_store = selector_runner(
            tmp_path,
            book=portfolio_book(),
            reads=portfolio_reads(),
            sources=SelectorCycleSources(usdc_units=30_000_000),
            balances=FakeBalances(usdc_units=30_000_000, aero_units=10**18),
        )
        first = runner.run(CycleMode.DRY_RUN)
        assert len(first.positions) == 2
        assert first.equity_usd == Decimal("46.6")
        assert first.day_start_equity_usd == first.equity_usd
        assert first.day_pnl_usdc == Decimal("0")
        assert state_store.load().peak_equity_usdc == first.equity_usd

    def test_peak_equity_survives_cycle_boundaries(self, tmp_path: Path) -> None:
        """The book carries the running peak forward between scheduled cycles."""
        runner, _, _, state_store = make_runner(tmp_path)

        first = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert first.peak_equity_usd is not None
        saved = state_store.load()
        assert saved.peak_equity_usdc == first.peak_equity_usd

        second = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert second.peak_equity_usd == first.peak_equity_usd
        assert state_store.load().peak_equity_usdc == first.peak_equity_usd


def test_token1_stock_in_range_clears_stale_recenter_anchor(tmp_path: Path) -> None:
    """An in-range token1-stock NFT cannot recenter from a stale wait anchor."""
    book = tracked_book()
    assert book.position is not None
    stale_anchor = QUIET_INSTANT - timedelta(minutes=30)
    book = book.model_copy(
        update={
            "positions": (
                book.positions[0].model_copy(update={"out_of_range_since": stale_anchor}),
            )
        }
    )

    reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
    reads.set_status(TRACKED_TOKEN_ID, tracked_status(owner=GAUGE_ADDRESS))

    runner, _, _, state_store = make_runner(
        tmp_path,
        book=book,
        reads=reads,
    )

    reconciliation = runner._reconcile(book)
    state = runner._policy_state(book, reconciliation)

    assert state.position is not None
    assert state.position.price_range.lower_price < FIXTURE_AMM_PRICE
    assert state.position.price_range.upper_price > FIXTURE_AMM_PRICE

    report = runner.run(
        CycleMode.DRY_RUN,
        reference_price_usdc=FIXTURE_AMM_PRICE,
    )

    assert report.decision_action == "hold"
    assert report.decision_reason != "recenter_wait_elapsed"

    saved = state_store.load()
    assert saved.position is not None
    assert saved.position.out_of_range_since is None


class TestFlatLabelAndIdleCashEvidence:
    """The gnhf 34 label and idle-book fixes, pinned over the thin flat book.

    The flat thin book (ten USDC of cash, qualifying pools on the board,
    every tier target floored to the eighty-USDC minimum the cash cannot
    fund) is exactly the posture the production bot sat in all night
    while its top-level label read ``open_in_range``. The minimum now
    binds only at or above the 1000-USDC activation equity (the
    captain's sub-1000 correction), so the fixture prices the book
    there through its unclaimed AERO - 1700 AERO at the fixture's 0.6
    USDC price on ten USDC of cash - keeping every refusal line exactly
    as the sub-1000 book used to read while the machinery stays pinned
    at the scale where the minimum still governs.
    """

    # 1700 AERO x 0.6 USDC = 1020 USDC on 10 USDC of cash: equity 1030.
    ABOVE_ACTIVATION_AERO_UNITS = 1700 * 10**18

    def flat_runner(
        self, tmp_path: Path, *, live: bool = False
    ) -> tuple[CycleRunner, FakeExecutor | None, CycleStateStore]:
        """Assemble the ten-USDC flat book priced above the activation equity."""
        return selector_runner(
            tmp_path,
            sources=SelectorCycleSources(),
            balances=FakeBalances(aero_units=self.ABOVE_ACTIVATION_AERO_UNITS),
            # The reward conversion is suppressed so the flat LIVE cycle
            # takes no treasury action: this fixture prices equity, it
            # does not test conversion.
            aero_conversion_min_usdc=(Decimal("100000") if live else None),
        )

    def test_flat_unfunded_book_carries_the_flat_label_not_open_in_range(
        self, tmp_path: Path
    ) -> None:
        """A flat book never reports a position-scoped reason."""
        runner, executor, state_store = self.flat_runner(tmp_path, live=True)
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "hold"
        assert report.decision_reason == "flat_awaiting_entry"
        # The tracked-position fields stay explicitly empty while flat,
        # matching the pnl layer that already says no tracked position.
        assert report.pnl_diagnostic == "no tracked position"
        assert report.positions == ()
        assert report.reconciliation.tracked_token_id is None
        assert report.reconciliation.tracked_status is None
        assert executor.calls == []
        assert state_store.load().positions == ()

    def test_the_gate_chain_and_idle_consequences_ride_the_decision_diagnostics(
        self, tmp_path: Path
    ) -> None:
        """Every cycle carries the per-gate evaluation and the consequence lines."""
        runner, _, _ = self.flat_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert any(
            text.startswith("entry gate chain for BBBc at the")
            for text in report.decision_diagnostics
        )
        assert any(
            text.startswith("gate daily_loss_halt: ") for text in report.decision_diagnostics
        )
        assert any(
            text.startswith("gate emissions_floor: ") for text in report.decision_diagnostics
        )
        assert any(
            text.startswith("idle-cash exclusion BBBc (insufficient_cash): ")
            for text in report.decision_diagnostics
        )

    def test_the_idle_episode_alerts_once_then_stays_quiet(self, tmp_path: Path) -> None:
        """The signature changes on the first cycle and persists after."""
        runner, _, state_store = self.flat_runner(tmp_path)
        first = runner.run(CycleMode.DRY_RUN)
        assert first.idle_cash is not None
        assert first.idle_cash.signature_changed
        assert any(
            row.symbol == "BBBc" and row.forgone_income_usdc_per_day is not None
            for row in first.idle_cash.exclusions
        )
        # The book stamps the episode signature the alert rate-limits on.
        assert state_store.load().idle_cash_alert_signature == first.idle_cash.signature
        second = runner.run(CycleMode.DRY_RUN)
        assert second.idle_cash is not None
        assert not second.idle_cash.signature_changed
        assert second.idle_cash.signature == first.idle_cash.signature

    def test_a_deployed_book_carries_no_idle_evidence(self, tmp_path: Path) -> None:
        """A funding cycle is not idle: no idle state, no episode stamp."""
        runner, _, state_store = selector_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert report.decision_action == "enter"
        assert report.idle_cash is None
        assert state_store.load().idle_cash_alert_signature is None

    def test_the_audit_payload_carries_the_gate_chain_and_idle_evidence(
        self, tmp_path: Path
    ) -> None:
        """The why-is-it-flat question is answerable from the store alone."""
        from aero_bot.cycle import record_cycle_report

        runner, _, _ = self.flat_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        audit = AuditStore(tmp_path / "chain-audit.sqlite3")
        record_cycle_report(audit, report, QUIET_INSTANT)
        record = audit.read_records(limit=1)[0]
        payload = json.loads(record.payload_json)
        assert any(
            text.startswith("gate daily_loss_halt: ") for text in payload["decision_diagnostics"]
        )
        assert any(
            text.startswith("entry gate chain for BBBc") for text in payload["decision_diagnostics"]
        )
        assert report.idle_cash is not None
        assert payload["idle_cash_signature"] == report.idle_cash.signature
        assert payload["idle_cash_fraction"] is not None
        assert payload["idle_cash_exclusions"]
        # The tracked fields stay empty while flat.
        assert payload["tracked_token_id"] is None
        assert payload["position_value_usdc"] is None
        assert payload["position_count"] == 0


def thin_staked_listings(
    bbb_emissions_multiplier: int = 2,
) -> tuple[BoardListing, ...]:
    """Build the thin-staked high-emissions board (the venue's real shape).

    The default candidate's staked liquidity (250 USDC and 3 stock in the
    current cell) reproduces the live stock-pool shape the captain
    verified on the venue UI itself: displayed APRs of about 15,445 and
    30,891 percent - real readings under the venue convention, exactly
    what the boosted-yield thesis deploys into.
    """
    return (
        BoardListing(symbol="AAAc", pool=make_candidate()),
        BoardListing(
            symbol="BBBc",
            pool=make_candidate(
                pool_address=SELECTOR_BBB_POOL,
                token1_address=SELECTOR_BBB_TOKEN,
                emissions_per_second=bbb_emissions_multiplier * 4_494_371_922_759_724,
            ),
        ),
    )


class TestConservativeIncomeCycles:
    """The captain's 2026-09-28 correction over live selector cycles.

    The venue's displayed convention is the reference: high emissions
    APRs are real and the book DEPLOYS into them. The conservative income
    expectation only floors what the yield-assuming surfaces read -
    min(current, median of the trailing window threaded through the
    cycle book) - so a transient spike never sizes or justifies a
    position. Information, never exclusion.
    """

    def thin_runner(
        self, tmp_path: Path, *, listings: tuple[BoardListing, ...] | None = None
    ) -> tuple[CycleRunner, CycleStateStore]:
        """Assemble one selector runner over the thin-staked board."""
        runner, _, _, state_store = make_runner(
            tmp_path,
            sources=SelectorCycleSources(usdc_units=SELECTOR_BOOK_USDC_UNITS).with_listings(
                listings if listings is not None else thin_staked_listings()
            ),
            balances=FakeBalances(usdc_units=SELECTOR_BOOK_USDC_UNITS),
            symbol=None,
        )
        return runner, state_store

    def test_high_readings_still_deploy_the_boosted_yield_thesis(self, tmp_path: Path) -> None:
        """The core strategy: a 15,445-percent board funds, never excluded."""
        runner, _ = self.thin_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert report.decision_action == "enter"
        assert report.symbol == "BBBc"  # the top emitter wins the ranking
        # The raw readings are far above the historical external band and
        # the book deploys into them anyway - the venue convention is the
        # reference and the boost is the strategy.
        assert "board [AAAc: qualified at APR 154." in " ".join(report.input_notes)
        assert "BBBc: qualified at APR 308." in " ".join(report.input_notes)

    def test_a_cold_book_reads_unchanged_and_threads_its_history(self, tmp_path: Path) -> None:
        """Cycle one: no history, the basis equals the reading, samples thread."""
        runner, state_store = self.thin_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert not any("conservative income basis" in text for text in report.decision_diagnostics)
        book = state_store.load()
        assert [sample.symbol for sample in book.apr_history] == ["AAAc", "BBBc"]
        assert all(sample.reading > 0 for sample in book.apr_history)

    def test_a_spike_is_floored_in_sizing_but_still_deploys(self, tmp_path: Path) -> None:
        """The correction's exact shape: information, never exclusion.

        Cycle one reads the thin board (about 154.45 and 308.90 as raw
        fractions); cycle two spikes the reward streams tenfold. The
        spiked cycle still ENTERS - the readings are real - but its
        income expectations floor at the trailing median and the evidence
        line names both numbers.
        """
        first_runner, state_store = self.thin_runner(tmp_path)
        first = first_runner.run(CycleMode.DRY_RUN)
        assert first.decision_action == "enter"
        assert not any("conservative income basis" in text for text in first.decision_diagnostics)
        second_runner, _ = self.thin_runner(
            tmp_path, listings=thin_staked_listings(bbb_emissions_multiplier=20)
        )
        second = second_runner.run(CycleMode.DRY_RUN)
        # The spiked board still deploys: the boosted-yield thesis.
        assert second.decision_action == "enter"
        assert second.symbol == "BBBc"
        # The income basis evidence names the flooring for the spiked
        # pool; AAAc read unchanged, so its expectation floors at itself
        # and carries no line.
        joined = " ".join(second.decision_diagnostics)
        assert "conservative income basis: BBBc" in joined
        assert "flooring the instantaneous" in joined
        assert "the qualifying gates and the ranking keep the raw" in joined
        assert "conservative income basis: AAAc" not in joined
        # The book threads the spiked readings for the next window.
        book = state_store.load()
        spiked = {sample.symbol: sample.reading for sample in book.apr_history}
        assert spiked["BBBc"] > Decimal("3000")  # the tenfold reading

    def test_the_window_bounds_and_env_wiring(self) -> None:
        """The window defaults to six cycles and refuses out-of-interval."""
        assert _income_history_cycles_from_environment({}) == 6
        assert (
            _income_history_cycles_from_environment({CYCLE_INCOME_HISTORY_CYCLES_ENV: "12"}) == 12
        )
        with pytest.raises(ValueError, match="at least one"):
            _income_history_cycles_from_environment({CYCLE_INCOME_HISTORY_CYCLES_ENV: "0"})
        with pytest.raises(ValueError, match="must not exceed 48"):
            _income_history_cycles_from_environment({CYCLE_INCOME_HISTORY_CYCLES_ENV: "49"})

    def test_the_trim_keeps_the_most_recent_window_per_symbol(self) -> None:
        """The successor history bounds each pool to the window."""
        prior = tuple(
            AprReadingSample(symbol="AAAc", reading=Decimal(str(value)))
            for value in range(1, 7)  # six prior samples
        )
        current = (AprReadingSample(symbol="AAAc", reading=Decimal("99")),)
        trimmed = _trim_apr_history(current, prior, 6)
        readings = [sample.reading for sample in trimmed if sample.symbol == "AAAc"]
        assert readings == [Decimal(str(value)) for value in range(2, 7)] + [Decimal("99")]
