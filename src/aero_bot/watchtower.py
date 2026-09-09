"""The range watchtower: seconds-level defensive exits on range trips.

The scheduled cycle decides on an hourly timer, but a concentrated-liquidity
position stops earning the moment the pool tick leaves its range. The
``aero-bot-watchtower`` command is the always-on complement: one tiny
long-running process per symbol that polls the tracked pool's current tick
through the fast-path view set - one cheap block-pinned ``slot0`` read -
every few seconds and compares it against the tracked position's range
bounds read from the pool's own NFPM.

On a hard trip - the tick out of range on either side, inclusive lower and
exclusive upper, the same convention the policy engine and executor use -
the watchtower fires the defensive close immediately through the existing
audited exit surfaces (unstake when staked, withdraw, exit swap): the close
is never gated by market windows or the reference quote, because those gate
entries only. Every trigger appends the same ``cycle_reported`` audit
record a cycle does and delivers the same alert email.

The fail-safe posture is absolute: unreadable or contradictory state never
fires an exit - it alerts and retries on the next poll. The watcher is
read-only until a genuine trip, loads no signing key until a trip demands
the close, and refuses to fire until the audited status read confirms the
trip live (custody in the Safe or the gauge, the same range bounds, out of
range, liquidity to exit). One cheap poll trips; one verified read
authorizes; one audited sequence acts.

Discipline:

- **One-shot latch per trip.** A trip latches; the latch resets only after
  a successful reconcile - a later poll observing the position back in
  range, the tracked position gone flat, or the close completing.
- **Cooldown.** Every close attempt stamps ``last_fired_at``; a new
  attempt waits out the cooldown window so a refusing or failing close can
  never hammer the chain in a loop.
- **Dark by default.** The enable flag defaults off and the systemd unit
  ships installed but not enabled; arming is the deploy operator's
  explicit two-step (see ``docs/watchtower.md``).
"""

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol, TextIO

from pydantic import BaseModel, Field

from aero_bot.alerts import (
    AlertTransportError,
    build_alert_transport,
    compose_cycle_email,
    evaluate_alerts,
    parse_alert_config,
)
from aero_bot.audit import AuditStore
from aero_bot.concentrated import PositionRangeState
from aero_bot.config import Settings
from aero_bot.cycle import (
    RELAYER_ADDRESS_ENV,
    CycleActionRecord,
    CycleBalanceBoundary,
    CycleExecutorBoundary,
    CycleMode,
    CycleReadBoundary,
    CycleReconciliation,
    CycleReport,
    CycleStateBook,
    CycleStateStore,
    TrackedPosition,
    _action_completed,
    _action_hashes_and_fees,
    _print_report,
    record_cycle_report,
)
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, normalize_evm_address
from aero_bot.executor import (
    DEFAULT_CANARY_SAFE_ADDRESS,
    SAFE_ADDRESS_ENV,
    ExecutionUnavailableError,
)
from aero_bot.lp_calldata import (
    LpPositionView,
    build_gauge_factory_nft_read_calldata,
    build_gauge_gauge_factory_read_calldata,
    build_lp_positions_read_calldata,
    build_pool_gauge_read_calldata,
    build_pool_slot0_read_calldata,
    decode_address_view_result,
    decode_lp_positions_view,
    decode_pool_slot0_view,
)
from aero_bot.lp_executor import (
    EXIT_FAILURE,
    EXIT_OK,
    LpActionExecutionReport,
    LpExecutionRefusalError,
    LpPositionStatusReport,
)
from aero_bot.lp_plan import LpPlanRefusalError, position_range_state
from aero_bot.policy import evaluate_event_window, load_event_calendar
from aero_bot.venues import BASE_USDC_ADDRESS

# Environment variable selecting whether the watchtower is armed at all.
WATCHTOWER_ENABLED_ENV = "AERO_BOT_WATCHTOWER_ENABLED"
# Environment variable carrying the poll interval in seconds.
WATCHTOWER_POLL_SECONDS_ENV = "AERO_BOT_WATCHTOWER_POLL_SECONDS"
# Environment variable carrying the close-attempt cooldown in seconds.
WATCHTOWER_COOLDOWN_SECONDS_ENV = "AERO_BOT_WATCHTOWER_COOLDOWN_SECONDS"
# Environment variable carrying an explicit latch-state path override.
WATCHTOWER_STATE_PATH_ENV = "AERO_BOT_WATCHTOWER_STATE_PATH"
# The default poll interval: one cheap read every five seconds.
DEFAULT_WATCHTOWER_POLL_SECONDS = 5.0
# The default cooldown: fifteen minutes between close attempts.
DEFAULT_WATCHTOWER_COOLDOWN_SECONDS = 900.0
# The block tag every cheap read pins to; one read is one coherent snapshot.
LATEST_BLOCK_TAG = "latest"
# The alert subject prefix every watchtower notice shares with the cycle.
_WATCHTOWER_SUBJECT_PREFIX = "[aero-bot][ALERT]"
# The subject cap alerts.py applies; shared for identical truncation.
_SUBJECT_CAP = 120


def _watchtower_progress(line: str) -> None:
    """Print one operator progress line on stderr.

    Args:
        line: One human-readable progress line from the long-running loop.
    """
    print(f"[aero-bot-watchtower] {line}", file=sys.stderr)


