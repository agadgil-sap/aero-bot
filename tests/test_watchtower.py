"""Pin the range watchtower's trip, fail-safe, latch, and wiring contract."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_alerts import SMTP_ENV, FakeTransport
from test_lp_executor import (
    B20_ADDRESS,
    GAUGE_ADDRESS,
    NFPM_ADDRESS,
    POOL_ADDRESS,
    SAFE_ADDRESS,
)

from aero_bot.audit import AuditEventType, AuditRecord, AuditStore
from aero_bot.concentrated import PositionRangeState
from aero_bot.cycle import (
    AuditStoreReader,
    CycleStateBook,
    CycleStateStore,
    TrackedPosition,
)
from aero_bot.executor import ExecutionUnavailableError
from aero_bot.lp_calldata import LpPositionView
from aero_bot.lp_executor import (
    LpActionExecutionReport,
    LpExecutionRefusalCode,
    LpExecutionRefusalError,
    LpPositionStatusReport,
    LpSafePositionsSnapshot,
)
from aero_bot.venues import BASE_USDC_ADDRESS
from aero_bot.watchtower import (
    WATCHTOWER_COOLDOWN_SECONDS_ENV,
    WATCHTOWER_ENABLED_ENV,
    WATCHTOWER_POLL_SECONDS_ENV,
    WATCHTOWER_STATE_PATH_ENV,
    RangeWatchtower,
    WatchtowerConfig,
    WatchtowerLatchBook,
    WatchtowerLatchStore,
    WatchtowerPollOutcome,
    WatchtowerPollState,
    deliver_watchtower_notice,
    deliver_watchtower_trigger,
    main,
    parse_watchtower_config,
)

# 2026-09-08 is a Tuesday; the quiet instant keeps event windows calm.
QUIET_INSTANT = datetime(2026, 9, 8, 20, 30, tzinfo=UTC)
# The tracked fixture position's token id.
TRACKED_TOKEN_ID = 5_703_026
# The fixture position's aligned range bounds on the spacing-10 grid.
RANGE_LOWER = -11_630
RANGE_UPPER = -11_610
# One tick inside, one below, and one at the exclusive upper boundary.
IN_RANGE_TICK = -11_620
BELOW_TICK = -11_640
ABOVE_TICK = -11_600
# A fixture signing key's raw bytes; only its shape ever matters here.
KEY_BYTES = b"\x07" * 32
# The default test configuration: armed, five-second polls, fifteen-minute
# cooldown - exactly the shipped defaults with the enable flag on.
ARMED_CONFIG = WatchtowerConfig(enabled=True, poll_interval_seconds=5.0, cooldown_seconds=900.0)
# The committed entry cost the tracked fixture position carries.
COMMITTED_USD = Decimal("7")


class MutableClock:
    """Serve one injectable timezone-aware instant the tests advance."""

    def __init__(self, start: datetime = QUIET_INSTANT) -> None:
        """Start the clock at one fixture instant."""
        self.instant = start

    def __call__(self) -> datetime:
        """Return the current instant."""
        return self.instant

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        self.instant = self.instant + timedelta(seconds=seconds)


class FakeTickReader:
    """Serve one mutable pool tick, optionally failing."""

    def __init__(self, tick: int) -> None:
        """Start every poll at one tick."""
        self.tick = tick
        self.fail = False

    def read_current_tick(self, pool_address: str) -> int:
        """Return the served tick or fail like an RPC outage."""
        assert pool_address == POOL_ADDRESS
        if self.fail:
            raise ExecutionUnavailableError("the endpoint is unavailable")
        return self.tick


def position_view(*, liquidity: int = 10**10) -> LpPositionView:
    """Build one decoded twelve-word view for the tracked fixture."""
    return LpPositionView(
        nonce=1,
        operator_address="0x0000000000000000000000000000000000000000",
        token0_address=BASE_USDC_ADDRESS,
        token1_address=B20_ADDRESS,
        tick_spacing=10,
        tick_lower=RANGE_LOWER,
        tick_upper=RANGE_UPPER,
        liquidity=liquidity,
        fee_growth_inside0_last_x128=0,
        fee_growth_inside1_last_x128=0,
        tokens_owed0_units=0,
        tokens_owed1_units=0,
    )


class FakeBoundsReader:
    """Serve the tracked position's immutable view, optionally failing."""

    def __init__(self, view: LpPositionView | None = None) -> None:
        """Serve one view for the tracked token id."""
        self._views: dict[int, LpPositionView] = {
            TRACKED_TOKEN_ID: view if view is not None else position_view()
        }
        self.fail = False

    def read_position_bounds(self, nfpm_address: str, token_id: int) -> LpPositionView:
        """Return the served view or fail like an RPC outage."""
        assert nfpm_address == NFPM_ADDRESS
        if self.fail or token_id not in self._views:
            raise ValueError(f"token {token_id} is unknown to the NFPM fake")
        return self._views[token_id]


