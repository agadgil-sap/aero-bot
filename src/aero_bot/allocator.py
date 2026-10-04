"""Portfolio allocation over the ranked qualifying board.

The captain's allocator ruling (gnhf 33) moves the book from one funded
position to a portfolio: up to ten concurrent positions on the proven
~100-per-position scale, with the deployed count an OUTPUT of the
qualifying yield distribution under the risk bounds - sometimes five
positions using the full 1000 USDC total cap, sometimes cash held as dry
powder for APR spikes, and never an entry-bar tightened to chase
deployment.

This module holds the pure portfolio mathematics - no I/O, no clock, no
network - exactly like the policy engine and the selector it composes.
The ranked qualifying board (the selector's entry-gate evaluations), the
held position facts, the cash balance, and the portfolio equity are
injected, and every outcome is deterministic with ties broken
lexicographically by symbol so runs are reproducible.

The allocation is tiered: the top-ranked pool by weighted qualifying APR
receives the largest tranche, pools inside the configurable band of the
top (default: qualifying APR at or above fifty percent of the top's)
share the remaining tiers, and the residual is cash. The bounds that
make count-as-output honest are locked parameters with hard ceilings:
at most ten concurrent positions, no tranche below the per-name minimum
position size, and no name above the thirty-five percent concentration
cap of book equity.

The minimum and the concentration cap are coherent under the
captain's 2026-09-28 activation ruling, as corrected the same day by
the captain's sub-1000 ruling: below the activation equity (default
1000 USDC) there is NO per-name minimum and NO minimum-driven reserve
at all - the book deploys its available funds into the qualifying
board, because a minimum the unfunded book cannot meet is a reserve
that starves it (the live 2026-09-28 trap: 78.03 USDC of free cash
against the 80-USDC minimum froze the 105.73-USDC book fully idle
while a pool qualified). At or above the activation equity the funded
scale's bounds govern unchanged - the configured eighty-USDC minimum
and the thirty-five percent concentration cap exactly as the ruling
locked them - and the captain will revisit that policy once the book
is actually funded. The gnhf 36 coherence machinery stays for the
sealed early-activation override, where an engaged clamp below the
configured minimum still binds: engaged books compute the effective
minimum as ``max(floor, min(configured minimum, concentration clamp))``
with the hard gas-efficiency floor (default thirty USDC) bounding how
far the rule may lower the bound - never a license to breach the cap.
Rebalancing generalizes the
selector's thirty percent switch margin from switch-to-switch to
portfolio reallocation: a held pool whose weighted APR decayed below the
margin versus the next qualifying candidate inside the band is exited
and the candidate entered, exit-before-entry, so the total cap is never
breached even transiently and exactly one position is funded per step.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, NonNegativeDecimal
from aero_bot.emissions_apr import format_apr_percent
from aero_bot.lp_plan import DEFAULT_MINT_SLIPPAGE_TOLERANCE, DEFAULT_SWAP_BUFFER_FRACTION
from aero_bot.policy import (
    PolicyActionKind,
    PolicyDecision,
    PolicyEngine,
    PolicyObservation,
    PolicyOutcome,
    PolicyReason,
    PolicyState,
)
from aero_bot.selector import (
    DEFAULT_SWITCH_MARGIN_FRACTION,
    DEFAULT_SWITCH_MIN_HOLD,
    PoolEntryEvaluation,
    closest_call_evaluation,
    switch_gas_economics,
)

# The default tier band: a qualifying pool shares the tiers when its
# weighted qualifying APR sits at or above this fraction of the top's
# (captain's allocator ruling; configurable in the sealed cycle
# environment through AERO_BOT_CYCLE_TIER_BAND_FRACTION).
DEFAULT_TIER_BAND_FRACTION = Decimal("0.50")
# The hard ceiling on concurrent positions: the captain's ten, locked.
HARD_MAX_CONCURRENT_POSITIONS = 10
# The default maximum concurrent deployed positions.
DEFAULT_MAX_CONCURRENT_POSITIONS = 10
# The default minimum position size in USDC at or above the activation
# equity: below it, cash stays cash (locked parameter; the captain's
# count-as-output ruling). Below the activation equity there is NO
# minimum at all (the captain's 2026-09-28 sub-1000 correction).
DEFAULT_MIN_POSITION_USDC = Decimal("80")
# The default hard floor under the EFFECTIVE minimum position size in
# USDC (the gnhf 36 parameter-coherence ruling): the coherence rule
# lowers the per-name minimum to the concentration clamp when the clamp
# governs, but never below this gas-efficiency floor (configurable in
# the sealed cycle environment through
# AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC).
DEFAULT_MIN_POSITION_FLOOR_USDC = Decimal("30")
# The default per-name concentration cap as a fraction of book equity
# (locked parameter; the measured-edge baseline's MSTRc argument).
DEFAULT_CONCENTRATION_CAP_FRACTION = Decimal("0.35")
# The default book equity at or above which the per-name concentration
# cap engages (the captain's 2026-09-28 ruling): below it the cap does
# not bind at all - the trial-scale book funds toward the 1000-USDC
# total cap running the proven ~100-per-position shape - and at or
# above it the thirty-five-percent bound governs every name (sealed-env
# configurable through AERO_BOT_CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC;
# the hard ceiling is the same 1000 USDC total cap, so a sealed override
# can only engage the cap sooner, never later than the funded scale).
DEFAULT_CONCENTRATION_CAP_ACTIVATION_USDC = Decimal("1000")
# The total exposure ceiling the portfolio spans, aligned with the LP
# executor's hard pilot cap (raised to 1000/1000 USDC by the captain's
# 2026-09-27 performance ruling).
PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC = Decimal("1000")
# The money grid every tier share quantizes down to: USDC's own six
# decimal places, so the engine's scaled-basis sizing round-trips a
# share exactly and a share can never read above the budget that funded
# it through intermediate rounding.
TIER_SHARE_QUANTUM = Decimal("0.000001")


class PortfolioParameters(BaseModel):
    """Lock the portfolio allocation parameters as one immutable input.

    Every parameter is sealed-environment configurable through its
    default, and every bound carries a hard ceiling the configuration
    validator enforces so no sealed environment can loosen the captain's
    ruling: the count cap never exceeds ten, the minimum size stays
    positive and inside the total cap, and the concentration and band
    fractions stay inside their meaningful intervals.
    """

    # Frozen strict fields make the locked parameter set reproducible.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The maximum concurrent deployed positions (default ten, the
    # captain's ceiling; the deployed count emerges under it).
    max_concurrent_positions: Annotated[int, Field(ge=1)] = DEFAULT_MAX_CONCURRENT_POSITIONS
    # The minimum position size in USDC at or above the activation
    # equity: a tranche below the EFFECTIVE minimum stays cash. Below the
    # activation equity there is no minimum at all (the captain's
    # sub-1000 correction), so the book deploys its available funds. At
    # or above activation the effective minimum is the coherent
    # max(floor, min(this configured minimum, the per-name concentration
    # clamp at the live equity)); the configured value governs naturally
    # whenever the clamp sits above it (the 300-plus-equity regime).
    min_position_usdc: Annotated[Decimal, Field(gt=0)] = DEFAULT_MIN_POSITION_USDC
    # The hard floor under the effective minimum position size in USDC:
    # the coherence rule never lowers a per-name minimum below it. A
    # floor above the configured minimum is incoherent (it would raise
    # the minimum above the configured bound) and refuses validation.
    min_position_floor_usdc: Annotated[Decimal, Field(gt=0)] = DEFAULT_MIN_POSITION_FLOOR_USDC
    # The per-name concentration cap as a fraction of book equity.
    concentration_cap_fraction: Annotated[Decimal, Field(gt=0)] = DEFAULT_CONCENTRATION_CAP_FRACTION
    # The book equity at or above which the per-name concentration cap
    # engages (the captain's 2026-09-28 ruling): below the activation
    # equity the cap does not bind and neither does any per-name
    # minimum - the unfunded book deploys its available funds into the
    # qualifying board while it funds toward the cap - and at or above
    # it the thirty-five-percent bound governs every name beside the
    # configured minimum. The hard ceiling is the total exposure cap
    # itself, so a sealed override can only engage the cap sooner,
    # never later.
    concentration_cap_activation_equity_usdc: Annotated[Decimal, Field(gt=0)] = (
        DEFAULT_CONCENTRATION_CAP_ACTIVATION_USDC
    )
    # The tier band: qualifying APR at or above this fraction of the
    # top's shares the tiers; below it the pool sits out as cash.
    tier_band_fraction: Annotated[Decimal, Field(gt=0)] = DEFAULT_TIER_BAND_FRACTION
    # The relative weighted-APR margin a candidate must beat a held pool
    # by before a reallocation may fire (the selector's thirty percent
    # switch margin generalized to the portfolio).
    switch_margin_fraction: Decimal = DEFAULT_SWITCH_MARGIN_FRACTION
    # The total exposure the whole portfolio spans, matching the LP
    # executor's hard cap.
    total_exposure_cap_usdc: Annotated[Decimal, Field(gt=0)] = PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC

    @model_validator(mode="after")
    def require_ceiling_compliance(self) -> Self:
        """Enforce the hard ceilings no sealed configuration may exceed.

        Raises:
            ValueError: If any bound breaches its hard ceiling or
                interval.
        """
        if self.max_concurrent_positions > HARD_MAX_CONCURRENT_POSITIONS:
            raise ValueError(
                f"max_concurrent_positions {self.max_concurrent_positions} exceeds the hard "
                f"ceiling of {HARD_MAX_CONCURRENT_POSITIONS} concurrent positions"
            )
        if self.min_position_usdc > PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC:
            raise ValueError(
                f"min_position_usdc {self.min_position_usdc} exceeds the hard "
                f"{PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC} USDC total cap"
            )
        if self.min_position_floor_usdc > self.min_position_usdc:
            raise ValueError(
                f"min_position_floor_usdc {self.min_position_floor_usdc} exceeds the "
                f"configured minimum position size {self.min_position_usdc}; the floor "
                "bounds the coherence rule, it never raises the minimum"
            )
        if self.concentration_cap_fraction > Decimal(1):
            raise ValueError("concentration_cap_fraction must not exceed one")
        if self.concentration_cap_activation_equity_usdc > PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC:
            raise ValueError(
                f"concentration_cap_activation_equity_usdc "
                f"{self.concentration_cap_activation_equity_usdc} exceeds the hard "
                f"{PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC} USDC total cap; the activation "
                "equity may only engage the concentration cap sooner, never later"
            )
        if self.tier_band_fraction > Decimal(1):
            raise ValueError("tier_band_fraction must not exceed one")
        if self.switch_margin_fraction < 0:
            raise ValueError("switch_margin_fraction must be non-negative")
        if self.total_exposure_cap_usdc > PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC:
            raise ValueError(
                f"total_exposure_cap_usdc {self.total_exposure_cap_usdc} exceeds the hard "
                f"ceiling of {PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC} USDC"
            )
        return self

    def concentration_bound_usdc(self, equity_usdc: Decimal) -> Decimal | None:
        """Return the per-name concentration bound, or None below activation.

        The captain's 2026-09-28 ruling: the per-name concentration bound
        applies only when the book has reached the activation equity
        (default 1000 USDC, the funded scale the trial book grows toward);
        below it the cap does not bind at all, so the trial-scale book runs
        the proven deploy-what-you-have shape with no per-name minimum
        at all (the captain's sub-1000 correction).

        Args:
            equity_usdc: The portfolio equity the bound is judged against.

        Returns:
            The per-name concentration bound in USDC, or None when the cap
            is not engaged.
        """
        if equity_usdc < self.concentration_cap_activation_equity_usdc:
            return None
        return self.concentration_cap_fraction * equity_usdc

    def effective_minimum_position_usdc(self, equity_usdc: Decimal) -> Decimal:
        """Return the coherent per-name entry minimum at one book equity.

        The captain's 2026-09-28 ruling as corrected the same day by the
        captain's sub-1000 ruling: below the activation equity neither
        the concentration cap nor ANY per-name minimum binds - the
        unfunded book deploys its available funds, because a minimum the
        book cannot meet is a reserve that starves it. At or above the
        activation equity the funded scale's bounds govern: the clamp
        engages at thirty-five percent of a book that is already at the
        thousand-USDC scale, the configured eighty governs there at the
        defaults, and the coherence rule still protects the sealed
        early-activation override, where ``max(floor, min(configured
        minimum, concentration clamp))`` keeps the two bounds
        satisfiable without ever breaching the cap to reach the floor.

        Args:
            equity_usdc: The portfolio equity pricing the concentration
                clamp.

        Returns:
            The per-name minimum a tranche must meet to fund; zero below
            the activation equity (no minimum at all).
        """
        concentration_bound = self.concentration_bound_usdc(equity_usdc)
        if concentration_bound is None:
            return Decimal("0")
        return max(
            self.min_position_floor_usdc,
            min(self.min_position_usdc, concentration_bound),
        )

    def describe_effective_minimum(self, equity_usdc: Decimal) -> str:
        """Name the effective minimum's derivation and governing term.

        Every below-effective-minimum exclusion carries this line so an
        operator reads the binding term and both bounds at a glance (the
        gnhf 34-35 evidence style), never a bare number.

        Args:
            equity_usdc: The portfolio equity pricing the concentration
                clamp.

        Returns:
            One phrase stating the full derivation and its governing term.
        """
        if self.concentration_bound_usdc(equity_usdc) is None:
            return (
                f"no minimum below the "
                f"{self.concentration_cap_activation_equity_usdc} USDC activation equity "
                f"(book equity {equity_usdc}); the book deploys its available funds "
                "without a per-name minimum or reserve (the captain's sub-1000 ruling)"
            )
        concentration_bound = self.concentration_cap_fraction * equity_usdc
        if concentration_bound < self.min_position_floor_usdc:
            governing = "the hard floor governs"
        elif concentration_bound < self.min_position_usdc:
            governing = "the concentration clamp governs under the configured minimum"
        else:
            governing = "the configured minimum governs"
        return (
            f"effective minimum min({self.min_position_usdc} configured, "
            f"{concentration_bound} concentration clamp on equity {equity_usdc}) "
            f"floored at {self.min_position_floor_usdc} = "
            f"{self.effective_minimum_position_usdc(equity_usdc)}; {governing}"
        )


class PortfolioExclusionReason(StrEnum):
    """Catalog the stable typed reasons a pool earns no tranche."""

    # The pool's weighted qualifying APR sits below the tier band.
    BELOW_TIER_BAND = "below_tier_band"
    # The count bound (the ten-position ceiling minus held slots) is full.
    MAX_POSITIONS_REACHED = "max_positions_reached"
    # The tranche fell below the minimum position size; cash stays cash.
    BELOW_MIN_POSITION_SIZE = "below_min_position_size"
    # The concentration cap clamped the tranche to the per-name bound.
    CONCENTRATION_CAP_CLAMPED = "concentration_cap_clamped"
    # The engine's own entry gate refused at the tranche's scaled equity
    # (gas economics, depth cap, cooldown, halt, or staleness).
    ENTRY_GATE_REFUSED = "entry_gate_refused"
    # The deployable budget could not fund the derived size; cash stays.
    INSUFFICIENT_CASH = "insufficient_cash"
    # Unsold inventory from a stale-low burn blocks new entries.
    INVENTORY_UNWIND_PENDING = "inventory_unwind_pending"
    # No deployable budget remained: cash or total-cap headroom is zero.
    NO_DEPLOYABLE_BUDGET = "no_deployable_budget"
    # A reallocation's exit-plus-entry gas economics refused.
    REALLOCATION_GAS_REFUSED = "reallocation_gas_refused"
    # The held position has not run its minimum hold window yet.
    REALLOCATION_MIN_HOLD_ACTIVE = "reallocation_min_hold_active"
    # The held position sits inside the gauge's early-exit penalty window.
    REALLOCATION_PENALTY_WINDOW_ACTIVE = "reallocation_penalty_window_active"
    # The freed capital could not fund a minimum-size replacement.
    REALLOCATION_TOO_SMALL = "reallocation_too_small"
    # The post-swap book would breach the total exposure cap.
    REALLOCATION_CAP_BREACH = "reallocation_cap_breach"


class ExcludedPool(BaseModel):
    """Carry one pool's typed exclusion from the allocation."""

    # Frozen strict fields keep one exclusion exactly as reported.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol excluded, or "*" when the
    # exclusion applies to the whole board.
    symbol: str
    # The stable typed reason no tranche was earned.
    reason: PortfolioExclusionReason
    # The refusing gate's own stable name when this exclusion wraps an
    # engine refusal (for example ``daily_loss_halt_active`` or
    # ``gas_gate_deferred``); empty when the allocator's own bounds
    # decided. The summary line carries it so an operator reads the
    # refusing gate at a glance, never an opaque typed label.
    gate: str = ""
    # One compact cause phrase naming the binding bound and its measured
    # value (for example ``weighted 15.29 below band floor 34.79``); the
    # summary line carries it beside the typed reason.
    cause: str = ""
    # The excluded pool's qualifying emissions APR when one evaluation
    # was in hand; None for whole-board exclusions.
    emissions_apr: NonNegativeDecimal | None = None
    # One human evidence line carrying the numbers: the refusing gate,
    # its measured value, its bound, and the income the refusal forgoes
    # per day at the pool's qualifying APR (the gnhf 34 lost-yield
    # framing, the same one the out-of-range diagnostics carry).
    detail: str
    # The approximate daily income the exclusion forgoes at the pool's
    # qualifying APR on the tranche it would have earned, None when no
    # sized tranche was ever derived (tier-band and count exclusions).
    forgone_income_usdc_per_day: Decimal | None = None


class HeldPositionFact(BaseModel):
    """Carry one held position's facts the allocator plans against."""

    # Frozen strict fields keep one held fact coherent for the pass.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the position lives in.
    symbol: str
    # The position NFT id on the pool's NFPM.
    token_id: Annotated[int, Field(ge=0)]
    # The committed USDC value at entry, the planning basis.
    committed_usd: Annotated[Decimal, Field(gt=0)]
    # The live marked USDC value when readable; None falls back to the
    # committed value for headroom accounting.
    marked_usd: NonNegativeDecimal | None = None
    # When the position was entered, timezone-aware; the minimum hold
    # window for voluntary reallocations anchors here.
    entered_at: datetime
    # The held pool's current qualifying emissions APR.
    emissions_apr: NonNegativeDecimal

    @property
    def value_usd(self) -> Decimal:
        """Return the marked value when readable, else the committed."""
        return self.marked_usd if self.marked_usd is not None else self.committed_usd


class PortfolioTranche(BaseModel):
    """Carry one qualified tier the allocator would fund."""

    # Frozen strict fields keep one tranche exactly as it will execute.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol the tranche deploys into.
    symbol: str
    # The verified pool the tranche deploys into.
    pool_address: EvmAddress
    # The committed USDC value the tranche funds.
    budget_usd: Annotated[Decimal, Field(gt=0)]
    # The weighted qualifying APR that ranked the pool.
    weighted_apr: NonNegativeDecimal
    # The raw qualifying emissions APR behind the weight.
    emissions_apr: NonNegativeDecimal
    # The tier rank: one for the top-ranked funded pool.
    tier_rank: Annotated[int, Field(ge=1)]
    # The tranche-scaled observation the entry re-derivation ran over,
    # carried so the reallocation gas model reads the same gas price and
    # APR the tier decision read.
    observation: PolicyObservation
    # The engine's complete ENTER outcome re-derived at the tranche's own
    # scaled equity, so the range width, swap plan, and gas evidence all
    # belong to exactly the size being committed.
    entry_outcome: PolicyOutcome


class PortfolioAllocation(BaseModel):
    """Carry the allocator's complete target composition for one cycle."""

    # Frozen strict fields keep one allocation bound to its evidence.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The funded tiers, tier rank ascending (the top-ranked pool first).
    tranches: tuple[PortfolioTranche, ...] = ()
    # Every excluded qualifying pool with its typed reason.
    excluded: tuple[ExcludedPool, ...] = ()
    # The USDC available for new deployment this pass (cash bounded by
    # the total-cap headroom).
    deployable_usdc: NonNegativeDecimal = Decimal("0")
    # The cash left after the tranches: dry powder for APR spikes once
    # the activation equity engages the cap; plain cash below it.
    cash_residual_usdc: NonNegativeDecimal = Decimal("0")
    # The total committed value the allocation projects across every
    # position once its tranches fund.
    projected_committed_usdc: NonNegativeDecimal = Decimal("0")
    # The gate-chain evidence's subject symbol (the top-ranked pool the
    # trace evaluates; empty only when the board enumerated nothing).
    gate_trace_symbol: str = ""
    # One header line naming the traced pool and the exact sizing basis
    # its gates were judged at (the tranche target when the allocator
    # sized one, else the full-board observation basis).
    gate_trace_basis: str = ""
    # The complete per-gate evaluation for the traced pool: every ordered
    # entry gate with its verdict, measured value, and bound, so a
    # why-is-it-flat question is answerable from the audit store alone
    # (the captain's gnhf 34 ruling).
    gate_trace: tuple[str, ...] = ()
    # One human evidence line summarizing the allocation.
    summary: str


class PortfolioStepKind(StrEnum):
    """Identify every step a portfolio rebalance plan can carry."""

    # Execute one held position's own engine outcome (a safety exit or a
    # recenter) for its exact token id.
    POSITION_ACTION = "position_action"
    # Execute the held-inventory fold's outcome (convergence or sell).
    INVENTORY_ACTION = "inventory_action"
    # Exit one decayed held position and enter its qualified replacement,
    # exit-before-entry inside the one step.
    REALLOCATE = "reallocate"
    # Fund one new tranche: mint and stake exactly one position.
    ENTER = "enter"


class PortfolioRebalanceStep(BaseModel):
    """Carry one ordered executable step of the rebalance plan."""

    # Frozen strict fields keep one step exactly as it will execute.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Which kind of step this is.
    kind: PortfolioStepKind
    # The primary symbol the step acts on (the exit or entry symbol).
    symbol: str
    # The held NFT the step exits or recenters, when it acts on one.
    token_id: Annotated[int, Field(ge=0)] | None = None
    # The replacement symbol a reallocation enters.
    to_symbol: str | None = None
    # The exact engine (or composed switch) outcome the step executes.
    outcome: PolicyOutcome
    # One human evidence line for the report.
    diagnostic: str


class DeferredReallocation(BaseModel):
    """Carry one considered-and-refused reallocation with its typed reason."""

    # Frozen strict fields keep one deferral exactly as reported.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The held symbol whose reallocation was considered.
    symbol: str
    # The candidate the deferral names, when one cleared the margin.
    to_symbol: str | None = None
    # The stable typed reason the reallocation did not fire.
    reason: PortfolioExclusionReason
    # One human evidence line carrying the numbers.
    detail: str


class PortfolioRebalancePlan(BaseModel):
    """Carry the allocator's complete ordered rebalance plan for one cycle."""

    # Frozen strict fields keep one plan bound to its allocation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The allocation the plan converges the book toward.
    allocation: PortfolioAllocation
    # The ordered steps: safety exits, inventory resolution, recenters,
    # reallocations, then entries - exits precede entries so the total
    # cap is never breached even transiently.
    steps: tuple[PortfolioRebalanceStep, ...] = ()
    # The total committed value the plan projects once every step lands.
    projected_committed_usdc: NonNegativeDecimal = Decimal("0")
    # The position count the plan projects once every step lands.
    projected_position_count: Annotated[int, Field(ge=0)] = 0
    # The considered-and-refused reallocations with their typed reasons.
    deferred: tuple[DeferredReallocation, ...] = ()
    # One human evidence line summarizing the plan.
    summary: str


def weighted_apr_of(
    emissions_apr: Decimal, discipline_by_symbol: Mapping[str, Decimal], symbol: str
) -> Decimal:
    """Weight one pool's qualifying APR by its measured range discipline.

    The measured-edge baseline names in-range time the largest lever in
    the programme, so the tier ranking multiplies each pool's qualifying
    APR by its measured in-range fraction when the caller carries one; a
    name without a measurement ranks on its raw qualifying APR exactly
    like the locked selector ranking.

    Args:
        emissions_apr: The pool's raw qualifying emissions APR.
        discipline_by_symbol: Measured in-range fractions in [0, 1] per
            symbol; names absent carry no measurement.
        symbol: The pool's registry-matched symbol.

    Returns:
        The weighted qualifying APR.

    Raises:
        ValueError: If a carried discipline weight sits outside [0, 1].
    """
    weight = discipline_by_symbol.get(symbol)
    if weight is None:
        return emissions_apr
    if not Decimal(0) <= weight <= Decimal(1):
        raise ValueError(f"range discipline weight for {symbol} must sit in [0, 1], not {weight}")
    return emissions_apr * weight


def rank_qualifying_pools(
    evaluations: Sequence[PoolEntryEvaluation],
    discipline_by_symbol: Mapping[str, Decimal] | None = None,
) -> tuple[tuple[PoolEntryEvaluation, Decimal], ...]:
    """Rank every qualifying pool by weighted qualifying APR.

    The ranking is weighted qualifying APR descending with ties broken by
    the lexicographically smallest symbol, mirroring the selector's
    deterministic ranking so runs are reproducible.

    Args:
        evaluations: The board's complete entry-gate evaluations.
        discipline_by_symbol: Optional measured in-range fractions.

    Returns:
        The qualifying evaluations with their weighted APRs, best first.

    Raises:
        ValueError: If a carried discipline weight sits outside [0, 1].
    """
    discipline = discipline_by_symbol or {}
    ranked: list[tuple[PoolEntryEvaluation, Decimal]] = [
        (
            evaluation,
            weighted_apr_of(evaluation.emissions_apr, discipline, evaluation.symbol),
        )
        for evaluation in evaluations
        if evaluation.qualifies
    ]
    ranked.sort(key=lambda pair: (-pair[1], pair[0].symbol))
    return tuple(ranked)


def band_candidates(
    evaluations: Sequence[PoolEntryEvaluation],
    held_symbols: set[str],
    parameters: PortfolioParameters,
    discipline_by_symbol: Mapping[str, Decimal] | None = None,
) -> tuple[tuple[PoolEntryEvaluation, Decimal], ...]:
    """Cut the ranked qualifying board to the in-band candidate list.

    Held symbols never compete for fresh tiers - their capital is
    deployed - and pools whose weighted qualifying APR sits below the
    band fraction of the top's are cut: they neither earn a tranche nor
    attract a reallocation.

    Args:
        evaluations: The board's complete entry-gate evaluations.
        held_symbols: The held positions' symbols.
        parameters: The portfolio parameter set naming the band.
        discipline_by_symbol: Optional measured in-range fractions.

    Returns:
        The in-band candidates with their weighted APRs, best first.

    Raises:
        ValueError: If a carried discipline weight sits outside [0, 1].
    """
    ranked = [
        (evaluation, weight)
        for evaluation, weight in rank_qualifying_pools(evaluations, discipline_by_symbol)
        if evaluation.symbol not in held_symbols
    ]
    if not ranked:
        return ()
    band_floor = ranked[0][1] * parameters.tier_band_fraction
    return tuple((evaluation, weight) for evaluation, weight in ranked if weight >= band_floor)


def _scaled_entry_observation(
    engine: PolicyEngine,
    evaluation: PoolEntryEvaluation,
    budget_usdc: Decimal,
) -> PolicyObservation:
    """Build the tranche-sized observation the engine judges entries at.

    The observation's equity carries the tranche's own sizing basis (so
    the engine's eighty-percent cap sizes the tranche exactly) beside the
    full portfolio equity (so the day machinery and its drawdown latches
    keep judging the whole book, never the tranche's small basis).

    Args:
        engine: The locked per-pool policy engine naming the equity cap.
        evaluation: The qualifying evaluation being sized.
        budget_usdc: The tranche target in USDC.
        dilution_exit_pending_by_symbol: The per-pool dilution re-entry
            markers, arming the APR margin on re-entry.

    Returns:
        The scaled observation for the entry re-derivation.
    """
    scaled_equity = budget_usdc / engine.parameters.max_position_equity_fraction
    return evaluation.observation.model_copy(
        update={
            "equity_usd": scaled_equity,
            "portfolio_equity_usd": evaluation.observation.equity_usd,
        }
    )


def _gate_trace_lines(
    engine: PolicyEngine,
    base_state: PolicyState,
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
    evaluation: PoolEntryEvaluation | None,
    budget_usdc: Decimal | None,
    dilution_exit_pending_by_symbol: Mapping[str, bool] | None = None,
) -> tuple[str, str, tuple[str, ...]]:
    """Trace the complete entry gate chain for one pool at one basis.

    Args:
        engine: The locked per-pool policy engine.
        base_state: The threaded session state.
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.
        dilution_exit_pending_by_symbol: The per-pool dilution re-entry
            markers, arming the APR margin on re-entry.
        evaluation: The pool being traced; None yields no evidence.
        budget_usdc: The tranche target the gates judge at, or None to
            trace at the observation's own full-board basis.

    Returns:
        The traced symbol, one header line naming the basis, and one
        line per ordered gate with verdict, measurement, and bound.
    """
    if evaluation is None:
        return "", "", ()
    if budget_usdc is None:
        observation = evaluation.observation
        basis = (
            f"entry gate chain for {evaluation.symbol} at the full-board basis "
            f"(portfolio equity {observation.equity_usd} USDC):"
        )
    else:
        observation = _scaled_entry_observation(engine, evaluation, budget_usdc)
        basis = (
            f"entry gate chain for {evaluation.symbol} at the {budget_usdc} USDC "
            f"tranche basis (sized equity {observation.equity_usd} USDC, portfolio "
            f"equity {observation.portfolio_equity_usd} USDC):"
        )
    state = base_state.model_copy(
        update={
            "position": None,
            "held_inventory": None,
            "reentry_blocked_until": reentry_blocked_until_by_symbol.get(evaluation.symbol),
            "dilution_exit_pending": (dilution_exit_pending_by_symbol or {}).get(
                evaluation.symbol, False
            ),
        }
    )
    return evaluation.symbol, basis, engine.entry_gate_trace(state, observation)


def _entry_outcome_at_budget(
    engine: PolicyEngine,
    base_state: PolicyState,
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
    evaluation: PoolEntryEvaluation,
    budget_usdc: Decimal,
    dilution_exit_pending_by_symbol: Mapping[str, bool] | None = None,
) -> tuple[PolicyOutcome | None, PolicyObservation | None, str, str]:
    """Re-derive one pool's ENTER outcome at exactly the tranche budget.

    The engine stays the sole sizing authority: the observation's equity
    is scaled so the engine's eighty-percent equity cap equals the
    tranche target, and the complete entry gate chain re-runs at that
    size - the depth cap, the gas sense-check, the cooldown, the halt,
    and reference staleness all judge the tranche honestly, and a
    refusal at the smaller size records the pool as cash.

    The scaled observation carries the full portfolio equity through
    ``portfolio_equity_usd`` so the engine's day machinery - the
    day-start anchor and both drawdown latches - keeps judging the whole
    book, never the tranche's own sizing basis: before the gnhf 34 fix a
    small tranche's scaled equity read as a portfolio drawdown and the
    daily loss halt refused every fresh entry while the book was healthy.

    Args:
        engine: The locked per-pool policy engine.
        base_state: The threaded session state (position and inventory
            stripped; qualification is a flat-posture question).
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.
        evaluation: The qualifying evaluation being sized.
        budget_usdc: The tranche target in USDC.
        dilution_exit_pending_by_symbol: The per-pool dilution re-entry
            markers, arming the APR margin on re-entry.

    Returns:
        A triple of the ENTER outcome with its tranche-scaled observation
        (its size may sit below the target when the pool's depth cap
        binds), the refusing gate's stable name when the chain refused,
        and one evidence line - or None, the gate, and the line when the
        gate chain refused.
    """
    observation: PolicyObservation = _scaled_entry_observation(engine, evaluation, budget_usdc)
    state = base_state.model_copy(
        update={
            "position": None,
            "held_inventory": None,
            "reentry_blocked_until": reentry_blocked_until_by_symbol.get(evaluation.symbol),
            "dilution_exit_pending": (dilution_exit_pending_by_symbol or {}).get(
                evaluation.symbol, False
            ),
        }
    )
    outcome = engine.decide(state, observation)
    decision = outcome.decision
    if decision.action is not PolicyActionKind.ENTER:
        gate = decision.reason.value
        diagnostic = decision.diagnostics[0] if decision.diagnostics else ""
        return (
            None,
            None,
            gate,
            f"the entry gate refused at the tranche size ({gate}): {diagnostic}",
        )
    return outcome, observation, "", ""


def _build_tranche(
    engine: PolicyEngine,
    base_state: PolicyState,
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
    evaluation: PoolEntryEvaluation,
    weight: Decimal,
    rank: int,
    budget_usdc: Decimal,
    dilution_exit_pending_by_symbol: Mapping[str, bool] | None = None,
) -> tuple[PortfolioTranche | None, str, str]:
    """Derive one candidate's tranche at its budget, engine-judged.

    Args:
        engine: The locked per-pool policy engine.
        base_state: The threaded session state.
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.
        evaluation: The in-band candidate being sized.
        weight: The candidate's weighted qualifying APR.
        rank: The candidate's tier rank.
        budget_usdc: The committed USDC value the tranche targets.
        dilution_exit_pending_by_symbol: The per-pool dilution re-entry
            markers, arming the APR margin on re-entry.

    Returns:
        The derived tranche and empty gate and evidence strings, or None
        with the refusing gate's name and one refusal evidence line.
    """
    outcome, observation, gate, refusal = _entry_outcome_at_budget(
        engine,
        base_state,
        reentry_blocked_until_by_symbol,
        evaluation,
        budget_usdc,
        dilution_exit_pending_by_symbol,
    )
    if outcome is None or observation is None or outcome.decision.size_usd is None:
        return None, gate, refusal or "the entry gate carried no positive size"
    return (
        PortfolioTranche(
            symbol=evaluation.symbol,
            pool_address=evaluation.pool_address,
            budget_usd=outcome.decision.size_usd,
            weighted_apr=weight,
            emissions_apr=evaluation.emissions_apr,
            tier_rank=rank,
            observation=observation,
            entry_outcome=outcome,
        ),
        "",
        "",
    )


def allocate_portfolio(  # noqa: PLR0912, PLR0915 - one fixed tier construction
    engine: PolicyEngine,
    base_state: PolicyState,
    evaluations: Sequence[PoolEntryEvaluation],
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
    held: Sequence[HeldPositionFact],
    cash_usdc: Decimal,
    equity_usdc: Decimal,
    dilution_exit_pending_by_symbol: Mapping[str, bool] | None = None,
    parameters: PortfolioParameters | None = None,
    discipline_by_symbol: Mapping[str, Decimal] | None = None,
    inventory_pending: bool = False,
) -> PortfolioAllocation:
    """Build the tiered target composition from the ranked board.

    The deployed count is an output: every qualifying pool inside the
    tier band of the top earns a weight-proportional tranche of the
    deployable budget until the count bound, the minimum size, and the
    concentration cap say otherwise, and everything left is cash. No
    entry bar is ever tightened or loosened to chase deployment - the
    bounds decide, and cash is dry powder once the activation equity
    engages the cap (below it the book deploys its available funds with
    no reserve held back).

    Args:
        engine: The locked per-pool policy engine re-deriving entries.
        base_state: The threaded session state carrying the day, the
            anchors, and any halt latch.
        evaluations: The board's complete entry-gate evaluations.
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.
        held: The held position facts occupying slots and capital.
        dilution_exit_pending_by_symbol: The per-pool dilution re-entry
            markers, arming the APR margin on re-entry.
        cash_usdc: The Safe's live USDC available for deployment.
        equity_usdc: The portfolio equity pricing the concentration cap.
        parameters: The portfolio parameter set; None uses the locked
            defaults.
        discipline_by_symbol: Optional measured in-range fractions
            weighting the tier ranking.
        inventory_pending: True while unsold stock from a stale-low burn
            is held; the board never enters then.

    Returns:
        The complete allocation with its typed exclusions and summary.

    Raises:
        ValueError: If a carried discipline weight sits outside [0, 1].
    """
    resolved = parameters if parameters is not None else PortfolioParameters()
    held_symbols = {fact.symbol for fact in held}
    committed = sum((fact.value_usd for fact in held), Decimal("0"))
    excluded: list[ExcludedPool] = []
    headroom = resolved.total_exposure_cap_usdc - committed
    deployable = min(cash_usdc, headroom)
    if inventory_pending and deployable > 0:
        excluded.append(
            ExcludedPool(
                symbol="*",
                reason=PortfolioExclusionReason.INVENTORY_UNWIND_PENDING,
                cause="held inventory pending unwind",
                detail=(
                    "unsold inventory from a stale-low burn is held; the board "
                    "never enters until the convergence machinery unwinds it"
                ),
            )
        )
        deployable = Decimal("0")
    in_band = band_candidates(evaluations, held_symbols, resolved, discipline_by_symbol)
    ranked_not_held = [
        (evaluation, weight)
        for evaluation, weight in rank_qualifying_pools(evaluations, discipline_by_symbol)
        if evaluation.symbol not in held_symbols
    ]
    # The gate-chain evidence defaults to the best-ranked pool the book
    # could fund (or the closest call when nothing qualifies), traced at
    # the full-board basis; the tranche loop below overwrites it with the
    # tranche basis it actually judged rank one at.
    trace_evaluation = (
        ranked_not_held[0][0] if ranked_not_held else closest_call_evaluation(evaluations)
    )
    gate_trace_symbol, gate_trace_basis, gate_trace = _gate_trace_lines(
        engine, base_state, reentry_blocked_until_by_symbol, trace_evaluation, None
    )
    in_band_symbols = {evaluation.symbol for evaluation, _ in in_band}
    if ranked_not_held:
        band_floor = ranked_not_held[0][1] * resolved.tier_band_fraction
        for evaluation, weight in ranked_not_held:
            if evaluation.symbol not in in_band_symbols:
                excluded.append(
                    ExcludedPool(
                        symbol=evaluation.symbol,
                        reason=PortfolioExclusionReason.BELOW_TIER_BAND,
                        cause=(
                            f"weighted {format_apr_percent(weight)} below band floor "
                            f"{format_apr_percent(band_floor)} "
                            f"({resolved.tier_band_fraction} of the top's "
                            f"{format_apr_percent(ranked_not_held[0][1])})"
                        ),
                        emissions_apr=evaluation.emissions_apr,
                        detail=(
                            f"weighted APR {format_apr_percent(weight)} sits below "
                            f"the tier band floor {format_apr_percent(band_floor)} "
                            f"({resolved.tier_band_fraction} of the top's "
                            f"{format_apr_percent(ranked_not_held[0][1])})"
                        ),
                    )
                )
    if deployable <= 0:
        reason_detail = (
            f"the total cap leaves {headroom} USDC of headroom against {cash_usdc} USDC of cash"
            if cash_usdc > 0
            else "the Safe holds no USDC to deploy"
        )
        if inventory_pending:
            reason_detail += "; unsold inventory is pending"
        return PortfolioAllocation(
            excluded=tuple(excluded),
            deployable_usdc=Decimal("0"),
            cash_residual_usdc=cash_usdc,
            projected_committed_usdc=committed,
            gate_trace_symbol=gate_trace_symbol,
            gate_trace_basis=gate_trace_basis,
            gate_trace=gate_trace,
            summary=(
                f"allocation holds {len(held)} position(s) committed {committed} USDC; "
                f"{reason_detail}"
            ),
        )
    # The per-name concentration bound, None while the book sits below the
    # activation equity (the captain's 2026-09-28 rulings): the unfunded
    # book deploys its available funds with no per-name clamp and no
    # per-name minimum at all while it funds toward the cap.
    concentration_bound = resolved.concentration_bound_usdc(equity_usdc)
    # The count bound: held slots come off the concurrent ceiling first.
    open_slots = resolved.max_concurrent_positions - len(held)
    slotted = in_band[: max(open_slots, 0)]
    if open_slots <= 0:
        for evaluation, _ in in_band:
            excluded.append(
                ExcludedPool(
                    symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.MAX_POSITIONS_REACHED,
                    cause=(
                        f"{len(held)} of {resolved.max_concurrent_positions} slots held; "
                        "no slot open"
                    ),
                    emissions_apr=evaluation.emissions_apr,
                    detail=(
                        f"the book holds {len(held)} of at most "
                        f"{resolved.max_concurrent_positions} positions; no slot is open"
                    ),
                )
            )
        return PortfolioAllocation(
            excluded=tuple(excluded),
            deployable_usdc=Decimal("0"),
            cash_residual_usdc=cash_usdc,
            projected_committed_usdc=committed,
            gate_trace_symbol=gate_trace_symbol,
            gate_trace_basis=gate_trace_basis,
            gate_trace=gate_trace,
            summary=(
                f"allocation holds {len(held)} position(s) at the "
                f"{resolved.max_concurrent_positions}-position count bound; cash "
                f"{cash_usdc} USDC stays cash"
                + (" (dry powder)" if concentration_bound is not None else "")
            ),
        )
    for evaluation, _ in in_band[open_slots:]:
        excluded.append(
            ExcludedPool(
                symbol=evaluation.symbol,
                reason=PortfolioExclusionReason.MAX_POSITIONS_REACHED,
                cause=(
                    f"all {resolved.max_concurrent_positions} slots filled by better-ranked pools"
                ),
                emissions_apr=evaluation.emissions_apr,
                detail=(
                    f"{resolved.max_concurrent_positions} better-ranked pools fill every open slot"
                ),
            )
        )
    if not slotted:
        return PortfolioAllocation(
            excluded=tuple(excluded),
            deployable_usdc=deployable,
            cash_residual_usdc=cash_usdc,
            projected_committed_usdc=committed,
            gate_trace_symbol=gate_trace_symbol,
            gate_trace_basis=gate_trace_basis,
            gate_trace=gate_trace,
            summary=(
                f"allocation holds {len(held)} position(s) committed {committed} USDC; "
                f"no qualifying pool earned a tranche and {cash_usdc} USDC stays cash"
            ),
        )
    # The tier shares quantize DOWN to USDC's own six-decimal grid: the
    # engine re-derives each tranche at its scaled basis with a divide-by-
    # then-multiply-by the equity fraction, and that round-trip is exact
    # only when the target sits on a finite money grid. Unquantized shares
    # carry dozens of repeating digits, so an intermediate rounding can
    # land the engine's size - or the next tier's share - one ulp ABOVE
    # the deployable budget and wrongly refuse the book's entries as
    # insufficient cash, a defect the activation ruling exposed once the
    # clamp stopped masking every target under the bound. Rounding down
    # also guarantees a single-pool board's share never exceeds its
    # deployable budget.
    total_weight = sum((weight for _, weight in slotted), Decimal("0"))
    # The executable-cost reserve: every entry's on-chain spend is its
    # tranche plus the balancing swap's acquisition buffer over the stock
    # side (the planner buys the shortfall times one-plus-buffer in
    # DEFAULT_SWAP_BUFFER_FRACTION - the single authoritative value shared
    # with planning), so tier targets normalize over the deployable budget
    # divided by one-plus-buffer and the remaining-cash ledger decrements
    # each tranche's executable cost. Without this reserve a plan whose
    # shares fill the whole deployable deterministically starves its last
    # entry - the planner's live-balance preflight refuses it and the cycle
    # halts instead of leaving that tier as cash (the 2026-10-03 fixture
    # reproduction: needs 148.222222 against 147.972223 held).
    executable_multiplier = Decimal(1) + DEFAULT_SWAP_BUFFER_FRACTION
    # The net distributable budget: the deployable budget minus each funded
    # tranche's raw-unit ceiling allowance (one money-grid quantum per
    # planned tranche, the bound on the share floor plus the executable
    # cost's ceiling), all divided by one-plus-buffer. Without the up-front
    # allowance the ceiled executable costs can strand a profitable final
    # tier by a single raw USDC unit - a numerical artifact that discarded
    # an otherwise affordable tranche wholesale (the reviewer's 500-USDC
    # evidence: executable 185.185186 against 185.185185 remaining).
    tranche_allowance = TIER_SHARE_QUANTUM * Decimal(len(slotted))
    tier_budget = max(
        (deployable - tranche_allowance) / executable_multiplier,
        Decimal("0"),
    )
    # The coherent per-name entry minimum (the gnhf 36 rule over the
    # activated cap): whichever of the configured minimum and the engaged
    # concentration clamp binds, over the hard floor - the two bounds can
    # never again be mutually unsatisfiable the way 80-versus-36.84 starved
    # the 105-USDC book.
    effective_minimum = resolved.effective_minimum_position_usdc(equity_usdc)
    # The lost-yield basis: one day of the pool's qualifying emissions APR
    # on the tranche it would have earned, the same framing the
    # out-of-range diagnostics carry (the captain's gnhf 34 ruling).
    days_per_year = Decimal(365)
    tranches: list[PortfolioTranche] = []
    remaining = deployable
    # Each funded tranche's executable cost (its engine-sized budget plus
    # the acquisition-buffer reserve, ceiled on the money grid), so the
    # reported residual is the honest expected post-execution cash rather
    # than the pre-swap budget sum.
    executable_costs: list[Decimal] = []
    for rank, (evaluation, weight) in enumerate(slotted, start=1):
        target = (tier_budget * weight / total_weight).quantize(
            TIER_SHARE_QUANTUM, rounding=ROUND_DOWN
        )
        # The effective minimum position size floors the tranche - a
        # position near the proven per-position scale - so a thin budget
        # deploys to the top names first instead of being split into
        # sub-minimum stubs.
        if target < effective_minimum:
            target = effective_minimum
        clamped = False
        if concentration_bound is not None and target > concentration_bound:
            target = concentration_bound
            clamped = True
        if rank == 1:
            # The gate-chain evidence names the top-ranked pool at the
            # exact tranche basis the engine judged it at.
            gate_trace_symbol, gate_trace_basis, gate_trace = _gate_trace_lines(
                engine,
                base_state,
                reentry_blocked_until_by_symbol,
                evaluation,
                target,
                dilution_exit_pending_by_symbol,
            )
        forgone_per_day = +(target * evaluation.observation.income_expectation_apr / days_per_year)
        forgone_line = (
            f"income forgone about {forgone_per_day} USDC per day at the conservative "
            f"income APR {format_apr_percent(evaluation.observation.income_expectation_apr)}"
        )
        # The executable cost reserves the acquisition buffer over the
        # tranche: rounded UP on the money grid so raw-unit rounding can
        # never leave the ledger a fraction of a cent short.
        executable_cost = (target * executable_multiplier).quantize(
            TIER_SHARE_QUANTUM, rounding=ROUND_UP
        )
        if executable_cost > remaining:
            excluded.append(
                ExcludedPool(
                    symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.INSUFFICIENT_CASH,
                    cause=(
                        f"tier target {target} plus the "
                        f"{DEFAULT_SWAP_BUFFER_FRACTION} acquisition buffer costs "
                        f"{executable_cost}, above the {remaining} deployable left"
                    ),
                    emissions_apr=evaluation.emissions_apr,
                    detail=(
                        f"the tier target {target} USDC needs {executable_cost} USDC of "
                        f"executable entry cost (tranche plus the "
                        f"{DEFAULT_SWAP_BUFFER_FRACTION} acquisition buffer the "
                        "balancing swap adds over its stock side) but only "
                        f"{remaining} USDC of deployable budget remains after earlier "
                        f"tiers' executable costs; cash stays cash; {forgone_line}"
                    ),
                    forgone_income_usdc_per_day=forgone_per_day,
                )
            )
            continue
        tranche, gate, refusal = _build_tranche(
            engine,
            base_state,
            reentry_blocked_until_by_symbol,
            evaluation,
            weight,
            rank,
            target,
            dilution_exit_pending_by_symbol,
        )
        if tranche is None:
            excluded.append(
                ExcludedPool(
                    symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.ENTRY_GATE_REFUSED,
                    gate=gate,
                    cause=gate,
                    emissions_apr=evaluation.emissions_apr,
                    detail=f"{refusal}; {forgone_line}",
                    forgone_income_usdc_per_day=forgone_per_day,
                )
            )
            continue
        if tranche.budget_usd < effective_minimum:
            derivation = resolved.describe_effective_minimum(equity_usdc)
            if clamped and concentration_bound is not None:
                bound_line = (
                    f"the tier target clamped to the {concentration_bound} USDC per-name "
                    f"concentration bound ({resolved.concentration_cap_fraction} of "
                    f"equity {equity_usdc}) and that locked bound sits below the "
                    f"effective minimum {effective_minimum} USDC, so the engine sized "
                    f"{tranche.budget_usd} USDC; the book stays cash rather than "
                    f"breach the concentration cap to reach the floor; {derivation}"
                )
            else:
                bound_line = (
                    f"the engine's capped size {tranche.budget_usd} USDC sits below "
                    f"the effective minimum {effective_minimum} USDC; {derivation}"
                )
            excluded.append(
                ExcludedPool(
                    symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.BELOW_MIN_POSITION_SIZE,
                    cause=(
                        f"size {tranche.budget_usd} below the effective minimum "
                        f"{effective_minimum}"
                        + (
                            f" after the {concentration_bound} concentration clamp"
                            if clamped and concentration_bound is not None
                            else ""
                        )
                    ),
                    emissions_apr=evaluation.emissions_apr,
                    detail=(
                        f"{bound_line}; cash stays cash; income forgone about "
                        f"{
                            tranche.budget_usd
                            * evaluation.observation.income_expectation_apr
                            / days_per_year
                        } "
                        f"USDC per day at the conservative income APR "
                        f"{format_apr_percent(evaluation.observation.income_expectation_apr)}"
                    ),
                    forgone_income_usdc_per_day=(
                        +tranche.budget_usd
                        * evaluation.observation.income_expectation_apr
                        / days_per_year
                    ),
                )
            )
            continue
        remaining -= (tranche.budget_usd * executable_multiplier).quantize(
            TIER_SHARE_QUANTUM, rounding=ROUND_UP
        )
        executable_costs.append(
            (tranche.budget_usd * executable_multiplier).quantize(
                TIER_SHARE_QUANTUM, rounding=ROUND_UP
            )
        )
        tranches.append(tranche)
        if clamped and concentration_bound is not None:
            excluded.append(
                ExcludedPool(
                    symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.CONCENTRATION_CAP_CLAMPED,
                    cause=(
                        f"target clamped to {concentration_bound} "
                        f"({resolved.concentration_cap_fraction} of equity {equity_usdc})"
                    ),
                    emissions_apr=evaluation.emissions_apr,
                    detail=(
                        f"the tier target clamped to the {concentration_bound} USDC "
                        f"per-name bound ({resolved.concentration_cap_fraction} of "
                        f"equity {equity_usdc}); the tranche still funds"
                    ),
                )
            )
    funded = sum((tranche.budget_usd for tranche in tranches), Decimal("0"))
    # The residual names the cash the executed plan is honestly expected to
    # leave: every funded tranche spends its budget plus its acquisition
    # buffer, so the pre-swap budget sum would overstate leftover cash.
    residual = cash_usdc - sum(executable_costs, Decimal("0"))
    projected = committed + funded
    tier_text = ", ".join(f"{tranche.symbol} {tranche.budget_usd}" for tranche in tranches)
    excluded_text = (
        "; excluded "
        + ", ".join(
            f"{item.symbol} ({item.reason.value}: {item.gate})"
            if item.gate
            else (
                f"{item.symbol} ({item.reason.value}: {item.cause})"
                if item.cause
                else f"{item.symbol} ({item.reason.value})"
            )
            for item in excluded
        )
        if excluded
        else ""
    )
    # Below the activation equity there is no dry-powder policy - the
    # captain's sub-1000 ruling - so the residual wording names plain
    # cash; the dry-powder framing stays for the engaged-cap book where
    # the gnhf 33 count-as-output ruling genuinely holds cash back.
    residual_wording = (
        f"{residual} USDC stays cash"
        if concentration_bound is None
        else f"{residual} USDC stays cash (dry powder)"
    )
    return PortfolioAllocation(
        tranches=tuple(tranches),
        excluded=tuple(excluded),
        deployable_usdc=deployable,
        cash_residual_usdc=residual,
        projected_committed_usdc=projected,
        gate_trace_symbol=gate_trace_symbol,
        gate_trace_basis=gate_trace_basis,
        gate_trace=gate_trace,
        summary=(
            f"allocation funds {len(tranches)} tranche(s) [{tier_text}] totaling {funded} "
            f"USDC beside {len(held)} held position(s) committed {committed} USDC; "
            f"{residual_wording} under the "
            f"{resolved.total_exposure_cap_usdc} USDC total cap with {projected} USDC projected"
            f"{excluded_text}"
        ),
    )