class WatchtowerConfig(BaseModel):
    """Carry the environment-driven watchtower configuration."""

    # Frozen strict fields keep one configuration coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Whether the watchtower is armed; dark until explicitly enabled.
    enabled: bool = False
    # Seconds between cheap tick polls.
    poll_interval_seconds: Annotated[float, Field(gt=0)] = DEFAULT_WATCHTOWER_POLL_SECONDS
    # Seconds every close attempt waits before another may fire.
    cooldown_seconds: Annotated[float, Field(gt=0)] = DEFAULT_WATCHTOWER_COOLDOWN_SECONDS


def _parse_seconds(value: str, variable: str) -> float:
    """Parse one strictly positive seconds value, naming its variable."""
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{variable} must be a number of seconds, not {value!r}") from error
    if parsed <= 0:
        raise ValueError(f"{variable} must be positive, not {value!r}")
    return parsed


def parse_watchtower_config(
    environ: Mapping[str, str] | None = None,
) -> WatchtowerConfig:
    """Build the watchtower configuration from the sealed environment.

    Args:
        environ: The environment mapping carrying the sealed values; None
            reads the live process environment.

    Returns:
        The validated configuration; disabled until the flag is set.

    Raises:
        ValueError: If any value is malformed; the message names the
            variable, never a secret.
    """
    resolved = os.environ if environ is None else environ
    raw_enabled = resolved.get(WATCHTOWER_ENABLED_ENV, "").strip().lower()
    raw_poll = resolved.get(WATCHTOWER_POLL_SECONDS_ENV, "").strip()
    raw_cooldown = resolved.get(WATCHTOWER_COOLDOWN_SECONDS_ENV, "").strip()
    return WatchtowerConfig(
        enabled=raw_enabled in {"1", "true", "yes", "on"},
        poll_interval_seconds=(
            _parse_seconds(raw_poll, WATCHTOWER_POLL_SECONDS_ENV) if raw_poll else None
        )
        or DEFAULT_WATCHTOWER_POLL_SECONDS,
        cooldown_seconds=(
            _parse_seconds(raw_cooldown, WATCHTOWER_COOLDOWN_SECONDS_ENV) if raw_cooldown else None
        )
        or DEFAULT_WATCHTOWER_COOLDOWN_SECONDS,
    )


class WatchtowerTickReader(Protocol):
    """Define the one-read tick surface the watchtower polls through."""

    def read_current_tick(self, pool_address: str) -> int:
        """Read the pool's current signed tick in one block-pinned read."""
        ...


class WatchtowerBoundsReader(Protocol):
    """Define the position-bounds surface the watchtower compares against."""

    def read_position_bounds(self, nfpm_address: str, token_id: int) -> LpPositionView:
        """Read one position NFT's immutable twelve-word view."""
        ...


class ExecutorRpcCallBoundary(Protocol):
    """Define the bounded read-only RPC surface the live reads consume."""

    def eth_call_at(self, to_address: str, calldata: str, block_tag: str) -> str:
        """Perform one read-only eth_call pinned to an explicit block tag."""
        ...


class RpcWatchtowerReads:
    """Serve the watchtower's cheap reads from one RPC backend.

    Every read is one ``eth_call`` pinned to ``latest`` through the same
    bounded backend the executor's fast path uses, so the poll costs one
    request per interval and inherits the backend's retry and politeness
    discipline.
    """

    def __init__(self, rpc: ExecutorRpcCallBoundary) -> None:
        """Bind the reads to one RPC backend.

        Args:
            rpc: The bounded read-only RPC backend.
        """
        self._rpc = rpc

    def read_current_tick(self, pool_address: str) -> int:
        """Read the pool's current tick from its slot0 view.

        Args:
            pool_address: The pool contract whose tick is polled.

        Returns:
            The pool's live signed tick.

        Raises:
            ExecutionUnavailableError: If the read cannot complete.
            ValueError: If the return is malformed.
        """
        _, current_tick = decode_pool_slot0_view(
            self._rpc.eth_call_at(pool_address, build_pool_slot0_read_calldata(), LATEST_BLOCK_TAG)
        )
        return current_tick

    def read_position_bounds(self, nfpm_address: str, token_id: int) -> LpPositionView:
        """Read one position NFT's immutable view from the NFPM.

        Args:
            nfpm_address: The pool's NonfungiblePositionManager.
            token_id: The position NFT whose range bounds are read.

        Returns:
            The decoded twelve-word position view.

        Raises:
            ExecutionUnavailableError: If the read cannot complete.
            ValueError: If the token is unknown or the return is malformed.
        """
        return decode_lp_positions_view(
            self._rpc.eth_call_at(
                nfpm_address, build_lp_positions_read_calldata(token_id), LATEST_BLOCK_TAG
            )
        )

    def resolve_nfpm_address(self, pool_address: str) -> str:
        """Derive the pool's NFPM through the pool's own contract chain.

        The pool names its gauge, the gauge names its factory, and the
        factory names the NFPM - the same verified view chain the
        known-pool fast path re-verifies identity through.

        Args:
            pool_address: The pool contract whose NFPM is resolved.

        Returns:
            The NFPM address the pool's own contracts name.

        Raises:
            ExecutionUnavailableError: If any read cannot complete.
            ValueError: If any return is malformed.
        """
        gauge = decode_address_view_result(
            self._rpc.eth_call_at(pool_address, build_pool_gauge_read_calldata(), LATEST_BLOCK_TAG)
        )
        gauge_factory = decode_address_view_result(
            self._rpc.eth_call_at(
                gauge, build_gauge_gauge_factory_read_calldata(), LATEST_BLOCK_TAG
            )
        )
        return decode_address_view_result(
            self._rpc.eth_call_at(
                gauge_factory, build_gauge_factory_nft_read_calldata(), LATEST_BLOCK_TAG
            )
        )


