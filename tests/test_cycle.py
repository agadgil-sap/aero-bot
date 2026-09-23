"""Pin the scheduled decision cycle's reconcile-decide-act behavior."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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

from aero_bot.audit import AuditEventType, AuditRecord, AuditStore
from aero_bot.cycle import (
    CYCLE_REFERENCE_PRICE_ENV,
    CYCLE_SWITCH_MARGIN_ENV,
    CYCLE_SYMBOL_ENV,
    CycleMode,
    CycleRunner,
    CycleStateBook,
    CycleStateStore,
    HeldInventoryRecord,
    ReentryCooldown,
    TrackedPosition,
    _reference_price_from_environment,
    _switch_margin_from_environment,
    _symbol_from_arguments_and_environment,
    decode_minted_token_id,
)
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
    AlignedPriceRange,
    PolicyActionKind,
    PolicyDecision,
    PolicyOutcome,
    PolicyReason,
    PolicyState,
)
from aero_bot.strategy import BoardListing
from aero_bot.venues import BASE_USDC_ADDRESS, PoolCandidate

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

    budget_usdc: str


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
        relayer_eth_wei: int = 10**15,
        block_number: int = 99_999_999,
    ) -> None:
        """Configure every served balance."""
        self.usdc_units = usdc_units
        self.stock_units = stock_units
        self.relayer_eth_wei = relayer_eth_wei
        self.block_number = block_number
        self.receipts: dict[str, dict[str, object]] = {}

    def fetch_token_balance(self, token_address: str, owner_address: str) -> int:
        """Serve the configured per-token balance."""
        if token_address.lower() == BASE_USDC_ADDRESS.lower():
            return self.usdc_units
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
        self.mint_executed_budget: Decimal | None = None
        self.fee_wei_per_step = 90_000
        self.confirmed_block_number = 51_000_000

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
    ) -> LpActionExecutionReport:
        """Complete one mint, minting the scripted token id on-chain."""
        self.calls.append(("mint", symbol, budget_usdc, width_spacings))
        if self.refuse_next == "mint":
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING, "scripted refusal"
            )
        if self.mint_receipt_token_id is not None:
            self._balances.receipts[MINT_TX_HASH] = mint_receipt(self.mint_receipt_token_id)
        else:
            self._balances.receipts[MINT_TX_HASH] = {"logs": []}
        self._audit_confirm("mint", LpExecutionRole.MINT, MINT_TX_HASH)
        self._audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(budget_usdc=str(budget_usdc)),
            self._now,
        )
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
        self._reads.set_status(token_id, tracked_status(owner=GAUGE_ADDRESS))
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


def tracked_status(
    *,
    owner: str = SAFE_ADDRESS,
    value: Decimal = Decimal("8"),
    pnl: Decimal | None = Decimal("1"),
    fees_usdc: Decimal | None = None,
    observed_at: datetime = QUIET_INSTANT,
) -> LpPositionStatusReport:
    """Build one minimal tracked-position status via unchecked construction."""
    view = SimpleNamespace(tick_lower=FIXTURE_RANGE_LOWER, tick_upper=FIXTURE_RANGE_UPPER)
    return LpPositionStatusReport.model_construct(
        symbol="FIXc",
        pool_address=POOL_ADDRESS,
        token_owner_address=owner,
        gauge_address=GAUGE_ADDRESS,
        position=view,
        position_value_usdc=value,
        unrealized_pnl_usdc=pnl,
        pnl_diagnostic="" if pnl is not None else "no entry cost",
        fees_owed_usdc=fees_usdc,
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
        position=TrackedPosition(
            symbol=symbol,
            token_id=TRACKED_TOKEN_ID,
            pool_address=POOL_ADDRESS,
            committed_usd=committed,
            entered_at=entered_at,
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
            AuditEventType.LP_EXECUTE_CONFIRMED,
            FakeConfirmPayload(
                outcome="confirmed",
                action="mint",
                role="mint",
                transaction_hash=MINT_TX_HASH,
            ),
            QUIET_INSTANT,
        )
        audit.append(
            AuditEventType.LP_MINT_PLANNED,
            FakePlanPayload(budget_usdc="7"),
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
            update={"position": SimpleNamespace(tick_lower=-10, tick_upper=10)}
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
# Both pools quote the same fixture price, so one reference map serves both.
SELECTOR_REFERENCES = {"AAAc": FIXTURE_AMM_PRICE, "BBBc": FIXTURE_AMM_PRICE}


def selector_listings(bbb_emissions_multiplier: int = 2) -> tuple[BoardListing, ...]:
    """Build the two-pool scripted board: AAAc plain, BBBc scaled emissions."""
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


def selector_runner(
    tmp_path: Path,
    *,
    book: CycleStateBook | None = None,
    reads: FakeReads | None = None,
    executor: object | None = None,
    sources: SelectorCycleSources | None = None,
) -> tuple[CycleRunner, FakeExecutor | None, CycleStateStore]:
    """Assemble one selector-mode cycle runner over the scripted board."""
    runner, fake_executor, _, state_store = make_runner(
        tmp_path,
        book=book,
        reads=reads,
        sources=sources if sources is not None else SelectorCycleSources(),
        executor=executor,
        symbol=None,
    )
    return runner, fake_executor, state_store


class TestSelectorCycles:
    """Cross-board selection cycles: entry, hysteresis, and one position."""

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
            "selector equity includes tracked LP marked value 8 USDC" in note
            for note in report.input_notes
        )
        assert state_store.load().halted_day is None

    def test_selector_enters_the_best_qualifying_pool(self, tmp_path: Path) -> None:
        """The live cycle mints and stakes the higher-APR pool only."""
        runner, executor, state_store = selector_runner(tmp_path)
        assert executor is not None
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "enter"
        assert report.decision_reason == "entry_threshold_met"
        assert report.symbol == "BBBc"
        assert [call[0] for call in executor.calls] == ["mint", "stake"]
        assert executor.calls[0][1] == "BBBc"
        book = state_store.load()
        assert book.position is not None
        assert book.position.symbol == "BBBc"
        assert book.position.committed_usd == EXPECTED_ENTER_SIZE

    def test_selector_holds_below_the_switch_margin(self, tmp_path: Path) -> None:
        """A funded position stays when no pool clears the thirty percent margin."""
        sources = SelectorCycleSources().with_listings(selector_listings(1))
        runner, executor, state_store = selector_runner(
            tmp_path,
            book=tracked_book(symbol="AAAc", entered_at=QUIET_INSTANT - timedelta(hours=2)),
            reads=_tracked_reads(staked=True),
            sources=sources,
        )
        assert executor is not None
        # BBBc at ten percent over AAAc sits inside the default thirty
        # percent margin, so no switch fires: hold the funded AAAc position.
        report = runner.run(
            CycleMode.LIVE,
            key_bytes=b"\x01" * 32,
            reference_prices_by_symbol=SELECTOR_REFERENCES,
        )
        assert report.decision_action == "hold"
        assert executor.calls == []
        assert state_store.load().position is not None

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


def test_token1_stock_in_range_clears_stale_recenter_anchor(tmp_path: Path) -> None:
    """An in-range token1-stock NFT cannot recenter from a stale wait anchor."""
    book = tracked_book()
    assert book.position is not None
    stale_anchor = QUIET_INSTANT - timedelta(minutes=30)
    book = book.model_copy(
        update={"position": book.position.model_copy(update={"out_of_range_since": stale_anchor})}
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