def _is_safety_exit(action: PolicyActionKind) -> bool:
    """Return whether one action kind fully unwinds a held position."""
    return action in (
        PolicyActionKind.STOP_OUT,
        PolicyActionKind.DILUTION_EXIT,
        PolicyActionKind.EVENT_EXIT,
        PolicyActionKind.DISLOCATION_EXIT,
        PolicyActionKind.DEFENSIVE_EXIT,
        PolicyActionKind.RANGE_GRACE_EXIT,
        PolicyActionKind.STALE_LOW_BURN,
    )


def _composed_reallocation_decision(
    held: HeldPositionFact,
    tranche: PortfolioTranche,
    margin_fraction: Decimal,
    combined_gas_units: int,
    combined_gas_cost_usd: Decimal | None,
    gas_diagnostics: tuple[str, ...],
) -> PolicyOutcome:
    """Compose one reallocation's switch decision from its evidence.

    Args:
        held: The decayed position being exited.
        tranche: The replacement tier being entered.
        margin_fraction: The relative margin the candidate cleared.
        combined_gas_units: The exit-plus-entry batch gas units.
        combined_gas_cost_usd: The combined cost under the ETH price
            assumption; None never occurs on a qualified reallocation
            because the economics check fails closed without gas.
        gas_diagnostics: The gas evidence behind the economics check.

    Returns:
        The composed POOL_SWITCH outcome the act layer executes.
    """
    entry_decision = tranche.entry_outcome.decision
    decision = PolicyDecision(
        action=PolicyActionKind.POOL_SWITCH,
        reason=PolicyReason.POOL_SWITCH_TRIGGERED,
        diagnostics=(
            f"Reallocating {held.symbol} -> {tranche.symbol}: the candidate's weighted "
            f"qualifying APR {format_apr_percent(tranche.weighted_apr)} exceeds the "
            f"held {format_apr_percent(held.emissions_apr)} by more than the "
            f"{margin_fraction} relative margin "
            "while the held pool earned no tier it outranks.",
            *gas_diagnostics,
            *entry_decision.diagnostics,
        ),
        price_range=entry_decision.price_range,
        size_usd=entry_decision.size_usd,
        swap_plan=entry_decision.swap_plan,
        estimated_gas_units=combined_gas_units,
        estimated_gas_cost_usd=combined_gas_cost_usd,
        width_solution=entry_decision.width_solution,
    )
    return PolicyOutcome(
        decision=decision,
        # The successor state is planning evidence only: the cycle's act
        # layer threads the book, never this composed outcome's state.
        next_state=tranche.entry_outcome.next_state,
    )