class WatchtowerLatchBook(BaseModel):
    """Carry the watchtower's trip latch and cooldown memory."""

    # Frozen strict fields keep one latch book coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Whether a trip is currently latched awaiting resolution.
    tripped: bool = False
    # When the last close attempt fired; the cooldown anchor.
    last_fired_at: datetime | None = None
    # When the last unreadable-state notice emailed; the notice throttle.
    last_read_alert_at: datetime | None = None
    # When this book was last persisted, timezone-aware.
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WatchtowerLatchStore:
    """Persist the latch book as one self-healing JSON file.

    The store mirrors the cycle-book store's discipline: any load failure
    yields a fresh unlatched book (a lost latch costs at most one extra
    close attempt, still bounded by the persisted cooldown when readable),
    and every save is an atomic rewrite so a crash can never tear it.
    """

    def __init__(self, path: Path) -> None:
        """Point the store at one JSON path.

        Args:
            path: Absolute path of the latch book file.
        """
        self._path = path.expanduser()

    @property
    def path(self) -> Path:
        """Return the book path; paths are not secrets."""
        return self._path

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] = os.environ, settings: Settings | None = None
    ) -> "WatchtowerLatchStore":
        """Build the store from the environment and application settings.

        Args:
            environ: Environment mapping carrying the optional path override.
            settings: Application settings; None constructs fresh ones.

        Returns:
            The store rooted at the override or beside the audit store.
        """
        resolved = settings if settings is not None else Settings()
        override = environ.get(WATCHTOWER_STATE_PATH_ENV, "").strip()
        base = (
            Path(override)
            if override
            else resolved.audit_database_path.parent / "watchtower_state.json"
        )
        return cls(base)

    def load(self) -> WatchtowerLatchBook:
        """Load the book, yielding a fresh unlatched book on any failure.

        Returns:
            The persisted book, or the dark default when nothing readable
            exists.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return WatchtowerLatchBook()
        try:
            return WatchtowerLatchBook.model_validate(raw)
        except ValueError:
            return WatchtowerLatchBook()

    def save(self, book: WatchtowerLatchBook) -> None:
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


class WatchtowerPollState(StrEnum):
    """Classify one poll's outcome for the journal and the tests."""

    # No tracked position; nothing to defend.
    FLAT = "flat"
    # The tracked position is inside its range; the doctrine no-op.
    IN_RANGE = "in_range"
    # A trip stands but the cooldown suppresses the retry attempt.
    COOLDOWN = "cooldown"
    # A close attempt ran; the carried report holds the outcome.
    FIRED = "fired"
    # A cheap read failed; fail-safe, no fire, retry next poll.
    READ_FAILED = "read_failed"
    # The verified read stood the trip down; no fire.
    STOOD_DOWN = "stood_down"


class WatchtowerPollOutcome(BaseModel):
    """Carry one poll's structured result."""

    # Frozen strict fields keep one outcome coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The poll's classification.
    state: WatchtowerPollState
    # The signed tick the poll observed, when one was read.
    observed_tick: int | None = None
    # The trigger's complete cycle report when a close attempt ran.
    report: CycleReport | None = None
    # One human-readable evidence line.
    note: str = ""