def status_report(
    *,
    owner: str = GAUGE_ADDRESS,
    range_state: PositionRangeState = PositionRangeState.BELOW_RANGE,
    liquidity: int = 10**10,
) -> LpPositionStatusReport:
    """Build one verified status shape via unchecked construction."""
    view = SimpleNamespace(
        tick_lower=RANGE_LOWER,
        tick_upper=RANGE_UPPER,
        liquidity=liquidity,
        token0_address=BASE_USDC_ADDRESS,
        token1_address=B20_ADDRESS,
    )
    return LpPositionStatusReport.model_construct(
        symbol="FIXc",
        pool_address=POOL_ADDRESS,
        gauge_address=GAUGE_ADDRESS,
        token_owner_address=owner,
        position=view,
        range_state=range_state,
        position_value_usdc=Decimal("8"),
        unrealized_pnl_usdc=Decimal("1"),
        pnl_diagnostic="",
        snapshot_block=123,
    )


class FakeStatusReads:
    """Serve the audited position-status verification, optionally failing."""

    def __init__(self, status: LpPositionStatusReport | None = None) -> None:
        """Serve one verified status for the tracked token id."""
        self._status = status
        self.calls = 0

    def position_status(
        self,
        symbol: str,
        token_id: int,
        aero_price_usdc: Decimal | None = None,
        entry_cost_usdc: Decimal | None = None,
    ) -> LpPositionStatusReport:
        """Return the served status or refuse like an unreadable gate."""
        self.calls += 1
        assert symbol == "FIXc" and token_id == TRACKED_TOKEN_ID
        assert entry_cost_usdc == COMMITTED_USD
        if self._status is None:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.POSITION_UNKNOWN, "no scripted status"
            )
        return self._status

    def safe_position_inventory(self, symbol: str) -> LpSafePositionsSnapshot:
        """The watchtower never enumerates inventory; refuse if asked."""
        raise AssertionError("the watchtower must never enumerate inventory")


class FakeBalances:
    """Serve the trigger report's balance reads."""

    def __init__(self) -> None:
        """Configure the served balances."""
        self.usdc_units = 10_000_000
        self.stock_units = 0
        self.relayer_eth_wei = 10**15

    def fetch_token_balance(self, token_address: str, owner_address: str) -> int:
        """Serve the configured per-token balance."""
        if token_address.lower() == BASE_USDC_ADDRESS.lower():
            return self.usdc_units
        return self.stock_units

    def fetch_eth_balance(self, account_address: str) -> int:
        """Serve the relayer's configured ETH balance."""
        return self.relayer_eth_wei

    def fetch_transaction_receipt(self, transaction_hash: str) -> dict[str, object] | None:
        """Serve no receipts; the watchtower decodes none."""
        return None