def _reallocation_tranche(
    engine: PolicyEngine,
    base_state: PolicyState,
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
    evaluation: PoolEntryEvaluation,
    weight: Decimal,
    rank: int,
    held_value: Decimal,
    cap_after_exit: Decimal,
    deployable: Decimal,
    total_weight: Decimal,
    concentration_bound: Decimal | None,
) -> tuple[PortfolioTranche | None, str, str]:
    """Size one reallocation replacement for the freed capital.

    The replacement commits the larger of its tier-weight share of the
    deployable budget and the freed marked value - a swap redeploys at
    the proven per-position scale - clamped by the per-name
    concentration bound and by whatever the total cap leaves after the
    exit, and judged by the engine's complete entry gate chain at that
    exact size.

    Args:
        engine: The locked per-pool policy engine.
        base_state: The threaded session state.
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.
        evaluation: The in-band candidate being sized.
        weight: The candidate's weighted qualifying APR.
        rank: The candidate's tier rank.
        held_value: The exiting position's marked value in USDC.
        cap_after_exit: The total cap headroom after the exit frees the
            held value.
        deployable: The pass's deployable budget for weight shares.
        total_weight: The band's total weight.
        concentration_bound: The per-name concentration bound in USDC, or
            None while the book sits below the activation equity (the
            captain's 2026-09-28 ruling: the trial-scale book reallocates
            at the proven per-position scale with no per-name clamp).

    Returns:
        The sized replacement tranche, or None and one evidence line.
    """
    # The share quantizes down to USDC's six-decimal grid for the same
    # reason the tier targets do: the engine's scaled-basis round-trip is
    # exact only on a finite money grid, and rounding down never pushes a
    # freed-capital replacement past its bound. The share base carries the
    # same executable-cost reserve the tier targets do (the acquisition
    # buffer over the replacement's stock side), so a replacement funded
    # from the whole remaining deployable can always pay its own swap.
    if total_weight > 0:
        weight_share = (
            deployable / (Decimal(1) + DEFAULT_SWAP_BUFFER_FRACTION) * weight / total_weight
        ).quantize(TIER_SHARE_QUANTUM, rounding=ROUND_DOWN)
    else:
        weight_share = Decimal("0")
    # The proven per-position scale carries the executable-cost reserve
    # the tier targets do, plus the switch preflight's own conservative
    # sale haircut: the preflight projects the freed stock's USDC proceeds
    # at one-minus the mint slippage tolerance, so the replacement must
    # fit the projected post-exit inventory under both shared bounds or
    # the reallocation never fires on a fully funded book.
    executable_bounds = (Decimal(1) - DEFAULT_MINT_SLIPPAGE_TOLERANCE) / (
        Decimal(1) + DEFAULT_SWAP_BUFFER_FRACTION
    )
    held_floor = (held_value * executable_bounds).quantize(TIER_SHARE_QUANTUM, rounding=ROUND_DOWN)
    budget = max(weight_share, held_floor)
    if concentration_bound is not None:
        budget = min(budget, concentration_bound)
    budget = min(budget, cap_after_exit)
    tranche, gate, refusal = _build_tranche(
        engine,
        base_state,
        reentry_blocked_until_by_symbol,
        evaluation,
        weight,
        rank,
        budget,
    )
    return tranche, gate, refusal