class RangeWatchtower:
    """Run the range watchtower loop over every injectable boundary."""

    def __init__(
        self,
        symbol: str,
        safe_address: str,
        relayer_address: str | None,
        config: WatchtowerConfig,
        tick_reader: WatchtowerTickReader,
        bounds_reader: WatchtowerBoundsReader,
        nfpm_resolver: Callable[[str], str],
        reads: CycleReadBoundary,
        balances: CycleBalanceBoundary,
        executor: CycleExecutorBoundary,
        key_loader: Callable[[], bytes],
        audit_sink: AuditStore | None,
        state_store: CycleStateStore,
        latch_store: WatchtowerLatchStore,
        stock_decimals: int | None = None,
        deliver_trigger: Callable[[CycleReport, str], None] | None = None,
        deliver_notice: Callable[[str, str], None] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] = _watchtower_progress,
    ) -> None:
        """Configure one watchtower over every injectable boundary.

        Args:
            symbol: The registry-matched symbol this watchtower defends.
            safe_address: The Safe whose tracked position is defended.
            relayer_address: The relaying EOA's public address, or None.
            config: The poll interval, cooldown, and enable flag.
            tick_reader: The one-read tick surface being polled.
            bounds_reader: The position-bounds surface being compared
                against.
            nfpm_resolver: Derives one pool's NFPM address through the
                pool's own contract chain.
            reads: The audited read-only position surface verifying trips.
            balances: The RPC balance surface backing the trigger report.
            executor: The audited execution surface closing positions.
            key_loader: Loads the signing key; never called before a
                verified trip demands the close.
            audit_sink: The store receiving the trigger's cycle-summary
                record; None records nothing.
            state_store: The cycle book store the close reconciles.
            latch_store: The latch book store persisting trip memory.
            stock_decimals: The stock token's decimals for report
                quantities, from the pool pin when present.
            deliver_trigger: Delivers one trigger's alert email; the
                default wires the sealed alert configuration.
            deliver_notice: Delivers one plain fail-safe notice email.
            now: Injected clock producing timezone-aware instants.
            sleep: Injected interval sleep.
            progress: Injected stderr progress line sink.
        """
        self._symbol = symbol
        self._safe_address = normalize_evm_address(safe_address)
        self._relayer_address = normalize_evm_address(relayer_address) if relayer_address else None
        self._config = config
        self._tick_reader = tick_reader
        self._bounds_reader = bounds_reader
        self._nfpm_resolver = nfpm_resolver
        self._reads = reads
        self._balances = balances
        self._executor = executor
        self._key_loader = key_loader
        self._audit_sink = audit_sink
        self._state_store = state_store
        self._latch_store = latch_store
        self._stock_decimals = stock_decimals
        self._deliver_trigger = deliver_trigger or (
            lambda report, trip: deliver_watchtower_trigger(report, trip)
        )
        self._deliver_notice = deliver_notice or (
            lambda subject, body: deliver_watchtower_notice(subject, body)
        )
        self._now = now
        self._sleep = sleep
        self._progress = progress
        self._bounds_cache: dict[int, LpPositionView] = {}
        self._nfpm_by_pool: dict[str, str] = {}
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Ask the loop to exit cleanly after the current poll."""
        self._stop.set()

    def run(self, max_polls: int | None = None) -> None:
        """Run the poll loop until stopped or bounded.

        Args:
            max_polls: Bound the run for smoke checks and tests; None
                runs until a stop is requested.
        """
        completed = 0
        while max_polls is None or completed < max_polls:
            if self._stop.is_set():
                break
            self.poll_once()
            completed += 1
            if max_polls is not None and completed >= max_polls:
                break
            self._sleep(self._config.poll_interval_seconds)

    def poll_once(self) -> WatchtowerPollOutcome:
        """Run exactly one poll: read, compare, and maybe fire.

        Returns:
            The structured outcome of one poll.
        """
        book = self._state_store.load()
        tracked = book.position
        latch = self._latch_store.load()
        if tracked is None:
            if latch.tripped:
                self._save_latch(tripped=False, note="unlatched: no tracked position remains")
            return WatchtowerPollOutcome(state=WatchtowerPollState.FLAT)
        bounds = self._bounds_for(tracked)
        if bounds is None:
            return self._unreadable(
                tracked,
                f"the tracked position {tracked.token_id} bounds are unreadable; no exit fired",
            )
        try:
            tick = self._tick_reader.read_current_tick(tracked.pool_address)
        except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
            return self._unreadable(tracked, f"the pool tick read failed: {error}; no exit fired")
        trip_state = position_range_state(bounds.tick_lower, bounds.tick_upper, tick)
        if trip_state is PositionRangeState.IN_RANGE:
            if latch.tripped:
                self._save_latch(
                    tripped=False,
                    note=f"unlatched: tick {tick} reconciled back inside "
                    f"[{bounds.tick_lower}, {bounds.tick_upper})",
                )
            return WatchtowerPollOutcome(state=WatchtowerPollState.IN_RANGE, observed_tick=tick)
        if not latch.tripped:
            self._save_latch(
                tripped=True,
                note=f"range trip latched: tick {tick} is {trip_state.value} the position "
                f"range [{bounds.tick_lower}, {bounds.tick_upper})",
            )
        now = self._now()
        if latch.last_fired_at is not None and (
            (now - latch.last_fired_at).total_seconds() < self._config.cooldown_seconds
        ):
            return WatchtowerPollOutcome(
                state=WatchtowerPollState.COOLDOWN,
                observed_tick=tick,
                note="a trip stands but the cooldown suppresses the retry",
            )
        return self._fire(book, tracked, bounds, tick, trip_state)

    # ------------------------------------------------------------------
    # The defensive close
    # ------------------------------------------------------------------

    def _fire(  # noqa: PLR0915 - one fixed close order, explicit branches
        self,
        book: CycleStateBook,
        tracked: TrackedPosition,
        bounds: LpPositionView,
        tick: int,
        trip_state: PositionRangeState,
    ) -> WatchtowerPollOutcome:
        """Verify one trip live, then close through the audited surfaces.

        Args:
            book: The cycle book whose tracked position is closing.
            tracked: The tracked position being closed.
            bounds: The position's immutable range bounds.
            tick: The polled tick that tripped.
            trip_state: Which side of the range the tick left.

        Returns:
            The trigger outcome carrying its complete cycle report.
        """
        started_at = self._now()
        lower, upper = bounds.tick_lower, bounds.tick_upper
        if trip_state is PositionRangeState.BELOW_RANGE:
            trip_line = f"pool tick {tick} is below the position range [{lower}, {upper})"
        else:
            trip_line = f"pool tick {tick} is at or above the position range [{lower}, {upper})"
        self._progress(f"hard trip: {trip_line}; attempting the defensive close")
        # The attempt stamp lands before anything irreversible: a crash
        # mid-close must restart inside the cooldown, never re-fire at once.
        latch = self._latch_store.load()
        self._latch_store.save(
            latch.model_copy(
                update={"tripped": True, "last_fired_at": started_at, "updated_at": started_at}
            )
        )

        def finish(
            actions: list[CycleActionRecord],
            halted_reason: str,
            status: LpPositionStatusReport | None,
        ) -> WatchtowerPollOutcome:
            """Assemble, record, and deliver one trigger's report."""
            report = self._assemble_report(
                started_at, tracked, status, bounds, tick, trip_line, tuple(actions), halted_reason
            )
            if self._audit_sink is not None:
                record_cycle_report(self._audit_sink, report, self._now())
            _print_report(report)
            self._deliver_trigger(report, trip_line)
            return WatchtowerPollOutcome(
                state=WatchtowerPollState.FIRED, observed_tick=tick, report=report
            )

        status, refusal = self._verify(tracked, bounds)
        actions: list[CycleActionRecord] = []
        if status is None or refusal:
            if refusal == "back_in_range":
                self._save_latch(
                    tripped=False,
                    note="unlatched: the verified read reconciled the position back in range",
                )
                return WatchtowerPollOutcome(
                    state=WatchtowerPollState.STOOD_DOWN,
                    observed_tick=tick,
                    note="the verified read reconciled the position back in range; no exit fired",
                )
            return finish(
                actions, f"the defensive close stood down: {refusal or 'unverified'}", status
            )

        # The key loads only now, after the verified trip demanded the close.
        try:
            key_bytes = self._key_loader()
        except (RuntimeError, ValueError) as error:
            return finish(actions, f"the signing key is unavailable: {error}", status)

        from eth_account import Account

        derived_relayer = normalize_evm_address(Account.from_key(key_bytes).address)
        if self._relayer_address is not None and self._relayer_address != derived_relayer:
            return finish(
                actions,
                f"the configured relayer {self._relayer_address} does not match the signing "
                f"key's address {derived_relayer}; refusing the defensive close",
                status,
            )

        staked = normalize_evm_address(status.token_owner_address) == normalize_evm_address(
            status.gauge_address
        )
        halted = ""

        def run(name: str, call: Callable[[], LpActionExecutionReport]) -> bool:
            """Run one audited close step, recording its outcome."""
            nonlocal halted
            try:
                report = call()
            except (LpExecutionRefusalError, LpPlanRefusalError) as error:
                code = str(getattr(error, "code", "plan_refused"))
                actions.append(
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
                actions.append(
                    CycleActionRecord(action=name, status="failed", diagnostic=str(error))
                )
                halted = f"the {name} action failed: {error}"
                return False
            hashes, fees = _action_hashes_and_fees(report)
            actions.append(
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

        symbol = self._symbol
        if staked and not run(
            "unstake",
            lambda: self._executor.execute_unstake(
                symbol, tracked.token_id, key_bytes, confirm_broadcast=True
            ),
        ):
            return finish(actions, halted, status)
        if not run(
            "withdraw",
            lambda: self._executor.execute_withdraw(
                symbol, tracked.token_id, key_bytes, confirm_broadcast=True
            ),
        ):
            return finish(actions, halted, status)
        if not run(
            "exit_swap",
            lambda: self._executor.execute_exit_swap(symbol, key_bytes, confirm_broadcast=True),
        ):
            return finish(actions, halted, status)
        # The close completed: reconcile the book exactly like the cycle's
        # exit completion and reset the latch through that success.
        self._state_store.save(book.model_copy(update={"position": None, "held_inventory": None}))
        self._save_latch(tripped=False, note="unlatched: the defensive close completed")
        return finish(actions, "", status)

    def _verify(
        self, tracked: TrackedPosition, bounds: LpPositionView
    ) -> tuple[LpPositionStatusReport | None, str]:
        """Confirm one trip through the audited status read before firing.

        Args:
            tracked: The tracked position being verified.
            bounds: The polled immutable range bounds.

        Returns:
            The verified status report with an empty refusal when the trip
            stands, else None or a stood-down status with the refusal
            reason: unreadable, contradictory, back_in_range, or empty.
        """
        try:
            status = self._reads.position_status(
                self._symbol, tracked.token_id, entry_cost_usdc=tracked.committed_usd
            )
        except (
            LpExecutionRefusalError,
            ExecutionUnavailableError,
            ValueError,
            RuntimeError,
        ) as error:
            return None, f"unreadable ({error})"
        owner = normalize_evm_address(status.token_owner_address)
        gauge = normalize_evm_address(status.gauge_address)
        if owner != self._safe_address and owner != gauge:
            return None, (
                f"contradictory (tracked position {tracked.token_id} is owned by {owner}, "
                "which is neither the Safe nor the pool's gauge)"
            )
        if (status.position.tick_lower, status.position.tick_upper) != (
            bounds.tick_lower,
            bounds.tick_upper,
        ):
            return None, "contradictory (the verified range disagrees with the polled bounds)"
        if status.range_state is PositionRangeState.IN_RANGE:
            return status, "back_in_range"
        if status.position.liquidity <= 0:
            return status, "empty (the position carries no liquidity to exit)"
        return status, ""

    # ------------------------------------------------------------------
    # Reads and state
    # ------------------------------------------------------------------

    def _bounds_for(self, tracked: TrackedPosition) -> LpPositionView | None:
        """Read and cache one tracked position's immutable bounds.

        Args:
            tracked: The tracked position whose bounds are read.

        Returns:
            The position view, or None while unreadable - never a guess.
        """
        if tracked.token_id in self._bounds_cache:
            return self._bounds_cache[tracked.token_id]
        nfpm = self._nfpm_by_pool.get(tracked.pool_address)
        if nfpm is None:
            try:
                nfpm = self._nfpm_resolver(tracked.pool_address)
            except (ExecutionUnavailableError, ValueError, RuntimeError):
                return None
            self._nfpm_by_pool[tracked.pool_address] = nfpm
        try:
            view = self._bounds_reader.read_position_bounds(nfpm, tracked.token_id)
        except (ExecutionUnavailableError, ValueError, RuntimeError):
            return None
        self._bounds_cache[tracked.token_id] = view
        return view

    def _unreadable(self, tracked: TrackedPosition, message: str) -> WatchtowerPollOutcome:
        """Handle one unreadable poll: alert throttled, never fire.

        Args:
            tracked: The tracked position whose read failed.
            message: The honest failure line.

        Returns:
            The fail-safe READ_FAILED outcome.
        """
        self._progress(message)
        now = self._now()
        latch = self._latch_store.load()
        if latch.last_read_alert_at is not None and (
            (now - latch.last_read_alert_at).total_seconds() < self._config.cooldown_seconds
        ):
            return WatchtowerPollOutcome(state=WatchtowerPollState.READ_FAILED, note=message)
        self._latch_store.save(
            latch.model_copy(update={"last_read_alert_at": now, "updated_at": now})
        )
        subject = f"{self._symbol} watchtower unreadable - no exit fired"
        body = "\n".join(
            (
                f"Aero Bot range watchtower - {self._symbol}",
                "",
                f"  {message}",
                "",
                "The watchtower never fires an exit on unreadable state;",
                f"it retries every {self._config.poll_interval_seconds:g} seconds and emails",
                f"at most once per {self._config.cooldown_seconds:g}-second cooldown window.",
                "",
            )
        )
        self._deliver_notice(subject, body)
        return WatchtowerPollOutcome(state=WatchtowerPollState.READ_FAILED, note=message)

    def _save_latch(self, *, tripped: bool, note: str) -> None:
        """Persist one latch transition and report it.

        Args:
            tripped: The new latch state.
            note: The transition's evidence line.
        """
        now = self._now()
        latch = self._latch_store.load()
        self._latch_store.save(latch.model_copy(update={"tripped": tripped, "updated_at": now}))
        self._progress(note)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _stock_address(self, status: LpPositionStatusReport | None) -> str:
        """Derive the stock token address from the verified pool tokens."""
        if status is None:
            return ""
        token0 = str(getattr(status.position, "token0_address", "") or "")
        token1 = str(getattr(status.position, "token1_address", "") or "")
        if token0 and token0.lower() != BASE_USDC_ADDRESS.lower():
            return token0
        if token1 and token1.lower() != BASE_USDC_ADDRESS.lower():
            return token1
        return ""

    def _assemble_report(  # noqa: PLR0913 - one complete evidence assembly
        self,
        started_at: datetime,
        tracked: TrackedPosition,
        status: LpPositionStatusReport | None,
        bounds: LpPositionView,
        tick: int,
        trip_line: str,
        actions: tuple[CycleActionRecord, ...],
        halted_reason: str,
    ) -> CycleReport:
        """Assemble one trigger's complete cycle-shaped report.

        Args:
            started_at: When the trigger fired.
            tracked: The tracked position being closed.
            status: The verified status read, when one completed.
            bounds: The position's immutable range bounds.
            tick: The polled tick that tripped.
            trip_line: The trip's evidence line.
            actions: Every close action record, in order.
            halted_reason: Empty when the close completed.

        Returns:
            The complete report the audit chain and email carry.
        """
        diagnostics: list[str] = []
        safe_usdc = 0
        relayer_eth = 0
        stock_units = 0
        try:
            safe_usdc = self._balances.fetch_token_balance(BASE_USDC_ADDRESS, self._safe_address)
        except (ExecutionUnavailableError, RuntimeError, ValueError) as error:
            diagnostics.append(f"Safe USDC unread: {error}")
        stock_address = self._stock_address(status)
        if stock_address:
            try:
                stock_units = self._balances.fetch_token_balance(stock_address, self._safe_address)
            except (ExecutionUnavailableError, RuntimeError, ValueError) as error:
                diagnostics.append(f"Safe stock unread: {error}")
        if self._relayer_address is not None:
            try:
                relayer_eth = self._balances.fetch_eth_balance(self._relayer_address)
            except (ExecutionUnavailableError, RuntimeError, ValueError) as error:
                diagnostics.append(f"relayer ETH unread: {error}")
        else:
            diagnostics.append("relayer ETH unread: no relayer address configured for this run")
        if stock_units and self._stock_decimals is None:
            diagnostics.append("stock quantity unread: no pool pin carrying stock decimals")
        held_quantity = (
            Decimal(stock_units).scaleb(-self._stock_decimals)
            if self._stock_decimals is not None
            else Decimal("0")
        )
        staked = False
        if status is not None:
            staked = normalize_evm_address(status.token_owner_address) == normalize_evm_address(
                status.gauge_address
            )
            diagnostics.append(
                f"tracked position {tracked.token_id} "
                f"({'staked in the gauge' if staked else 'held in the Safe'}) valued "
                f"{status.position_value_usdc} USDC against {tracked.committed_usd} committed"
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
        reconciliation = CycleReconciliation(
            symbol=self._symbol,
            safe_usdc_units=safe_usdc,
            relayer_eth_wei=relayer_eth,
            safe_stock_units=stock_units,
            inventory_live_token_ids=(),
            inventory_empty_count=0,
            tracked_status=status,
            tracked_token_id=tracked.token_id,
            tracked_staked=staked,
            held_stock_quantity=held_quantity,
            out_of_band="",
            diagnostics=tuple(diagnostics),
        )
        decision_diagnostics: list[str] = [
            f"watchtower range trip: {trip_line}",
            f"the position's immutable bounds came from the pool's NFPM view: "
            f"[{bounds.tick_lower}, {bounds.tick_upper})",
        ]
        if status is not None:
            decision_diagnostics.append(
                f"the audited status read at block {status.snapshot_block} verified the trip "
                f"with range state {status.range_state.value}"
            )
        event_window = evaluate_event_window(
            started_at, stock_address, load_event_calendar()
        ).description
        pnl = status.unrealized_pnl_usdc if status is not None else None
        pnl_diagnostic = status.pnl_diagnostic if status is not None else "no verified status"
        return CycleReport(
            started_at=started_at,
            mode=CycleMode.LIVE,
            symbol=self._symbol,
            reconciliation=reconciliation,
            decision_action="defensive_exit",
            decision_reason="watchtower_range_trip",
            decision_diagnostics=tuple(decision_diagnostics),
            event_window=event_window,
            actions=actions,
            pnl_vs_entry_usdc=pnl,
            pnl_diagnostic=pnl_diagnostic if pnl is None else "",
            fee_wei=sum(action.fee_wei for action in actions),
            halted_reason=halted_reason,
            input_notes=(
                "the watchtower's defensive close is never gated by market windows or the "
                "reference quote; those gate entries only",
                f"poll interval {self._config.poll_interval_seconds:g} seconds, cooldown "
                f"{self._config.cooldown_seconds:g} seconds",
                "the hourly cycle owns the full inventory reconciliation; this report "
                "reconciles only the tracked position",
            ),
        )


def deliver_watchtower_trigger(
    report: CycleReport,
    trip_line: str,
    environ: Mapping[str, str] | None = None,
    error_stream: TextIO | None = None,
) -> bool:
    """Deliver one trigger's email exactly like a cycle alert.

    The trip line leads the alert set; every alert the report itself
    deserves follows, and the body is the cycle email composition.

    Args:
        report: The trigger's complete cycle report.
        trip_line: The trip's evidence line.
        environ: The environment carrying the alert configuration; None
            reads the live process environment.
        error_stream: Where delivery warnings land; stderr by default.

    Returns:
        Whether an email was sent.
    """
    resolved_environ = os.environ if environ is None else environ
    stream = error_stream if error_stream is not None else sys.stderr
    try:
        config = parse_alert_config(resolved_environ)
    except ValueError as error:
        print(f"email alerts are misconfigured: {error}", file=stream)
        return False
    transport = build_alert_transport(config)
    if transport is None:
        return False
    alerts = (f"watchtower range trip: {trip_line}",) + evaluate_alerts(report, config)
    subject, body = compose_cycle_email(report, alerts)
    try:
        transport.send(subject, body)
    except AlertTransportError as error:
        print(f"email alert delivery failed: {error}", file=stream)
        return False
    return True


def deliver_watchtower_notice(
    subject_line: str,
    body: str,
    environ: Mapping[str, str] | None = None,
    error_stream: TextIO | None = None,
) -> bool:
    """Deliver one plain fail-safe notice through the alert transport.

    Args:
        subject_line: The alert subject after the shared prefix.
        body: The plain-text body.
        environ: The environment carrying the alert configuration; None
            reads the live process environment.
        error_stream: Where delivery warnings land; stderr by default.

    Returns:
        Whether an email was sent.
    """
    resolved_environ = os.environ if environ is None else environ
    stream = error_stream if error_stream is not None else sys.stderr
    try:
        config = parse_alert_config(resolved_environ)
    except ValueError as error:
        print(f"email alerts are misconfigured: {error}", file=stream)
        return False
    transport = build_alert_transport(config)
    if transport is None:
        return False
    subject = f"{_WATCHTOWER_SUBJECT_PREFIX} {subject_line}"
    if len(subject) > _SUBJECT_CAP:
        subject = subject[: _SUBJECT_CAP - 3] + "..."
    try:
        transport.send(subject, body)
    except AlertTransportError as error:
        print(f"email alert delivery failed: {error}", file=stream)
        return False
    return True


def build_watchtower(
    settings: Settings,
    symbol: str,
    safe_address: str,
    relayer_address: str | None,
    config: WatchtowerConfig,
) -> RangeWatchtower:
    """Assemble the live watchtower from the application settings.

    Args:
        settings: The application settings backing every boundary.
        symbol: The registry symbol this watchtower defends.
        safe_address: The Safe whose tracked position is defended.
        relayer_address: The relayer's public address, or None.
        config: The parsed watchtower configuration.

    Returns:
        The fully wired watchtower; nothing has been read yet.
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
    from aero_bot.signing_key import load_signing_key_source

    rpc = ExecutorRpcBackend(rpc_url=settings.base_rpc_url, progress=_watchtower_progress)
    audit_store = AuditStore(settings.audit_database_path)
    pin_store = LpPoolPinStore(settings.lp_pool_pins_path)
    pin = pin_store.load().get(symbol.strip().lower())
    reads = RpcWatchtowerReads(rpc)
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
            progress=_watchtower_progress,
        ),
        rpc=rpc,
        safe_rpc=safe_rpc,
        audit_sink=audit_store,
        receipt_backends=receipt_backends,
        pool_pin_store=pin_store,
    )
    return RangeWatchtower(
        symbol=symbol,
        safe_address=safe_address,
        relayer_address=relayer_address,
        config=config,
        tick_reader=reads,
        bounds_reader=reads,
        nfpm_resolver=reads.resolve_nfpm_address,
        reads=executor,
        balances=rpc,
        executor=executor,
        key_loader=lambda: load_signing_key_source().load_signing_key(),
        audit_sink=audit_store,
        state_store=CycleStateStore.from_environment(settings=settings),
        latch_store=WatchtowerLatchStore.from_environment(settings=settings),
        stock_decimals=pin.stock_decimals if pin is not None else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the range watchtower loop.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on a clean stop (a disabled watchtower
        is dark, not failed), one on failures.
    """
    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="aero-bot-watchtower",
        description=(
            "Run the range watchtower: poll the tracked pool's current tick "
            "every few seconds and fire the audited defensive close the "
            "moment the tracked position leaves its earning range. Dark "
            "until AERO_BOT_WATCHTOWER_ENABLED arms it."
        ),
    )
    parser.add_argument("--symbol", required=True, help="Registry symbol like AAPLc.")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        help="Override the poll interval in seconds (default: 5).",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=None,
        help="Override the close-attempt cooldown in seconds (default: 900).",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        help="Bound the run to N polls for smoke checks; default runs forever.",
    )
    arguments = parser.parse_args(argv)
    if arguments.poll_seconds is not None and arguments.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if arguments.cooldown_seconds is not None and arguments.cooldown_seconds <= 0:
        parser.error("--cooldown-seconds must be positive")
    if arguments.max_polls is not None and arguments.max_polls <= 0:
        parser.error("--max-polls must be positive")
    try:
        config = parse_watchtower_config(os.environ)
    except ValueError as error:
        print(f"the watchtower configuration is invalid: {error}", file=sys.stderr)
        return EXIT_FAILURE
    if arguments.poll_seconds is not None:
        config = config.model_copy(update={"poll_interval_seconds": arguments.poll_seconds})
    if arguments.cooldown_seconds is not None:
        config = config.model_copy(update={"cooldown_seconds": arguments.cooldown_seconds})
    if not config.enabled:
        _watchtower_progress(
            "disabled: set AERO_BOT_WATCHTOWER_ENABLED=1 in the sealed environment to arm it"
        )
        return EXIT_OK
    safe_address = os.environ.get(SAFE_ADDRESS_ENV, DEFAULT_CANARY_SAFE_ADDRESS)
    relayer_address = os.environ.get(RELAYER_ADDRESS_ENV, "").strip() or None
    try:
        watchtower = build_watchtower(
            settings, arguments.symbol, safe_address, relayer_address, config
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"the watchtower is unavailable: {error}", file=sys.stderr)
        return EXIT_FAILURE
    _watchtower_progress(
        f"armed on {arguments.symbol}: polling every {config.poll_interval_seconds:g}s with a "
        f"{config.cooldown_seconds:g}s cooldown; read-only until a verified range trip"
    )

    def stop(_signum: int, _frame: object) -> None:
        watchtower.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with contextlib.suppress(KeyboardInterrupt):
        watchtower.run(max_polls=arguments.max_polls)
    return EXIT_OK


__all__ = [
    "DEFAULT_WATCHTOWER_COOLDOWN_SECONDS",
    "DEFAULT_WATCHTOWER_POLL_SECONDS",
    "RangeWatchtower",
    "RpcWatchtowerReads",
    "WATCHTOWER_COOLDOWN_SECONDS_ENV",
    "WATCHTOWER_ENABLED_ENV",
    "WATCHTOWER_POLL_SECONDS_ENV",
    "WATCHTOWER_STATE_PATH_ENV",
    "WatchtowerConfig",
    "WatchtowerLatchBook",
    "WatchtowerLatchStore",
    "WatchtowerPollOutcome",
    "WatchtowerPollState",
    "build_watchtower",
    "deliver_watchtower_notice",
    "deliver_watchtower_trigger",
    "main",
    "parse_watchtower_config",
]