class FakeCloseExecutor:
    """Record every close invocation shape and serve scripted outcomes."""

    def __init__(self, *, refuse: str | None = None) -> None:
        """Configure the scripted refusal, if any."""
        self.calls: list[dict[str, object]] = []
        self._refuse = refuse

    def _run(self, action: str, *args: object, **kwargs: object) -> LpActionExecutionReport:
        """Record one invocation's complete shape."""
        self.calls.append({"action": action, "args": args, "kwargs": kwargs})
        if self._refuse == action:
            raise LpExecutionRefusalError(
                LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING, "scripted refusal"
            )
        step = SimpleNamespace(
            transaction_hash="0x" + "ab" * 32, fee_wei=90_000, status="confirmed"
        )
        return LpActionExecutionReport.model_construct(
            action=action, build=None, steps=(step,), completed=True, halted_reason=""
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
        """Record the unstake shape."""
        return self._run(
            "unstake", symbol, token_id, key_bytes, confirm_broadcast=confirm_broadcast
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
        """Record the withdraw shape."""
        return self._run(
            "withdraw", symbol, token_id, key_bytes, confirm_broadcast=confirm_broadcast
        )

    def execute_exit_swap(
        self,
        symbol: str,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Record the exit-swap shape."""
        return self._run("exit_swap", symbol, key_bytes, confirm_broadcast=confirm_broadcast)

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
        """The watchtower never mints; refuse if asked."""
        raise AssertionError("the watchtower must never mint")

    def execute_stake(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """The watchtower never stakes; refuse if asked."""
        raise AssertionError("the watchtower must never stake")


class RecordingKeyLoader:
    """Load one fixture key while counting every load."""

    def __init__(self) -> None:
        """Start unloaded."""
        self.loads = 0

    def __call__(self) -> bytes:
        """Load the fixture key once more."""
        self.loads += 1
        return KEY_BYTES


class AlertRecorder:
    """Record every trigger email and fail-safe notice."""

    def __init__(self) -> None:
        """Start with an empty outbox."""
        self.triggers: list[tuple[CycleStateBook | None, str]] = []
        self.notices: list[tuple[str, str]] = []

    def deliver_trigger(self, report: object, trip_line: str) -> None:
        """Record one trigger email."""
        self.triggers.append((None, trip_line))

    def deliver_notice(self, subject: str, body: str) -> None:
        """Record one fail-safe notice."""
        self.notices.append((subject, body))


def tracked_book() -> CycleStateBook:
    """Build one book tracking the fixture position."""
    return CycleStateBook(
        position=TrackedPosition(
            symbol="FIXc",
            token_id=TRACKED_TOKEN_ID,
            pool_address=POOL_ADDRESS,
            committed_usd=COMMITTED_USD,
            entered_at=QUIET_INSTANT,
        ),
        updated_at=QUIET_INSTANT,
    )


class WatchtowerHarness:
    """Assemble one watchtower over fully scripted boundaries."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        tick: int = IN_RANGE_TICK,
        status: LpPositionStatusReport | None = None,
        book: CycleStateBook | None = None,
        refuse: str | None = None,
        config: WatchtowerConfig = ARMED_CONFIG,
        key_loader: RecordingKeyLoader | None = None,
    ) -> None:
        """Wire every fake and store around one mutable clock."""
        self.clock = MutableClock()
        self.ticks = FakeTickReader(tick)
        self.bounds = FakeBoundsReader()
        self.statuses = FakeStatusReads(status)
        self.balances = FakeBalances()
        self.executor = FakeCloseExecutor(refuse=refuse)
        self.key_loader = key_loader if key_loader is not None else RecordingKeyLoader()
        self.alerts = AlertRecorder()
        self.state_store = CycleStateStore(tmp_path / "cycle_state.json")
        if book is None:
            book = tracked_book()
        self.state_store.save(book)
        self.latch_store = WatchtowerLatchStore(tmp_path / "watchtower_state.json")
        self.audit = AuditStore(tmp_path / "audit.sqlite3")
        self.watchtower = RangeWatchtower(
            symbol="FIXc",
            safe_address=SAFE_ADDRESS,
            relayer_address=None,
            config=config,
            tick_reader=self.ticks,
            bounds_reader=self.bounds,
            nfpm_resolver=lambda pool: NFPM_ADDRESS,
            reads=self.statuses,
            balances=self.balances,
            executor=self.executor,
            key_loader=self.key_loader,
            audit_sink=self.audit,
            state_store=self.state_store,
            latch_store=self.latch_store,
            stock_decimals=8,
            deliver_trigger=self.alerts.deliver_trigger,
            deliver_notice=self.alerts.deliver_notice,
            now=self.clock,
            sleep=lambda seconds: None,
            progress=lambda line: None,
        )

    def poll(self) -> WatchtowerPollOutcome:
        """Run one poll through the assembled watchtower."""
        return self.watchtower.poll_once()

    def audit_records(self) -> tuple[AuditStoreReader, list[AuditRecord]]:
        """Read the complete audit chain."""
        reader = AuditStoreReader(self.audit)
        return reader, list(reader.recent_records())


class TestWatchtowerConfigParsing:
    """The sealed environment drives the watcher's three knobs."""

    def test_defaults_are_dark_and_calm(self) -> None:
        """Nothing configured means disabled, five seconds, fifteen minutes."""
        config = parse_watchtower_config({})
        assert config.enabled is False
        assert config.poll_interval_seconds == 5.0
        assert config.cooldown_seconds == 900.0

    def test_the_enable_flag_accepts_every_truthy_spelling(self) -> None:
        """One spell of true arms; everything else stays dark."""
        for raw in ("1", "true", "yes", "on", "TRUE", " On "):
            assert parse_watchtower_config({WATCHTOWER_ENABLED_ENV: raw}).enabled is True
        for raw in ("", "0", "false", "no", "off", "maybe"):
            assert parse_watchtower_config({WATCHTOWER_ENABLED_ENV: raw}).enabled is False

    def test_intervals_parse_as_positive_seconds(self) -> None:
        """Float seconds parse; malformed values name their variable."""
        config = parse_watchtower_config(
            {
                WATCHTOWER_POLL_SECONDS_ENV: "2.5",
                WATCHTOWER_COOLDOWN_SECONDS_ENV: "60",
            }
        )
        assert config.poll_interval_seconds == 2.5
        assert config.cooldown_seconds == 60.0
        for variable, value in (
            (WATCHTOWER_POLL_SECONDS_ENV, "zero"),
            (WATCHTOWER_POLL_SECONDS_ENV, "0"),
            (WATCHTOWER_POLL_SECONDS_ENV, "-5"),
            (WATCHTOWER_COOLDOWN_SECONDS_ENV, "0"),
            (WATCHTOWER_COOLDOWN_SECONDS_ENV, "-1"),
        ):
            with pytest.raises(ValueError, match=variable):
                parse_watchtower_config({variable: value})

    def test_the_latch_store_honors_its_path_override(self, tmp_path: Path) -> None:
        """The explicit override beats the beside-audit default."""
        override = tmp_path / "sealed" / "latch.json"
        store = WatchtowerLatchStore.from_environment({WATCHTOWER_STATE_PATH_ENV: str(override)})
        assert store.path == override
        store.save(WatchtowerLatchBook(tripped=True, updated_at=QUIET_INSTANT))
        assert (
            WatchtowerLatchStore.from_environment({WATCHTOWER_STATE_PATH_ENV: str(override)})
            .load()
            .tripped
            is True
        )

    def test_the_latch_store_self_heals_on_garbage(self, tmp_path: Path) -> None:
        """A torn or malformed file yields a fresh unlatched book."""
        path = tmp_path / "latch.json"
        path.write_text("{not json", encoding="utf-8")
        assert WatchtowerLatchStore(path).load().tripped is False
        path.write_text('{"tripped": "not-a-bool"}', encoding="utf-8")
        assert WatchtowerLatchStore(path).load().tripped is False


class TestTripSemantics:
    """A hard trip on either side fires; everything calmer does not."""

    def test_below_range_fires_the_complete_close(self, tmp_path: Path) -> None:
        """A tick below the range unstakes, withdraws, and swaps to USDC."""
        harness = WatchtowerHarness(
            tmp_path, tick=BELOW_TICK, status=status_report(owner=GAUGE_ADDRESS)
        )
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FIRED
        assert [call["action"] for call in harness.executor.calls] == [
            "unstake",
            "withdraw",
            "exit_swap",
        ]
        assert harness.key_loader.loads == 1
        assert harness.state_store.load().position is None
        assert harness.latch_store.load().tripped is False
        _, records = harness.audit_records()
        summaries = [
            record for record in records if record.event_type is AuditEventType.CYCLE_REPORTED
        ]
        assert len(summaries) == 1
        payload = json.loads(summaries[0].payload_json)
        assert payload["action"] == "defensive_exit"
        assert payload["reason"] == "watchtower_range_trip"
        assert payload["mode"] == "live"
        assert payload["tracked_token_id"] == TRACKED_TOKEN_ID
        assert payload["halted_reason"] == ""
        assert len(harness.alerts.triggers) == 1
        assert (
            f"pool tick {BELOW_TICK} is below the position range" in harness.alerts.triggers[0][1]
        )

    def test_at_or_above_the_exclusive_upper_bound_fires(self, tmp_path: Path) -> None:
        """A tick at the upper boundary has left the range and fires."""
        harness = WatchtowerHarness(
            tmp_path,
            tick=ABOVE_TICK,
            status=status_report(range_state=PositionRangeState.ABOVE_RANGE),
        )
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FIRED
        assert harness.key_loader.loads == 1
        assert (
            f"pool tick {ABOVE_TICK} is at or above the position range"
            in (harness.alerts.triggers[0][1])
        )

    def test_in_range_is_a_silent_no_op(self, tmp_path: Path) -> None:
        """A tick inside the range - boundaries included - never acts."""
        harness = WatchtowerHarness(tmp_path, tick=IN_RANGE_TICK)
        for tick in (IN_RANGE_TICK, RANGE_LOWER, RANGE_UPPER - 1):
            harness.ticks.tick = tick
            outcome = harness.poll()
            assert outcome.state is WatchtowerPollState.IN_RANGE
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0
        assert harness.statuses.calls == 0
        _, records = harness.audit_records()
        assert records == []
        assert harness.alerts.triggers == []
        assert harness.alerts.notices == []
        assert harness.latch_store.load().tripped is False

    def test_flat_never_acts(self, tmp_path: Path) -> None:
        """No tracked position means nothing to defend."""
        harness = WatchtowerHarness(tmp_path, book=CycleStateBook(updated_at=QUIET_INSTANT))
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FLAT
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0


class TestLatchAndCooldown:
    """One shot per trip, cooldown between attempts, reset on reconcile."""

    def test_a_failed_close_retries_only_after_the_cooldown(self, tmp_path: Path) -> None:
        """A refused close stays latched; the cooldown bounds its retries."""
        harness = WatchtowerHarness(
            tmp_path,
            tick=BELOW_TICK,
            status=status_report(),
            refuse="withdraw",
        )
        first = harness.poll()
        assert first.state is WatchtowerPollState.FIRED
        assert first.report is not None
        assert first.report.halted_reason.startswith("the withdraw action refused")
        assert harness.key_loader.loads == 1
        assert harness.latch_store.load().tripped is True
        # The immediate retry is suppressed by the cooldown.
        second = harness.poll()
        assert second.state is WatchtowerPollState.COOLDOWN
        assert harness.key_loader.loads == 1
        assert len(harness.executor.calls) == 2
        # Inside the cooldown window the trip still never re-fires.
        harness.clock.advance(899)
        assert harness.poll().state is WatchtowerPollState.COOLDOWN
        assert len(harness.executor.calls) == 2
        # Past the cooldown the retry fires while the trip stands.
        harness.clock.advance(2)
        third = harness.poll()
        assert third.state is WatchtowerPollState.FIRED
        assert len(harness.executor.calls) == 4

    def test_a_successful_close_resets_through_the_flat_reconcile(self, tmp_path: Path) -> None:
        """The close clears the book; a fresh trip is a fresh shot."""
        harness = WatchtowerHarness(
            tmp_path, tick=BELOW_TICK, status=status_report(owner=SAFE_ADDRESS)
        )
        assert harness.poll().state is WatchtowerPollState.FIRED
        assert harness.poll().state is WatchtowerPollState.FLAT
        assert harness.latch_store.load().tripped is False
        # A re-entered position that trips again waits out the cooldown.
        harness.state_store.save(tracked_book())
        harness.ticks.tick = BELOW_TICK
        assert harness.poll().state is WatchtowerPollState.COOLDOWN
        harness.clock.advance(901)
        assert harness.poll().state is WatchtowerPollState.FIRED

    def test_a_trip_that_reconciles_back_in_range_unlatches(self, tmp_path: Path) -> None:
        """The price returning inside the range resolves the trip."""
        harness = WatchtowerHarness(
            tmp_path,
            tick=BELOW_TICK,
            status=status_report(),
            refuse="withdraw",
        )
        assert harness.poll().state is WatchtowerPollState.FIRED
        harness.ticks.tick = IN_RANGE_TICK
        assert harness.poll().state is WatchtowerPollState.IN_RANGE
        assert harness.latch_store.load().tripped is False
        # The next trip is one fresh shot, still cooldown-bounded.
        harness.ticks.tick = BELOW_TICK
        assert harness.poll().state is WatchtowerPollState.COOLDOWN

    def test_the_cooldown_survives_a_restart(self, tmp_path: Path) -> None:
        """A rebuilt watcher inherits the persisted latch and stamp."""
        harness = WatchtowerHarness(
            tmp_path, tick=BELOW_TICK, status=status_report(), refuse="unstake"
        )
        assert harness.poll().state is WatchtowerPollState.FIRED
        rebuilt = WatchtowerHarness(tmp_path, tick=BELOW_TICK, status=status_report())
        assert rebuilt.poll().state is WatchtowerPollState.COOLDOWN
        assert rebuilt.executor.calls == []
        rebuilt.clock.advance(901)
        assert rebuilt.poll().state is WatchtowerPollState.FIRED


class TestFailSafePosture:
    """Unreadable or contradictory state never fires an exit."""

    def test_a_failed_tick_read_alerts_and_never_fires(self, tmp_path: Path) -> None:
        """An RPC outage reports, emails once, and keeps retrying."""
        harness = WatchtowerHarness(tmp_path)
        harness.ticks.fail = True
        first = harness.poll()
        assert first.state is WatchtowerPollState.READ_FAILED
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0
        assert len(harness.alerts.notices) == 1
        subject, body = harness.alerts.notices[0]
        assert "no exit fired" in subject
        assert "never fires an exit on unreadable state" in body
        # The throttle holds the notice to one per cooldown window.
        harness.clock.advance(10)
        assert harness.poll().state is WatchtowerPollState.READ_FAILED
        assert len(harness.alerts.notices) == 1
        harness.clock.advance(900)
        assert harness.poll().state is WatchtowerPollState.READ_FAILED
        assert len(harness.alerts.notices) == 2

    def test_unreadable_bounds_never_fire(self, tmp_path: Path) -> None:
        """An NFPM the watcher cannot read is a fail-safe, not a trip."""
        harness = WatchtowerHarness(tmp_path)
        harness.bounds.fail = True
        assert harness.poll().state is WatchtowerPollState.READ_FAILED
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0

    def test_an_unreadable_verification_stands_down(self, tmp_path: Path) -> None:
        """A trip whose audited status read refuses records and retries."""
        harness = WatchtowerHarness(tmp_path, tick=BELOW_TICK, status=None)
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FIRED
        assert outcome.report is not None
        assert outcome.report.halted_reason.startswith("the defensive close stood down")
        assert "unreadable" in outcome.report.halted_reason
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0
        assert len(harness.alerts.triggers) == 1
        _, records = harness.audit_records()
        assert any(record.event_type is AuditEventType.CYCLE_REPORTED for record in records)

    def test_a_contradictory_owner_stands_down(self, tmp_path: Path) -> None:
        """A position held by neither the Safe nor the gauge never fires."""
        stranger = "0x9999999999999999999999999999999999999999"
        harness = WatchtowerHarness(tmp_path, tick=BELOW_TICK, status=status_report(owner=stranger))
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FIRED
        assert outcome.report is not None
        assert "contradictory" in outcome.report.halted_reason
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0

    def test_a_verification_back_in_range_stands_down_and_unlatches(self, tmp_path: Path) -> None:
        """The price returning before the close cancels the trip."""
        harness = WatchtowerHarness(
            tmp_path,
            tick=BELOW_TICK,
            status=status_report(range_state=PositionRangeState.IN_RANGE),
        )
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.STOOD_DOWN
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0
        assert harness.latch_store.load().tripped is False

    def test_an_empty_position_stands_down(self, tmp_path: Path) -> None:
        """A position with no liquidity has nothing to exit."""
        harness = WatchtowerHarness(tmp_path, tick=BELOW_TICK, status=status_report(liquidity=0))
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FIRED
        assert outcome.report is not None
        assert "empty" in outcome.report.halted_reason
        assert harness.executor.calls == []
        assert harness.key_loader.loads == 0

    def test_no_key_loads_before_any_trip(self, tmp_path: Path) -> None:
        """Calm polls and fail-safe polls never touch the key source."""
        harness = WatchtowerHarness(tmp_path)
        harness.poll()
        harness.ticks.fail = True
        harness.poll()
        harness.ticks.fail = False
        harness.ticks.tick = BELOW_TICK
        harness.statuses._status = None
        harness.poll()
        assert harness.key_loader.loads == 0

    def test_an_unavailable_signing_key_records_and_retries(self, tmp_path: Path) -> None:
        """A trip without its key alerts, records, and waits out cooldown."""

        class UnavailableKey(RecordingKeyLoader):
            """Refuse every load like a locked keychain."""

            def __call__(self) -> bytes:
                raise RuntimeError("the keychain is locked")

        harness = WatchtowerHarness(
            tmp_path,
            tick=BELOW_TICK,
            status=status_report(),
            key_loader=UnavailableKey(),
        )
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FIRED
        assert outcome.report is not None
        assert "the signing key is unavailable" in outcome.report.halted_reason
        assert harness.executor.calls == []
        assert harness.poll().state is WatchtowerPollState.COOLDOWN

    def test_a_mismatched_relayer_refuses_the_close(self, tmp_path: Path) -> None:
        """A configured relayer that disagrees with the key never fires."""
        harness = WatchtowerHarness(tmp_path, tick=BELOW_TICK, status=status_report())
        harness.watchtower._relayer_address = "0x9999999999999999999999999999999999999999"
        outcome = harness.poll()
        assert outcome.state is WatchtowerPollState.FIRED
        assert outcome.report is not None
        assert "does not match the signing key's address" in outcome.report.halted_reason
        assert harness.executor.calls == []


class TestExitInvocationShape:
    """The close drives the existing audited surfaces exactly."""

    def test_the_staked_close_runs_the_full_sequence(self, tmp_path: Path) -> None:
        """A staked position unstakes, withdraws, and swaps in order."""
        harness = WatchtowerHarness(
            tmp_path, tick=BELOW_TICK, status=status_report(owner=GAUGE_ADDRESS)
        )
        harness.poll()
        unstake, withdraw, exit_swap = harness.executor.calls
        assert unstake["args"] == ("FIXc", TRACKED_TOKEN_ID, KEY_BYTES)
        assert withdraw["args"] == ("FIXc", TRACKED_TOKEN_ID, KEY_BYTES)
        assert exit_swap["args"] == ("FIXc", KEY_BYTES)
        for call in harness.executor.calls:
            assert call["kwargs"] == {"confirm_broadcast": True}

    def test_the_unstaked_close_skips_the_unstake(self, tmp_path: Path) -> None:
        """A Safe-held position withdraws and swaps without unstaking."""
        harness = WatchtowerHarness(
            tmp_path, tick=BELOW_TICK, status=status_report(owner=SAFE_ADDRESS)
        )
        harness.poll()
        assert [call["action"] for call in harness.executor.calls] == ["withdraw", "exit_swap"]

    def test_the_trigger_report_carries_the_cycle_evidence(self, tmp_path: Path) -> None:
        """The report is one complete cycle-shaped story."""
        harness = WatchtowerHarness(
            tmp_path, tick=BELOW_TICK, status=status_report(owner=GAUGE_ADDRESS)
        )
        outcome = harness.poll()
        assert outcome.report is not None
        report = outcome.report
        assert report.mode.value == "live"
        assert report.decision_action == "defensive_exit"
        assert report.decision_reason == "watchtower_range_trip"
        assert report.reconciliation.tracked_token_id == TRACKED_TOKEN_ID
        assert report.reconciliation.tracked_staked is True
        assert report.pnl_vs_entry_usdc == Decimal("1")
        assert report.fee_wei == 270_000
        assert report.halted_reason == ""
        assert [action.action for action in report.actions] == ["unstake", "withdraw", "exit_swap"]
        assert all(action.status == "completed" for action in report.actions)
        assert any(
            "never gated by market windows or the reference quote" in note
            for note in report.input_notes
        )


class TestAlertDelivery:
    """The trigger email composes exactly like a cycle alert."""

    def test_provider_none_stays_silent(self) -> None:
        """No provider configured means no email and no crash."""
        from test_alerts import calm_report

        report = calm_report()
        assert (
            deliver_watchtower_trigger(report, "pool tick -1 left the range", environ={}) is False
        )
        assert deliver_watchtower_notice("FIXc unreadable", "body", environ={}) is False

    def test_the_trigger_email_leads_with_the_trip_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trip line fronts the alert set under the ALERT subject."""
        from test_alerts import calm_report

        transport = FakeTransport()
        monkeypatch.setattr("aero_bot.watchtower.build_alert_transport", lambda config: transport)
        trip_line = "pool tick -11640 is below the position range [-11630, -11610)"
        assert deliver_watchtower_trigger(calm_report(), trip_line, environ=SMTP_ENV) is True
        assert len(transport.sent) == 1
        subject, body = transport.sent[0]
        assert subject.startswith("[aero-bot][ALERT]")
        assert "watchtower range trip" in subject
        assert "! watchtower range trip: " + trip_line in body

    def test_the_notice_email_carries_the_shared_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-safe notices render under the same ALERT subject."""
        transport = FakeTransport()
        monkeypatch.setattr("aero_bot.watchtower.build_alert_transport", lambda config: transport)
        assert (
            deliver_watchtower_notice("FIXc watchtower unreadable", "body line", environ=SMTP_ENV)
            is True
        )
        subject, body = transport.sent[0]
        assert subject == "[aero-bot][ALERT] FIXc watchtower unreadable"
        assert body == "body line"


class TestSystemdUnitContract:
    """The unit mirrors the dashboard's hardened long-running contract."""

    def test_the_watchtower_unit_is_hardened_and_dark(self) -> None:
        """The service ships hardened, sealed, and restart-on-failure."""
        unit = Path("deploy/systemd/aero-bot-watchtower@.service").read_text(encoding="utf-8")
        assert "Type=simple" in unit
        assert "User=aero-bot" in unit
        assert "Group=aero-bot" in unit
        assert "EnvironmentFile=/etc/aero-bot/cycle.env" in unit
        assert "ExecStart=/opt/aero-bot/.venv/bin/aero-bot-watchtower --symbol %i" in unit
        assert "Restart=on-failure" in unit
        assert "RestartSec=10" in unit
        assert "NoNewPrivileges=true" in unit
        assert "ProtectSystem=strict" in unit
        assert "ProtectHome=true" in unit
        assert "PrivateTmp=true" in unit
        assert "ReadWritePaths=/var/lib/aero-bot" in unit
        assert "StateDirectory=aero-bot" in unit

    def test_the_watchtower_doc_carries_the_operating_contract(self) -> None:
        """Semantics, config, arming, and triage are all documented."""
        doc = Path("docs/watchtower.md").read_text(encoding="utf-8")
        for anchor in (
            "never gated by market windows",
            "AERO_BOT_WATCHTOWER_ENABLED",
            "AERO_BOT_WATCHTOWER_POLL_SECONDS",
            "AERO_BOT_WATCHTOWER_COOLDOWN_SECONDS",
            "Arming",
            "Day-two triage",
        ):
            assert anchor in doc, anchor


class TestCommandLine:
    """The CLI stays dark until the sealed environment arms it."""

    def test_a_disabled_watchtower_exits_clean_without_building(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dark means exit zero with no boundaries constructed."""
        monkeypatch.delenv(WATCHTOWER_ENABLED_ENV, raising=False)

        def fail_build(*args: object, **kwargs: object) -> None:
            raise AssertionError("a disabled watchtower must build nothing")

        monkeypatch.setattr("aero_bot.watchtower.build_watchtower", fail_build)
        assert main(["--symbol", "FIXc"]) == 0

    def test_an_invalid_configuration_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Malformed seconds name their variable and fail closed."""
        monkeypatch.setenv(WATCHTOWER_POLL_SECONDS_ENV, "zero")
        assert main(["--symbol", "FIXc"]) == 1
        assert WATCHTOWER_POLL_SECONDS_ENV in capsys.readouterr().err

    def test_non_positive_overrides_are_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The interval and cooldown flags validate like their variables."""
        monkeypatch.delenv(WATCHTOWER_ENABLED_ENV, raising=False)
        with pytest.raises(SystemExit) as raised:
            main(["--symbol", "FIXc", "--poll-seconds", "0"])
        assert raised.value.code == 2
        with pytest.raises(SystemExit) as raised:
            main(["--symbol", "FIXc", "--cooldown-seconds", "-1"])
        assert raised.value.code == 2

    def test_the_symbol_is_required(self) -> None:
        """No symbol, no watcher."""
        with pytest.raises(SystemExit) as raised:
            main([])
        assert raised.value.code == 2