def plan_portfolio_rebalance(  # noqa: PLR0912, PLR0915 - one fixed precedence
    engine: PolicyEngine,
    allocation: PortfolioAllocation,
    evaluations: Sequence[PoolEntryEvaluation],
    base_state: PolicyState,
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
    held: Sequence[HeldPositionFact],
    held_outcomes: Mapping[str, PolicyOutcome],
    cash_usdc: Decimal,
    equity_usdc: Decimal,
    inventory_symbol: str | None = None,
    inventory_outcome: PolicyOutcome | None = None,
    parameters: PortfolioParameters | None = None,
    discipline_by_symbol: Mapping[str, Decimal] | None = None,
    now: datetime | None = None,
    penalty_blocked_symbols: frozenset[str] = frozenset(),
) -> PortfolioRebalancePlan:
    """Plan the ordered rebalance from the held book toward the allocation.

    Precedence mirrors the engine's own, per position: safety exits
    first, then the held-inventory resolution, then recenters, then the
    decay-margin reallocations, then the fresh entries. Every exit
    precedes every entry it funds, so the total cap is never breached
    even transiently and exactly one position is funded per step.

    A held pool is reallocated only when its weighted APR decayed below
    the switch margin versus an in-band qualifying candidate - the
    selector's thirty percent margin generalized from switch-to-switch
    to portfolio reallocation - and only past the minimum hold window
    with the exit-plus-entry gas economics passing.

    Args:
        engine: The locked per-pool policy engine supplying the gas model.
        allocation: The target composition being converged toward.
        evaluations: The board's complete entry-gate evaluations.
        base_state: The threaded session state.
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.
        held: The held position facts.
        held_outcomes: Each held position's own engine fold outcome,
            keyed by symbol; every held symbol must carry one.
        cash_usdc: The Safe's live USDC.
        equity_usdc: The portfolio equity pricing the concentration cap.
        inventory_symbol: The held inventory's symbol, when one is held.
        inventory_outcome: The held-inventory fold's outcome, if any.
        parameters: The portfolio parameter set; None uses the locked
            defaults.
        discipline_by_symbol: Optional measured in-range fractions.
        now: The planning instant anchoring the minimum hold window;
            None applies no minimum hold (pure replays inject their own).
        penalty_blocked_symbols: Held symbols inside the gauge's
            early-exit penalty window; their reallocations defer.

    Returns:
        The complete ordered plan with its projected committed value,
        position count, and deferred-reallocation evidence.

    Raises:
        ValueError: If a held position carries no fold outcome, the plan
            would breach the total exposure cap or the count bound, or a
            fold produced an action the plan cannot map.
    """
    resolved = parameters if parameters is not None else PortfolioParameters()
    safety_steps: list[PortfolioRebalanceStep] = []
    recenter_steps: list[PortfolioRebalanceStep] = []
    reallocation_steps: list[PortfolioRebalanceStep] = []
    entry_steps: list[PortfolioRebalanceStep] = []
    held_by_symbol = {fact.symbol: fact for fact in held}
    deferred: list[DeferredReallocation] = []
    for fact in held:
        if fact.symbol not in held_outcomes:
            raise ValueError(f"the held position {fact.symbol} carries no engine fold outcome")
    committed = sum((fact.value_usd for fact in held), Decimal("0"))
    in_band = band_candidates(evaluations, set(held_by_symbol), resolved, discipline_by_symbol)
    # Held positions whose own fold produced an action keep the engine's
    # verdict; a safety exit frees its slot and capital.
    exiting_symbols: set[str] = set()
    for fact in held:
        outcome = held_outcomes[fact.symbol]
        action = outcome.decision.action
        if action is PolicyActionKind.HOLD:
            continue
        if _is_safety_exit(action):
            exiting_symbols.add(fact.symbol)
            safety_steps.append(
                PortfolioRebalanceStep(
                    kind=PortfolioStepKind.POSITION_ACTION,
                    symbol=fact.symbol,
                    token_id=fact.token_id,
                    outcome=outcome,
                    diagnostic=(
                        f"{fact.symbol} position {fact.token_id}: the engine ordered "
                        f"{action.value} ({outcome.decision.reason.value})"
                    ),
                )
            )
        elif action is PolicyActionKind.RECENTER:
            recenter_steps.append(
                PortfolioRebalanceStep(
                    kind=PortfolioStepKind.POSITION_ACTION,
                    symbol=fact.symbol,
                    token_id=fact.token_id,
                    outcome=outcome,
                    diagnostic=(
                        f"{fact.symbol} position {fact.token_id}: the engine ordered a "
                        f"recenter ({outcome.decision.reason.value})"
                    ),
                )
            )
        else:
            raise ValueError(
                f"the held fold for {fact.symbol} produced unmapped action {action.value}"
            )
    inventory_steps: list[PortfolioRebalanceStep] = []
    if (
        inventory_outcome is not None
        and inventory_symbol is not None
        and inventory_outcome.decision.action is not PolicyActionKind.HOLD
    ):
        inventory_steps.append(
            PortfolioRebalanceStep(
                kind=PortfolioStepKind.INVENTORY_ACTION,
                symbol=inventory_symbol,
                outcome=inventory_outcome,
                diagnostic=(
                    "held inventory resolves first: the engine ordered "
                    f"{inventory_outcome.decision.action.value} "
                    f"({inventory_outcome.decision.reason.value})"
                ),
            )
        )
    # Reallocation: a held pool whose fold held, past its minimum hold,
    # displaced by an in-band candidate past the margin with the
    # exit-plus-entry gas economics passing. The replacement sizes
    # against the freed capital, so a full book still rotates its
    # decayed names without ever breaching the total cap.
    claimed_targets: set[str] = set()
    deployable = allocation.deployable_usdc
    total_weight = sum((weight for _, weight in in_band), Decimal("0"))
    # The per-name concentration bound, None below the activation equity
    # (the captain's 2026-09-28 ruling): the trial-scale book rotates its
    # decayed names at the proven per-position scale with no clamp.
    concentration_bound = resolved.concentration_bound_usdc(equity_usdc)
    # The same coherent per-name minimum governs replacements: a freed
    # tranche must meet the effective minimum, never the unsatisfiable
    # configured-versus-clamp pair that starved the small book.
    effective_minimum = resolved.effective_minimum_position_usdc(equity_usdc)
    for fact in sorted(held, key=lambda item: item.symbol):
        if fact.symbol in exiting_symbols:
            continue
        if held_outcomes[fact.symbol].decision.action is not PolicyActionKind.HOLD:
            continue
        if fact.symbol in penalty_blocked_symbols:
            deferred.append(
                DeferredReallocation(
                    symbol=fact.symbol,
                    reason=PortfolioExclusionReason.REALLOCATION_PENALTY_WINDOW_ACTIVE,
                    detail=(
                        "the held position sits inside the gauge's early-exit penalty "
                        "window; deferring the voluntary reallocation"
                    ),
                )
            )
            continue
        if now is not None:
            held_for = now - fact.entered_at
            if held_for < DEFAULT_SWITCH_MIN_HOLD:
                remaining = DEFAULT_SWITCH_MIN_HOLD - held_for
                deferred.append(
                    DeferredReallocation(
                        symbol=fact.symbol,
                        reason=PortfolioExclusionReason.REALLOCATION_MIN_HOLD_ACTIVE,
                        detail=(
                            f"reallocation deferred for "
                            f"{remaining.total_seconds():.0f}s of the minimum hold window"
                        ),
                    )
                )
                continue
        threshold = fact.emissions_apr * (Decimal(1) + resolved.switch_margin_fraction)
        candidates = [
            (evaluation, weight)
            for evaluation, weight in in_band
            if evaluation.symbol not in claimed_targets and weight > threshold
        ]
        if not candidates:
            continue
        evaluation, weight = candidates[0]
        cap_after_exit = resolved.total_exposure_cap_usdc - committed + fact.value_usd
        tranche, _gate, refusal = _reallocation_tranche(
            engine,
            base_state,
            reentry_blocked_until_by_symbol,
            evaluation,
            weight,
            in_band.index((evaluation, weight)) + 1,
            fact.value_usd,
            cap_after_exit,
            deployable,
            total_weight,
            concentration_bound,
        )
        if tranche is None:
            deferred.append(
                DeferredReallocation(
                    symbol=fact.symbol,
                    to_symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.REALLOCATION_TOO_SMALL,
                    detail=refusal,
                )
            )
            continue
        if tranche.budget_usd < effective_minimum:
            deferred.append(
                DeferredReallocation(
                    symbol=fact.symbol,
                    to_symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.REALLOCATION_TOO_SMALL,
                    detail=(
                        f"the freed capital sizes a {tranche.budget_usd} USDC replacement "
                        f"below the effective minimum {effective_minimum} USDC "
                        f"({resolved.describe_effective_minimum(equity_usdc)}); "
                        "the held pool stays"
                    ),
                )
            )
            continue
        economics_pass, combined_units, combined_cost, gas_diagnostics = switch_gas_economics(
            engine, tranche.budget_usd, tranche.observation
        )
        if not economics_pass:
            deferred.append(
                DeferredReallocation(
                    symbol=fact.symbol,
                    to_symbol=evaluation.symbol,
                    reason=PortfolioExclusionReason.REALLOCATION_GAS_REFUSED,
                    detail=f"reallocation gas economics refused ({gas_diagnostics[0]})",
                )
            )
            continue
        claimed_targets.add(evaluation.symbol)
        reallocation_steps.append(
            PortfolioRebalanceStep(
                kind=PortfolioStepKind.REALLOCATE,
                symbol=fact.symbol,
                token_id=fact.token_id,
                to_symbol=evaluation.symbol,
                outcome=_composed_reallocation_decision(
                    fact,
                    tranche,
                    resolved.switch_margin_fraction,
                    combined_units,
                    combined_cost,
                    gas_diagnostics,
                ),
                diagnostic=(
                    f"reallocating {fact.symbol} -> {evaluation.symbol}: held weighted "
                    f"APR {format_apr_percent(fact.emissions_apr)} decayed below the "
                    f"{threshold} margin threshold "
                    f"and the candidate earned tier {tranche.tier_rank}"
                ),
            )
        )
    # Fresh entries fill whatever tranches the allocation funded and no
    # reallocation claimed; held symbols never double-enter.
    claimed_by_reallocation = {
        step.to_symbol for step in reallocation_steps if step.to_symbol is not None
    }
    for tranche in allocation.tranches:
        if tranche.symbol in claimed_by_reallocation or tranche.symbol in held_by_symbol:
            continue
        entry_steps.append(
            PortfolioRebalanceStep(
                kind=PortfolioStepKind.ENTER,
                symbol=tranche.symbol,
                outcome=tranche.entry_outcome,
                diagnostic=(
                    f"entering {tranche.symbol} at tier {tranche.tier_rank}: "
                    f"{tranche.budget_usd} USDC at weighted APR "
                    f"{format_apr_percent(tranche.weighted_apr)}"
                ),
            )
        )
    steps: tuple[PortfolioRebalanceStep, ...] = (
        *sorted(safety_steps, key=lambda step: step.symbol),
        *inventory_steps,
        *sorted(recenter_steps, key=lambda step: step.symbol),
        *sorted(reallocation_steps, key=lambda step: step.symbol),
        *entry_steps,
    )
    # The headroom projection: exits free, reallocations swap one for
    # one, entries commit; the invariant must hold at every prefix.
    projected = committed
    projected_count = len(held)
    for step in steps:
        if step.kind is PortfolioStepKind.POSITION_ACTION and _is_safety_exit(
            step.outcome.decision.action
        ):
            fact = held_by_symbol[step.symbol]
            projected -= fact.value_usd
            projected_count -= 1
        elif step.kind is PortfolioStepKind.REALLOCATE:
            fact = held_by_symbol[step.symbol]
            size = step.outcome.decision.size_usd or Decimal("0")
            projected = projected - fact.value_usd + size
        elif step.kind is PortfolioStepKind.ENTER:
            size = step.outcome.decision.size_usd or Decimal("0")
            projected += size
            projected_count += 1
            if projected_count > resolved.max_concurrent_positions:
                raise ValueError(
                    f"the plan projects {projected_count} positions above the "
                    f"{resolved.max_concurrent_positions} count bound"
                )
        if projected > resolved.total_exposure_cap_usdc:
            raise ValueError(
                f"the plan projects {projected} USDC committed above the "
                f"{resolved.total_exposure_cap_usdc} USDC total cap"
            )
    step_text = ", ".join(f"{step.kind.value}:{step.symbol}" for step in steps) or "no steps"
    summary = (
        f"rebalance plan carries {len(steps)} step(s) [{step_text}] projecting "
        f"{projected} USDC committed across {projected_count} position(s)"
    )
    if deferred:
        summary += "; " + "; ".join(f"{item.symbol}: {item.reason.value}" for item in deferred)
    return PortfolioRebalancePlan(
        allocation=allocation,
        steps=tuple(steps),
        projected_committed_usdc=projected,
        projected_position_count=projected_count,
        deferred=tuple(deferred),
        summary=summary,
    )
