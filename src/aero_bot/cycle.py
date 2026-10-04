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
   through the complete entry gate chain and hands the ranked qualifying
   board to the portfolio allocator (the captain's gnhf 33 ruling): tiered
   positions with the deployed count as an output of qualification, cash
   as dry powder once the activation equity engages the cap (below it the
   book deploys its available funds, the captain's sub-1000 ruling), and
   per-position lifecycle folds for safety and maintenance; market
   windows no longer gate entries since the
   2026-09-09 twenty-four-seven ruling.
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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, model_validator

from aero_bot.allocator import (
    PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC,
    HeldPositionFact,
    PortfolioAllocation,
    PortfolioExclusionReason,
    PortfolioParameters,
    PortfolioRebalancePlan,
    PortfolioStepKind,
    allocate_portfolio,
    plan_portfolio_rebalance,
)
from aero_bot.audit import AuditEventType, AuditRecord, AuditStore
from aero_bot.config import Settings
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.emissions_apr import (
    AprReadingSample,
    conservative_income_apr,
    format_apr_percent,
)
from aero_bot.execution_lock import ExecutionLockUnavailableError, exclusive_execution_lock
from aero_bot.executor import (
    DEFAULT_CANARY_SAFE_ADDRESS,
    SAFE_ADDRESS_ENV,
    ExecutionMode,
    ExecutionUnavailableError,
)
from aero_bot.history import price_usdc_per_stock
from aero_bot.lp_executor import (
    AERO_DECIMALS,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REFUSED,
    NFPM_INCREASE_LIQUIDITY_TOPIC0,
    LpActionExecutionReport,
    LpExecutionRefusalCode,
    LpExecutionRefusalError,
    LpPositionStatusReport,
    LpSafePositionsSnapshot,
    fees_earned_from_growth,
)
from aero_bot.lp_plan import LpPlanRefusalError
from aero_bot.policy import (
    LOCKED_POLICY_PARAMETERS,
    MATH_PRECISION,
    TICK_PRICE_RATIO,
    AlignedPriceRange,
    HeldInventory,
    PolicyActionKind,
    PolicyDecision,
    PolicyEngine,
    PolicyOutcome,
    PolicyParameters,
    PolicyPosition,
    PolicyReason,
    PolicyState,
    evaluate_event_window,
    load_event_calendar,
)
from aero_bot.selector import (
    DEFAULT_SWITCH_MARGIN_FRACTION,
    PoolBoardOption,
    PoolEntryEvaluation,
    SwitchDirective,
    board_summary_line,
    closest_call_evaluation,
    evaluate_pool_entries,
)
from aero_bot.stock_reference import (
    FinnhubQuoteStockReferenceBackend,
    StockReferenceFeed,
    YahooChartStockReferenceBackend,
)
from aero_bot.strategy import (
    SELECTOR_SYMBOL,
    BoardListing,
    IdleCashExclusion,
    IdleCashState,
    StrategyDecisionReport,
    StrategySources,
    assemble_board,
    assemble_observation,
    parse_reference_quotes,
)
from aero_bot.venues import AERO_TOKEN_ADDRESS, BASE_USDC_ADDRESS, PoolCandidate

# Environment variable carrying an explicit cycle-state path override.
CYCLE_STATE_PATH_ENV = "AERO_BOT_CYCLE_STATE_PATH"
# Environment variable carrying an optional injected reference quote.
CYCLE_REFERENCE_PRICE_ENV = "AERO_BOT_CYCLE_REFERENCE_PRICE_USDC"
# Environment variable selecting the live underlying-equity reference feed
# (off, yahoo, or finnhub); the default off keeps production unchanged
# until the operator arms the feed in the sealed cycle environment.
CYCLE_REFERENCE_FEED_ENV = "AERO_BOT_CYCLE_REFERENCE_FEED"
# Environment variable carrying the sealed provider API token for keyed
# reference-feed backends (Finnhub today); never displayed or logged.
STOCK_REFERENCE_TOKEN_ENV = "AERO_BOT_STOCK_REFERENCE_TOKEN"  # noqa: S105 - a name, not a secret
# Environment variable carrying the relayer's public address for dry runs.
RELAYER_ADDRESS_ENV = "AERO_BOT_RELAYER_ADDRESS"
# Environment variable optionally pinning one registry symbol for the cycle.
# Unset or "auto" runs the cross-board selector (the default); an explicit
# symbol pins one pool for operator runs.
CYCLE_SYMBOL_ENV = "AERO_BOT_CYCLE_SYMBOL"
# Environment variable carrying the cross-board switch margin as a fraction
# (default 0.30, the captain's 2026-09-09 trial ruling).
CYCLE_SWITCH_MARGIN_ENV = "AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION"
# Environment variable carrying the out-of-range grace window in minutes
# (default 10, the captain's 2026-09-27 performance correction): once a
# tracked position has sat outside its earning range this long, the policy
# must act - recenter when the economics pass, otherwise exit - because every
# minute out of range forgoes emissions income.
CYCLE_OUT_OF_RANGE_GRACE_ENV = "AERO_BOT_CYCLE_OUT_OF_RANGE_GRACE_MINUTES"
# Environment variable carrying the reward-conversion threshold in USDC:
# accumulated unclaimed AERO converts to USDC inside the cycle's act step
# once its value at the last observed price exceeds it (default 5).
CYCLE_AERO_CONVERSION_MIN_ENV = "AERO_BOT_CYCLE_AERO_CONVERSION_MIN_USDC"
# Environment variable carrying the reward posture (the captain's
# retained-AERO ruling): "convert" (the default) claims and swaps rewards
# to USDC inside the act step; "retain" keeps claiming through the same
# audited collect surface but holds the AERO in the Safe as book equity and
# never invokes the conversion swap - so a restart cannot trip over a
# failing conversion surface while rewards keep accruing and attributing.
CYCLE_REWARD_POSTURE_ENV = "AERO_BOT_CYCLE_REWARD_POSTURE"
# Environment variables carrying the allocator's portfolio bounds (the
# captain's gnhf 33 ruling): every default is locked and every override
# stays under the hard ceilings PortfolioParameters enforces.
CYCLE_TIER_BAND_ENV = "AERO_BOT_CYCLE_TIER_BAND_FRACTION"
CYCLE_MAX_POSITIONS_ENV = "AERO_BOT_CYCLE_MAX_POSITIONS"
CYCLE_MIN_POSITION_USDC_ENV = "AERO_BOT_CYCLE_MIN_POSITION_USDC"
CYCLE_CONCENTRATION_CAP_ENV = "AERO_BOT_CYCLE_CONCENTRATION_CAP_FRACTION"
# Environment variable carrying the hard floor under the effective
# minimum position size in USDC (the gnhf 36 parameter-coherence
# ruling, default 30): the effective minimum lowers to the per-name
# concentration clamp whenever the clamp governs, never below this
# gas-efficiency floor.
CYCLE_MIN_POSITION_FLOOR_USDC_ENV = "AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC"
# Environment variable carrying the book equity at or above which the
# per-name concentration cap engages (the captain's 2026-09-28 ruling,
# default 1000 USDC): below the activation equity the cap does not bind
# at all, so the trial-scale book funds toward the cap running the
# proven ~100-per-position shape under the configured minimum.
CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC_ENV = "AERO_BOT_CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC"
# Environment variable carrying the trailing window of cycle readings the
# conservative income expectation floors itself at (the captain's
# 2026-09-28 correction, default 6 cycles = half an hour at the five-minute
# cadence): every surface that assumes an expected daily yield reads
# min(current, median(window)) so a transient spike never sizes or
# justifies a position. Information, never exclusion - the qualifying
# gates and the ranking keep the raw venue-convention APR.
CYCLE_INCOME_HISTORY_CYCLES_ENV = "AERO_BOT_CYCLE_INCOME_HISTORY_CYCLES"
# The default trailing window in cycles.
DEFAULT_INCOME_HISTORY_CYCLES = 6
# The hard ceiling on the window: a sealed override may lengthen the
# memory, never shorten it below the single-cycle minimum.
HARD_MAX_INCOME_HISTORY_CYCLES = 48
# Environment variable carrying the per-token stray-stock dust floor in
# USDC (default 0.01): a stray stock balance whose value at its own pool's
# pinned snapshot price sits strictly below this floor is swap dust -
# retained in the Safe, never adopted as held inventory, never swapped -
# because an exit swap of sub-cent stock costs more than it returns and
# the reconciliation's fail-closed guard exists for meaningful exposure,
# not rounding remainders (the 2026-10-02 post-closeout halt: three
# sub-cent balancing-leg remainders refused every cycle forever).
CYCLE_STOCK_DUST_FLOOR_USDC_ENV = "AERO_BOT_CYCLE_STOCK_DUST_FLOOR_USDC"
# Environment variable carrying the aggregate bound in USDC across every
# ignored dust balance (default 0.10): the floor is per token, so many
# dust tokens could otherwise sum to meaningful hidden exposure; the
# reconcile ignores dust only while the sum of every ignored value stays
# at or below this bound, and refuses the cycle the moment it does not.
CYCLE_STOCK_DUST_AGGREGATE_USDC_ENV = "AERO_BOT_CYCLE_STOCK_DUST_AGGREGATE_USDC"
# The default per-token dust floor in USDC: one cent. A Base swap's gas
# alone costs on the order of a cent, so sub-cent stock can never pay for
# its own conversion, and one cent is a hundredth of a percent of the
# trial book's scale - invisible to every equity decision.
DEFAULT_STOCK_DUST_FLOOR_USDC = Decimal("0.01")
# The default aggregate bound across all ignored dust balances: ten
# cents, the floor's value across the book's full width of names.
DEFAULT_STOCK_DUST_AGGREGATE_USDC = Decimal("0.10")
# The hard ceilings sealed overrides may never breach: a floor above one
# USDC per token (or an aggregate above one USDC) could hide a whole
# percent of the trial-scale book as "dust" - the bound exists to keep
# ignored exposure economically meaningless, and an override may only
# narrow it, never widen it past meaninglessness.
HARD_MAX_STOCK_DUST_FLOOR_USDC = Decimal("1")
HARD_MAX_STOCK_DUST_AGGREGATE_USDC = Decimal("1")
# The default reward-conversion threshold in USDC.
DEFAULT_AERO_CONVERSION_MIN_USDC = Decimal("5")
# Dynamic selector sizing keeps ten percent of the observed in-range depth cap
# unused. The target pool can move between preflight, source exit, balancing
# swap, and the final mint rebuild; this headroom keeps a still-safe switch
# from failing solely because live depth moved a few percent during execution.
SELECTOR_DEPTH_HEADROOM_FRACTION = Decimal("0.90")
# After a confirmed live action, the primary read endpoint must catch up to
# the action's inclusion block before final reconciliation. This prevents a
# load-balanced or briefly lagging RPC from reporting the pre-action balance
# as if it were the final state.
POST_ACTION_VISIBILITY_ATTEMPTS = 6
POST_ACTION_VISIBILITY_BASE_BACKOFF_SECONDS = 0.5
POST_ACTION_VISIBILITY_MAX_BACKOFF_SECONDS = 4.0
# The policy day boundary follows the engine's America/New_York convention.
POLICY_TIMEZONE = ZoneInfo("America/New_York")


# The bounded count of untracked stake-plan candidates one reconcile
# verifies: the book holds at most ten concurrent positions, and a killed
# cycle's completed siblings always sit at the newest end of the audit
# chain, so the newest dozen candidates cover every incident shape however
# long the chain grows.
STAKE_RECOVERY_CANDIDATE_LIMIT = 12


def stock_value_usdc_at_pinned_snapshot(
    units: int, decimals: int, pool: PoolCandidate
) -> Decimal | None:
    """Value one stock balance at its own pool's pinned snapshot price.

    The valuation is the same pinned-snapshot doctrine every decision
    runs under: the pool candidate's own square-root price, pinned to the
    block its discovery snapshot carried, converted through the token's
    validated decimals. A price that cannot be computed is unknown truth,
    not zero: None returns so the caller treats the balance as
    unpriceable, and unpriceable stock is never safe to ignore.

    Args:
        units: The raw token-unit balance being valued.
        decimals: The stock token's decimal count.
        pool: The verified pool whose pinned snapshot prices the token.

    Returns:
        The balance's USDC value, or None when the snapshot cannot price it.
    """
    try:
        token0 = pool.token0_address.lower()
        token1 = pool.token1_address.lower()
        usdc = BASE_USDC_ADDRESS.lower()
        if token0 == usdc:
            stock_is_token0 = False
        elif token1 == usdc:
            stock_is_token0 = True
        else:
            # Neither side is USDC: the candidate is not a verified USDC
            # pair, so its ratio prices nothing in USDC terms.
            return None
        price = price_usdc_per_stock(pool.sqrt_ratio, stock_is_token0, decimals, 6)
    except ValueError:
        return None
    with localcontext() as context:
        context.prec = MATH_PRECISION
        return +(Decimal(units).scaleb(-decimals) * price)


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


class RewardPosture(StrEnum):
    """Identify what the act step does with claimed AERO rewards.

    The captain's retained-AERO ruling (2026-10): the automatic conversion
    stays the shipped default, but the venue's own AERO accumulation is the
    strategy's tracked asset, so a sealed posture can retain rewards instead
    of converting them - claims, accounting, and value attribution keep
    running identically, and the conversion swap simply never fires. This
    also keeps a restart from immediately invoking the conversion swap while
    its routing defect (the 47-of-47 GS013 era) is under reassessment.
    """

    # Claim rewards and swap the Safe's whole AERO balance to USDC inside
    # the act step once the value crosses the sealed threshold (the
    # long-standing default behavior).
    CONVERT = "convert"
    # Claim rewards through the same audited collect surface, but retain
    # the AERO in the Safe as book equity; never invoke the conversion
    # swap.
    RETAIN = "retain"


# The default reward posture: claim and convert, the shipped behavior.
DEFAULT_REWARD_POSTURE = RewardPosture.CONVERT
# Every posture the sealed environment may select.
REWARD_POSTURE_CHOICES = frozenset({posture.value for posture in RewardPosture})


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
    # Why the stock is held, so failed entries can retry before sell-back and
    # grace exits route through convergence.
    origin: Literal[
        "stale_low_exit",
        "failed_entry",
        "failed_recenter",
        "adopted_balance",
        "out_of_range_exit",
    ] = "adopted_balance"


class ReentryCooldown(BaseModel):
    """Carry one pool's re-entry cooldown from a stop or dilution exit."""

    # Frozen strict fields keep one cooldown record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the cooldown applies to.
    symbol: str
    # Re-entry into this pool stays blocked until this instant.
    blocked_until: datetime
    # A dilution exit also arms the re-entry margin: re-entry then requires
    # the emissions APR to clear the floor by the locked relative margin
    # until the next successful entry clears the record. The marker lives in
    # the persisted book, so a cooldown expiring - or a day rolling over -
    # never silently erases it.
    dilution: bool = False


class CycleFeeSampleRecord(BaseModel):
    """Carry one position's latest claimable-fee reading for the window."""

    # Frozen strict fields keep one fee sample coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The position NFT id the sample observed.
    token_id: Annotated[int, Field(ge=0)]
    # When the sample was read, timezone-aware.
    observed_at: datetime
    # The checkpointed pool fees claimable at the sample, in USDC.
    claimable_pool_fees_usdc: Annotated[Decimal, Field(ge=0)]
    # The position's marked USDC value at the sample.
    position_value_usdc: Annotated[Decimal, Field(ge=0)]


class CyclePositionBaseline(BaseModel):
    """Carry one position's day-start observables for its attribution."""

    # Frozen strict fields keep one row coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the position lives in; empty on a
    # legacy single-position baseline folded up from the scalar fields.
    symbol: str = ""
    # The position NFT the fee words belong to; None while flat.
    token_id: Annotated[int, Field(ge=0)] | None = None
    # The position's liquidity at the baseline.
    liquidity_units: Annotated[int, Field(ge=0)] | None = None
    # The position's token-zero inside fee growth at the baseline.
    fee_growth_inside0_x128: int | None = None
    # The token-one twin of the inside fee growth.
    fee_growth_inside1_x128: int | None = None
    # The position's staked AERO earned at the baseline, raw units.
    aero_earned_units: Annotated[int, Field(ge=0)] = 0
    # The whole-token stock quantity at the baseline: Safe inventory plus
    # the position's stock side for this symbol.
    stock_quantity: Annotated[Decimal, Field(ge=0)] | None = None
    # The pool's stock price at the baseline, USDC per stock.
    stock_price_usdc: Annotated[Decimal, Field(gt=0)] | None = None


class CycleDayBaseline(BaseModel):
    """Carry the day-start observables the yield attribution measures from.

    The baseline snapshots one America/New_York day's first cycle - the
    unclaimed-AERO units and, per position, the stock quantity and price
    and the live fee-growth words - so every later cycle can decompose the
    day's P&L into AERO rewards accrued, fees earned (computed from the
    pool's fee-growth accumulators), and stock mark-to-market, with the
    residual named honestly as unattributed. One row per funded position
    (the allocator ruling); legacy single-position baselines fold into
    their one row.
    """

    # Frozen strict fields keep one day's baseline coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The New York day this baseline belongs to.
    day: date
    # One row per funded position; the scalar legacy fields below fold
    # into the first row on load.
    positions: tuple[CyclePositionBaseline, ...] = ()
    # Unclaimed AERO at the baseline: the Safe balance plus staked earned,
    # raw units.
    aero_units: Annotated[int, Field(ge=0)] = 0
    # AERO converted to USDC since the baseline, raw units, so a conversion
    # never reads as lost rewards.
    aero_converted_units: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="before")
    @classmethod
    def fold_legacy_scalars(cls, data: object) -> object:
        """Fold a legacy single-position baseline into its one row.

        Baselines persisted before the allocator ruling carried scalar
        position fields; the upgrade wraps them into the first row so old
        books keep their day's measurement.

        Args:
            data: Raw model input mapping or value.

        Returns:
            Input with the legacy scalars folded into ``positions``.
        """
        if not isinstance(data, dict) or "positions" in data:
            return data
        row_fields = (
            "token_id",
            "liquidity_units",
            "fee_growth_inside0_x128",
            "fee_growth_inside1_x128",
            "stock_quantity",
            "stock_price_usdc",
        )
        if any(data.get(field) is not None for field in row_fields):
            row = {field: data.get(field) for field in row_fields}
            data = {key: value for key, value in data.items() if key not in row_fields}
            data["positions"] = [row]
        return data

    @property
    def token_id(self) -> int | None:
        """The first row's token id, for single-position readers."""
        return self.positions[0].token_id if self.positions else None

    @property
    def liquidity_units(self) -> int | None:
        """The first row's liquidity, for single-position readers."""
        return self.positions[0].liquidity_units if self.positions else None

    @property
    def fee_growth_inside0_x128(self) -> int | None:
        """The first row's token-zero fee growth, for single-position readers."""
        return self.positions[0].fee_growth_inside0_x128 if self.positions else None

    @property
    def fee_growth_inside1_x128(self) -> int | None:
        """The first row's token-one fee growth, for single-position readers."""
        return self.positions[0].fee_growth_inside1_x128 if self.positions else None

    @property
    def stock_quantity(self) -> Decimal | None:
        """The first row's stock quantity, for single-position readers."""
        return self.positions[0].stock_quantity if self.positions else None

    @property
    def stock_price_usdc(self) -> Decimal | None:
        """The first row's stock price, for single-position readers."""
        return self.positions[0].stock_price_usdc if self.positions else None


class CycleStateBook(BaseModel):
    """Carry every engine-owned fact the next cycle must thread forward.

    The book tracks the portfolio's positions - one tuple entry per funded
    position (the captain's allocator ruling) - beside the session facts
    every fold shares: the day anchors, the halt latch, and the per-pool
    re-entry cooldowns.
    """

    # Every tracked open position, one per funded pool; empty while flat.
    positions: tuple[TrackedPosition, ...] = ()
    # Stock held unsold after a stale-low burn, or None.
    held_inventory: HeldInventoryRecord | None = None
    # Re-entry cooldowns, one per pool: an exit from one pool never blocks
    # another pool's entry (the captain's 2026-09-09 cross-board ruling).
    reentry_cooldowns: tuple[ReentryCooldown, ...] = ()
    # The latest claimable-fee sample per recent position NFT, the window
    # the measured fee-accrual evidence reads; bounded to recent ids so a
    # long-running book cannot grow without limit.
    fee_samples: tuple[CycleFeeSampleRecord, ...] = ()
    # The America/New_York day the day-start equity anchor belongs to.
    day: date | None = None
    # Day-start equity anchors the five-percent daily loss halt.
    day_start_equity_usd: Decimal | None = None
    # The running equity high-water mark carried across day rollovers, the
    # continuous drawdown latch's anchor (captain's 2026-09-27 ruling).
    peak_equity_usdc: Decimal | None = None
    # The day a daily loss halt tripped, if any.
    halted_day: date | None = None
    # The current day's yield-attribution baseline, None before the first
    # cycle of a day.
    day_baseline: CycleDayBaseline | None = None
    # The idle-cash alert episode's signature while it persists: the
    # sorted symbol/reason/gate set the alert fired on, or None once the
    # book deploys, the band empties, or no cycle has alerted yet. The
    # alert fires only on the first cycle of an episode or when its cause
    # changes (the captain's gnhf 34 ruling).
    idle_cash_alert_signature: str | None = None
    # The trailing per-symbol qualifying-APR readings (the captain's
    # 2026-09-28 correction): the conservative income expectation floors
    # itself at the median of each pool's trailing window, so no position
    # is ever sized or justified by a transient reading. Bounded to the
    # window per symbol.
    apr_history: tuple[AprReadingSample, ...] = ()
    # When this book was last persisted, timezone-aware.
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def position(self) -> TrackedPosition | None:
        """Return the one tracked position, or None while flat or plural.

        Pinned-symbol surfaces and legacy readers consume the single
        position; portfolio surfaces iterate ``positions`` directly.
        """
        return self.positions[0] if len(self.positions) == 1 else None

    @model_validator(mode="before")
    @classmethod
    def fold_legacy_fields(cls, data: object) -> object:
        """Fold legacy single-position fields into the portfolio shapes.

        Books persisted before the allocator ruling carried one
        ``position`` (and, older still, one global
        ``reentry_blocked_until``); the upgrade folds the position into
        the ``positions`` tuple and applies the legacy cooldown to the
        tracked pool when one exists.

        Args:
            data: Raw model input mapping or value.

        Returns:
            Input with the legacy fields folded into the portfolio shapes.
        """
        if not isinstance(data, dict):
            return data
        if "positions" not in data:
            legacy_position = data.get("position")
            if legacy_position is not None:
                data = {key: value for key, value in data.items() if key != "position"}
                data["positions"] = [legacy_position]
        if "reentry_cooldowns" not in data:
            legacy = data.get("reentry_blocked_until")
            tracked = data.get("position") or next(iter(data.get("positions") or []), None)
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


class PositionStatusRecord(BaseModel):
    """Carry one tracked position's live reconciliation record."""

    # Frozen strict fields keep one record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the position lives in.
    symbol: str
    # The position NFT id on the pool's NFPM.
    token_id: Annotated[int, Field(ge=0)]
    # The pool the position was minted in.
    pool_address: EvmAddress
    # The committed USDC value at entry, the P&L basis.
    committed_usd: Annotated[Decimal, Field(gt=0)]
    # Whether the position is staked in its gauge.
    staked: bool
    # The position's live status read.
    status: LpPositionStatusReport


class RecoveredPositionRecord(BaseModel):
    """Carry one audit-proven untracked position's recovery evidence.

    The 2026-09-30 timeout incident: a killed multi-entry cycle leaves its
    completed siblings staked in their gauges, invisible to the Safe-owned
    inventory, and the next cycle marks their value as a loss. This record
    is the reconcile's proof that one such NFT is ours - the audit chain's
    execute-mode stake plan naming it, verified against its live custody.
    """

    # Frozen strict fields keep one record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the stake plan named.
    symbol: str
    # The position NFT id the stake plan named.
    token_id: Annotated[int, Field(ge=0)]
    # The pool the live status read resolved the position in.
    pool_address: EvmAddress
    # The audited mint plan's budget, the committed basis at entry.
    committed_usd: Annotated[Decimal, Field(gt=0)]
    # The stake plan record's instant, the entry's proven timestamp.
    entered_at: datetime
    # Whether the verified custody is the pool's gauge (True) or the Safe.
    staked: bool
    # The position's live status read backing the custody verification.
    status: LpPositionStatusReport


class IgnoredStockDust(BaseModel):
    """Carry one stray stock balance ignored as economically meaningless dust.

    The reconcile's stray-stock sweep classifies each unrecorded balance at
    its own pool's freshly pinned snapshot price; a balance strictly below
    the dust floor is retained in the Safe untouched - never adopted as
    held inventory, never swapped - and rides the reconciliation's
    diagnostics, report, and audit record so ignored never means invisible.
    """

    # Frozen strict fields keep one dust record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched symbol whose stock token carries the dust.
    symbol: str
    # The stock token contract holding the dust balance.
    token_address: EvmAddress
    # The dust balance in whole stock tokens.
    quantity: Annotated[Decimal, Field(ge=0)]
    # The balance's USDC value at the pool's pinned snapshot price.
    value_usdc: Annotated[Decimal, Field(ge=0)]


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
    # The Safe's live AERO balance in raw units, the idle emissions the reward
    # conversion counts in the book's reconcile.
    safe_aero_units: Annotated[int, Field(ge=0)] = 0
    # Every live Safe-held NFT id on the pool's NFPM.
    inventory_live_token_ids: Annotated[tuple[int, ...], Field(min_length=0)]
    # How many held NFTs are empty residuals carrying no exposure.
    inventory_empty_count: Annotated[int, Field(ge=0)]
    # Every tracked position's live status, one record per funded pool
    # (the allocator ruling); empty while flat.
    position_statuses: tuple[PositionStatusRecord, ...] = ()
    # The tracked position's live status, None while flat; the primary
    # (first) position for pinned surfaces and legacy readers.
    tracked_status: LpPositionStatusReport | None = None
    # The tracked position id after reconciliation (adoptions included).
    tracked_token_id: Annotated[int, Field(ge=0)] | None = None
    # Whether the tracked position is staked in the gauge.
    tracked_staked: bool = False
    # Every tracked position id that reconciles empty on-chain - zero
    # liquidity, zero checkpointed fees, unstaked in the Safe - and so
    # leaves the book's tracking this cycle. The crashed-exit heal: a
    # cycle that dies between a completed withdraw and its re-entry
    # leaves exactly this shape tracked, and a stale tracking re-commands
    # a dead NFT every cycle (the 2026-09-28 position_empty halt loop).
    empty_tracked_token_ids: Annotated[tuple[int, ...], Field(min_length=0)] = ()
    # Every audit-proven position recovered this cycle: untracked NFTs
    # whose execute-mode stake plan the audit chain carries and whose live
    # custody verifies as ours - the killed multi-entry cycle's gauge-held
    # siblings the Safe-owned inventory cannot see (the 2026-09-30 timeout
    # incident's phantom 36-USDC drawdown). Empty when nothing recovers.
    recovered_positions: tuple[RecoveredPositionRecord, ...] = ()
    # The held-inventory quantity in whole stock tokens, zero when none.
    held_stock_quantity: Annotated[Decimal, Field(ge=0)] = Decimal("0")
    # The symbol whose stock balance held_stock_quantity measures; None
    # when no stock is held or the balance is not one pool's inventory.
    held_symbol: str | None = None
    # Every stray stock balance ignored as dust this cycle: each valued
    # strictly below the dust floor at its own pool's pinned snapshot
    # price, retained in the Safe, never adopted, never swapped. Ignored
    # stays visible - the report, diagnostics, and audit record all carry
    # these rows, and the aggregate bound guards against many-token
    # splitting (a dust set whose total exceeds the bound refuses the
    # cycle instead of hiding meaningful exposure).
    ignored_stock_dust: tuple[IgnoredStockDust, ...] = ()
    # Nonempty when an out-of-band condition refuses the whole cycle.
    out_of_band: str = ""
    # Human-readable evidence lines covering the reconciliation.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class CycleFeeEvidence(BaseModel):
    """Carry one cycle's live fee evidence for the tracked position.

    Measurement only: nothing here feeds a decision. The policy's expected
    yield keeps its conservative zero fee APR while this surface proves the
    claimable-now truth and, once two samples exist, the measured accrual
    window against the position's marked value.
    """

    # Frozen strict fields keep one fee-evidence record coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The tracked position NFT the evidence covers, None while flat.
    token_id: Annotated[int, Field(ge=0)] | None = None
    # The checkpointed pool fees claimable now, in USDC, None when the
    # status read carried no valuation.
    claimable_pool_fees_usdc: Decimal | None = None
    # The accrued AERO earned on the position when staked, raw units.
    claimable_aero_units: Annotated[int, Field(ge=0)] | None = None
    # The measured checkpointed-fee accrual over the sample window, USDC
    # per day, None until a second sample exists.
    measured_fee_usdc_per_day: Decimal | None = None
    # The accrual as an annual fraction of the position's marked value,
    # None until both a window and a positive mark exist.
    measured_fee_apr: Decimal | None = None
    # The computed collect-now estimate - checkpointed owed plus the
    # uncheckpointed growth computed from the pool's fee-growth
    # accumulators - in USDC; None when the computed measurement was
    # unavailable. This supersedes the checkpointed lower bound as the
    # day's fee evidence (the captain's 2026-09-27 correction).
    claimable_pool_fees_computed_usdc: Decimal | None = None
    # The named method behind the computed fee numbers, empty when absent.
    fee_method_diagnostic: str = ""
    # Why any piece is absent, empty when everything computed.
    diagnostic: str = ""


class CyclePositionYield(BaseModel):
    """Carry one position's slice of the day's yield attribution."""

    # Frozen strict fields keep one row coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the position lives in.
    symbol: str
    # The position NFT the attribution covers.
    token_id: Annotated[int, Field(ge=0)] | None = None
    # The position's AERO rewards accrued since its baseline (its staked
    # earned delta), valued at the last observed price, None when
    # unmeasurable.
    aero_rewards_usdc: Decimal | None = None
    # The same reward accrual as raw AERO units (the staked earned delta),
    # reported beside its mark-to-market so the units themselves stay
    # visible: a reward balance is never a performance number until it is
    # priced and decomposed (the captain's retained-AERO ruling).
    aero_rewards_units: int | None = None
    # The position's fees earned since its baseline, computed from fee
    # growth, None when unmeasurable.
    fees_earned_usdc: Decimal | None = None
    # The position's stock mark-to-market at its own pool's price, None
    # when unmeasurable.
    stock_mark_to_market_usdc: Decimal | None = None
    # Why any component is absent, empty when everything computed.
    diagnostic: str = ""


class CyclePositionSummary(BaseModel):
    """Carry one tracked position's headline summary for the report."""

    # Frozen strict fields keep one row coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the position lives in.
    symbol: str
    # The position NFT id.
    token_id: Annotated[int, Field(ge=0)]
    # The committed USDC value at entry, the P&L basis.
    committed_usd: Annotated[Decimal, Field(gt=0)]
    # The live marked USDC value, None when unvalued.
    value_usdc: Decimal | None = None
    # The unrealized P&L vs entry, None when uncomputed.
    unrealized_pnl_usdc: Decimal | None = None
    # Whether the position is staked in its gauge.
    staked: bool
    # The position's slice of the day's yield attribution, when one runs.
    yield_attribution: CyclePositionYield | None = None


class CycleYieldAttribution(BaseModel):
    """Decompose one day's P&L into its yield-duration components.

    The strategy's thesis is yield duration - maximize time-in-range times
    the qualifying emissions APR - so the daily report names the income
    streams explicitly: AERO rewards accrued, fees earned (computed from
    the pool's fee-growth accumulators, never the stale checkpoint), and
    stock-token mark-to-market on the day's opening quantity. Everything
    else - actions, gas, collections, marking flows - lands in the named
    unattributed residual so the three components never masquerade as a
    complete ledger (the captain's 2026-09-27 corrections). The
    decomposition runs per position (the allocator ruling) and rolls up:
    the tier decisions are judged by measured income daily, name by name.
    """

    # Frozen strict fields keep one attribution coherent.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The day's P&L the decomposition explains, None without day economics.
    day_pnl_usdc: Decimal | None = None
    # AERO rewards accrued since the day baseline, valued at the last
    # observed price, None when unmeasurable.
    aero_rewards_usdc: Decimal | None = None
    # The same day's reward accrual as raw AERO units (unclaimed now, plus
    # anything converted today, minus the day-start baseline), reported
    # beside its mark-to-market so earned units and their value stay
    # separately visible.
    aero_rewards_units: int | None = None
    # Fees earned since the day baseline, computed from fee growth, None
    # when unmeasurable.
    fees_earned_usdc: Decimal | None = None
    # Mark-to-market of the day's opening stock quantity at the current
    # pool price, None when unmeasurable.
    stock_mark_to_market_usdc: Decimal | None = None
    # The residual: day P&L minus the three components, None when either
    # side is unmeasurable.
    unattributed_usdc: Decimal | None = None
    # One row per funded position: its own AERO, fee, and mark-to-market
    # slices at its own pool's price.
    positions: tuple[CyclePositionYield, ...] = ()
    # The named methods behind each component, for the report and audit.
    method: Annotated[tuple[str, ...], Field(min_length=1)]
    # Why any component is absent, empty when everything computed.
    diagnostic: str = ""


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
    # One summary row per tracked position (the allocator ruling): symbol,
    # committed and marked value, P&L, custody, and its attribution slice.
    positions: tuple[CyclePositionSummary, ...] = ()
    # The portfolio equity the engine acted on - Safe USDC, held stock, and
    # the tracked LP mark - None when the cycle refused out-of-band.
    equity_usd: Decimal | None = None
    # The day-start equity anchor in force, priced on the same composition.
    day_start_equity_usd: Decimal | None = None
    # The day's P&L against the anchor, None when either input is absent.
    day_pnl_usdc: Decimal | None = None
    # Why day economics are absent, empty when computed.
    day_diagnostic: str = ""
    # The running equity high-water mark in force at the decision, carried
    # across New York day rollovers by the continuous drawdown latch.
    peak_equity_usd: Decimal | None = None
    # The day's yield decomposition: AERO rewards, computed fees, stock
    # mark-to-market, and the honest residual.
    yield_attribution: CycleYieldAttribution | None = None
    # The idle-book evidence when qualifying pools stayed excluded while
    # cash sat undeployed (the captain's gnhf 34 ruling): the cash
    # fraction, every in-band exclusion's gate and lost-yield line, and
    # the episode signature the alert layer rate-limits on. None when
    # the book deployed or nothing ranked in-band.
    idle_cash: IdleCashState | None = None
    # Unclaimed AERO at the final reconciliation: the Safe balance plus the
    # staked position's earned, raw units; None when unmeasurable.
    unclaimed_aero_units: Annotated[int, Field(ge=0)] | None = None
    # The unclaimed AERO valued at the last observed price, None when either
    # input is absent.
    unclaimed_aero_value_usdc: Decimal | None = None
    # The live fee evidence for the tracked position, measurement only.
    fee_evidence: CycleFeeEvidence | None = None
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
    # The portfolio equity the engine acted on, else None.
    equity_usdc: str | None = None
    # The day-start equity anchor in force, else None.
    day_start_equity_usdc: str | None = None
    # The day's P&L against the anchor, else None.
    day_pnl_usdc: str | None = None
    # The running equity high-water mark the continuous latch carries.
    peak_equity_usdc: str | None = None
    # AERO rewards accrued since the day baseline, else None.
    yield_aero_rewards_usdc: str | None = None
    # The same reward accrual as raw AERO units, so the audited record
    # separates earned units from their mark-to-market (the retained-AERO
    # ruling: no raw reward balance is ever net performance).
    yield_aero_rewards_units: str | None = None
    # Fees earned since the day baseline, computed, else None.
    yield_fees_earned_usdc: str | None = None
    # Stock mark-to-market since the day baseline, else None.
    yield_stock_mark_to_market_usdc: str | None = None
    # The unattributed residual of the yield decomposition, else None.
    yield_unattributed_usdc: str | None = None
    # Unclaimed AERO valued at the last observed price, else None.
    unclaimed_aero_value_usdc: str | None = None
    # The checkpointed pool fees claimable on the tracked position, else None.
    claimable_pool_fees_usdc: str | None = None
    # The measured checkpointed-fee accrual as an annual fraction of the
    # position's marked value, else None.
    measured_fee_apr: str | None = None
    # The symbols whose stray stock balances the reconcile ignored as dust.
    ignored_dust_symbols: tuple[str, ...] = ()
    # The total USDC value of every ignored dust balance, else None when
    # no dust was ignored.
    ignored_dust_usdc: str | None = None
    # Total delivery fees paid, in wei.
    fee_wei: Annotated[int, Field(ge=0)] = 0
    # The number of actions attempted this cycle.
    action_count: Annotated[int, Field(ge=0)] = 0
    # How many positions the book tracked at the report (the allocator).
    position_count: Annotated[int, Field(ge=0)] = 0
    # The total committed value across every tracked position, else None.
    total_committed_usdc: str | None = None
    # The largest position's share of book equity as a fraction, else None.
    largest_position_share: str | None = None
    # Empty when the cycle completed; otherwise why it halted.
    halted_reason: str = ""
    # The engine's complete numeric evidence lines, including the
    # per-gate chain evaluation for the top-ranked pool and every
    # idle-cash exclusion's consequence line, so any why-is-it-flat
    # question is answerable from the audit store alone (gnhf 34).
    decision_diagnostics: tuple[str, ...] = ()
    # The idle-book episode signature while cash sat undeployed beside
    # excluded in-band pools, else None.
    idle_cash_signature: str | None = None
    # Cash as a fraction of equity while the idle-book episode held,
    # else None.
    idle_cash_fraction: str | None = None
    # Every in-band pool kept out of the book by a gate or bound while
    # cash sat idle, as compact SYMBOL (reason: gate) tokens.
    idle_cash_exclusions: tuple[str, ...] = ()


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
        portfolio_live_positions: Sequence[tuple[int, Decimal]] | None = None,
        exact_tick_bounds: tuple[int, int] | None = None,
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
        portfolio_live_positions: Sequence[tuple[int, Decimal]] | None = None,
        exact_tick_bounds: tuple[int, int] | None = None,
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
        portfolio_live_positions: Sequence[tuple[int, Decimal]] | None = None,
        exact_tick_bounds: tuple[int, int] | None = None,
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

    def execute_collect(
        self,
        symbol: str,
        token_id: int,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Broadcast one fee or emissions claim for one position."""
        ...

    def execute_aero_swap(
        self,
        key_bytes: bytes,
        *,
        confirm_broadcast: bool,
        ephemeral_key: bool = False,
    ) -> LpActionExecutionReport:
        """Broadcast the AERO-to-USDC reward conversion."""
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

    def fetch_chain_id(self) -> int:
        """Read the endpoint's chain id, proving the expected chain."""
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


def _dilution_pending(book: CycleStateBook, symbol: str) -> bool:
    """Read one pool's pending dilution re-entry margin from the book.

    Args:
        book: The persisted cycle book.
        symbol: The registry-matched stock symbol being read.

    Returns:
        True when the pool's cooldown record carries the dilution marker -
        the record persists past its blocked_until instant, so the margin
        applies until a successful entry clears the record.
    """
    for cooldown in book.reentry_cooldowns:
        if cooldown.symbol.lower() == symbol.lower():
            return cooldown.dilution
    return False


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


# The exclusion reasons that mark a book idle while a pool ranked
# in-band: every bound or gate that kept qualifying capital in cash.
# Below-band exclusions are lower yield, not idling, and the
# concentration-cap note rides tranches that funded.
_IDLE_EXCLUSION_REASONS = frozenset(
    {
        PortfolioExclusionReason.ENTRY_GATE_REFUSED,
        PortfolioExclusionReason.BELOW_MIN_POSITION_SIZE,
        PortfolioExclusionReason.INSUFFICIENT_CASH,
        PortfolioExclusionReason.MAX_POSITIONS_REACHED,
        PortfolioExclusionReason.NO_DEPLOYABLE_BUDGET,
        PortfolioExclusionReason.INVENTORY_UNWIND_PENDING,
    }
)


def _idle_cash_state(
    allocation: PortfolioAllocation,
    cash_usdc: Decimal,
    equity_usdc: Decimal,
    book: CycleStateBook,
) -> IdleCashState | None:
    """Build the idle-book evidence when in-band pools stayed excluded.

    The state exists only when at least one pool that ranked in-band was
    kept out of the book by a gate or bound while cash sat undeployed -
    the posture the captain rated a horrible mistake when it sat silent
    all night. The episode signature is the sorted symbol/reason/gate
    set; it changes exactly when the idle cause changes, so the alert
    layer fires on the first cycle of an episode and stays quiet while
    the same cause persists.

    Args:
        allocation: The allocator's target composition with exclusions.
        cash_usdc: The Safe's live USDC the decision pass priced.
        equity_usdc: The portfolio equity the decision pass priced.
        book: The persisted book carrying the prior episode signature.

    Returns:
        The idle-book evidence, or None when no in-band pool was excluded.
    """
    idle_rows = tuple(
        item for item in allocation.excluded if item.reason in _IDLE_EXCLUSION_REASONS
    )
    if not idle_rows or equity_usdc <= 0:
        return None
    exclusions = tuple(
        IdleCashExclusion(
            symbol=item.symbol,
            reason=item.reason.value,
            gate=item.gate,
            detail=item.detail,
            emissions_apr=item.emissions_apr if item.emissions_apr is not None else Decimal("0"),
            forgone_income_usdc_per_day=item.forgone_income_usdc_per_day,
        )
        for item in idle_rows
    )
    signature = "; ".join(f"{row.symbol}:{row.reason}:{row.gate}" for row in exclusions)
    return IdleCashState(
        cash_usdc=cash_usdc,
        equity_usd=equity_usdc,
        cash_fraction=+(cash_usdc / equity_usdc),
        exclusions=exclusions,
        signature=signature,
        signature_changed=signature != book.idle_cash_alert_signature,
    )


def _book_with_cooldowns(
    book: CycleStateBook, updates: Mapping[str, tuple[datetime | None, bool]]
) -> CycleStateBook:
    """Merge per-pool cooldown updates into the book.

    A None update clears its pool's cooldown record - and with it any
    dilution re-entry marker, exactly what a successful entry does; pools
    not named keep theirs, markers included.

    Args:
        book: The book being updated.
        updates: The per-symbol (blocked-until instant, dilution marker)
            pairs to merge.

    Returns:
        The book carrying the merged cooldown map.
    """
    lowered = {symbol.lower(): update for symbol, update in updates.items()}
    kept = tuple(
        cooldown for cooldown in book.reentry_cooldowns if cooldown.symbol.lower() not in lowered
    )
    fresh = tuple(
        ReentryCooldown(symbol=symbol, blocked_until=until, dilution=dilution)
        for symbol, (until, dilution) in lowered.items()
        if until is not None
    )
    return book.model_copy(update={"reentry_cooldowns": kept + fresh})


def _book_with_position_added(book: CycleStateBook, tracked: TrackedPosition) -> CycleStateBook:
    """Return the book carrying one freshly entered tracked position.

    A position replaces any prior tracked slot on the same pool - one
    position per pool is the book's shape - so a recenter or reallocation
    successor lands in place.

    Args:
        book: The book being updated.
        tracked: The freshly minted and staked position.

    Returns:
        The book carrying the added (or replaced) position.
    """
    kept = tuple(
        position for position in book.positions if position.symbol.lower() != tracked.symbol.lower()
    )
    return book.model_copy(update={"positions": (*kept, tracked)})


def _book_with_position_replaced(
    book: CycleStateBook, token_id: int, tracked: TrackedPosition
) -> CycleStateBook:
    """Return the book with one position replaced by its successor.

    Args:
        book: The book being updated.
        token_id: The NFT id being replaced (a recenter or reallocation).
        tracked: The successor position.

    Returns:
        The book carrying the replacement at the same slot order.
    """
    return book.model_copy(
        update={
            "positions": tuple(
                tracked if position.token_id == token_id else position
                for position in book.positions
            )
        }
    )


def _book_with_position_removed(book: CycleStateBook, token_id: int) -> CycleStateBook:
    """Return the book with one exited position removed.

    Args:
        book: The book being updated.
        token_id: The NFT id being removed.

    Returns:
        The book without the exited position.
    """
    return book.model_copy(
        update={
            "positions": tuple(
                position for position in book.positions if position.token_id != token_id
            )
        }
    )


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
        parameters: PolicyParameters = LOCKED_POLICY_PARAMETERS,
        aero_conversion_min_usdc: Decimal = DEFAULT_AERO_CONVERSION_MIN_USDC,
        reward_posture: RewardPosture = DEFAULT_REWARD_POSTURE,
        portfolio_parameters: PortfolioParameters | None = None,
        income_history_cycles: int | None = None,
        stock_dust_floor_usdc: Decimal = DEFAULT_STOCK_DUST_FLOOR_USDC,
        stock_dust_aggregate_usdc: Decimal = DEFAULT_STOCK_DUST_AGGREGATE_USDC,
        reference_feed: StockReferenceFeed | None = None,
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
            parameters: The policy parameter set decisions run under; the
                locked defaults with the sealed cycle environment's
                out-of-range grace override applied.
            aero_conversion_min_usdc: The unclaimed-AERO value threshold that
                triggers the reward conversion inside the act step.
            reward_posture: What the act step does with claimed rewards
                (the captain's retained-AERO ruling): convert swaps them to
                USDC once the value crosses the threshold, retain claims but
                holds the AERO in the Safe and never invokes the swap.
            portfolio_parameters: The portfolio allocation parameter set
                (the allocator ruling); None builds the locked defaults
                carrying this runner's switch margin.
            income_history_cycles: The trailing window of cycle readings
                the conservative income expectation floors itself at (the
                captain's 2026-09-28 correction); None uses the sealed
                default.
            stock_dust_floor_usdc: The per-token USDC value under which a
                stray stock balance is dust - retained in the Safe, never
                adopted, never swapped (the captain's tiny-dust ruling).
            stock_dust_aggregate_usdc: The bound the sum of every ignored
                dust balance must stay at or below, so many-token splitting
                cannot hide meaningful exposure behind the floor.
            reference_feed: The optional live underlying-equity reference
                feed whose per-symbol quotes and honest as-of ages join the
                decision observations; None keeps injected-constant
                behavior exactly as shipped.
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
        self._parameters = parameters
        self._aero_conversion_min_usdc = aero_conversion_min_usdc
        self._reward_posture = reward_posture
        self._stock_dust_floor_usdc = stock_dust_floor_usdc
        self._stock_dust_aggregate_usdc = stock_dust_aggregate_usdc
        self._portfolio_parameters_value = portfolio_parameters
        self._income_history_cycles_value = income_history_cycles
        self._reference_feed = reference_feed
        self._last_reconciliation: CycleReconciliation | None = None
        # One cycle process enumerates the board at most once; reconcile and
        # decide share the cached listing and its snapshot block.
        self._board: tuple[BoardListing, ...] | None = None
        self._board_block: int | None = None

    @property
    def selector_mode(self) -> bool:
        """Return whether this runner selects across the whole B20 board."""
        return self._symbol is None

    def _portfolio_parameters(self) -> PortfolioParameters:
        """Resolve the portfolio parameter set the allocator runs under.

        Returns:
            The configured parameter set, or the locked defaults carrying
            this runner's switch margin as the reallocation margin.
        """
        if self._portfolio_parameters_value is None:
            return PortfolioParameters(switch_margin_fraction=self._switch_margin_fraction)
        return self._portfolio_parameters_value

    def _income_history_cycles(self) -> int:
        """Resolve the conservative income window in cycles.

        Returns:
            The configured window, or the sealed default.
        """
        if self._income_history_cycles_value is not None:
            return self._income_history_cycles_value
        return _income_history_cycles_from_environment()

    def _income_basis_stamping(
        self, options: tuple[PoolBoardOption, ...], book: CycleStateBook
    ) -> tuple[tuple[PoolBoardOption, ...], tuple[AprReadingSample, ...], tuple[str, ...]]:
        """Stamp each option's conservative income basis from the book's window.

        The captain's 2026-09-28 correction: the venue's displayed
        convention is the reference and its high readings are real, so
        nothing is excluded - but every surface that ASSUMES an expected
        daily yield reads ``min(current, median(trailing window))`` so a
        transient spike never sizes or justifies a position. Each board
        option's observation carries the basis; the successor history
        appends this cycle's readings bounded to the window per symbol;
        one evidence line names every pool whose expectation was floored.

        Args:
            options: The assembled board options.
            book: The persisted book carrying the trailing readings.

        Returns:
            The stamped options, the successor history, and the evidence
            lines for the floored pools.
        """
        window = self._income_history_cycles()
        stamped: list[PoolBoardOption] = []
        successor: list[AprReadingSample] = []
        evidence: list[str] = []
        for option in options:
            reading = option.observation.emissions_apr
            symbol_key = option.symbol.lower()
            prior = tuple(
                sample.reading for sample in book.apr_history if sample.symbol.lower() == symbol_key
            )
            basis = conservative_income_apr(reading, prior + (reading,))
            stamped.append(
                option.model_copy(
                    update={
                        "observation": option.observation.model_copy(
                            update={"conservative_income_apr": basis}
                        )
                    }
                )
            )
            successor.append(AprReadingSample(symbol=option.symbol, reading=reading))
            if basis < reading:
                evidence.append(
                    f"conservative income basis: {option.symbol} expected-yield surfaces "
                    f"read {format_apr_percent(basis)}, the trailing {len(prior) + 1}-cycle "
                    f"median flooring the instantaneous {format_apr_percent(reading)}; "
                    "the qualifying gates and the ranking keep the raw venue-convention "
                    "reading"
                )
        trimmed = _trim_apr_history(tuple(successor), book.apr_history, window)
        return tuple(stamped), trimmed, tuple(evidence)

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
        converted_units = 0
        unclaimed_aero_units: int | None = None
        unclaimed_aero_value: Decimal | None = None
        book = self._state_store.load()
        # A crashed act can die between a broadcast's durable send record
        # and its receipt row - chain truth confirmed, journal evidence one
        # row short - which left the position permanently unprovable and
        # every later cycle refused out-of-band (the 2026-10-03 gap
        # reproduction). The heal proves those deliveries from their
        # on-chain receipts and appends the truthful outcome rows before
        # anything reconciles; unprovable deliveries stay exactly as
        # fail-closed as before.
        self._heal_unconfirmed_sent_deliveries()
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
            if reconciliation.empty_tracked_token_ids:
                # The crashed-exit heal: an empty tracked NFT is chain truth
                # saying the exit finished, so the stale tracking drops
                # before decide re-commands a dead position into the
                # position_empty refusal loop (2026-09-28 live wedge).
                for empty_token_id in reconciliation.empty_tracked_token_ids:
                    book = _book_with_position_removed(book, empty_token_id)
            book, reconciliation = self._adopt_into_book(book, reconciliation)
            decision_reconciliation = reconciliation
            # The decide phase reads the runner's cached reconciliation:
            # the adopted row's freshly folded status must be visible to it.
            self._last_reconciliation = reconciliation
            book = self._fold_recovered_positions(book, reconciliation)
            decision_report = self._decide(
                book,
                reference_price_usdc,
                reference_age_seconds,
                reference_prices_by_symbol or {},
            )
            outcome = decision_report.outcome
            portfolio_plan = decision_report.portfolio_plan
            any_unstaked = bool(book.positions) and any(
                not record.staked for record in reconciliation.position_statuses
            )
            if (
                mode is CycleMode.LIVE
                and outcome.decision.action is PolicyActionKind.HOLD
                and not (portfolio_plan is not None and portfolio_plan.steps)
                and any_unstaked
            ):
                if self._executor is None or key_bytes is None:
                    raise ValueError("a live cycle requires its signing key and executor")
                actions, halted_reason = self._recover_unstaked_position(book, key_bytes)
                final_reconciliation_verified = self._await_post_action_visibility(tuple(actions))
                if not final_reconciliation_verified and not halted_reason:
                    target = max(
                        (action.confirmed_block_number or 0 for action in actions), default=0
                    )
                    halted_reason = (
                        "post-action stake recovery is unverified because the primary RPC "
                        f"did not reach confirmed block {target} within the bounded wait"
                    )
            elif mode is CycleMode.LIVE and portfolio_plan is not None and portfolio_plan.steps:
                if self._executor is None or key_bytes is None:
                    raise ValueError("a live cycle requires its signing key and executor")
                actions, halted_reason, book = self._act_portfolio(book, portfolio_plan, key_bytes)
                final_reconciliation_verified = self._await_post_action_visibility(tuple(actions))
                if not final_reconciliation_verified and not halted_reason:
                    target = max(
                        (action.confirmed_block_number or 0 for action in actions), default=0
                    )
                    halted_reason = (
                        "post-action reconciliation is unverified because the primary RPC "
                        f"did not reach confirmed block {target} within the bounded wait"
                    )
            elif mode is CycleMode.LIVE and outcome.decision.action is not PolicyActionKind.HOLD:
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
            # The reward conversion is a treasury action of the act step, never
            # a policy verdict: it runs after the main action (so newly
            # claimed emissions convert too) and only on live cycles whose
            # main action did not halt (captain's 2026-09-27 ruling).
            if mode is CycleMode.LIVE and not halted_reason and decision_report is not None:
                aero_actions, aero_halted, book, converted_units = self._convert_rewards_if_due(
                    book, decision_report, key_bytes
                )
                actions.extend(aero_actions)
                if aero_halted and not halted_reason:
                    halted_reason = aero_halted
                if aero_actions:
                    conversion_visible = self._await_post_action_visibility(tuple(actions))
                    final_reconciliation_verified = final_reconciliation_verified and (
                        conversion_visible
                    )
                    if not conversion_visible and not halted_reason:
                        target = max(
                            (action.confirmed_block_number or 0 for action in actions), default=0
                        )
                        halted_reason = (
                            "post-action reconciliation is unverified because the primary RPC "
                            f"did not reach confirmed block {target} within the bounded wait"
                        )
            final_reconciliation = self._reconcile(book)
            stake_recovery_completed = any(
                action.action == "stake_recovery" and action.status == "completed"
                for action in actions
            )
            unstaked_after_recovery = tuple(
                record.token_id
                for record in final_reconciliation.position_statuses
                if not record.staked
            )
            if stake_recovery_completed and unstaked_after_recovery and not halted_reason:
                halted_reason = (
                    "stake recovery was confirmed but final reconciliation still sees "
                    f"position NFT(s) {list(unstaked_after_recovery)} unstaked; leaving "
                    "the cycle fail-closed for the next retry"
                )
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
                    decision_report,
                )
            elif not halted_reason:
                halted_reason = final_reconciliation.out_of_band
            reconciliation = final_reconciliation
        fee_evidence = self._fee_evidence(book, reconciliation)
        book = self._book_with_fee_sample(book, reconciliation)
        unclaimed_aero_units, unclaimed_aero_value = self._unclaimed_aero(
            reconciliation, decision_report
        )
        baseline_source = (
            decision_reconciliation if decision_reconciliation is not None else reconciliation
        )
        book = self._book_with_day_baseline(book, baseline_source, decision_report, converted_units)
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
            fee_evidence,
            unclaimed_aero_units,
            unclaimed_aero_value,
            self._yield_attribution(book, reconciliation, decision_report),
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
        if book.positions:
            return book.positions[0].symbol
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
        empty_tracked: list[int] = []
        safe_usdc = self._balances.fetch_token_balance(BASE_USDC_ADDRESS, self._safe_address)
        safe_aero = self._balances.fetch_token_balance(AERO_TOKEN_ADDRESS, self._safe_address)
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
        position_statuses: list[PositionStatusRecord] = []
        tracked_ids: set[int] = set()
        for tracked in book.positions:
            status = self._reads.position_status(
                tracked.symbol, tracked.token_id, entry_cost_usdc=tracked.committed_usd
            )
            owner = normalize_evm_address(status.token_owner_address)
            gauge = normalize_evm_address(status.gauge_address)
            if owner != self._safe_address and owner != gauge:
                out_of_band = (
                    f"tracked position {tracked.token_id} is owned by {owner}, which is "
                    "neither the Safe nor the pool's gauge; refusing the cycle"
                )
                break
            staked = owner == gauge
            tracked_ids.add(tracked.token_id)
            view = status.position
            fees_owed0_units = getattr(status, "fees_owed0_units", 0)
            fees_owed1_units = getattr(status, "fees_owed1_units", 0)
            if (
                not staked
                and getattr(view, "liquidity", 0) == 0
                and fees_owed0_units == 0
                and fees_owed1_units == 0
            ):
                # The position's exit already completed on-chain (a cycle
                # crashed between the withdraw and the re-entry); the empty
                # NFT carries no exposure to manage, so the tracking drops
                # here and decide never re-commands the dead token.
                empty_tracked.append(tracked.token_id)
                diagnostics.append(
                    f"tracked position {tracked.token_id} on {tracked.symbol} "
                    f"(in the Safe) valued {status.position_value_usdc} USDC against "
                    f"{tracked.committed_usd} committed"
                )
                diagnostics.append(
                    f"tracked position {tracked.token_id} on {tracked.symbol} reconciles "
                    "EMPTY on-chain (no liquidity, no owed fees, unstaked in the Safe); "
                    "dropping the stale tracking from the book - the exit already "
                    "completed on-chain when a crashed cycle died between the withdraw "
                    "and the re-entry, and the empty NFT carries no exposure to manage"
                )
                continue
            position_statuses.append(
                PositionStatusRecord(
                    symbol=tracked.symbol,
                    token_id=tracked.token_id,
                    pool_address=tracked.pool_address,
                    committed_usd=tracked.committed_usd,
                    staked=staked,
                    status=status,
                )
            )
            diagnostics.append(
                f"tracked position {tracked.token_id} on {tracked.symbol} "
                f"({'staked' if staked else 'in the Safe'}) valued "
                f"{status.position_value_usdc} USDC against "
                f"{tracked.committed_usd} committed"
            )
            if not staked:
                diagnostics.append(
                    f"tracked position {tracked.token_id} is unstaked in the Safe; "
                    "a live HOLD cycle will attempt bounded stake recovery before returning"
                )
        # The killed-multi-entry recovery (the 2026-09-30 timeout incident):
        # completed siblings staked in their gauges are invisible to the
        # Safe-owned inventory, so the audit chain's own evidence - one
        # execute-mode stake plan per completed sibling - verifies their
        # live custody and recovers them beside any surviving tracked
        # sibling before the equity observation judges anything.
        recovered: list[RecoveredPositionRecord] = []
        if not out_of_band:
            recovered, custody_refusal = self._recover_audit_proven_positions(
                tracked_ids, diagnostics
            )
            if custody_refusal:
                out_of_band = custody_refusal
            for record in recovered:
                position_statuses.append(
                    PositionStatusRecord(
                        symbol=record.symbol,
                        token_id=record.token_id,
                        pool_address=record.pool_address,
                        committed_usd=record.committed_usd,
                        staked=record.staked,
                        status=record.status,
                    )
                )
        recovered_ids = {record.token_id for record in recovered}
        if position_statuses and not out_of_band:
            primary = position_statuses[0]
            tracked_status = primary.status
            tracked_token_id = primary.token_id
            tracked_staked = primary.staked
            untracked_live = tuple(
                token
                for token in live_ids
                if token not in tracked_ids and token not in recovered_ids
            )
            if untracked_live:
                out_of_band = (
                    f"live untracked position NFT(s) {untracked_live} sit beside the "
                    "tracked positions; refusing the cycle until reconciled"
                )
        elif live_ids and not book.positions:
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
        # Each nonzero balance is then classified at its own pool's pinned
        # snapshot price: meaningful stock flows into the existing adoption
        # and conversion lanes, while dust below the economic floor is
        # retained in the Safe, visible in diagnostics and the audit record,
        # and never swapped (an exit swap of sub-cent stock costs more than
        # it returns - the 2026-10-02 post-closeout halt pinned this repair).
        stock_units = 0
        held_quantity = Decimal("0")
        held_symbol: str | None = None
        stray_candidates: list[tuple[str, str, int, PoolCandidate]] = []
        if book.held_inventory is not None:
            held_token = book.held_inventory.token_address
            held_units = self._balances.fetch_token_balance(held_token, self._safe_address)
            held_quantity = Decimal(held_units).scaleb(-self._sources.token_decimals(held_token))
            stock_units = held_units
            held_symbol = book.held_inventory.symbol
            if held_quantity == 0 and tracked_token_id is None:
                diagnostics.append("recorded held inventory no longer exists on-chain; clearing")
        else:
            if self.selector_mode and not book.positions:
                for listing in self._board_listings():
                    token = self._stock_token_of_pool(listing.pool)
                    units = self._balances.fetch_token_balance(token, self._safe_address)
                    if units > 0:
                        stray_candidates.append((listing.symbol, token, units, listing.pool))
            elif anchor != SELECTOR_SYMBOL:
                stock_address = self._stock_token_address_for(anchor)
                anchor_units = self._balances.fetch_token_balance(stock_address, self._safe_address)
                if anchor_units > 0:
                    stray_candidates.append(
                        (anchor, stock_address, anchor_units, self._pool_for_symbol(anchor))
                    )
        stray_stocks, ignored_dust, dust_refusal = self._partition_stray_stock(
            stray_candidates, diagnostics
        )
        if dust_refusal and not out_of_band:
            out_of_band = dust_refusal
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
            + (
                f", Safe holds {Decimal(safe_aero).scaleb(-AERO_DECIMALS)} unclaimed AERO"
                if safe_aero > 0
                else ""
            )
        )
        return CycleReconciliation(
            symbol=anchor,
            safe_usdc_units=safe_usdc,
            relayer_eth_wei=relayer_eth,
            safe_stock_units=stock_units,
            safe_aero_units=safe_aero,
            inventory_live_token_ids=live_ids,
            inventory_empty_count=empty_count,
            position_statuses=tuple(position_statuses),
            tracked_status=tracked_status,
            tracked_token_id=tracked_token_id,
            tracked_staked=tracked_staked,
            held_stock_quantity=held_quantity,
            held_symbol=held_symbol,
            ignored_stock_dust=tuple(ignored_dust),
            out_of_band=out_of_band,
            diagnostics=tuple(diagnostics),
            empty_tracked_token_ids=tuple(empty_tracked),
            recovered_positions=tuple(recovered),
        )

    def _partition_stray_stock(
        self,
        candidates: Sequence[tuple[str, str, int, PoolCandidate]],
        diagnostics: list[str],
    ) -> tuple[list[tuple[str, str, int]], list[IgnoredStockDust], str]:
        """Classify stray stock balances into meaningful stock and dust.

        Every nonzero unrecorded balance is valued at its own pool's freshly
        pinned snapshot price with the token's validated decimals. A balance
        strictly below the per-token dust floor is dust - retained in the
        Safe, never adopted, never swapped - but only while the sum of every
        ignored value stays at or below the aggregate bound, so splitting
        meaningful exposure across many tokens cannot hide behind the floor.
        A balance that cannot be priced is unknown truth, never safe to
        ignore: unpriceable stock refuses the cycle fail-closed.

        Args:
            candidates: Every nonzero stray balance as (symbol, token,
                units, pool), in board order.
            diagnostics: The reconciliation's evidence lines, receiving one
                line per ignored dust balance plus the aggregate summary.

        Returns:
            The meaningful stray stocks as (symbol, token, units) tuples,
            the ignored dust records, and any fail-closed refusal (empty
            when none).
        """
        if not candidates:
            return [], [], ""
        meaningful: list[tuple[str, str, int]] = []
        dust: list[IgnoredStockDust] = []
        for symbol, token, units, pool in candidates:
            try:
                decimals = self._sources.token_decimals(token)
                value = stock_value_usdc_at_pinned_snapshot(units, decimals, pool)
            except ValueError:
                value = None
            if value is None:
                return (
                    meaningful,
                    dust,
                    f"the Safe holds unrecorded {symbol} stock whose USDC value "
                    "cannot be priced at the pinned snapshot (untrusted decimals or a "
                    "degenerate pool ratio); refusing the cycle until reconciled - "
                    "unpriceable stock is never safe to ignore",
                )
            if value < self._stock_dust_floor_usdc:
                dust.append(
                    IgnoredStockDust(
                        symbol=symbol,
                        token_address=token,
                        quantity=Decimal(units).scaleb(-decimals),
                        value_usdc=value,
                    )
                )
            else:
                meaningful.append((symbol, token, units))
        if dust:
            total = sum((row.value_usdc for row in dust), Decimal("0"))
            if total > self._stock_dust_aggregate_usdc:
                return (
                    meaningful,
                    dust,
                    "the Safe's stray stock dust across ("
                    + ", ".join(row.symbol for row in dust)
                    + f") totals {total} USDC above the "
                    f"{self._stock_dust_aggregate_usdc} USDC aggregate bound; refusing "
                    "the cycle until reconciled - ignored exposure may never sum to "
                    "meaningful value",
                )
            for row in dust:
                diagnostics.append(
                    f"ignored {row.symbol} stock dust {row.quantity} tokens worth "
                    f"{row.value_usdc} USDC at the pinned snapshot price; retained in "
                    f"the Safe below the {self._stock_dust_floor_usdc} USDC dust floor - "
                    "never adopted as inventory, never swapped"
                )
            diagnostics.append(
                f"ignored stock dust across {len(dust)} pools totals {total} USDC, "
                f"within the {self._stock_dust_aggregate_usdc} USDC aggregate bound; "
                "every balance stays in the Safe"
            )
        return meaningful, dust, ""

    def _recover_audit_proven_positions(
        self,
        tracked_ids: set[int],
        diagnostics: list[str],
    ) -> tuple[list[RecoveredPositionRecord], str]:
        """Recover audit-proven untracked positions from their live custody.

        A killed multi-entry cycle (the 2026-09-30 thirty-minute systemd
        timeout) leaves its completed siblings staked in their gauges: the
        Safe-owned inventory cannot see gauge custody, so the next cycle
        omits their value from equity and the daily-loss latch reads the
        omission as a drawdown. The audit chain carries the proof - one
        execute-mode stake plan per completed sibling naming the NFT and
        its pool, the plan's recorded owner being this Safe - and the live
        status read verifies the custody. Only evidence both the audit
        chain and the chain state prove adopts; anything else skips
        quietly (burned or delisted history) or refuses the cycle (proven
        custody violated). A candidate whose status read cannot complete is
        unknown truth, not absence: the read propagates and fails the
        cycle for the retry, exactly like every other reconcile read.
        Pinned cycles never fold cross-symbol rows into their
        single-position book: they skip the recovery and say so, leaving
        the heal to the next selector cycle.

        Args:
            tracked_ids: Every tracked position's NFT id.
            diagnostics: The reconciliation's evidence lines.

        Returns:
            The recovered records and any out-of-band refusal (empty when
            none).

        Raises:
            ExecutionUnavailableError: If a candidate's live status read
                cannot complete.
            LpExecutionRefusalError: If a candidate's status read refuses
                for anything but burned or delisted history.
        """
        if self._audit_reader is None:
            return [], ""
        plans: dict[int, tuple[str, Decimal, datetime]] = {}
        last_budget_by_symbol: dict[str, Decimal] = {}
        for record in self._audit_reader.recent_records():
            payload = json.loads(record.payload_json)
            if record.event_type is AuditEventType.LP_MINT_PLANNED:
                # Only execute-mode plans price real deployments; rehearsal
                # plans price intentions.
                if payload.get("mode") != ExecutionMode.EXECUTE.value:
                    continue
                symbol = payload.get("symbol")
                budget = payload.get("budget_usdc")
                if not isinstance(symbol, str) or not isinstance(budget, str):
                    continue
                try:
                    budget_value = Decimal(budget)
                except InvalidOperation:
                    continue
                if budget_value > 0:
                    last_budget_by_symbol[symbol] = budget_value
                continue
            if record.event_type is not AuditEventType.LP_STAKE_PLANNED:
                continue
            if payload.get("mode") != ExecutionMode.EXECUTE.value:
                continue
            token_id = payload.get("token_id")
            symbol = payload.get("symbol")
            if not isinstance(token_id, int) or not isinstance(symbol, str):
                continue
            plan_owner = payload.get("token_owner_address")
            if not isinstance(plan_owner, str) or plan_owner.lower() != self._safe_address:
                # The audit store is shared by every Safe that executes
                # through it and a gauge is a shared custodian, so only the
                # plan's own recorded owner proves a candidate was minted
                # for this Safe.
                continue
            if token_id in tracked_ids or token_id in plans:
                continue
            budget = last_budget_by_symbol.get(symbol)
            if budget is None:
                # No audited mint basis for this sibling: nothing honest to
                # commit against, so nothing adopts.
                continue
            plans[token_id] = (symbol, budget, record.created_at)
        if self._symbol is not None:
            if plans:
                committed = sum(
                    (budget for _symbol, budget, _entered in plans.values()), Decimal("0")
                )
                diagnostics.append(
                    f"pinned {self._symbol} cycle skips {len(plans)} audit-proven "
                    f"untracked position candidate(s) {sorted(plans)} carrying "
                    f"{committed} USDC committed: a pinned book prices one position "
                    "only and never folds cross-symbol rows, so the next selector "
                    "cycle verifies and recovers them"
                )
            return [], ""
        recovered: list[RecoveredPositionRecord] = []
        # Newest plans first, bounded: the book holds at most ten concurrent
        # positions, so the newest dozen untracked candidates cover every
        # killed cycle's completed siblings however long the chain grows.
        for token_id, (symbol, budget, entered_at) in sorted(
            plans.items(), key=lambda item: item[1][2], reverse=True
        )[:STAKE_RECOVERY_CANDIDATE_LIMIT]:
            try:
                status = self._reads.position_status(symbol, token_id)
            except LpExecutionRefusalError as error:
                if error.code == LpExecutionRefusalCode.POSITION_NOT_OWNED:
                    return recovered, (
                        f"audit-proven position NFT {token_id} on {symbol} is owned by "
                        f"a stranger ({error}); refusing the cycle"
                    )
                if error.code in (
                    LpExecutionRefusalCode.POSITION_UNKNOWN,
                    LpExecutionRefusalCode.SYMBOL_NOT_IN_REGISTRY,
                    LpExecutionRefusalCode.POOL_NOT_DISCOVERED,
                    LpExecutionRefusalCode.POOL_MISSING_NFPM_OR_GAUGE,
                ):
                    # Burned or delisted history - an exited position's
                    # burned plan, or a pool the registry or venue no
                    # longer lists. Nothing adopts and nothing refuses.
                    continue
                raise
            owner = normalize_evm_address(status.token_owner_address)
            gauge = normalize_evm_address(status.gauge_address)
            view = getattr(status, "position", None)
            liquidity = getattr(view, "liquidity", 0)
            owed_fees = (
                getattr(status, "fees_owed0_units", 0) > 0
                or getattr(status, "fees_owed1_units", 0) > 0
            )
            if owner != self._safe_address and owner != gauge:
                return recovered, (
                    f"audit-proven position NFT {token_id} on {symbol} is owned by "
                    f"{owner}, which is neither the Safe nor the pool's gauge; refusing "
                    "the cycle"
                )
            if liquidity <= 0 and not owed_fees:
                # An empty residual NFT carries no exposure (the crashed-exit
                # doctrine); it never adopts and never blocks.
                continue
            staked = owner == gauge
            marked = (
                str(status.position_value_usdc)
                if status.position_value_usdc is not None
                else "an unmeasured"
            )
            recovered.append(
                RecoveredPositionRecord(
                    symbol=symbol,
                    token_id=token_id,
                    pool_address=normalize_evm_address(status.pool_address),
                    committed_usd=budget,
                    entered_at=entered_at,
                    staked=staked,
                    status=status,
                )
            )
            diagnostics.append(
                f"recovering audit-proven position NFT {token_id} on {symbol} "
                f"({'staked in the gauge' if staked else 'held in the Safe'}) valued "
                f"{marked} USDC against {budget} committed: the killed cycle's "
                "execute-mode stake plan and the live custody read prove it ours"
            )
        return recovered, ""

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

    # The Safe contract's ExecutionSuccess event topic, the inner-success
    # proof a delivery receipt must carry before crash-recovery may grant
    # it successful provenance (keccak of ExecutionSuccess(bytes32,uint256)).
    SAFE_EXECUTION_SUCCESS_TOPIC0 = (
        "0x442e715f626346e8c54381002da614f62bee8d27386535b2521ec8540898556e"
    )

    def _heal_unconfirmed_sent_deliveries(self) -> None:
        """Prove sent-but-unrecorded deliveries from their on-chain receipts.

        The executor appends one durable send record before every broadcast -
        the sent row, or its broadcast-unknown sibling when the endpoint's
        acknowledgement was lost after the node may already have accepted the
        transaction - and its receipt row after inclusion. A process death
        between the two leaves chain truth confirmed but the journal one row
        short, so the crashed entry could never prove itself and every later
        cycle refused out-of-band forever. The heal closes exactly that gap:
        for every send record of this runner's Safe that carries no receipt
        row, the on-chain receipt is fetched and its truthful outcome row
        appended - nothing more. Unknown or still-pending deliveries stay
        unproven (the fail-closed refusals stand), reverted deliveries
        record their failure, and no delivery is ever re-broadcast or
        guessed. The appended rows carry the posthumous provenance so the
        journal never misattributes them to a live execution.

        Raises:
            ExecutionUnavailableError: If a gap's receipt cannot be read -
                unknown truth fails the cycle for the retry, exactly like
                every other reconcile read.
        """
        if self._audit_reader is None or self._audit_sink is None:
            return
        records = self._audit_reader.recent_records()
        receipted_hashes: set[str] = set()
        sent_rows: list[tuple[str, str, str, str, str]] = []
        for record in records:
            payload = json.loads(record.payload_json)
            if record.event_type in (
                AuditEventType.LP_EXECUTE_SENT,
                AuditEventType.LP_EXECUTE_BROADCAST_UNKNOWN,
            ):
                # The audit store is shared by every Safe that executes
                # through it: only this runner's own sends heal here. A
                # malformed send row - missing its action, role, hashes, or
                # nonce - is ambiguous evidence and stays unproven.
                if str(payload.get("safe_address", "")).lower() != self._safe_address:
                    continue
                if not all(
                    str(payload.get(field, ""))
                    for field in ("action", "role", "safe_tx_hash", "transaction_hash")
                ):
                    continue
                sent_rows.append(
                    (
                        str(payload.get("action", "")),
                        str(payload.get("role", "")),
                        str(payload.get("relayer_address", "")),
                        str(payload.get("safe_tx_hash", "")),
                        str(payload.get("transaction_hash", "")),
                    )
                )
            elif record.event_type in (
                AuditEventType.LP_EXECUTE_CONFIRMED,
                AuditEventType.LP_EXECUTE_FAILED,
            ):
                receipted_hashes.add(str(payload.get("transaction_hash", "")))
        from aero_bot.lp_executor import LpExecuteReceiptPayload, LpExecutionRole
        from aero_bot.safe_tx import SAFE_CHAIN_ID

        # Receipt-derived provenance describes exactly one chain: verify
        # the endpoint serves the expected Base mainnet before anything is
        # appended, so a foreign-chain endpoint can never lend its truth
        # to this Safe's journal. The refusal appends nothing and
        # broadcasts nothing.
        if sent_rows:
            chain_id = self._balances.fetch_chain_id()
            if chain_id != SAFE_CHAIN_ID:
                raise ValueError(
                    "refusing to recover sent deliveries: the RPC endpoint reports "
                    f"chain {chain_id}, not Base mainnet {SAFE_CHAIN_ID}; recovered "
                    "provenance would describe a foreign chain"
                )

        for action, role, relayer, safe_tx_hash, transaction_hash in sent_rows:
            if transaction_hash in receipted_hashes:
                continue
            try:
                execution_role = LpExecutionRole(role)
            except ValueError as error:
                # A corrupt role is corrupt evidence: refuse the cycle with
                # a typed message - never crash, never heal, never guess.
                raise ValueError(
                    f"corrupt audit evidence: the sent row's role {role!r} is not a "
                    "known execution role; refusing to recover it"
                ) from error
            # Receipt lookups stay bounded to the unresolved gaps: every
            # already-receipted send is skipped above, so a healthy journal
            # costs this pass one local scan and zero RPC calls.
            receipt = self._balances.fetch_transaction_receipt(transaction_hash)
            if receipt is None:
                # Still pending or unknown: nothing is proven, nothing is
                # appended - the fail-closed refusal path stands.
                continue
            receipt_hash = str(receipt.get("transactionHash", transaction_hash))
            receipt_to = str(receipt.get("to", "")).lower()
            receipt_from = str(receipt.get("from", "")).lower()
            if (
                receipt_hash.lower() != transaction_hash.lower()
                or (receipt_to and receipt_to != self._safe_address)
                or (receipt_from and relayer and receipt_from != relayer.lower())
            ):
                # The receipt does not describe this Safe's own send through
                # its relayer: foreign evidence never gains provenance.
                continue
            status_word = receipt.get("status")
            try:
                status = (
                    int(str(status_word), 16)
                    if isinstance(status_word, str)
                    else int(status_word)
                    if isinstance(status_word, int)
                    else 0
                )
                block_number = int(str(receipt.get("blockNumber", "0x0")), 16)
                gas_used = int(str(receipt.get("gasUsed", "0x0")), 16)
                effective_gas_price = int(str(receipt.get("effectiveGasPrice", "0x0")), 16)
            except (TypeError, ValueError) as error:
                raise ExecutionUnavailableError(
                    f"the recovery receipt for {transaction_hash} was malformed: {error}"
                ) from error
            raw_logs = receipt.get("logs")
            logs = raw_logs if isinstance(raw_logs, list) else []
            topics = [
                str(topic).lower()
                for log in logs
                if isinstance(log, dict)
                for topic in (log.get("topics") or [])
            ]
            # The inner-success proof is the Safe's own ExecutionSuccess
            # event naming THIS submitted call's safe_tx_hash - not merely
            # an outer status word, and never an unrelated success event.
            safe_inner_success = any(
                str(log.get("topics", [""])[0]).lower() == self.SAFE_EXECUTION_SUCCESS_TOPIC0
                and len(log.get("topics") or []) > 1
                and str(log["topics"][1]).lower() == safe_tx_hash.lower()
                for log in logs
                if isinstance(log, dict)
            )
            # The mint's financial effect is the IncreaseLiquidity event
            # naming the position it created; without it a status-one
            # receipt still proves no mint happened. Adoption and accounting
            # keep consuming the existing reconciliation's custody and
            # balance effects - this proof only closes the journal gap.
            effect_proof = NFPM_INCREASE_LIQUIDITY_TOPIC0 in topics if role == "mint" else True
            confirmed = status == 1 and safe_inner_success and effect_proof
            failure_diagnostic = ""
            if status == 1 and not confirmed:
                failure_diagnostic = (
                    "the outer delivery reports success but the Safe's "
                    "ExecutionSuccess proof is missing"
                    if not safe_inner_success
                    else "the mint delivery carries no IncreaseLiquidity effect"
                )
            self._audit_sink.append(
                AuditEventType.LP_EXECUTE_CONFIRMED
                if confirmed
                else AuditEventType.LP_EXECUTE_FAILED,
                LpExecuteReceiptPayload(
                    outcome="confirmed" if confirmed else "failed",
                    action=action,
                    role=execution_role,
                    safe_tx_hash=safe_tx_hash,
                    transaction_hash=transaction_hash,
                    block_number=block_number,
                    gas_used=gas_used,
                    effective_gas_price_wei=effective_gas_price,
                    inclusion_ms=0,
                    diagnostic=(
                        (failure_diagnostic + "; " if failure_diagnostic else "")
                        + "recovered posthumously by crash-recovery: the durable send "
                        "record outlived its process before the receipt row landed; "
                        "the on-chain receipt proves this delivery's outcome"
                    ),
                ),
                self._now(),
            )
            receipted_hashes.add(transaction_hash)
            _cycle_progress(
                f"crash-recovery proved the sent {action} delivery {transaction_hash} "
                f"from its on-chain receipt ({'confirmed' if confirmed else 'failed'}); "
                "the truthful outcome row now closes the journal gap"
            )

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
    ) -> tuple[CycleStateBook, CycleReconciliation]:
        """Fold reconciliation adoptions into the book before deciding.

        The adopted row must also enter the reconciliation's own status
        rows: every status-driven surface - the per-position policy folds,
        the allocator's held facts, the equity's LP value - reads the
        reconciliation, and an adopted position without a status row is
        invisible to the decision that must steward it (the 2026-10-03
        gap-repair evidence: the allocator re-entered the adopted symbol,
        the fresh row replaced the adopted one, and the orphaned NFT
        refused the final reconciliation).
        """
        updates: dict[str, object] = {}
        adopted_reconciliation = reconciliation
        if (
            not book.positions
            and reconciliation.tracked_token_id is not None
            and not reconciliation.recovered_positions
            and not reconciliation.out_of_band
        ):
            proven = self._adoption_mint_label(reconciliation.tracked_token_id)
            if proven is None:
                _cycle_progress(
                    f"skipping the adoption of live position NFT "
                    f"{reconciliation.tracked_token_id}: no execute-mode mint plan "
                    "receipt-linked to its confirmed delivery proves its symbol and "
                    "committed basis; never guessing from the anchor pool or a "
                    "newer plan"
                )
            else:
                adopted_symbol, committed = proven
                adopted_token = reconciliation.tracked_token_id
                updates["positions"] = (
                    TrackedPosition(
                        symbol=adopted_symbol,
                        token_id=adopted_token,
                        pool_address=self._pool_for_symbol(adopted_symbol).pool_address,
                        committed_usd=committed,
                        entered_at=self._now(),
                    ),
                )
                adopted_status = self._adopted_position_status(
                    adopted_symbol, adopted_token, committed
                )
                adopted_reconciliation = reconciliation.model_copy(
                    update={
                        "position_statuses": (
                            *reconciliation.position_statuses,
                            PositionStatusRecord(
                                symbol=adopted_symbol,
                                token_id=adopted_token,
                                pool_address=self._pool_for_symbol(adopted_symbol).pool_address,
                                committed_usd=committed,
                                staked=normalize_evm_address(adopted_status.token_owner_address)
                                == normalize_evm_address(adopted_status.gauge_address),
                                status=adopted_status,
                            ),
                        )
                    }
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
            return book, adopted_reconciliation
        return book.model_copy(update=updates), adopted_reconciliation

    def _adopted_position_status(
        self, symbol: str, token_id: int, committed: Decimal
    ) -> LpPositionStatusReport:
        """Read one adopted position's live custody for this cycle's folds.

        The adoption happened after the reconciliation enumerated statuses,
        so the row would otherwise be invisible to the decision; the read
        is the same audited status surface the reconcile itself uses, and a
        refusal fails the cycle for the next tick's retry exactly like the
        reconcile's own status reads - never a guessed custody, and never a
        book-only adoption the decision cannot see and the allocator would
        re-enter.

        Raises:
            LpExecutionRefusalError: If any status gate refuses.
            ExecutionUnavailableError: If the read cannot complete.
        """
        return self._reads.position_status(symbol, token_id, entry_cost_usdc=committed)

    def _fold_recovered_positions(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> CycleStateBook:
        """Fold the reconcile's recovered positions into the book.

        Every recovered record is chain truth the book owes a tracking row:
        the equity observation, the held folds, and the total cap all see
        the position the moment it folds. When the recovery proves the
        prior cycle's equity reading omitted value, the daily-loss latch
        carried for today is dropped for re-derivation - the engine's own
        day observation re-latches immediately on any real drawdown, so a
        phantom latch from omitted siblings clears exactly here while a
        real loss never stops protecting (the 2026-09-30 timeout
        incident's fail-closed containment ending through verified
        reconciliation, never a manual reset).

        Args:
            book: The reconciled book before the fold.
            reconciliation: The reconciliation carrying the recovery.

        Returns:
            The book with every recovered position tracked.
        """
        if not reconciliation.recovered_positions:
            return book
        existing = {position.token_id for position in book.positions}
        additions = tuple(
            TrackedPosition(
                symbol=record.symbol,
                token_id=record.token_id,
                pool_address=record.pool_address,
                committed_usd=record.committed_usd,
                entered_at=record.entered_at,
            )
            for record in reconciliation.recovered_positions
            if record.token_id not in existing
        )
        if not additions:
            return book
        folded = book.model_copy(update={"positions": tuple(book.positions) + additions})
        _cycle_progress(
            "recovered audit-proven positions into the book: "
            + ", ".join(
                f"{record.symbol} NFT {record.token_id}"
                for record in reconciliation.recovered_positions
            )
        )
        today = self._now().astimezone(POLICY_TIMEZONE).date()
        if folded.halted_day == today:
            folded = folded.model_copy(update={"halted_day": None})
            _cycle_progress(
                "the carried daily-loss latch is dropped for re-derivation over the "
                "recovered book: the prior equity reading omitted the recovered "
                "positions, and the engine re-latches on any real drawdown"
            )
        return folded

    def _adoption_mint_label(self, token_id: int) -> tuple[str, Decimal] | None:
        """Read the symbol and committed basis proven for one adopted position.

        The proof is the execute-mode mint plan receipt-linked to the NFT's
        own confirmed delivery: the executor records the plan during the
        build, before the delivery it confirms, under the exclusive
        execution lock - so the newest plan at or before that confirmation
        is that NFT's own plan, and its payload names the pool the mint
        targeted. Neither the reconcile anchor (one shared NFPM spans every
        pool, so the adopting inventory proves nothing about the NFT's
        pool) nor the newest plan in the whole chain (a refused mint for
        another symbol can crown it) labels the row.

        Args:
            token_id: The adopted position's NFT id.

        Returns:
            The receipt-linked plan's symbol and mint budget, or None when
            no plan is proven for the NFT's confirmed delivery.
        """
        if self._audit_reader is None:
            return None
        records = self._audit_reader.recent_records()
        for offset, record in enumerate(reversed(records)):
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
                decoded = decode_minted_token_id(receipt)
            except (ValueError, ExecutionUnavailableError):
                continue
            if decoded != token_id:
                continue
            for older in reversed(records[: len(records) - offset - 1]):
                if older.event_type is not AuditEventType.LP_MINT_PLANNED:
                    continue
                older_payload = json.loads(older.payload_json)
                if older_payload.get("mode") != ExecutionMode.EXECUTE.value:
                    return None
                symbol = older_payload.get("symbol")
                budget = older_payload.get("budget_usdc")
                if not isinstance(symbol, str) or not isinstance(budget, str):
                    return None
                try:
                    value = Decimal(budget)
                except InvalidOperation:
                    return None
                return (symbol, value) if value > 0 else None
            return None
        return None

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------

    def _policy_position_of(
        self, tracked: TrackedPosition, status: LpPositionStatusReport
    ) -> PolicyPosition:
        """Reconstruct one tracked position's engine shape from live custody.

        Args:
            tracked: The book's record of the position.
            status: The position's live status read.

        Returns:
            The engine's typed position for one funded pool.
        """
        stock_address = self._stock_token_address_for(tracked.symbol)
        pool = self._pool_for_symbol(tracked.symbol)
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
        return PolicyPosition(
            pool_address=status.pool_address,
            token_address=stock_address,
            price_range=AlignedPriceRange(
                lower_tick=status.position.tick_lower,
                upper_tick=status.position.tick_upper,
                lower_price=min(first_edge_price, second_edge_price),
                upper_price=max(first_edge_price, second_edge_price),
            ),
            committed_usd=tracked.committed_usd,
            entered_at=tracked.entered_at,
            out_of_range_since=tracked.out_of_range_since,
            out_of_range_side=tracked.out_of_range_side,
        )

    def _policy_states(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> tuple[tuple[TrackedPosition, PolicyState], ...]:
        """Reconstruct one engine state per tracked position.

        Each fold judges exactly its own position over the shared session
        facts - the day anchors, the halt latch, and the portfolio equity
        seed - so per-position lifecycle discipline (safety exits,
        recenters, the out-of-range grace) survives the portfolio shape.

        Args:
            book: The persisted book whose positions reconcile.
            reconciliation: The reconciliation whose status reads carry
                each position's live custody.

        Returns:
            One (tracked position, engine state) pair per funded pool, in
            book order.
        """
        statuses = {record.token_id: record.status for record in reconciliation.position_statuses}
        # Each position's fold judges exactly its own lifecycle: no held
        # inventory (that resolves in its own lane) and no sibling blur.
        session = self._session_state(book, reconciliation).model_copy(
            update={"held_inventory": None}
        )
        states: list[tuple[TrackedPosition, PolicyState]] = []
        for tracked in book.positions:
            status = statuses.get(tracked.token_id)
            if status is None:
                continue
            states.append(
                (
                    tracked,
                    session.model_copy(
                        update={
                            "position": self._policy_position_of(tracked, status),
                            "reentry_blocked_until": _cooldown_until(book, tracked.symbol),
                            "dilution_exit_pending": _dilution_pending(book, tracked.symbol),
                        }
                    ),
                )
            )
        return tuple(states)

    def _session_state(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> PolicyState:
        """Reconstruct the shared session state every fold threads.

        The day anchors, the continuous-latch peak, and any halt latch are
        portfolio-level facts: the equity seed prices cash plus every
        tracked LP mark, so a deployed book never reads as a drawdown
        against a cash-only anchor.

        Args:
            book: The persisted book whose day facts carry forward.
            reconciliation: The reconciliation whose balances seed equity.

        Returns:
            The flat session state (no position, no inventory).
        """
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
            # The engine's day rollover is the authoritative anchor reset -
            # it prices the fully composed observation including held stock.
            # This seed only shapes the pre-decision state, so it carries the
            # same cash-plus-LP composition to keep deployed positions from
            # ever reading as a drawdown against a cash-only seed.
            day_start = Decimal(reconciliation.safe_usdc_units).scaleb(-6)
            for record in reconciliation.position_statuses:
                value = record.status.position_value_usdc
                if value is not None:
                    day_start += value
        return PolicyState(
            day=day if same_day else None,
            day_start_equity_usd=day_start,
            peak_equity_usd=book.peak_equity_usdc,
            halted_day=book.halted_day if same_day else None,
            position=None,
            held_inventory=held,
            reentry_blocked_until=None,
        )

    def _policy_state(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> PolicyState:
        """Reconstruct the single-position engine state from the book.

        The pinned-symbol path keeps its exact single-fold semantics: the
        first tracked position (there is at most one) over the shared
        session facts.

        Args:
            book: The persisted book whose position reconciles.
            reconciliation: The reconciliation whose status read carries
                the position's live custody.

        Returns:
            The engine state carrying the tracked position and any held
            inventory.
        """
        session = self._session_state(book, reconciliation)
        pairs = self._policy_states(book, reconciliation)
        if not pairs:
            return session
        _, state = pairs[0]
        return state

    def _cooldown_map(self, book: CycleStateBook) -> dict[str, datetime]:
        """Read the book's per-pool re-entry cooldowns as a mapping.

        Args:
            book: The persisted cycle book.

        Returns:
            Every pool's blocked-until instant keyed by symbol.
        """
        return {cooldown.symbol: cooldown.blocked_until for cooldown in book.reentry_cooldowns}

    def _dilution_map(self, book: CycleStateBook) -> dict[str, bool]:
        """Read the book's per-pool dilution re-entry markers as a mapping.

        Args:
            book: The persisted cycle book.

        Returns:
            Every marked pool's True keyed by symbol; unmarked pools are
            absent so lookups default to no marker.
        """
        return {
            cooldown.symbol: cooldown.dilution
            for cooldown in book.reentry_cooldowns
            if cooldown.dilution
        }

    def _reference_inputs_from_feed(
        self, b20_symbols: Sequence[str]
    ) -> tuple[dict[str, Decimal], dict[str, int], tuple[str, ...]]:
        """Read live reference quotes for the given symbols, fail-closed.

        Args:
            b20_symbols: The board symbols needing quotes; symbols the feed
                cannot quote are simply absent from the returned maps and
                carry explicit diagnostic notes instead.

        Returns:
            The per-symbol prices, the per-symbol honest ages measured from
            each provider's own as-of time at the decision instant, and the
            provenance/evidence notes for the report; no feed configured
            returns three empty containers.
        """
        if self._reference_feed is None or not b20_symbols:
            return {}, {}, ()
        result = self._reference_feed.fetch_quotes(b20_symbols)
        decided_at = self._now()
        return (
            result.price_by_symbol(),
            result.age_seconds_by_symbol(decided_at),
            result.notes(decided_at),
        )

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
            update={
                "reentry_blocked_until": _cooldown_until(book, self._symbol),
                "dilution_exit_pending": _dilution_pending(book, self._symbol),
            }
        )
        # The live reference feed fills the pinned symbol only when the
        # operator did not inject an explicit constant; an injected quote
        # keeps its operator-owned age, a feed quote carries the honest
        # as-of age instead.
        feed_notes: tuple[str, ...] = ()
        if reference_price_usdc is None:
            feed_prices, feed_ages, feed_notes = self._reference_inputs_from_feed((self._symbol,))
            if self._symbol in feed_prices:
                reference_price_usdc = feed_prices[self._symbol]
                reference_age_seconds = feed_ages[self._symbol]
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
        notes = notes + feed_notes
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
        # Idle AERO is book equity too: the reward conversion counts it in
        # the reconcile, so the equity the halt measures cannot ignore it.
        aero_units = self._balances.fetch_token_balance(AERO_TOKEN_ADDRESS, self._safe_address)
        if aero_units > 0 and aero_price is not None:
            aero_value = +(Decimal(aero_units).scaleb(-AERO_DECIMALS) * aero_price)
            observation = observation.model_copy(
                update={"equity_usd": observation.equity_usd + aero_value}
            )
            notes = notes + (
                f"equity includes {aero_value} USDC of unclaimed AERO at the observed price",
            )

        engine = PolicyEngine(self._parameters, load_event_calendar())
        # The pinned path stamps the same conservative income basis (the
        # captain's 2026-09-28 correction): expected-yield surfaces read
        # min(current, the trailing median) so a transient spike never
        # sizes a pinned entry either. Pinned runs carry their own book
        # window; the qualifying gates keep the raw reading.
        basis = conservative_income_apr(
            observation.emissions_apr,
            tuple(
                sample.reading
                for sample in book.apr_history
                if sample.symbol.lower() == (self._symbol or "").lower()
            )
            + (observation.emissions_apr,),
        )
        observation = observation.model_copy(update={"conservative_income_apr": basis})
        pinned_history = _trim_apr_history(
            (AprReadingSample(symbol=self._symbol, reading=observation.emissions_apr),),
            book.apr_history,
            self._income_history_cycles(),
        )
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
            apr_history=pinned_history,
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
        # The feed reads at decide time - after the board sweep - so quote
        # ages never silently absorb the sweep's minutes; injected
        # constants still win for their own symbols, and every feed quote
        # carries its provider's own as-of age into the per-symbol gate.
        uninjected = tuple(
            listing.symbol
            for listing in listings
            if listing.symbol not in reference_prices_by_symbol
        )
        feed_prices, feed_ages, feed_notes = self._reference_inputs_from_feed(uninjected)
        if feed_notes and len(uninjected) < len(listings):
            # Explicit precedence evidence: an injected constant excluded
            # these symbols from the feed read.
            injected_symbols = ", ".join(
                listing.symbol
                for listing in listings
                if listing.symbol in reference_prices_by_symbol
            )
            feed_notes = feed_notes + (
                f"reference feed skipped the injected symbol(s) {injected_symbols}; "
                "an injected constant wins over the live feed for its own symbols",
            )
        merged_references: dict[str, Decimal] = dict(reference_prices_by_symbol)
        merged_references.update(feed_prices)
        options, aero_price, gas_price, notes = assemble_board(
            self._sources,
            listings,
            snapshot_block,
            self._now(),
            None,
            merged_references,
            reference_age_seconds,
            self._safe_address,
            reference_ages_by_symbol=feed_ages or None,
        )
        notes = notes + feed_notes
        # Apply the same pool-authoritative doctrine to every selector option.
        # Selector sizing also reserves ten percent of the observed depth cap
        # so a few-percent live-depth move during a multi-step switch cannot
        # strand inventory between source exit and target mint.
        options = tuple(
            option.model_copy(
                update={
                    "observation": option.observation.model_copy(
                        update={
                            "reference_enforcement_enabled": False,
                            "pool_depth_usd": (
                                option.observation.pool_depth_usd * SELECTOR_DEPTH_HEADROOM_FRACTION
                            ),
                        }
                    )
                }
            )
            for option in options
        )
        notes = notes + (
            "selector sizing reserves 10% headroom below each observed pool-depth cap",
        )
        # Selector observations start from loose Safe balances. When LPs are
        # already tracked, add every position's live marked value to each
        # board option before policy evaluation so the daily-loss guard
        # sees total managed portfolio equity rather than falsely treating
        # deployed LP capital as a drawdown.
        statuses = self._last_reconciliation.position_statuses
        lp_value = sum(
            (
                record.status.position_value_usdc
                for record in statuses
                if record.status.position_value_usdc is not None
            ),
            Decimal("0"),
        )
        if lp_value > 0:
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
            notes = notes + (
                f"selector equity includes {lp_value} USDC of tracked LP marked value across "
                f"{len(statuses)} position(s)",
            )
        aero_units = self._balances.fetch_token_balance(AERO_TOKEN_ADDRESS, self._safe_address)
        if aero_units > 0 and aero_price is not None:
            aero_value = +(Decimal(aero_units).scaleb(-AERO_DECIMALS) * aero_price)
            options = tuple(
                option.model_copy(
                    update={
                        "observation": option.observation.model_copy(
                            update={"equity_usd": option.observation.equity_usd + aero_value}
                        )
                    }
                )
                for option in options
            )
            notes = notes + (
                f"selector equity includes {aero_value} USDC of unclaimed AERO at the "
                "observed price",
            )
        notes = notes + (
            "external references are diagnostic-only; selector actions use each "
            "resolved Aerodrome pool's on-chain price and state",
        )
        engine = PolicyEngine(self._parameters, load_event_calendar())
        session_state = self._session_state(book, self._last_reconciliation)
        cooldowns = self._cooldown_map(book)
        # The conservative income basis (the captain's 2026-09-28
        # correction) stamps every board observation before ANY evaluation:
        # each pool's expected-yield surfaces - the held folds' recenter
        # economics included - read min(current, the trailing-cycle median)
        # so a transient spike never sizes or justifies a position, while
        # the qualifying gates, the ranking, and the dilution monitor keep
        # the raw venue-convention reading - information, never exclusion.
        options, successor_history, income_evidence = self._income_basis_stamping(options, book)
        options_by_symbol = {option.symbol: option for option in options}
        # One fold per held position: each position's own lifecycle - its
        # safety exits, its recenters, its out-of-range grace - is judged by
        # the per-pool engine over exactly its own position, never blurred
        # across the portfolio.
        held_folds: dict[str, PolicyOutcome] = {}
        held_facts: list[HeldPositionFact] = []
        penalty_blocked: set[str] = set()
        for tracked, state in self._policy_states(book, self._last_reconciliation):
            option = options_by_symbol.get(tracked.symbol)
            if option is None:
                raise ValueError(
                    f"the tracked position's pool for {tracked.symbol} is not on the "
                    "enumerated board"
                )
            fold = engine.decide(state, option.observation)
            held_folds[tracked.symbol] = fold
            status = next(
                (record.status for record in statuses if record.token_id == tracked.token_id),
                None,
            )
            penalty = getattr(status, "penalty", None)
            if penalty is not None and penalty.remaining_seconds > 0:
                penalty_blocked.add(tracked.symbol)
            held_facts.append(
                HeldPositionFact(
                    symbol=tracked.symbol,
                    token_id=tracked.token_id,
                    committed_usd=tracked.committed_usd,
                    marked_usd=(
                        status.position_value_usdc
                        if status is not None and status.position_value_usdc is not None
                        else None
                    ),
                    entered_at=tracked.entered_at,
                    emissions_apr=option.observation.emissions_apr,
                )
            )
        # The held-inventory fold resolves its own lane: convergence or the
        # timeout sell, judged without any position so a stale-low burn never
        # blocks another position's safety exits.
        inventory_symbol: str | None = None
        inventory_outcome: PolicyOutcome | None = None
        if book.held_inventory is not None:
            inventory_symbol = book.held_inventory.symbol
            inventory_option = options_by_symbol.get(inventory_symbol)
            if inventory_option is None:
                raise ValueError(
                    f"the held inventory's pool for {inventory_symbol} is not on the "
                    "enumerated board"
                )
            inventory_outcome = engine.decide(session_state, inventory_option.observation)
        # The board evaluation stays exactly the selector's: the complete
        # entry gate chain per pool over the flat session posture, over the
        # conservatively-stamped observations.
        dilutions = self._dilution_map(book)
        evaluations = evaluate_pool_entries(engine, session_state, options, cooldowns, dilutions)
        cash_usdc = Decimal(self._last_reconciliation.safe_usdc_units).scaleb(-6)
        equity_usdc = options[0].observation.equity_usd if options else cash_usdc + lp_value
        portfolio_parameters = self._portfolio_parameters()
        allocation = allocate_portfolio(
            engine,
            session_state,
            evaluations,
            cooldowns,
            held=held_facts,
            dilution_exit_pending_by_symbol=dilutions,
            cash_usdc=cash_usdc,
            equity_usdc=equity_usdc,
            parameters=portfolio_parameters,
            inventory_pending=book.held_inventory is not None,
        )
        plan = plan_portfolio_rebalance(
            engine,
            allocation,
            evaluations,
            session_state,
            cooldowns,
            held_facts,
            held_folds,
            cash_usdc,
            equity_usdc,
            inventory_symbol=inventory_symbol,
            inventory_outcome=inventory_outcome,
            parameters=portfolio_parameters,
            now=self._now(),
            penalty_blocked_symbols=frozenset(penalty_blocked),
        )
        # Every pool fold calls the engine's day transition, but the shared
        # session_state above was still its unobserved INPUT. Persisting it
        # reset the NY day and high-water mark on every portfolio cycle,
        # losing a five-percent halt before the next cycle could honor it.
        # Observe the selected pool's full portfolio equity once for the
        # session facts shared by every fold and by the reported day P&L.
        decision_option = self._option_for_plan(plan, options, evaluations)
        session_state = engine.observe_day(session_state, decision_option.observation)
        # The leading outcome: the first planned step when the plan acts,
        # else a portfolio hold over the observed session facts. A flat book never
        # carries a position-scoped reason (the gnhf 34 flat-label fix):
        # flat with qualifying pools names the awaiting-entry posture and
        # flat with none names the empty board, exactly like the
        # pnl_diagnostic layer that already says no tracked position.
        if plan.steps:
            outcome = plan.steps[0].outcome
        else:
            any_qualified = any(evaluation.qualifies for evaluation in evaluations)
            if book.positions:
                reason = PolicyReason.OPEN_IN_RANGE
            elif any_qualified:
                reason = PolicyReason.FLAT_AWAITING_ENTRY
            else:
                reason = PolicyReason.NO_QUALIFYING_POOL
            outcome = PolicyOutcome(
                decision=PolicyDecision(
                    action=PolicyActionKind.HOLD,
                    reason=reason,
                    diagnostics=(allocation.summary, plan.summary, *income_evidence),
                ),
                next_state=session_state,
            )
        # The idle-book evidence: cash undeployed while in-band pools
        # stayed excluded by gates or bounds, with the episode signature
        # the alert layer rate-limits on (the captain's gnhf 34 ruling -
        # a silent idle book must never happen again).
        idle_cash = _idle_cash_state(allocation, cash_usdc, equity_usdc, book)
        window = evaluate_event_window(self._now(), decision_option.token_address, engine.calendar)
        summary = f"portfolio: {allocation.summary}; {plan.summary}; " + board_summary_line(
            evaluations, decision_option.symbol
        )
        if income_evidence:
            summary += "; " + " ".join(income_evidence)
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
            reference_price_usdc=merged_references.get(decision_option.symbol),
            event_window=window,
            outcome=outcome,
            input_notes=notes + (summary,),
            selector_mode=True,
            board=evaluations,
            portfolio_plan=plan,
            held_folds=held_folds,
            session_state=session_state,
            board_summary=summary,
            gate_trace_basis=allocation.gate_trace_basis,
            gate_trace=allocation.gate_trace,
            idle_cash=idle_cash,
            apr_history=successor_history,
            income_basis_evidence=income_evidence,
        )

    def _option_for_plan(
        self,
        plan: PortfolioRebalancePlan,
        options: tuple[PoolBoardOption, ...],
        evaluations: tuple[PoolEntryEvaluation, ...],
    ) -> PoolBoardOption:
        """Resolve the board option the plan's leading step acts on.

        Args:
            plan: The portfolio rebalance plan.
            options: The assembled board options in listing order.
            evaluations: The board's complete entry-gate evaluations.

        Returns:
            The option the leading step enters, exits, or reallocates
            into; the closest-call pool when the plan holds.
        """
        if plan.steps:
            leading = plan.steps[0]
            symbol = leading.to_symbol or leading.symbol
            for option in options:
                if option.symbol == symbol:
                    return option
        closest = closest_call_evaluation(evaluations)
        if closest is not None:
            for option in options:
                if option.symbol == closest.symbol:
                    return option
        return options[0]

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------

    def _recover_unstaked_position(
        self,
        book: CycleStateBook,
        key_bytes: bytes,
    ) -> tuple[list[CycleActionRecord], str]:
        """Restake valid tracked NFTs left in the Safe after a partial cycle.

        This recovery is intentionally narrow: reconciliation already proved
        every tracked token is owned by the Safe (not a stranger), the
        policy verdict is HOLD, and no normal policy action needs the NFT
        unstaked. Only a position the decision reconciliation recorded
        unstaked is staked - restaking a gauge-held NFT is a broadcast the
        executor always refuses. The existing audited stake executor
        remains the only broadcast surface; one position's recovery
        failure stops the pass exactly like any other action.
        """
        executor = self._executor
        assert executor is not None  # noqa: S101 - live run validated the boundary
        records: list[CycleActionRecord] = []
        for tracked in book.positions:
            if self._position_staked(tracked.token_id):
                continue
            try:
                report = executor.execute_stake(
                    tracked.symbol,
                    tracked.token_id,
                    key_bytes,
                    confirm_broadcast=True,
                )
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
                        action="stake_recovery",
                        status="refused",
                        transaction_hashes=hashes,
                        fee_wei=fees,
                        confirmed_block_number=max(blocks) if blocks else None,
                        refusal_code=code,
                        diagnostic=str(error),
                    )
                )
                return records, f"the stake recovery action refused [{code}]"
            except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
                records.append(
                    CycleActionRecord(
                        action="stake_recovery",
                        status="failed",
                        diagnostic=str(error),
                    )
                )
                return records, f"the stake recovery action failed: {error}"
            hashes, fees, confirmed_block = _action_hashes_fees_and_block(report)
            completed = _action_completed(report)
            records.append(
                CycleActionRecord(
                    action="stake_recovery",
                    status="completed" if completed else "failed",
                    transaction_hashes=hashes,
                    fee_wei=fees,
                    confirmed_block_number=confirmed_block,
                    diagnostic=report.halted_reason,
                )
            )
            if not completed:
                return records, f"the stake recovery action halted: {report.halted_reason}"
        return records, ""

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
            mint_bounds = self._exact_bounds_from_decision(decision)
            if not run(
                "mint",
                lambda: executor.execute_mint(
                    decision_symbol,
                    budget,
                    mint_width,
                    key_bytes,
                    confirm_broadcast=True,
                    exact_tick_bounds=mint_bounds,
                ),
            ):
                held_quantity = self._live_stock_quantity(decision_symbol)
                failed_book = book
                if held_quantity > 0:
                    existing_retry = (
                        book.held_inventory
                        if book.held_inventory is not None
                        and book.held_inventory.symbol == decision_symbol
                        and book.held_inventory.origin in ("failed_entry", "failed_recenter")
                        else None
                    )
                    failed_book = book.model_copy(
                        update={
                            "held_inventory": HeldInventoryRecord(
                                symbol=decision_symbol,
                                token_address=self._stock_token_address_for(decision_symbol),
                                stock_quantity=held_quantity,
                                held_since=(
                                    existing_retry.held_since
                                    if existing_retry is not None
                                    else self._now()
                                ),
                                origin=(
                                    existing_retry.origin
                                    if existing_retry is not None
                                    else "failed_entry"
                                ),
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
                    exact_tick_bounds=self._exact_bounds_from_decision(decision),
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
            if book.position is None:
                halted = "the engine authorized a switch while flat"
                return records, halted, book
            if not self._exit_position(executor, book.position, key_bytes, run):
                return records, halted, book
            switched_book = book.model_copy(update={"positions": (), "held_inventory": None})
            if not run(
                "mint",
                lambda: executor.execute_mint(
                    switch.to_symbol,
                    switch_budget,
                    switch_width,
                    key_bytes,
                    confirm_broadcast=True,
                    exact_tick_bounds=self._exact_bounds_from_decision(decision),
                ),
            ):
                held_quantity = self._live_stock_quantity(switch.to_symbol)
                if held_quantity > 0:
                    switched_book = switched_book.model_copy(
                        update={
                            "held_inventory": HeldInventoryRecord(
                                symbol=switch.to_symbol,
                                token_address=self._stock_token_address_for(switch.to_symbol),
                                stock_quantity=held_quantity,
                                held_since=self._now(),
                                origin="failed_entry",
                            )
                        }
                    )
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
            PolicyActionKind.RANGE_GRACE_EXIT,
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
                        exact_tick_bounds=self._exact_bounds_from_decision(decision),
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
                self._burn_without_swap(executor, book.position, key_bytes, run)
                if action in (PolicyActionKind.RECENTER, PolicyActionKind.RANGE_GRACE_EXIT)
                else self._exit_position(executor, book.position, key_bytes, run)
            )
            if not exit_ok:
                return records, halted, book
            if action is PolicyActionKind.RANGE_GRACE_EXIT:
                # Income protection: the stock withdrawn below the range
                # routes through inventory convergence; above the range the
                # composition was already all USDC and the exit completes flat.
                held_quantity = (
                    self._live_stock_quantity(tracked_symbol)
                    if tracked_symbol is not None
                    else Decimal("0")
                )
                if held_quantity > 0 and tracked_symbol is not None:
                    return (
                        records,
                        halted,
                        _book_with_position_removed(
                            book.model_copy(
                                update={
                                    "held_inventory": HeldInventoryRecord(
                                        symbol=tracked_symbol,
                                        token_address=self._stock_token_address_for(tracked_symbol),
                                        stock_quantity=held_quantity,
                                        held_since=self._now(),
                                        origin="out_of_range_exit",
                                    )
                                }
                            ),
                            book.position.token_id,
                        ),
                    )
                return records, halted, _book_with_position_removed(book, book.position.token_id)
            if action is PolicyActionKind.RECENTER:
                size = decision.size_usd
                width = self._width_from_range(decision.price_range, tracked_symbol)
                if size is None or size <= 0 or width is None or tracked_symbol is None:
                    halted = "the recenter decision carried no complete fresh entry"
                    return (
                        records,
                        halted,
                        book.model_copy(update={"positions": (), "held_inventory": None}),
                    )
                recenter_budget: Decimal = size
                recenter_width: int = width
                recenter_bounds = self._exact_bounds_from_decision(decision)
                if not run(
                    "mint",
                    lambda: executor.execute_mint(
                        tracked_symbol,
                        recenter_budget,
                        recenter_width,
                        key_bytes,
                        confirm_broadcast=True,
                        exact_tick_bounds=recenter_bounds,
                    ),
                ):
                    held_quantity = self._live_stock_quantity(tracked_symbol)
                    failed_book = book.model_copy(update={"positions": (), "held_inventory": None})
                    if held_quantity > 0:
                        failed_book = failed_book.model_copy(
                            update={
                                "held_inventory": HeldInventoryRecord(
                                    symbol=tracked_symbol,
                                    token_address=self._stock_token_address_for(tracked_symbol),
                                    stock_quantity=held_quantity,
                                    held_since=self._now(),
                                    origin="failed_recenter",
                                )
                            }
                        )
                    return records, halted, failed_book
                token_id = self._decode_mint_token_id(records[-1])
                if token_id is None:
                    halted = "the recentered position id could not be decoded"
                    return (
                        records,
                        halted,
                        book.model_copy(update={"positions": (), "held_inventory": None}),
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
                        book.model_copy(update={"positions": (), "held_inventory": None}),
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
                book.model_copy(update={"positions": (), "held_inventory": None}),
            )

        if action is PolicyActionKind.STALE_LOW_BURN:
            if book.position is None or tracked_symbol is None:
                halted = "the engine authorized a stale-low burn while flat"
                return records, halted, book
            if not self._burn_without_swap(executor, book.position, key_bytes, run):
                return records, halted, book
            held_quantity = self._live_stock_quantity(tracked_symbol)
            if held_quantity <= 0:
                halted = "the stale-low burn left no stock balance to hold"
                return records, halted, _book_with_position_removed(book, book.position.token_id)
            return (
                records,
                halted,
                _book_with_position_removed(
                    book.model_copy(
                        update={
                            "held_inventory": HeldInventoryRecord(
                                symbol=tracked_symbol,
                                token_address=self._stock_token_address_for(tracked_symbol),
                                stock_quantity=held_quantity,
                                held_since=self._now(),
                                origin="stale_low_exit",
                            )
                        }
                    ),
                    book.position.token_id,
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

    def _act_portfolio(  # noqa: PLR0912, PLR0915 - one fixed step mapping
        self,
        book: CycleStateBook,
        plan: PortfolioRebalancePlan,
        key_bytes: bytes,
    ) -> tuple[list[CycleActionRecord], str, CycleStateBook]:
        """Execute the portfolio plan's ordered steps through audited surfaces.

        The steps run in the plan's fixed order - safety exits, the
        inventory resolution, recenters, reallocations, then entries - so
        every exit lands before the entry it funds and the total cap is
        never breached even transiently. Any refusal or failure halts the
        cycle exactly like the pinned path: a completed prefix is chain
        truth the next cycle reconciles.

        Args:
            book: The reconciled book the steps mutate.
            plan: The allocator's ordered rebalance plan.
            key_bytes: The signing key for live broadcasts.

        Returns:
            The action records, a halted reason (empty on success), and the
            mutated book.
        """
        executor = self._executor
        assert executor is not None  # noqa: S101 - the caller verified liveness
        records: list[CycleActionRecord] = []
        halted = ""

        def run(name: str, call: Callable[[], LpActionExecutionReport]) -> bool:
            """Run one audited action, recording its outcome."""
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

        def mint_and_stake(
            working: CycleStateBook, symbol: str, budget: Decimal, width: int
        ) -> CycleStateBook | None:
            """Mint and stake one position, returning the mutated book."""
            nonlocal halted
            # The executor's total-cap gate sees the book's other tracked
            # positions as expected exposure, never as strangers.
            live_positions = tuple(
                (position.token_id, position.committed_usd) for position in working.positions
            )
            step_bounds = self._exact_bounds_from_decision(decision)
            if not run(
                "mint",
                lambda: executor.execute_mint(
                    symbol,
                    budget,
                    width,
                    key_bytes,
                    confirm_broadcast=True,
                    portfolio_live_positions=live_positions,
                    exact_tick_bounds=step_bounds,
                ),
            ):
                held_quantity = self._live_stock_quantity(symbol)
                failed_book = working
                if held_quantity > 0:
                    existing_retry = (
                        working.held_inventory
                        if working.held_inventory is not None
                        and working.held_inventory.symbol == symbol
                        and working.held_inventory.origin in ("failed_entry", "failed_recenter")
                        else None
                    )
                    failed_book = working.model_copy(
                        update={
                            "held_inventory": HeldInventoryRecord(
                                symbol=symbol,
                                token_address=self._stock_token_address_for(symbol),
                                stock_quantity=held_quantity,
                                held_since=(
                                    existing_retry.held_since
                                    if existing_retry is not None
                                    else self._now()
                                ),
                                origin=(
                                    existing_retry.origin
                                    if existing_retry is not None
                                    else "failed_entry"
                                ),
                            )
                        }
                    )
                return failed_book
            token_id = self._decode_mint_token_id(records[-1])
            if token_id is None:
                halted = "the minted position id could not be decoded from the receipt"
                return working
            minted_id: int = token_id
            if not run(
                "stake",
                lambda: executor.execute_stake(
                    symbol, minted_id, key_bytes, confirm_broadcast=True
                ),
            ):
                return working
            committed = records[-2].executed_budget_usdc or budget
            return self._book_with_position(working, symbol, minted_id, committed)

        for step in plan.steps:
            if halted:
                break
            tracked = next(
                (position for position in book.positions if position.token_id == step.token_id),
                None,
            )
            decision = step.outcome.decision
            if step.kind is PortfolioStepKind.POSITION_ACTION:
                if tracked is None:
                    halted = (
                        f"the plan's {step.symbol} step names token {step.token_id} the "
                        "book does not track"
                    )
                    break
                action = decision.action
                if action is PolicyActionKind.RECENTER:
                    size = decision.size_usd
                    width = self._width_from_range(decision.price_range, step.symbol)
                    if size is None or size <= 0 or width is None:
                        halted = "the recenter decision carried no complete fresh entry"
                        break
                    try:
                        executor.dry_run_recenter(
                            step.symbol,
                            tracked.token_id,
                            width,
                            size,
                            key_bytes,
                            portfolio_live_positions=tuple(
                                (position.token_id, position.committed_usd)
                                for position in book.positions
                            ),
                            exact_tick_bounds=self._exact_bounds_from_decision(decision),
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
                        break
                    except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
                        records.append(
                            CycleActionRecord(
                                action="recenter_preflight",
                                status="failed",
                                diagnostic=str(error),
                            )
                        )
                        halted = f"the recenter preflight failed: {error}"
                        break
                    if not self._burn_without_swap(executor, tracked, key_bytes, run):
                        break
                    after_burn = _book_with_position_removed(book, tracked.token_id)
                    reborn = mint_and_stake(after_burn, step.symbol, size, width)
                    if reborn is not None:
                        book = reborn
                    if halted:
                        break
                    continue
                # Every other position action fully unwinds the position.
                burn_only = action is PolicyActionKind.RANGE_GRACE_EXIT or (
                    action is PolicyActionKind.STALE_LOW_BURN
                )
                exit_ok = (
                    self._burn_without_swap(executor, tracked, key_bytes, run)
                    if burn_only
                    else self._exit_position(executor, tracked, key_bytes, run)
                )
                if not exit_ok:
                    break
                book = _book_with_position_removed(book, tracked.token_id)
                if burn_only:
                    held_quantity = self._live_stock_quantity(step.symbol)
                    if held_quantity > 0:
                        book = book.model_copy(
                            update={
                                "held_inventory": HeldInventoryRecord(
                                    symbol=step.symbol,
                                    token_address=self._stock_token_address_for(step.symbol),
                                    stock_quantity=held_quantity,
                                    held_since=self._now(),
                                    origin=(
                                        "out_of_range_exit"
                                        if action is PolicyActionKind.RANGE_GRACE_EXIT
                                        else "stale_low_exit"
                                    ),
                                )
                            }
                        )
                continue
            if step.kind is PortfolioStepKind.INVENTORY_ACTION:
                if book.held_inventory is None:
                    halted = "the plan authorized an inventory sale with nothing held"
                    break
                inventory_symbol = step.symbol

                def _exit_inventory(
                    bound_symbol: str = inventory_symbol,
                ) -> LpActionExecutionReport:
                    return executor.execute_exit_swap(
                        bound_symbol, key_bytes, confirm_broadcast=True
                    )

                if not run("exit_swap", _exit_inventory):
                    break
                book = book.model_copy(update={"held_inventory": None})
                continue
            if step.kind is PortfolioStepKind.REALLOCATE:
                if tracked is None or step.to_symbol is None:
                    halted = "the reallocation step carried no complete pair"
                    break
                size = decision.size_usd
                width = self._width_from_range(decision.price_range, step.to_symbol)
                if size is None or size <= 0 or width is None:
                    halted = "the reallocation decision carried no complete fresh entry"
                    break
                try:
                    executor.dry_run_switch(
                        step.symbol,
                        tracked.token_id,
                        step.to_symbol,
                        width,
                        size,
                        key_bytes,
                        portfolio_live_positions=tuple(
                            (position.token_id, position.committed_usd)
                            for position in book.positions
                        ),
                        exact_tick_bounds=self._exact_bounds_from_decision(decision),
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
                    break
                except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
                    records.append(
                        CycleActionRecord(
                            action="switch_preflight",
                            status="failed",
                            diagnostic=str(error),
                        )
                    )
                    halted = f"the switch preflight failed: {error}"
                    break
                if not self._exit_position(executor, tracked, key_bytes, run):
                    break
                after_exit = _book_with_position_removed(book, tracked.token_id)
                reborn = mint_and_stake(after_exit, step.to_symbol, size, width)
                if reborn is not None:
                    book = reborn
                if halted:
                    break
                continue
            if step.kind is PortfolioStepKind.ENTER:
                size = decision.size_usd
                width = self._width_from_range(decision.price_range, step.symbol)
                if size is None or size <= 0 or width is None:
                    halted = "the enter decision carried no positive size or width"
                    break
                entered = mint_and_stake(book, step.symbol, size, width)
                if entered is not None:
                    book = entered
                if halted:
                    break
                continue
            halted = f"the portfolio plan carried an unmapped step kind {step.kind.value}"
            break
        return records, halted, book

    def _book_with_position(
        self, book: CycleStateBook, symbol: str, token_id: int, committed: Decimal
    ) -> CycleStateBook:
        """Return the book carrying one freshly entered tracked position."""
        tracked = TrackedPosition(
            symbol=symbol,
            token_id=token_id,
            pool_address=self._pool_for_symbol(symbol).pool_address,
            committed_usd=committed,
            entered_at=self._now(),
        )
        entered = _book_with_position_added(
            book.model_copy(update={"held_inventory": None}), tracked
        )
        # The per-sibling durability checkpoint (the 2026-09-30 timeout
        # incident): the mint and its gauge stake are confirmed on-chain,
        # so the book persists this position the moment it exists. An
        # interruption between siblings - the thirty-minute service
        # timeout, a crash, a host loss - can never again leave completed
        # positions outside the book for the next cycle's equity
        # observation to misread as a loss.
        self._state_store.save(entered)
        _cycle_progress(
            f"book checkpoint saved: {symbol} position NFT {token_id} minted, "
            "staked, and tracked; the aggregate rebuild still ends the cycle"
        )
        return entered

    def _position_staked(self, token_id: int) -> bool:
        """Read one position's staked custody from the decision reconciliation.

        Args:
            token_id: The position NFT id being looked up.

        Returns:
            True when the reconciliation saw the NFT in its gauge.
        """
        assert self._last_reconciliation is not None  # noqa: S101 - set by run()
        for record in self._last_reconciliation.position_statuses:
            if record.token_id == token_id:
                return record.staked
        return self._last_reconciliation.tracked_staked and (
            self._last_reconciliation.tracked_token_id == token_id
        )

    def _exit_position(
        self,
        executor: CycleExecutorBoundary,
        tracked: TrackedPosition,
        key_bytes: bytes,
        run: Callable[[str, Callable[[], LpActionExecutionReport]], bool],
    ) -> bool:
        """Unstake, withdraw, and swap one tracked position fully to USDC."""
        symbol = tracked.symbol
        if self._position_staked(tracked.token_id) and not run(
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
        tracked: TrackedPosition,
        key_bytes: bytes,
        run: Callable[[str, Callable[[], LpActionExecutionReport]], bool],
    ) -> bool:
        """Unstake and withdraw while holding the stock unsold."""
        symbol = tracked.symbol
        if self._position_staked(tracked.token_id) and not run(
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

    def _exact_bounds_from_decision(self, decision: PolicyDecision) -> tuple[int, int] | None:
        """Extract the adaptive solve's exact raw bounds from one decision.

        The scored bounds are the minted bounds: the width solution carries
        the raw-grid pair verbatim and the executor mints it with no
        symmetric re-derivation. None when the decision carries no solved
        bounds (the legacy width fallback then applies).

        Args:
            decision: An enter, recenter, or switch decision.

        Returns:
            The raw (lower, upper) tick pair, or None.
        """
        solution = decision.width_solution
        if solution is None or solution.lower_bound is None or solution.upper_bound is None:
            return None
        lower, upper = solution.lower_bound.tick, solution.upper_bound.tick
        if lower > upper:
            lower, upper = upper, lower
        return (lower, upper)

    def _width_from_range(
        self, price_range: AlignedPriceRange | None, symbol: str | None
    ) -> int | None:
        """Derive the explicit half width in tick spacings from a policy range.

        The engine's grid alignment can leave a non-integral spacing half
        width (a 70-tick span on a spacing-10 pool), so the mapping rounds
        up: the executed range always carries at least the policy's width,
        and the planner's own ceiling clamps anything wider. Exact-bound
        ranges order by PRICE, so under token1 orientation the raw ticks
        invert; the span's absolute value carries the width either way, and
        the adaptive path mints the exact bounds regardless.
        """
        if price_range is None or symbol is None:
            return None
        pool = self._pool_for_symbol(symbol)
        half_ticks = abs(price_range.upper_tick - price_range.lower_tick) // 2
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
        decision_report: StrategyDecisionReport | None = None,
    ) -> CycleStateBook:
        """Rebuild the book from post-action chain truth plus engine state.

        A portfolio decision report (selector mode under the allocator)
        rebuilds per position: the folds' successor states thread each
        position's out-of-range anchors and each exit's cooldown, and the
        session state threads the day anchors and the halt latch once.
        """
        if (
            decision_report is not None
            and decision_report.portfolio_plan is not None
            and decision_report.session_state is not None
        ):
            rebuilt = self._rebuild_book_portfolio(
                book, reconciliation, decision_report, decision_report.session_state
            )
            # The idle-cash episode stamp rides the rebuilt book: the
            # signature the alert layer compares against next cycle, or
            # None once the book deploys or the band empties so a later
            # episode alerts again from its first cycle. The conservative
            # income window's successor readings ride with it (the
            # captain's 2026-09-28 correction): the trailing median the
            # next cycle floors its income expectations at.
            return rebuilt.model_copy(
                update={
                    "idle_cash_alert_signature": (
                        decision_report.idle_cash.signature
                        if decision_report.idle_cash is not None
                        else None
                    ),
                    "apr_history": decision_report.apr_history,
                }
            )
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
            (
                {
                    cooldown_symbol: (
                        next_state.reentry_blocked_until,
                        next_state.dilution_exit_pending,
                    )
                }
                if cooldown_symbol is not None
                else {}
            ),
        )
        return rebuilt.model_copy(
            update={
                "positions": (position,) if position is not None else (),
                "held_inventory": held,
                "day": next_state.day,
                "day_start_equity_usd": next_state.day_start_equity_usd,
                "peak_equity_usdc": next_state.peak_equity_usd,
                "halted_day": next_state.halted_day,
                "apr_history": (
                    decision_report.apr_history
                    if decision_report is not None and decision_report.apr_history
                    else book.apr_history
                ),
                "updated_at": self._now(),
            }
        )

    def _rebuild_book_portfolio(
        self,
        book: CycleStateBook,
        reconciliation: CycleReconciliation,
        decision_report: StrategyDecisionReport,
        session_state: PolicyState,
    ) -> CycleStateBook:
        """Rebuild the book after one portfolio decision, per position.

        Args:
            book: The post-action book the act layer threaded.
            reconciliation: The final on-chain reconciliation.
            decision_report: The portfolio decision carrying the folds.
            session_state: The observed shared session facts, rolled over by the
                policy engine at the selected pool's full portfolio equity.

        Returns:
            The rebuilt book.
        """
        statuses = {record.token_id: record.status for record in reconciliation.position_statuses}
        folds = decision_report.held_folds
        positions: list[TrackedPosition] = []
        cooldown_updates: dict[str, tuple[datetime | None, bool]] = {}
        for tracked in book.positions:
            updated = tracked
            status = statuses.get(tracked.token_id)
            if status is not None and status.token_id != tracked.token_id:
                updated = updated.model_copy(update={"token_id": status.token_id})
            fold = folds.get(updated.symbol)
            successor = fold.next_state.position if fold is not None else None
            if successor is not None and fold is not None:
                if fold.decision.action is PolicyActionKind.HOLD:
                    updated = updated.model_copy(
                        update={
                            "out_of_range_since": successor.out_of_range_since,
                            "out_of_range_side": successor.out_of_range_side,
                        }
                    )
                elif fold.decision.action in (
                    PolicyActionKind.STOP_OUT,
                    PolicyActionKind.DILUTION_EXIT,
                ):
                    cooldown_updates[updated.symbol] = (
                        fold.next_state.reentry_blocked_until,
                        fold.decision.action is PolicyActionKind.DILUTION_EXIT,
                    )
            positions.append(updated)
        # Reallocation and grace exits arm their own pools' cooldowns when
        # the engine's successor names one; voluntary switches arm none.
        plan = decision_report.portfolio_plan
        if plan is not None:
            for step in plan.steps:
                if step.kind is PortfolioStepKind.POSITION_ACTION and step.token_id is not None:
                    action = step.outcome.decision.action
                    if (
                        action
                        in (
                            PolicyActionKind.STOP_OUT,
                            PolicyActionKind.DILUTION_EXIT,
                            PolicyActionKind.RANGE_GRACE_EXIT,
                        )
                        and step.outcome.next_state.reentry_blocked_until is not None
                    ):
                        cooldown_updates[step.symbol] = (
                            step.outcome.next_state.reentry_blocked_until,
                            action is PolicyActionKind.DILUTION_EXIT,
                        )
        held = book.held_inventory
        if held is not None and reconciliation.held_stock_quantity == 0:
            held = None
        rebuilt = _book_with_cooldowns(book, cooldown_updates)
        return rebuilt.model_copy(
            update={
                "positions": tuple(positions),
                "held_inventory": held,
                "day": session_state.day,
                "day_start_equity_usd": session_state.day_start_equity_usd,
                "peak_equity_usdc": session_state.peak_equity_usd,
                "halted_day": session_state.halted_day,
                "updated_at": self._now(),
            }
        )

    # ------------------------------------------------------------------
    # Fee evidence
    # ------------------------------------------------------------------

    def _fee_evidence(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> CycleFeeEvidence:
        """Measure the tracked position's live fee economics; decide nothing.

        The claimable-now reading is checkpointed truth from the audited
        status read; the accrual window compares it against the book's prior
        sample for the same token id. Measurement only - the policy keeps
        its conservative zero fee APR for every decision.

        Args:
            book: The book carrying the prior sample, if any.
            reconciliation: The reconciliation whose status read evidence.

        Returns:
            The complete fee-evidence record with honest absence diagnostics.
        """
        status = reconciliation.tracked_status
        token_id = reconciliation.tracked_token_id
        if status is None or token_id is None:
            return CycleFeeEvidence(diagnostic="no tracked position; no fee evidence this cycle")
        claimable = status.fees_owed_usdc
        diagnostics: list[str] = []
        measured_per_day: Decimal | None = None
        measured_apr: Decimal | None = None
        prior = next((sample for sample in book.fee_samples if sample.token_id == token_id), None)
        if claimable is None:
            diagnostics.append("the status read carried no fee valuation")
        elif prior is None:
            diagnostics.append(
                "first claimable sample recorded; the accrual window opens next cycle"
            )
        else:
            elapsed_seconds = (status.observed_at - prior.observed_at).total_seconds()
            if elapsed_seconds <= 0:
                diagnostics.append("no time elapsed since the prior sample; no window")
            else:
                measured_per_day = +(
                    (claimable - prior.claimable_pool_fees_usdc)
                    * Decimal(86_400)
                    / Decimal(elapsed_seconds)
                )
                if measured_per_day < 0:
                    diagnostics.append(
                        "a falling window means a collect or checkpoint refresh landed "
                        "inside it, not negative accrual"
                    )
                if status.position_value_usdc and status.position_value_usdc > 0:
                    measured_apr = +(measured_per_day * Decimal(365) / status.position_value_usdc)
                else:
                    diagnostics.append(
                        "the position mark is absent so the accrual has no APR denominator"
                    )
        diagnostics.append(
            "checkpointed claimable is a lower bound the pool refreshes on position "
            "modifications; a flat window means the checkpoint was not refreshed, not "
            "that no fees accrued"
        )
        return CycleFeeEvidence(
            token_id=token_id,
            claimable_pool_fees_usdc=claimable,
            claimable_aero_units=status.accrued_aero_earned_units,
            measured_fee_usdc_per_day=measured_per_day,
            measured_fee_apr=measured_apr,
            claimable_pool_fees_computed_usdc=status.claimable_fees_computed_usdc,
            fee_method_diagnostic=status.fee_method_diagnostic,
            diagnostic="; ".join(diagnostics),
        )

    def _book_with_fee_sample(
        self, book: CycleStateBook, reconciliation: CycleReconciliation
    ) -> CycleStateBook:
        """Fold the cycle's claimable reading into the book's fee window.

        Args:
            book: The book whose per-token sample map is updated.
            reconciliation: The reconciliation whose status read the sample.

        Returns:
            The book carrying the latest sample for the tracked token,
            bounded to the eight most recent token ids.
        """
        samples: list[CycleFeeSampleRecord] = []
        for record in reconciliation.position_statuses:
            status = record.status
            if status.fees_owed_usdc is None or status.position_value_usdc is None:
                continue
            samples.append(
                CycleFeeSampleRecord(
                    token_id=record.token_id,
                    observed_at=status.observed_at,
                    claimable_pool_fees_usdc=status.fees_owed_usdc,
                    position_value_usdc=status.position_value_usdc,
                )
            )
        if not samples:
            return book
        fresh_ids = {sample.token_id for sample in samples}
        kept = tuple(item for item in book.fee_samples if item.token_id not in fresh_ids)
        return book.model_copy(update={"fee_samples": (*kept, *samples)[-40:]})

    # ------------------------------------------------------------------
    # Reward conversion and yield attribution
    # ------------------------------------------------------------------

    def _unclaimed_aero(
        self,
        reconciliation: CycleReconciliation,
        decision_report: StrategyDecisionReport | None,
    ) -> tuple[int | None, Decimal | None]:
        """Measure the unclaimed AERO at the final reconciliation.

        Args:
            reconciliation: The final on-chain reconciliation.
            decision_report: The decision whose observed AERO price values it.

        Returns:
            The raw unclaimed units (Safe balance plus staked earned) and
            their value at the last observed price; either side None when its
            input is absent.
        """
        earned = sum(
            (
                record.status.accrued_aero_earned_units or 0
                for record in reconciliation.position_statuses
            ),
            0,
        )
        units = reconciliation.safe_aero_units + earned
        aero_price = decision_report.aero_price_usdc if decision_report is not None else None
        value = (
            +(Decimal(units).scaleb(-AERO_DECIMALS) * aero_price)
            if aero_price is not None
            else None
        )
        return units, value

    def _convert_rewards_if_due(  # noqa: PLR0912, PLR0915 - one fixed treasury sequence
        self,
        book: CycleStateBook,
        decision_report: StrategyDecisionReport,
        key_bytes: bytes | None,
    ) -> tuple[list[CycleActionRecord], str, CycleStateBook, int]:
        """Convert accumulated AERO rewards to USDC when the cadence fires.

        The cadence is the sealed threshold: unclaimed AERO (the Safe balance
        plus the staked position's earned) valued at the decision's last
        observed price must exceed it. The claim runs first through the
        audited collect surface (respecting the penalty window), then the
        swap converts the Safe's entire AERO balance through the capped
        executor surface. Nothing here is a policy verdict and nothing runs
        outside the act step; a refusal or failure halts the cycle honestly.

        Args:
            book: The post-action book.
            decision_report: The decision carrying the observed AERO price.
            key_bytes: The signing key for live broadcasts.

        Returns:
            The action records, a halted reason (empty on success), the book,
            and the raw AERO units converted this cycle.
        """
        executor = self._executor
        if executor is None or key_bytes is None:
            return [], "", book, 0
        aero_price = decision_report.aero_price_usdc
        if aero_price is None or aero_price <= 0:
            return [], "", book, 0
        records: list[CycleActionRecord] = []
        halted = ""
        safe_units = self._balances.fetch_token_balance(AERO_TOKEN_ADDRESS, self._safe_address)
        earned_units = 0
        # One collect candidate per staked position: every position's earned
        # counts, and every position's penalty window is respected on its own.
        collect_targets: list[tuple[TrackedPosition, int]] = []
        for tracked in book.positions:
            try:
                status = self._reads.position_status(tracked.symbol, tracked.token_id)
            except (LpExecutionRefusalError, ExecutionUnavailableError, ValueError, RuntimeError):
                continue
            if status is not None and status.accrued_aero_earned_units is not None:
                earned_units += status.accrued_aero_earned_units
                if status.accrued_aero_earned_units > 0:
                    penalty = status.penalty
                    penalty_clear = (
                        penalty is None
                        or penalty.remaining_seconds <= 0
                        or penalty.penalty_rate_bps <= 0
                    )
                    if penalty_clear:
                        collect_targets.append((tracked, status.accrued_aero_earned_units))
        unclaimed_units = safe_units + earned_units
        value_usdc = +(Decimal(unclaimed_units).scaleb(-AERO_DECIMALS) * aero_price)
        if value_usdc < self._aero_conversion_min_usdc:
            return records, halted, book, 0

        def run(
            name: str, call: Callable[[], LpActionExecutionReport]
        ) -> LpActionExecutionReport | None:
            """Run one audited treasury action, recording its outcome."""
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
                return None
            except (ExecutionUnavailableError, ValueError, RuntimeError) as error:
                records.append(
                    CycleActionRecord(action=name, status="failed", diagnostic=str(error))
                )
                halted = f"the {name} action failed: {error}"
                return None
            hashes, fees, confirmed = _action_hashes_fees_and_block(report)
            records.append(
                CycleActionRecord(
                    action=name,
                    status="completed" if _action_completed(report) else "failed",
                    transaction_hashes=hashes,
                    fee_wei=fees,
                    confirmed_block_number=confirmed,
                    diagnostic=report.halted_reason,
                )
            )
            if not _action_completed(report):
                halted = f"the {name} action halted: {report.halted_reason}"
                return None
            return report

        converted_units = 0
        for collect_target, _target_units in collect_targets:

            def _collect(bound: TrackedPosition = collect_target) -> LpActionExecutionReport:
                return executor.execute_collect(
                    bound.symbol, bound.token_id, key_bytes, confirm_broadcast=True
                )

            if run("collect_rewards", _collect) is None:
                return records, halted, book, 0
        if earned_units > sum(units for _, units in collect_targets):
            _cycle_progress(
                "reward claim deferred: a penalty window is still open; converting only "
                "the already-claimed Safe balance"
            )
        safe_units = self._balances.fetch_token_balance(AERO_TOKEN_ADDRESS, self._safe_address)
        if safe_units <= 0:
            return records, halted, book, 0
        if self._reward_posture is RewardPosture.RETAIN:
            # The retain posture (the captain's retained-AERO ruling): the
            # claim above already moved the earned rewards into the Safe,
            # and the AERO stays there as priced book equity. The conversion
            # swap never fires - so a restart cannot trip over a failing
            # conversion surface - while claims, accounting, and value
            # attribution keep running exactly as under convert.
            _cycle_progress(
                f"retained {Decimal(safe_units).scaleb(-AERO_DECIMALS)} AERO in the Safe "
                "under the retain reward posture; the conversion swap did not run"
            )
            return records, halted, book, 0
        swap_report = run(
            "aero_swap", lambda: executor.execute_aero_swap(key_bytes, confirm_broadcast=True)
        )
        if swap_report is None:
            return records, halted, book, 0
        converted_units = int(getattr(swap_report.build, "aero_balance_units", 0))
        _cycle_progress(
            f"converted {Decimal(converted_units).scaleb(-AERO_DECIMALS)} AERO to USDC "
            f"at the observed price {aero_price}"
        )
        return records, halted, book, converted_units

    def _price_map(self, decision_report: StrategyDecisionReport | None) -> dict[str, Decimal]:
        """Map every board symbol to its observed pool price.

        Args:
            decision_report: The decision whose board observations carry
                each pool's AMM price.

        Returns:
            The symbol-to-price map; the decision's own symbol rides the
            board evaluations or its reported price.
        """
        prices: dict[str, Decimal] = {}
        if decision_report is None:
            return prices
        for evaluation in decision_report.board:
            prices[evaluation.symbol] = evaluation.observation.amm_price_usdc
        if decision_report.symbol not in prices:
            prices[decision_report.symbol] = decision_report.amm_price_usdc
        return prices

    def _stock_quantities_by_symbol(
        self,
        reconciliation: CycleReconciliation,
        prices: Mapping[str, Decimal],
    ) -> dict[str, Decimal]:
        """Measure each symbol's whole-token stock quantity.

        Per symbol: the tracked positions' stock sides valued at their own
        pool's price, plus the Safe's stock balance when the reconciliation
        attributes it to that symbol.

        Args:
            reconciliation: The reconciliation whose balances are measured.
            prices: The symbol-to-price map valuing stock sides.

        Returns:
            The per-symbol whole-token quantities; symbols without a price
            are absent.
        """
        quantities: dict[str, Decimal] = {}
        for record in reconciliation.position_statuses:
            status = record.status
            price = prices.get(record.symbol)
            if price is None or price <= 0:
                continue
            token0_is_usdc = (
                normalize_evm_address(status.position.token0_address) == BASE_USDC_ADDRESS
            )
            stock_side_value = (
                status.token1_value_usdc if token0_is_usdc else status.token0_value_usdc
            )
            quantities[record.symbol] = (
                quantities.get(record.symbol, Decimal("0")) + stock_side_value / price
            )
        held_symbol = reconciliation.held_symbol
        if (
            held_symbol is not None
            and reconciliation.safe_stock_units > 0
            and held_symbol in prices
        ):
            try:
                token = self._stock_token_address_for(held_symbol)
                quantities[held_symbol] = quantities.get(held_symbol, Decimal("0")) + Decimal(
                    reconciliation.safe_stock_units
                ).scaleb(-self._sources.token_decimals(token))
            except ValueError:
                pass
        return {symbol: +value for symbol, value in quantities.items() if value > 0}

    def _baseline_rows(
        self,
        reconciliation: CycleReconciliation,
        prices: Mapping[str, Decimal],
    ) -> tuple[CyclePositionBaseline, ...]:
        """Build the day-start baseline rows from one reconciliation.

        Args:
            reconciliation: The pre-action reconciliation being snapshotted.
            prices: The symbol-to-price map valuing stock sides.

        Returns:
            One row per funded position, plus one inventory-only row when
            unsold stock exists without a position on its symbol.
        """
        quantities = self._stock_quantities_by_symbol(reconciliation, prices)
        rows: list[CyclePositionBaseline] = []
        for record in reconciliation.position_statuses:
            status = record.status
            # The live status read names the chain's token id; a recentered
            # NFT rebaselines even when the book's record lags it.
            token_id = getattr(status, "token_id", record.token_id)
            rows.append(
                CyclePositionBaseline(
                    symbol=record.symbol,
                    token_id=token_id,
                    liquidity_units=status.position.liquidity,
                    fee_growth_inside0_x128=status.fee_growth_inside0_x128,
                    fee_growth_inside1_x128=status.fee_growth_inside1_x128,
                    aero_earned_units=(
                        status.accrued_aero_earned_units
                        if status.accrued_aero_earned_units is not None
                        else 0
                    ),
                    stock_quantity=quantities.get(record.symbol),
                    stock_price_usdc=prices.get(record.symbol),
                )
            )
        held_symbol = reconciliation.held_symbol
        if (
            held_symbol is not None
            and held_symbol not in {row.symbol for row in rows}
            and held_symbol in quantities
        ):
            rows.append(
                CyclePositionBaseline(
                    symbol=held_symbol,
                    stock_quantity=quantities[held_symbol],
                    stock_price_usdc=prices[held_symbol],
                )
            )
        return tuple(rows)

    def _book_with_day_baseline(
        self,
        book: CycleStateBook,
        reconciliation: CycleReconciliation,
        decision_report: StrategyDecisionReport | None,
        converted_units: int,
    ) -> CycleStateBook:
        """Capture or carry the day's yield-attribution baseline.

        The baseline snapshots the first cycle of each New York day - before
        that cycle's actions, so a conversion never reads as lost rewards -
        and re-snapshots only the position-scoped fee words when a tracked
        token changes mid-day; the day-scoped AERO observations keep their
        day-start values.

        Args:
            book: The book carrying (or lacking) the baseline.
            reconciliation: The pre-action reconciliation being snapshotted.
            decision_report: The decision carrying the observed prices.
            converted_units: Raw AERO converted to USDC this cycle.

        Returns:
            The book carrying the updated baseline.
        """
        today = self._now().astimezone(POLICY_TIMEZONE).date()
        existing = book.day_baseline
        prices = self._price_map(decision_report)
        if existing is None or existing.day != today:
            earned = sum(
                (
                    record.status.accrued_aero_earned_units or 0
                    for record in reconciliation.position_statuses
                ),
                0,
            )
            baseline = CycleDayBaseline(
                day=today,
                positions=self._baseline_rows(reconciliation, prices),
                aero_units=reconciliation.safe_aero_units + earned,
                aero_converted_units=converted_units,
            )
            return book.model_copy(update={"day_baseline": baseline})
        updates: dict[str, object] = {}
        rows = self._baseline_rows(reconciliation, prices)
        existing_by_token = {row.token_id: row for row in existing.positions if row.token_id}
        # The day-start rows survive untouched while their tokens live; a
        # token change (a recenter or reallocation) or a new position
        # re-snapshots its row at adoption, and departed tokens drop.
        merged: list[CyclePositionBaseline] = []
        changed = False
        for row in rows:
            prior = existing_by_token.get(row.token_id) if row.token_id is not None else None
            if prior is not None:
                merged.append(prior)
            else:
                merged.append(row)
                changed = True
        if len(merged) != len(existing.positions):
            changed = True
        if changed:
            updates["positions"] = tuple(merged)
        if converted_units:
            updates["aero_converted_units"] = existing.aero_converted_units + converted_units
        if not updates:
            return book
        return book.model_copy(update={"day_baseline": existing.model_copy(update=updates)})

    def _yield_attribution(
        self,
        book: CycleStateBook,
        reconciliation: CycleReconciliation,
        decision_report: StrategyDecisionReport | None,
        day_pnl_usdc: Decimal | None = None,
    ) -> CycleYieldAttribution | None:
        """Decompose the day's P&L into its per-position yield components.

        Args:
            book: The book carrying the day's baseline.
            reconciliation: The final reconciliation carrying current values.
            decision_report: The decision carrying the observed prices.
            day_pnl_usdc: The day P&L the decomposition explains.

        Returns:
            The attribution with its per-position rows and the portfolio
            rollup, or None when no baseline exists yet.
        """
        baseline = book.day_baseline
        if baseline is None:
            return None
        prices = self._price_map(decision_report)
        aero_price = decision_report.aero_price_usdc if decision_report is not None else None
        diagnostics: list[str] = []
        statuses = {record.token_id: record for record in reconciliation.position_statuses}
        row_by_token = {row.token_id: row for row in baseline.positions if row.token_id}
        position_rows: list[CyclePositionYield] = []
        fees_total: Decimal | None = None
        mtm_total: Decimal | None = None
        for record in reconciliation.position_statuses:
            status = record.status
            row = row_by_token.get(record.token_id)
            row_diagnostic = ""
            row_fees: Decimal | None = None
            price_now = prices.get(record.symbol)
            if row is None:
                row_diagnostic = (
                    "no baseline row: the position entered after the day started "
                    "(its fee baseline re-snapshots at adoption)"
                )
            elif (
                row.liquidity_units is not None and row.liquidity_units != status.position.liquidity
            ):
                # The day's fee-growth delta spans a liquidity change, so one
                # liquidity number cannot price it: the accrual before the
                # change belonged to the baseline liquidity and the accrual
                # after to the new one. Report unmeasured rather than
                # attribute growth to liquidity that no longer backs it.
                row_diagnostic = (
                    f"liquidity changed since the day baseline "
                    f"({row.liquidity_units} -> {status.position.liquidity}); "
                    "the day's fee growth is unmeasurable against one liquidity"
                )
            elif (
                row.liquidity_units is not None
                and row.fee_growth_inside0_x128 is not None
                and row.fee_growth_inside1_x128 is not None
                and status.fee_growth_inside0_x128 is not None
                and status.fee_growth_inside1_x128 is not None
                and price_now is not None
            ):
                earned0 = fees_earned_from_growth(
                    row.liquidity_units,
                    status.fee_growth_inside0_x128 - row.fee_growth_inside0_x128,
                )
                earned1 = fees_earned_from_growth(
                    row.liquidity_units,
                    status.fee_growth_inside1_x128 - row.fee_growth_inside1_x128,
                )
                token0_is_usdc = (
                    normalize_evm_address(status.position.token0_address) == BASE_USDC_ADDRESS
                )
                usdc_side = Decimal(earned0 if token0_is_usdc else earned1).scaleb(-6)
                stock_side = Decimal(earned1 if token0_is_usdc else earned0).scaleb(
                    -self._sources.token_decimals(
                        status.position.token1_address
                        if token0_is_usdc
                        else status.position.token0_address
                    )
                )
                row_fees = +(usdc_side + stock_side * price_now)
            else:
                row_diagnostic = "fee words unmeasured this cycle"
            row_mtm: Decimal | None = None
            if (
                row is not None
                and row.stock_quantity is not None
                and row.stock_price_usdc is not None
                and price_now is not None
            ):
                row_mtm = +(row.stock_quantity * (price_now - row.stock_price_usdc))
            elif row is not None:
                row_diagnostic = (
                    row_diagnostic + "; " if row_diagnostic else ""
                ) + "stock mark-to-market unmeasured: no baseline price or quote"
            row_aero: Decimal | None = None
            row_aero_units: int | None = None
            earned_now = status.accrued_aero_earned_units or 0
            if row is not None:
                # Raw earned units report independently of the price: the
                # units are the reward accrual itself, never a performance
                # number until priced and decomposed (retained-AERO ruling).
                row_aero_units = earned_now - row.aero_earned_units
                if aero_price is not None:
                    row_aero = +(Decimal(row_aero_units).scaleb(-AERO_DECIMALS) * aero_price)
            if row_fees is not None:
                fees_total = (fees_total or Decimal("0")) + row_fees
            if row_mtm is not None:
                mtm_total = (mtm_total or Decimal("0")) + row_mtm
            position_rows.append(
                CyclePositionYield(
                    symbol=record.symbol,
                    token_id=record.token_id,
                    aero_rewards_usdc=row_aero,
                    aero_rewards_units=row_aero_units,
                    fees_earned_usdc=row_fees,
                    stock_mark_to_market_usdc=row_mtm,
                    diagnostic=row_diagnostic,
                )
            )
        # Inventory-only rows still mark their stock to market.
        for row in baseline.positions:
            if row.token_id is not None or row.symbol in {
                record.symbol for record in reconciliation.position_statuses
            }:
                continue
            price_now = prices.get(row.symbol)
            if (
                row.stock_quantity is not None
                and row.stock_price_usdc is not None
                and price_now is not None
            ):
                row_mtm = +(row.stock_quantity * (price_now - row.stock_price_usdc))
                mtm_total = (mtm_total or Decimal("0")) + row_mtm
                position_rows.append(
                    CyclePositionYield(
                        symbol=row.symbol,
                        aero_rewards_usdc=Decimal("0"),
                        stock_mark_to_market_usdc=row_mtm,
                        diagnostic="held-inventory row: convergence income lands in the residual",
                    )
                )
        aero_rewards: Decimal | None = None
        aero_rewards_units: int | None = None
        earned = sum(
            (record.status.accrued_aero_earned_units or 0 for record in statuses.values()),
            0,
        )
        unclaimed_now = reconciliation.safe_aero_units + earned
        delta_units = unclaimed_now + baseline.aero_converted_units - baseline.aero_units
        # The unit count is measurable whenever the baseline exists, even
        # without a price; only the value line needs the observed price.
        aero_rewards_units = delta_units
        if aero_price is not None:
            aero_rewards = +(Decimal(delta_units).scaleb(-AERO_DECIMALS) * aero_price)
        else:
            diagnostics.append("AERO rewards unmeasured: no AERO price was observed")
        if fees_total is None:
            diagnostics.append(
                "fees earned unmeasured: no tracked position spans the baseline and now "
                "(a position change resets its fee baseline at adoption)"
            )
        if mtm_total is None:
            diagnostics.append("stock mark-to-market unmeasured: no baseline price or quote")
        unattributed: Decimal | None = None
        if (
            day_pnl_usdc is not None
            and aero_rewards is not None
            and fees_total is not None
            and mtm_total is not None
        ):
            unattributed = +(day_pnl_usdc - aero_rewards - fees_total - mtm_total)
        return CycleYieldAttribution(
            day_pnl_usdc=day_pnl_usdc,
            aero_rewards_usdc=aero_rewards,
            aero_rewards_units=aero_rewards_units,
            fees_earned_usdc=fees_total,
            stock_mark_to_market_usdc=mtm_total,
            unattributed_usdc=unattributed,
            positions=tuple(position_rows),
            method=(
                "AERO rewards: unclaimed units (Safe balance plus staked earned, plus any "
                "converted today) minus the day-start baseline, valued at the last "
                "observed AERO price; the raw unit count rides beside the value so "
                "earned units never masquerade as net performance.",
                "Fees earned: liquidity times the delta of the pool's feeGrowthInside "
                "accumulators since the day-start (or position-adoption) baseline - "
                "computed, never the stale checkpoint; pre-share pool-side entitlement.",
                "Stock mark-to-market: each row's day-opening stock quantity times the "
                "change in its own pool's price; quantity flows land in the residual.",
                "Unattributed: the day P&L minus the three components - actions, gas, "
                "collections, and every marking the components do not price.",
                "Per position: one row per funded name at its own pool's price, so the "
                "allocator's tier decisions are judged by measured income daily.",
            ),
            diagnostic="; ".join(diagnostics),
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
        fee_evidence: CycleFeeEvidence | None = None,
        unclaimed_aero_units: int | None = None,
        unclaimed_aero_value_usdc: Decimal | None = None,
        yield_attribution: CycleYieldAttribution | None = None,
    ) -> CycleReport:
        """Assemble the structured cycle report from its complete evidence."""
        report_symbol = (
            decision_report.symbol if decision_report is not None else reconciliation.symbol
        )
        if decision_report is not None:
            decision_action = decision_report.outcome.decision.action.value
            decision_reason = decision_report.outcome.decision.reason.value
            decision_diagnostics = decision_report.outcome.decision.diagnostics
            # The gate-chain evidence and the idle-exclusion consequence
            # lines ride every cycle report (the captain's gnhf 34
            # ruling): any future why-is-it-flat question is answerable
            # from the report and the audit store alone.
            if decision_report.gate_trace_basis:
                decision_diagnostics = decision_diagnostics + (
                    decision_report.gate_trace_basis,
                    *decision_report.gate_trace,
                )
            if decision_report.idle_cash is not None:
                for row in decision_report.idle_cash.exclusions:
                    gate_segment = f": {row.gate}" if row.gate else ""
                    decision_diagnostics = decision_diagnostics + (
                        f"idle-cash exclusion {row.symbol} ({row.reason}{gate_segment}): "
                        f"{row.detail}",
                    )
            # The conservative income basis's evidence rides every cycle
            # report the same way (the captain's 2026-09-28 correction):
            # each floored pool's basis, window, and instantaneous reading
            # stay answerable from the report and the audit store alone.
            if decision_report.income_basis_evidence:
                decision_diagnostics = decision_diagnostics + decision_report.income_basis_evidence
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
        if decision_report is not None:
            equity = decision_report.equity_usd
            anchor = decision_report.outcome.next_state.day_start_equity_usd
            day_diagnostic = ""
            if equity is None or anchor is None:
                day_diagnostic = "the decision carried no complete day economics"
        else:
            equity = None
            anchor = None
            day_diagnostic = "out-of-band cycle carried no day economics"
        day_pnl = +(equity - anchor) if equity is not None and anchor is not None else None
        peak = (
            decision_report.outcome.next_state.peak_equity_usd
            if decision_report is not None
            else None
        )
        if yield_attribution is not None and day_pnl is not None:
            components: dict[str, Decimal | None] = {
                "day_pnl_usdc": day_pnl,
                "unattributed_usdc": None,
            }
            if (
                yield_attribution.aero_rewards_usdc is not None
                and yield_attribution.fees_earned_usdc is not None
                and yield_attribution.stock_mark_to_market_usdc is not None
            ):
                components["unattributed_usdc"] = +(
                    day_pnl
                    - yield_attribution.aero_rewards_usdc
                    - yield_attribution.fees_earned_usdc
                    - yield_attribution.stock_mark_to_market_usdc
                )
            yield_attribution = yield_attribution.model_copy(update=components)
        attribution_rows = {
            row.symbol: row
            for row in (yield_attribution.positions if yield_attribution is not None else ())
        }
        positions = tuple(
            CyclePositionSummary(
                symbol=record.symbol,
                token_id=record.token_id,
                committed_usd=record.committed_usd,
                value_usdc=record.status.position_value_usdc,
                unrealized_pnl_usdc=record.status.unrealized_pnl_usdc,
                staked=record.staked,
                yield_attribution=attribution_rows.get(record.symbol),
            )
            for record in reconciliation.position_statuses
        )
        return CycleReport(
            started_at=started_at,
            mode=mode,
            symbol=report_symbol,
            reconciliation=reconciliation,
            positions=positions,
            decision_reconciliation=decision_reconciliation,
            final_reconciliation_verified=final_reconciliation_verified,
            decision_action=decision_action,
            decision_reason=decision_reason,
            decision_diagnostics=decision_diagnostics,
            event_window=event_window,
            actions=actions,
            pnl_vs_entry_usdc=pnl,
            pnl_diagnostic=pnl_diagnostic if pnl is None else "",
            equity_usd=equity,
            day_start_equity_usd=anchor,
            day_pnl_usdc=day_pnl,
            day_diagnostic=day_diagnostic if day_pnl is None else "",
            peak_equity_usd=peak,
            yield_attribution=yield_attribution,
            idle_cash=decision_report.idle_cash if decision_report is not None else None,
            unclaimed_aero_units=unclaimed_aero_units,
            unclaimed_aero_value_usdc=unclaimed_aero_value_usdc,
            fee_evidence=fee_evidence,
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
            equity_usdc=str(report.equity_usd) if report.equity_usd is not None else None,
            day_start_equity_usdc=str(report.day_start_equity_usd)
            if report.day_start_equity_usd is not None
            else None,
            day_pnl_usdc=str(report.day_pnl_usdc) if report.day_pnl_usdc is not None else None,
            peak_equity_usdc=str(report.peak_equity_usd)
            if report.peak_equity_usd is not None
            else None,
            yield_aero_rewards_usdc=str(report.yield_attribution.aero_rewards_usdc)
            if report.yield_attribution is not None
            and report.yield_attribution.aero_rewards_usdc is not None
            else None,
            yield_aero_rewards_units=str(report.yield_attribution.aero_rewards_units)
            if report.yield_attribution is not None
            and report.yield_attribution.aero_rewards_units is not None
            else None,
            yield_fees_earned_usdc=str(report.yield_attribution.fees_earned_usdc)
            if report.yield_attribution is not None
            and report.yield_attribution.fees_earned_usdc is not None
            else None,
            yield_stock_mark_to_market_usdc=str(report.yield_attribution.stock_mark_to_market_usdc)
            if report.yield_attribution is not None
            and report.yield_attribution.stock_mark_to_market_usdc is not None
            else None,
            yield_unattributed_usdc=str(report.yield_attribution.unattributed_usdc)
            if report.yield_attribution is not None
            and report.yield_attribution.unattributed_usdc is not None
            else None,
            unclaimed_aero_value_usdc=str(report.unclaimed_aero_value_usdc)
            if report.unclaimed_aero_value_usdc is not None
            else None,
            claimable_pool_fees_usdc=str(report.fee_evidence.claimable_pool_fees_usdc)
            if report.fee_evidence is not None
            and report.fee_evidence.claimable_pool_fees_usdc is not None
            else None,
            measured_fee_apr=str(report.fee_evidence.measured_fee_apr)
            if report.fee_evidence is not None and report.fee_evidence.measured_fee_apr is not None
            else None,
            ignored_dust_symbols=tuple(
                row.symbol for row in report.reconciliation.ignored_stock_dust
            ),
            ignored_dust_usdc=(
                str(
                    sum(
                        (row.value_usdc for row in report.reconciliation.ignored_stock_dust),
                        Decimal("0"),
                    )
                )
                if report.reconciliation.ignored_stock_dust
                else None
            ),
            fee_wei=report.fee_wei,
            action_count=len(report.actions),
            position_count=len(report.positions),
            total_committed_usdc=(
                str(sum((row.committed_usd for row in report.positions), Decimal("0")))
                if report.positions
                else None
            ),
            largest_position_share=(
                str(max(row.committed_usd for row in report.positions) / report.equity_usd)
                if report.positions and report.equity_usd
                else None
            ),
            halted_reason=report.halted_reason,
            decision_diagnostics=report.decision_diagnostics,
            idle_cash_signature=report.idle_cash.signature
            if report.idle_cash is not None
            else None,
            idle_cash_fraction=str(report.idle_cash.cash_fraction)
            if report.idle_cash is not None
            else None,
            idle_cash_exclusions=(
                tuple(
                    f"{row.symbol} ({row.reason}: {row.gate})"
                    if row.gate
                    else f"{row.symbol} ({row.reason})"
                    for row in report.idle_cash.exclusions
                )
                if report.idle_cash is not None
                else ()
            ),
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
    if report.positions:
        print(f"portfolio: {len(report.positions)} tracked position(s)")
        for row in report.positions:
            custody = "staked" if row.staked else "unstaked"
            pnl = f", pnl {row.unrealized_pnl_usdc}" if row.unrealized_pnl_usdc is not None else ""
            print(
                f"  {row.symbol} #{row.token_id}: {row.committed_usd} committed, "
                f"value {row.value_usdc} {custody}{pnl}"
            )
            if row.yield_attribution is not None:
                slice_ = row.yield_attribution
                print(
                    f"    attribution: aero {slice_.aero_rewards_usdc}, fees "
                    f"{slice_.fees_earned_usdc}, mtm {slice_.stock_mark_to_market_usdc}"
                )
    if report.pnl_vs_entry_usdc is not None:
        print(f"pnl vs entry: {report.pnl_vs_entry_usdc} USDC")
    elif report.pnl_diagnostic:
        print(f"pnl vs entry: unavailable ({report.pnl_diagnostic})")
    if report.day_pnl_usdc is not None:
        print(
            f"day pnl: {report.day_pnl_usdc} USDC against the day-start anchor "
            f"{report.day_start_equity_usd} (running peak {report.peak_equity_usd})"
        )
    if report.unclaimed_aero_units is not None:
        value_line = (
            f" worth {report.unclaimed_aero_value_usdc} USDC at the last observed price"
            if report.unclaimed_aero_value_usdc is not None
            else ""
        )
        print(f"unclaimed AERO: {report.unclaimed_aero_units} raw units{value_line}")
    if report.yield_attribution is not None:
        attribution = report.yield_attribution
        print("yield attribution (day pnl decomposition):")

        def component(label: str, value: Decimal | None) -> str:
            return f"  {label}: {value if value is not None else 'unmeasured'} USDC"

        print(component("aero rewards accrued", attribution.aero_rewards_usdc))
        units_line = "unmeasured"
        if attribution.aero_rewards_units is not None:
            units_line = (
                f"{Decimal(attribution.aero_rewards_units).scaleb(-AERO_DECIMALS)} AERO "
                f"({attribution.aero_rewards_units} raw units)"
            )
        print(
            f"  aero rewards earned units: {units_line} - units beside their "
            "mark-to-market, never a net-performance number by themselves"
        )
        print(component("fees earned (computed)", attribution.fees_earned_usdc))
        print(component("stock mark-to-market", attribution.stock_mark_to_market_usdc))
        print(component("unattributed residual", attribution.unattributed_usdc))
        if attribution.diagnostic:
            print(f"  note: {attribution.diagnostic}")
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


def _out_of_range_grace_from_environment(environ: Mapping[str, str]) -> timedelta:
    """Read the out-of-range grace window from the sealed environment.

    Args:
        environ: The environment mapping carrying the optional window.

    Returns:
        The configured grace window, or the locked ten-minute default.

    Raises:
        ValueError: If the configured minutes are not a positive number.
    """
    raw = environ.get(CYCLE_OUT_OF_RANGE_GRACE_ENV, "").strip()
    if not raw:
        return LOCKED_POLICY_PARAMETERS.out_of_range_grace
    minutes = Decimal(raw)
    if minutes <= 0:
        raise ValueError(f"{CYCLE_OUT_OF_RANGE_GRACE_ENV} must be positive, not {raw!r}")
    return timedelta(minutes=float(minutes))


def _aero_conversion_min_from_environment(environ: Mapping[str, str]) -> Decimal:
    """Read the reward-conversion threshold from the sealed environment.

    Args:
        environ: The environment mapping carrying the optional threshold.

    Returns:
        The configured USDC threshold, or the five-USDC default.

    Raises:
        ValueError: If the configured threshold is negative or not a number.
    """
    raw = environ.get(CYCLE_AERO_CONVERSION_MIN_ENV, "").strip()
    if not raw:
        return DEFAULT_AERO_CONVERSION_MIN_USDC
    value = Decimal(raw)
    if value < 0:
        raise ValueError(f"{CYCLE_AERO_CONVERSION_MIN_ENV} must be non-negative, not {raw!r}")
    return value


def _reward_posture_from_environment(environ: Mapping[str, str]) -> RewardPosture:
    """Read the reward posture from the sealed environment.

    Args:
        environ: The environment mapping carrying the optional posture.

    Returns:
        The configured posture, or the claim-and-convert default.

    Raises:
        ValueError: If the configured posture is not one of the choices.
    """
    raw = environ.get(CYCLE_REWARD_POSTURE_ENV, "").strip()
    if not raw:
        return DEFAULT_REWARD_POSTURE
    if raw not in REWARD_POSTURE_CHOICES:
        raise ValueError(
            f"{CYCLE_REWARD_POSTURE_ENV} must be one of "
            f"{','.join(sorted(REWARD_POSTURE_CHOICES))}, not {raw!r}"
        )
    return RewardPosture(raw)


def _stock_dust_floor_from_environment(environ: Mapping[str, str]) -> Decimal:
    """Read the per-token stray-stock dust floor from the sealed environment.

    Args:
        environ: The environment mapping carrying the optional floor.

    Returns:
        The configured USDC floor, or the one-cent default.

    Raises:
        ValueError: If the configured floor is negative, not a number, or
            above the hard ceiling an override may never breach.
    """
    raw = environ.get(CYCLE_STOCK_DUST_FLOOR_USDC_ENV, "").strip()
    if not raw:
        return DEFAULT_STOCK_DUST_FLOOR_USDC
    value = Decimal(raw)
    if value < 0:
        raise ValueError(f"{CYCLE_STOCK_DUST_FLOOR_USDC_ENV} must be non-negative, not {raw!r}")
    if value > HARD_MAX_STOCK_DUST_FLOOR_USDC:
        raise ValueError(
            f"{CYCLE_STOCK_DUST_FLOOR_USDC_ENV} must not exceed "
            f"{HARD_MAX_STOCK_DUST_FLOOR_USDC} USDC, not {raw!r}; ignored exposure stays "
            "economically meaningless"
        )
    return value


def _stock_dust_aggregate_from_environment(
    environ: Mapping[str, str], floor_usdc: Decimal
) -> Decimal:
    """Read the aggregate dust bound from the sealed environment.

    Args:
        environ: The environment mapping carrying the optional bound.
        floor_usdc: The resolved per-token floor the bound must cover.

    Returns:
        The configured USDC aggregate bound, or the ten-cent default
        raised to the floor whenever the floor itself is higher.

    Raises:
        ValueError: If the configured bound is negative, not a number,
            below the per-token floor, or above the hard ceiling.
    """
    default = max(DEFAULT_STOCK_DUST_AGGREGATE_USDC, floor_usdc)
    raw = environ.get(CYCLE_STOCK_DUST_AGGREGATE_USDC_ENV, "").strip()
    if not raw:
        return default
    value = Decimal(raw)
    if value < 0:
        raise ValueError(f"{CYCLE_STOCK_DUST_AGGREGATE_USDC_ENV} must be non-negative, not {raw!r}")
    if value < floor_usdc:
        raise ValueError(
            f"{CYCLE_STOCK_DUST_AGGREGATE_USDC_ENV} must be at least the per-token "
            f"floor {floor_usdc} USDC, not {raw!r}"
        )
    if value > HARD_MAX_STOCK_DUST_AGGREGATE_USDC:
        raise ValueError(
            f"{CYCLE_STOCK_DUST_AGGREGATE_USDC_ENV} must not exceed "
            f"{HARD_MAX_STOCK_DUST_AGGREGATE_USDC} USDC, not {raw!r}; ignored exposure "
            "stays economically meaningless"
        )
    return value


def _portfolio_parameters_from_environment(
    environ: Mapping[str, str],
    switch_margin_fraction: Decimal,
) -> PortfolioParameters:
    """Read the allocator's portfolio bounds from the sealed environment.

    Args:
        environ: The environment mapping carrying the optional bounds.
        switch_margin_fraction: The switch margin the reallocation margin
            generalizes.

    Returns:
        The portfolio parameter set, with the locked defaults for every
        unset variable.

    Raises:
        ValueError: If any configured bound is malformed or breaches its
            hard ceiling.
    """

    def _decimal(name: str, default: Decimal) -> Decimal:
        raw = environ.get(name, "").strip()
        if not raw:
            return default
        value = Decimal(raw)
        if value <= 0:
            raise ValueError(f"{name} must be positive, not {raw!r}")
        return value

    def _int(name: str, default: int) -> int:
        raw = environ.get(name, "").strip()
        if not raw:
            return default
        value = int(raw)
        if value < 1:
            raise ValueError(f"{name} must be at least one, not {raw!r}")
        return value

    return PortfolioParameters(
        tier_band_fraction=_decimal(CYCLE_TIER_BAND_ENV, PortfolioParameters().tier_band_fraction),
        max_concurrent_positions=_int(
            CYCLE_MAX_POSITIONS_ENV, PortfolioParameters().max_concurrent_positions
        ),
        min_position_usdc=_decimal(
            CYCLE_MIN_POSITION_USDC_ENV, PortfolioParameters().min_position_usdc
        ),
        min_position_floor_usdc=_decimal(
            CYCLE_MIN_POSITION_FLOOR_USDC_ENV, PortfolioParameters().min_position_floor_usdc
        ),
        concentration_cap_fraction=_decimal(
            CYCLE_CONCENTRATION_CAP_ENV, PortfolioParameters().concentration_cap_fraction
        ),
        concentration_cap_activation_equity_usdc=_decimal(
            CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC_ENV,
            PortfolioParameters().concentration_cap_activation_equity_usdc,
        ),
        switch_margin_fraction=switch_margin_fraction,
    )


def _income_history_cycles_from_environment(
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Read the conservative income window from the sealed environment.

    Args:
        environ: Environment mapping carrying the optional override; an
            absent or empty variable keeps the sealed default.

    Returns:
        The trailing window in cycles, at least one and at most the hard
        ceiling.

    Raises:
        ValueError: If the override sits outside its interval.
    """
    raw = environ.get(CYCLE_INCOME_HISTORY_CYCLES_ENV, "").strip()
    if not raw:
        return DEFAULT_INCOME_HISTORY_CYCLES
    value = int(raw)
    if value < 1:
        raise ValueError(f"{CYCLE_INCOME_HISTORY_CYCLES_ENV} must be at least one, not {raw!r}")
    if value > HARD_MAX_INCOME_HISTORY_CYCLES:
        raise ValueError(
            f"{CYCLE_INCOME_HISTORY_CYCLES_ENV} must not exceed "
            f"{HARD_MAX_INCOME_HISTORY_CYCLES} cycles, not {raw!r}"
        )
    return value


def _trim_apr_history(
    current: tuple[AprReadingSample, ...],
    prior: tuple[AprReadingSample, ...],
    window: int,
) -> tuple[AprReadingSample, ...]:
    """Merge one cycle's readings into the trailing window, per symbol.

    Args:
        current: This cycle's per-symbol readings, board order.
        prior: The persisted history, oldest first.
        window: The maximum samples kept per symbol.

    Returns:
        The successor history, per symbol the most recent ``window``
        readings, ordered by symbol for reproducible books.
    """
    merged: dict[str, list[AprReadingSample]] = {}
    for sample in prior:
        merged.setdefault(sample.symbol.lower(), []).append(sample)
    for sample in current:
        merged.setdefault(sample.symbol.lower(), []).append(sample)
    kept: list[AprReadingSample] = []
    for samples in merged.values():
        kept.extend(samples[-window:])
    return tuple(sorted(kept, key=lambda sample: sample.symbol.lower()))


def build_cycle_runner(
    settings: Settings,
    symbol: str | None,
    safe_address: str,
    relayer_address: str | None,
    switch_margin_fraction: Decimal = DEFAULT_SWITCH_MARGIN_FRACTION,
    parameters: PolicyParameters = LOCKED_POLICY_PARAMETERS,
    aero_conversion_min_usdc: Decimal = DEFAULT_AERO_CONVERSION_MIN_USDC,
    reward_posture: RewardPosture = DEFAULT_REWARD_POSTURE,
    portfolio_parameters: PortfolioParameters | None = None,
    income_history_cycles: int | None = None,
    stock_dust_floor_usdc: Decimal = DEFAULT_STOCK_DUST_FLOOR_USDC,
    stock_dust_aggregate_usdc: Decimal = DEFAULT_STOCK_DUST_AGGREGATE_USDC,
    reference_feed: StockReferenceFeed | None = None,
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
        parameters: The policy parameters decisions run under.
        aero_conversion_min_usdc: The unclaimed-AERO value threshold that
            triggers the reward conversion inside the act step.
        reward_posture: What the act step does with claimed rewards
            (the captain's retained-AERO ruling): convert swaps them to
            USDC once the value crosses the threshold, retain claims but
            holds the AERO in the Safe and never invokes the swap.
        portfolio_parameters: The allocator's portfolio bounds; None keeps
            the locked defaults carrying the switch margin.
        income_history_cycles: The conservative income window in cycles;
            None keeps the sealed default.
        stock_dust_floor_usdc: The per-token USDC value under which stray
            stock is dust, retained in the Safe and never swapped.
        stock_dust_aggregate_usdc: The bound the sum of ignored dust must
            stay at or below.
        reference_feed: The optional live underlying-equity reference feed
            whose quotes join the decision observations; None keeps the
            injected-constant behavior exactly as shipped.

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
        parameters=parameters,
        aero_conversion_min_usdc=aero_conversion_min_usdc,
        reward_posture=reward_posture,
        portfolio_parameters=portfolio_parameters,
        income_history_cycles=income_history_cycles,
        stock_dust_floor_usdc=stock_dust_floor_usdc,
        stock_dust_aggregate_usdc=stock_dust_aggregate_usdc,
        reference_feed=reference_feed,
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
            "value when the flag is absent. An injected constant wins over "
            "the live reference feed for its own symbols, and its age stays "
            "operator-owned through --reference-age-seconds."
        ),
    )
    parser.add_argument(
        "--reference-age-seconds",
        type=int,
        default=0,
        help="Age of the injected reference quote in seconds (default: 0).",
    )
    parser.add_argument(
        "--reference-feed",
        default=None,
        choices=("off", "yahoo", "finnhub"),
        help=(
            "Arm the live underlying-equity reference feed: off keeps the "
            "injected-constant behavior, yahoo reads the credential-free "
            "public chart endpoint, and finnhub reads the documented "
            "API-key /quote endpoint (requires the sealed "
            "AERO_BOT_STOCK_REFERENCE_TOKEN). Every quote carries its "
            "provider's own as-of time as its age - a delayed or "
            "closed-market last price is never relabeled fresh because a "
            "poll ran (default: off; the sealed "
            "AERO_BOT_CYCLE_REFERENCE_FEED variable supplies the same value "
            "when the flag is absent)."
        ),
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
        "--out-of-range-grace-minutes",
        type=Decimal,
        default=None,
        help=(
            "How long a staked position may sit outside its earning range "
            "before the policy must act - recenter when the economics pass, "
            "otherwise exit - as minutes (default 10; the sealed "
            "AERO_BOT_CYCLE_OUT_OF_RANGE_GRACE_MINUTES variable supplies the "
            "same value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--tier-band",
        type=Decimal,
        default=None,
        help=(
            "Pools whose qualifying APR sits at or above this fraction of "
            "the top's share the tiers (default 0.50; the sealed "
            "AERO_BOT_CYCLE_TIER_BAND_FRACTION variable supplies the same "
            "value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=None,
        help=(
            "The maximum concurrent deployed positions, an output of "
            "qualification under the bounds (default 10, the hard ceiling; "
            "the sealed AERO_BOT_CYCLE_MAX_POSITIONS variable supplies the "
            "same value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--min-position-usdc",
        type=Decimal,
        default=None,
        help=(
            "The configured minimum position size in USDC at or above the "
            "concentration-cap activation equity (default 1000); below that "
            "equity there is no minimum - the book deploys its available "
            "funds (default 80; the sealed "
            "AERO_BOT_CYCLE_MIN_POSITION_USDC variable supplies the same value "
            "when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--min-position-floor-usdc",
        type=Decimal,
        default=None,
        help=(
            "The hard floor in USDC under the effective minimum position size: "
            "the effective minimum is max(this floor, min(the configured "
            "minimum, the per-name concentration clamp at the live equity)), "
            "so a small book deploys at the clamp instead of refusing forever "
            "(default 30; the sealed "
            "AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC variable supplies the same "
            "value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--concentration-cap",
        type=Decimal,
        default=None,
        help=(
            "The per-name concentration cap as a fraction of book equity "
            "(default 0.35; the sealed "
            "AERO_BOT_CYCLE_CONCENTRATION_CAP_FRACTION variable supplies "
            "the same value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--concentration-cap-activation-usdc",
        type=Decimal,
        default=None,
        help=(
            "The book equity at or above which the per-name concentration "
            "cap engages (default 1000, the hard ceiling - the captain's "
            "2026-09-28 ruling); below it the cap does not bind at all and "
            "the book funds toward the cap at the proven per-position scale. "
            "The sealed AERO_BOT_CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC "
            "variable supplies the same value when the flag is absent."
        ),
    )
    parser.add_argument(
        "--aero-conversion-min-usdc",
        type=Decimal,
        default=None,
        help=(
            "Unclaimed AERO whose value at the last observed price exceeds "
            "this many USDC converts to USDC inside the act step (default 5; "
            "the sealed AERO_BOT_CYCLE_AERO_CONVERSION_MIN_USDC variable "
            "supplies the same value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--reward-posture",
        choices=sorted(REWARD_POSTURE_CHOICES),
        default=None,
        help=(
            "What the act step does with claimed AERO rewards: convert "
            "(default) claims and swaps them to USDC once the value crosses "
            "the sealed threshold; retain claims through the same audited "
            "collect surface but holds the AERO in the Safe as book equity "
            "and never invokes the conversion swap (the sealed "
            "AERO_BOT_CYCLE_REWARD_POSTURE variable supplies the same value "
            "when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--stock-dust-floor-usdc",
        type=Decimal,
        default=None,
        help=(
            "A stray stock balance whose USDC value at its own pool's "
            "pinned snapshot price sits strictly below this floor is dust: "
            "retained in the Safe, never adopted as inventory, never "
            "swapped (default 0.01; the sealed "
            "AERO_BOT_CYCLE_STOCK_DUST_FLOOR_USDC variable supplies the same "
            "value when the flag is absent)."
        ),
    )
    parser.add_argument(
        "--stock-dust-aggregate-usdc",
        type=Decimal,
        default=None,
        help=(
            "The bound the sum of every ignored dust balance must stay at "
            "or below, so splitting exposure across many tokens cannot hide "
            "behind the floor (default 0.10, always at least the floor; the "
            "sealed AERO_BOT_CYCLE_STOCK_DUST_AGGREGATE_USDC variable supplies "
            "the same value when the flag is absent)."
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
    if (
        arguments.out_of_range_grace_minutes is not None
        and arguments.out_of_range_grace_minutes <= 0
    ):
        parser.error("--out-of-range-grace-minutes must be positive")
    if arguments.aero_conversion_min_usdc is not None and arguments.aero_conversion_min_usdc < 0:
        parser.error("--aero-conversion-min-usdc must be non-negative")
    if arguments.stock_dust_floor_usdc is not None and arguments.stock_dust_floor_usdc < 0:
        parser.error("--stock-dust-floor-usdc must be non-negative")
    if (
        arguments.stock_dust_floor_usdc is not None
        and arguments.stock_dust_floor_usdc > HARD_MAX_STOCK_DUST_FLOOR_USDC
    ):
        parser.error(
            "--stock-dust-floor-usdc must not exceed the hard "
            f"{HARD_MAX_STOCK_DUST_FLOOR_USDC} USDC ceiling; ignored exposure stays "
            "economically meaningless"
        )
    if arguments.stock_dust_aggregate_usdc is not None and arguments.stock_dust_aggregate_usdc < 0:
        parser.error("--stock-dust-aggregate-usdc must be non-negative")
    if arguments.stock_dust_aggregate_usdc is not None and (
        arguments.stock_dust_floor_usdc is not None
        and arguments.stock_dust_aggregate_usdc < arguments.stock_dust_floor_usdc
    ):
        parser.error(
            "--stock-dust-aggregate-usdc must be at least the dust floor "
            f"{arguments.stock_dust_floor_usdc} USDC"
        )
    # The aggregate bound covers the floor wherever the floor came from: a
    # sealed floor above a flagged aggregate would refuse every dust set
    # on a bound the floor itself already breaches. The sealed floor
    # resolves through the same reader the configuration phase uses, so a
    # malformed sealed value refuses here with the clean configuration
    # message instead of a raw parse traceback.
    try:
        resolved_dust_floor = (
            arguments.stock_dust_floor_usdc
            if arguments.stock_dust_floor_usdc is not None
            else _stock_dust_floor_from_environment(os.environ)
        )
    except (ValueError, ArithmeticError) as error:
        print(f"invalid configuration: {error}", file=sys.stderr)
        return EXIT_FAILURE
    if (
        arguments.stock_dust_aggregate_usdc is not None
        and arguments.stock_dust_aggregate_usdc < resolved_dust_floor
    ):
        parser.error(
            "--stock-dust-aggregate-usdc must be at least the dust floor "
            f"{resolved_dust_floor} USDC"
        )
    if (
        arguments.stock_dust_aggregate_usdc is not None
        and arguments.stock_dust_aggregate_usdc > HARD_MAX_STOCK_DUST_AGGREGATE_USDC
    ):
        parser.error(
            "--stock-dust-aggregate-usdc must not exceed the hard "
            f"{HARD_MAX_STOCK_DUST_AGGREGATE_USDC} USDC ceiling; ignored exposure stays "
            "economically meaningless"
        )
    for flag, value in (
        ("--tier-band", arguments.tier_band),
        ("--min-position-usdc", arguments.min_position_usdc),
        ("--min-position-floor-usdc", arguments.min_position_floor_usdc),
        ("--concentration-cap", arguments.concentration_cap),
        ("--concentration-cap-activation-usdc", arguments.concentration_cap_activation_usdc),
    ):
        if value is not None and value <= 0:
            parser.error(f"{flag} must be positive")
    # The floor bounds the coherence rule from below only: a floor above
    # the minimum it floors would raise the effective minimum above the
    # configured bound (model_copy bypasses the model validator, so the
    # CLI boundary checks the pair explicitly).
    raw_minimum = os.environ.get(CYCLE_MIN_POSITION_USDC_ENV, "").strip()
    resolved_min_position = (
        arguments.min_position_usdc
        if arguments.min_position_usdc is not None
        else (Decimal(raw_minimum) if raw_minimum else PortfolioParameters().min_position_usdc)
    )
    if (
        arguments.min_position_floor_usdc is not None
        and arguments.min_position_floor_usdc > resolved_min_position
    ):
        parser.error(
            "--min-position-floor-usdc must not exceed the minimum position size "
            f"({resolved_min_position} USDC); the floor bounds the coherence rule, "
            "it never raises the minimum"
        )
    if (
        arguments.concentration_cap_activation_usdc is not None
        and arguments.concentration_cap_activation_usdc > PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC
    ):
        parser.error(
            "--concentration-cap-activation-usdc must not exceed the hard "
            f"{PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC} USDC total cap; the activation equity may "
            "only engage the concentration cap sooner, never later"
        )
    if arguments.max_positions is not None and arguments.max_positions < 1:
        parser.error("--max-positions must be at least one")
    symbol = _symbol_from_arguments_and_environment(arguments.symbol, os.environ)
    try:
        switch_margin = (
            arguments.switch_margin
            if arguments.switch_margin is not None
            else _switch_margin_from_environment(os.environ)
        )
        grace_minutes = (
            arguments.out_of_range_grace_minutes
            if arguments.out_of_range_grace_minutes is not None
            else _out_of_range_grace_from_environment(os.environ)
        )
        parameters = LOCKED_POLICY_PARAMETERS.model_copy(
            update={"out_of_range_grace": grace_minutes}
        )
        aero_conversion_min = (
            arguments.aero_conversion_min_usdc
            if arguments.aero_conversion_min_usdc is not None
            else _aero_conversion_min_from_environment(os.environ)
        )
        reward_posture = (
            RewardPosture(arguments.reward_posture)
            if arguments.reward_posture is not None
            else _reward_posture_from_environment(os.environ)
        )
        stock_dust_floor = (
            arguments.stock_dust_floor_usdc
            if arguments.stock_dust_floor_usdc is not None
            else _stock_dust_floor_from_environment(os.environ)
        )
        stock_dust_aggregate = (
            arguments.stock_dust_aggregate_usdc
            if arguments.stock_dust_aggregate_usdc is not None
            else _stock_dust_aggregate_from_environment(os.environ, stock_dust_floor)
        )
        portfolio_parameters = _portfolio_parameters_from_environment(os.environ, switch_margin)
        income_history_cycles = _income_history_cycles_from_environment(os.environ)
        if arguments.tier_band is not None:
            portfolio_parameters = portfolio_parameters.model_copy(
                update={"tier_band_fraction": arguments.tier_band}
            )
        if arguments.max_positions is not None:
            portfolio_parameters = portfolio_parameters.model_copy(
                update={"max_concurrent_positions": arguments.max_positions}
            )
        if arguments.min_position_usdc is not None:
            portfolio_parameters = portfolio_parameters.model_copy(
                update={"min_position_usdc": arguments.min_position_usdc}
            )
        if arguments.min_position_floor_usdc is not None:
            portfolio_parameters = portfolio_parameters.model_copy(
                update={"min_position_floor_usdc": arguments.min_position_floor_usdc}
            )
        if arguments.concentration_cap is not None:
            portfolio_parameters = portfolio_parameters.model_copy(
                update={"concentration_cap_fraction": arguments.concentration_cap}
            )
        if arguments.concentration_cap_activation_usdc is not None:
            portfolio_parameters = portfolio_parameters.model_copy(
                update={
                    "concentration_cap_activation_equity_usdc": (
                        arguments.concentration_cap_activation_usdc
                    )
                }
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
    # The live reference feed selection: the flag wins, the sealed cycle
    # environment supplies the default, and off keeps production exactly
    # as shipped until the operator arms the feed.
    raw_feed_selection = (
        arguments.reference_feed
        if arguments.reference_feed is not None
        else os.environ.get(CYCLE_REFERENCE_FEED_ENV, "off")
    )
    feed_selection = raw_feed_selection.strip().lower()
    if feed_selection not in ("off", "", "yahoo", "finnhub"):
        parser.error(f"--reference-feed must be off, yahoo, or finnhub, not {raw_feed_selection!r}")
    reference_feed: StockReferenceFeed | None = None
    if feed_selection == "yahoo":
        reference_feed = StockReferenceFeed(YahooChartStockReferenceBackend())
    elif feed_selection == "finnhub":
        # The keyed provider fails closed at startup when its sealed token
        # is missing; the setup requirement is spelled out rather than
        # silently degrading to no quotes.
        finnhub_token = os.environ.get(STOCK_REFERENCE_TOKEN_ENV, "").strip()
        if not finnhub_token:
            parser.error(
                "--reference-feed finnhub requires the sealed "
                f"{STOCK_REFERENCE_TOKEN_ENV} variable: create a free "
                "Finnhub API key at finnhub.io/register and seal it in the "
                "cycle environment; no other setup is required"
            )
        reference_feed = StockReferenceFeed(FinnhubQuoteStockReferenceBackend(finnhub_token))
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
            settings,
            symbol,
            safe_address,
            configured_relayer,
            switch_margin,
            parameters,
            aero_conversion_min,
            reward_posture,
            portfolio_parameters,
            income_history_cycles,
            stock_dust_floor,
            stock_dust_aggregate,
            reference_feed,
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
