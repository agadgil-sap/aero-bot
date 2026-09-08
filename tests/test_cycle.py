"""Pin the scheduled decision cycle's reconcile-decide-act behavior."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import BaseModel
from test_lp_executor import (
    B20_ADDRESS,
    GAUGE_ADDRESS,
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
    CycleMode,
    CycleRunner,
    CycleStateBook,
    CycleStateStore,
    HeldInventoryRecord,
    TrackedPosition,
    _reference_price_from_environment,
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
    PolicyActionKind,
    PolicyDecision,
    PolicyOutcome,
    PolicyReason,
    PolicyState,
)
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
FIXTURE_RANGE_LOWER = -11630
FIXTURE_RANGE_UPPER = -11610
# The entry size and width the locked engine derives at a ten-USDC
# equity: the 20-percent equity cap and the ceiling-rounded spacing width.
EXPECTED_ENTER_SIZE = Decimal("2")
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

    def __init__(self, *, usdc_units: int = 10_000_000, stock_units: int = 0) -> None:
        """Configure the fixture pool and the Safe's live balances."""
        self._usdc_units = usdc_units
        self._stock_units = stock_units

    def discover(self) -> tuple[tuple[PoolCandidate, ...], int]:
        """Return the verified fixture pool and its snapshot block."""
        return (make_candidate(),), 123

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
    ) -> None:
        """Configure every served balance."""
        self.usdc_units = usdc_units
        self.stock_units = stock_units
        self.relayer_eth_wei = relayer_eth_wei
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
        self.fee_wei_per_step = 90_000

    def _complete(self, action: str, hashes: tuple[str, ...]) -> LpActionExecutionReport:
        """Build one completed execution report over scripted steps."""
        steps = tuple(
            cast(
                object,
                SimpleNamespace(
                    transaction_hash=h, fee_wei=self.fee_wei_per_step, status="confirmed"
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
        return self._complete("mint", (MINT_TX_HASH,))

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


def tracked_book(*, owner: str = SAFE_ADDRESS, committed: Decimal = Decimal("7")) -> CycleStateBook:
    """Build one book tracking the fixture position."""
    return CycleStateBook(
        position=TrackedPosition(
            symbol="FIXc",
            token_id=TRACKED_TOKEN_ID,
            pool_address=POOL_ADDRESS,
            committed_usd=committed,
            entered_at=QUIET_INSTANT,
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
        symbol="FIXc",
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

    def test_flat_cycle_without_a_reference_holds_fail_closed(self, tmp_path: Path) -> None:
        """The reference-stale hold is the honest verdict, audited once."""
        runner, _, audit, state_store = make_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        assert report.decision_action == "hold"
        assert report.decision_reason == "reference_stale"
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

    def test_market_window_flat_verdict_is_reported_as_doctrine(self, tmp_path: Path) -> None:
        """A session window holds flat inside the cycle too."""
        market_open = datetime(2026, 9, 8, 13, 40, tzinfo=UTC)
        runner, _, _, _ = make_runner(tmp_path, now=market_open)
        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert report.decision_action == "hold"
        assert report.decision_reason == "event_window_flat"
        assert report.event_window != ""


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
        actions, halted, book = runner._act(tracked_book(), outcome, b"\x01" * 32)
        assert halted == ""
        assert [record.action for record in actions] == [
            "unstake",
            "withdraw",
            "exit_swap",
        ]
        assert all(record.status == "completed" for record in actions)
        assert book.position is None and book.held_inventory is None

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
        actions, halted, book = runner._act(tracked_book(), outcome, b"\x01" * 32)
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
        actions, halted, new_book = runner._act(book, outcome, b"\x01" * 32)
        assert [record.action for record in actions] == ["exit_swap"]
        assert halted == ""
        assert new_book.held_inventory is None


class TestReconciliation:
    """The reconcile-first discipline and its out-of-band refusals."""

    def test_tracked_position_reports_custody_value_and_pnl(self, tmp_path: Path) -> None:
        """An open tracked position carries its P&L into the report."""
        reads = FakeReads(inventory_with(TRACKED_TOKEN_ID))
        reads.set_status(TRACKED_TOKEN_ID, tracked_status())
        runner, _, _, _ = make_runner(tmp_path, book=tracked_book(), reads=reads)
        report = runner.run(CycleMode.DRY_RUN, reference_price_usdc=FIXTURE_AMM_PRICE)
        assert report.reconciliation.tracked_staked is False
        assert report.pnl_vs_entry_usdc == Decimal("1")
        assert report.decision_action == "hold"

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

    def test_the_timer_defaults_hourly_with_persistence_and_jitter(self) -> None:
        """One cycle per hour, catching up after downtime, never overlapping."""
        timer = Path("deploy/systemd/aero-bot-cycle@.timer").read_text(encoding="utf-8")
        assert "OnCalendar=hourly" in timer
        assert "Persistent=true" in timer
        assert "RandomizedDelaySec=180" in timer
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
