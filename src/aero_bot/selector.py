"""Cross-board B20 pool selection over the locked per-pool policy engine.

The captain's 2026-09-09 trial ruling moved the cycle from one pinned symbol
to the whole verified B20 board: every pool that survives the screener's
factory, pair, kind, uniqueness, gauge-liveness, and AERO-emission validation
is evaluated through the COMPLETE locked entry gate chain, and the best
qualifying pool by qualifying emissions APR wins. This module holds the pure
selection mathematics - no I/O, no clock, no network - exactly like the
policy engine it composes: pool observations, the threaded engine state, and
the per-pool re-entry cooldowns are injected, and every verdict is
deterministic with ties broken lexicographically by symbol so runs are
reproducible.

The anti-churn discipline is part of the same ruling: while a position is
held, another pool only displaces it when its qualifying emissions APR
exceeds the held pool's by the configurable relative switch margin (default
thirty percent) AND the exit-plus-entry gas economics still pass the locked
cost-share bound. Safety exits, maintenance, and held-inventory resolution on
the held pool always take precedence over any switch, and the board never
enters while a position or unsold inventory exists, so at most one position
is ever held inside the unchanged 100 USDC total exposure cap.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, NonNegativeDecimal
from aero_bot.policy import (
    DAYS_PER_YEAR,
    GWEI_PER_ETH,
    PolicyActionKind,
    PolicyDecision,
    PolicyEngine,
    PolicyObservation,
    PolicyOutcome,
    PolicyReason,
    PolicyState,
)

# The default relative margin another pool's qualifying emissions APR must
# exceed the held pool's by before a switch may fire. Set by the captain's
# 2026-09-09 trial ruling: thirty percent absorbs the displayed APR's own
# cell-concentration drift (the anchor pool's 357 percent read computed 346.5
# percent forty minutes later) so ordinary APR wobble never churns a funded
# position, while a genuinely better pool still displaces a stale one.
DEFAULT_SWITCH_MARGIN_FRACTION = Decimal("0.30")


class PoolBoardOption(BaseModel):
    """Carry one enumerated pool with its complete decision inputs."""

    # Frozen strict fields keep one board option coherent for the whole pass.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol naming this pool.
    symbol: str
    # The verified Aerodrome Slipstream pool the observation came from.
    pool_address: EvmAddress
    # The B20 stock token paired with native USDC in that pool.
    token_address: EvmAddress
    # The complete policy observation assembled for this pool.
    observation: PolicyObservation


class PoolEntryEvaluation(BaseModel):
    """Carry one pool's complete entry-gate outcome on the board."""

    # Frozen strict fields keep one evaluation exactly as it will be reported.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched stock symbol this evaluation covers.
    symbol: str
    # The verified pool the evaluation ran against.
    pool_address: EvmAddress
    # The complete observation the evaluation ran over.
    observation: PolicyObservation
    # The qualifying emissions APR is the observation's raw emissions APR in
    # Aerodrome's displayed convention - the ranking key once the complete
    # entry gate chain has already passed for this pool.
    emissions_apr: NonNegativeDecimal
    # True only when the complete entry gate chain produced an ENTER verdict.
    qualifies: bool
    # The stable blocking reason when the pool did not qualify, else None.
    blocked_reason: PolicyReason | None = None
    # The blocking gate's primary diagnostic when the pool did not qualify.
    blocked_diagnostic: str = ""
    # The engine's full ENTER outcome when qualified, else None.
    entry_outcome: PolicyOutcome | None = None

    @property
    def entry_size_usd(self) -> NonNegativeDecimal | None:
        """Return the qualified entry's committed size, else None."""
        if self.entry_outcome is None:
            return None
        return self.entry_outcome.decision.size_usd


class SwitchDirective(BaseModel):
    """Carry one qualified cross-pool switch with its complete evidence."""

    # Frozen strict fields keep the switch exactly as it will be executed.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The symbol of the funded position being exited.
    from_symbol: str
    # The symbol of the better-qualifying pool being entered.
    to_symbol: str
    # The verified pool being entered.
    to_pool_address: EvmAddress
    # The winning pool's complete ENTER outcome from the gate-chain pass.
    entry_outcome: PolicyOutcome
    # The configured relative switch margin this switch cleared, as a fraction.
    margin_fraction: NonNegativeDecimal
    # The held pool's qualifying emissions APR the margin was measured against.
    held_emissions_apr: NonNegativeDecimal
    # The winning pool's qualifying emissions APR.
    candidate_emissions_apr: NonNegativeDecimal
    # The combined exit-plus-entry batch gas units, both Safe overheads included.
    combined_gas_units: int
    # The combined batch cost under the documented ETH price assumption;
    # None never occurs on a qualified switch because the economics check
    # fails closed without a gas reading.
    combined_gas_cost_usd: NonNegativeDecimal | None
    # The exit-plus-entry gas evidence behind the economics check.
    diagnostics: tuple[str, ...] = ()


class BoardSelection(BaseModel):
    """Carry the selector's complete verdict for one cycle over the board."""

    # Frozen strict fields keep the verdict bound to its evaluations.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The engine verdict the cycle should act on - a held-pool decision, a
    # winning pool's ENTER, or the composed POOL_SWITCH decision.
    outcome: PolicyOutcome
    # Every pool's entry-gate evaluation, in deterministic board order.
    evaluations: tuple[PoolEntryEvaluation, ...] = ()
    # The symbol the decision covers: the held or selected pool; None only
    # when no pool qualified or the board carried no options at all.
    selected_symbol: str | None
    # The qualified switch directive, present only on a POOL_SWITCH verdict.
    switch: SwitchDirective | None = None
    # One human evidence line summarizing the selection for the report.
    summary: str


def _board_summary(evaluations: Sequence[PoolEntryEvaluation], selected_symbol: str | None) -> str:
    """Compose one deterministic board evidence line.

    Args:
        evaluations: The board's complete entry-gate evaluations.
        selected_symbol: The held or selected symbol, or None.

    Returns:
        One line naming every pool's qualification outcome and the selection.
    """
    parts = [
        f"{evaluation.symbol}: "
        + (
            f"qualified at APR {evaluation.emissions_apr}"
            if evaluation.qualifies
            else (
                f"skipped ({evaluation.blocked_reason.value})"
                if evaluation.blocked_reason is not None
                else "skipped"
            )
        )
        for evaluation in evaluations
    ]
    board = "board [" + "; ".join(parts) + "]" if parts else "board is empty"
    if selected_symbol is not None:
        return f"{board}; selected {selected_symbol}"
    return board


def evaluate_pool_entries(
    engine: PolicyEngine,
    base_state: PolicyState,
    options: Sequence[PoolBoardOption],
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
) -> tuple[PoolEntryEvaluation, ...]:
    """Evaluate the complete entry gate chain for every board option.

    Each pool is judged exactly as a flat pinned-symbol decision would judge
    it: the threaded session facts (day, day-start equity, daily loss halt)
    carry over, the per-pool re-entry cooldown applies only to its own pool,
    and every ordered gate - condition flats, reference freshness, the
    emissions floor, the equity and depth caps, and the gas sense-check -
    must pass before the pool qualifies.

    Args:
        engine: The locked per-pool policy engine.
        base_state: The threaded engine state; its position and held
            inventory are stripped because qualification is a flat-posture
            question.
        options: The enumerated board options in deterministic order.
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.

    Returns:
        One evaluation per option, in the given board order.
    """
    flat_basis = base_state.model_copy(update={"position": None, "held_inventory": None})
    evaluations: list[PoolEntryEvaluation] = []
    for option in options:
        # The cooldown applies per pool: an exit from one pool never blocks
        # another pool's entry.
        state = flat_basis.model_copy(
            update={"reentry_blocked_until": reentry_blocked_until_by_symbol.get(option.symbol)}
        )
        outcome = engine.decide(state, option.observation)
        decision = outcome.decision
        qualified = decision.action is PolicyActionKind.ENTER
        evaluations.append(
            PoolEntryEvaluation(
                symbol=option.symbol,
                pool_address=option.pool_address,
                observation=option.observation,
                emissions_apr=option.observation.emissions_apr,
                qualifies=qualified,
                blocked_reason=None if qualified else decision.reason,
                blocked_diagnostic="" if qualified else decision.diagnostics[0],
                entry_outcome=outcome if qualified else None,
            )
        )
    return tuple(evaluations)


def _rank_better(candidate: PoolEntryEvaluation, incumbent: PoolEntryEvaluation) -> bool:
    """Rank one qualifying evaluation against the incumbent best.

    The ranking is qualifying emissions APR descending with ties broken by
    the lexicographically smallest symbol, so selection is deterministic.

    Args:
        candidate: The qualifying evaluation being considered.
        incumbent: The current best qualifying evaluation.

    Returns:
        True when the candidate outranks the incumbent.
    """
    if candidate.emissions_apr != incumbent.emissions_apr:
        return candidate.emissions_apr > incumbent.emissions_apr
    return candidate.symbol < incumbent.symbol


def select_best_entry(evaluations: Sequence[PoolEntryEvaluation]) -> PoolEntryEvaluation | None:
    """Select the best-qualifying pool by qualifying emissions APR.

    Ties break deterministically on the lexicographically smallest symbol so
    selection is reproducible run over run.

    Args:
        evaluations: The board's complete entry-gate evaluations.

    Returns:
        The best qualifying evaluation, or None when no pool qualifies.
    """
    best: PoolEntryEvaluation | None = None
    for evaluation in evaluations:
        if not evaluation.qualifies:
            continue
        if best is None or _rank_better(evaluation, best):
            best = evaluation
    return best


def closest_call_evaluation(
    evaluations: Sequence[PoolEntryEvaluation],
) -> PoolEntryEvaluation | None:
    """Name the closest-call pool when nothing on the board qualifies.

    The ranking is emissions APR descending with the same lexicographic
    tie-break, ignoring qualification: the report names the most attractive
    pool whose blocking gate led the hold.

    Args:
        evaluations: The board's complete entry-gate evaluations.

    Returns:
        The closest-call evaluation, or None when the board is empty.
    """
    closest: PoolEntryEvaluation | None = None
    for evaluation in evaluations:
        if closest is None or _rank_better(evaluation, closest):
            closest = evaluation
    return closest


def switch_gas_economics(
    engine: PolicyEngine,
    entry_size_usd: Decimal,
    candidate_observation: PolicyObservation,
) -> tuple[bool, int, Decimal | None, tuple[str, ...]]:
    """Check the exit-plus-entry gas economics for one candidate switch.

    The combined cost of the held pool's exit batch and the new pool's entry
    batch - each carrying its own Safe proxy overhead, because they are two
    delivered batches - must stay within the locked cost-share bound of the
    new position's expected daily gross yield, the same bound the entry gas
    gate applies to the entry alone.

    Args:
        engine: The locked per-pool policy engine supplying the gas model.
        entry_size_usd: The candidate pool's qualified entry size.
        candidate_observation: The candidate pool's observation carrying the
            gas price reading and yield inputs.

    Returns:
        Whether the economics pass, the combined gas units, the combined
        cost (None without a gas reading), and the evidence lines.
    """
    parameters = engine.parameters
    gas_price = candidate_observation.gas_price_gwei
    exit_units = parameters.exit_batch_gas_units + parameters.safe_overhead_gas_per_batch
    enter_units = parameters.enter_batch_gas_units + parameters.safe_overhead_gas_per_batch
    combined_units = exit_units + enter_units
    if gas_price is None:
        # Fail closed exactly like the entry gas gate: an unreadable gas
        # price never authorizes a voluntary two-batch switch.
        return (
            False,
            combined_units,
            None,
            ("Base gas price reading is unavailable; deferring the switch fail-closed.",),
        )
    combined_cost_usd = (
        Decimal(combined_units) * gas_price / GWEI_PER_ETH * parameters.eth_price_assumption_usd
    )
    expected_daily_gross_yield = (
        entry_size_usd
        * (candidate_observation.emissions_apr + candidate_observation.fee_apr)
        / DAYS_PER_YEAR
    )
    bound_usd = parameters.gas_cost_max_gross_yield_fraction * expected_daily_gross_yield
    diagnostics = (
        f"Exit-plus-entry gas {combined_units} units costs {combined_cost_usd} USDC against "
        f"the {bound_usd} USDC cost-share bound of the new position's expected daily gross "
        f"yield {expected_daily_gross_yield} USDC.",
    )
    return combined_cost_usd <= bound_usd, combined_units, combined_cost_usd, diagnostics


def evaluate_switch(
    engine: PolicyEngine,
    held_symbol: str,
    held_emissions_apr: Decimal,
    evaluations: Sequence[PoolEntryEvaluation],
    switch_margin_fraction: Decimal,
) -> tuple[SwitchDirective | None, str]:
    """Evaluate the anti-churn switch discipline against the held pool.

    A switch fires only when the best other qualifying pool's emissions APR
    exceeds the held pool's by strictly more than the configured relative
    margin AND the exit-plus-entry gas economics pass. The best candidate is
    chosen with the same deterministic ranking as a flat selection.

    Args:
        engine: The locked per-pool policy engine supplying the gas model.
        held_symbol: The symbol of the funded position being held.
        held_emissions_apr: The held pool's current qualifying emissions APR.
        evaluations: The board's complete entry-gate evaluations.
        switch_margin_fraction: The relative APR margin, as a fraction.

    Returns:
        The qualified switch directive (or None) and one evidence note
        explaining the outcome either way.

    Raises:
        ValueError: If the switch margin is negative.
    """
    if switch_margin_fraction < 0:
        raise ValueError("switch_margin_fraction must be non-negative")
    others = tuple(evaluation for evaluation in evaluations if evaluation.symbol != held_symbol)
    best = select_best_entry(others)
    if best is None or best.entry_outcome is None:
        return None, "no other pool qualified for entry, so no switch was considered"
    threshold = held_emissions_apr * (Decimal(1) + switch_margin_fraction)
    margin_note = (
        f"best other qualifying APR {best.emissions_apr} on {best.symbol} against the "
        f"held {held_emissions_apr} on {held_symbol} plus the {switch_margin_fraction} "
        f"margin (threshold {threshold})"
    )
    if best.emissions_apr <= threshold:
        return None, f"switch margin not met: {margin_note}"
    entry_size = best.entry_outcome.decision.size_usd
    if entry_size is None or entry_size <= 0:
        return None, f"the {best.symbol} entry carried no positive size, so no switch fired"
    economics_pass, combined_units, combined_cost, economics_diagnostics = switch_gas_economics(
        engine, entry_size, best.observation
    )
    if not economics_pass:
        return None, f"switch gas economics refused: {margin_note}; {economics_diagnostics[0]}"
    return (
        SwitchDirective(
            from_symbol=held_symbol,
            to_symbol=best.symbol,
            to_pool_address=best.pool_address,
            entry_outcome=best.entry_outcome,
            margin_fraction=switch_margin_fraction,
            held_emissions_apr=held_emissions_apr,
            candidate_emissions_apr=best.emissions_apr,
            combined_gas_units=combined_units,
            combined_gas_cost_usd=combined_cost,
            diagnostics=economics_diagnostics,
        ),
        f"switching {held_symbol} -> {best.symbol}: {margin_note}",
    )


def _composed_switch_decision(directive: SwitchDirective) -> PolicyDecision:
    """Compose the POOL_SWITCH decision from a qualified directive.

    Args:
        directive: The qualified switch with its winning entry outcome.

    Returns:
        The immutable decision the cycle's action mapping executes.
    """
    entry_decision = directive.entry_outcome.decision
    return PolicyDecision(
        action=PolicyActionKind.POOL_SWITCH,
        reason=PolicyReason.POOL_SWITCH_TRIGGERED,
        diagnostics=(
            f"Switching {directive.from_symbol} -> {directive.to_symbol}: the candidate's "
            f"qualifying emissions APR {directive.candidate_emissions_apr} exceeds the held "
            f"{directive.held_emissions_apr} by more than the {directive.margin_fraction} "
            "relative switch margin.",
            *directive.diagnostics,
            *entry_decision.diagnostics,
        ),
        price_range=entry_decision.price_range,
        size_usd=entry_decision.size_usd,
        swap_plan=entry_decision.swap_plan,
        estimated_gas_units=directive.combined_gas_units,
        estimated_gas_cost_usd=directive.combined_gas_cost_usd,
        width_solution=entry_decision.width_solution,
    )


def _option_for_held_position(
    options: Sequence[PoolBoardOption], state: PolicyState
) -> PoolBoardOption | None:
    """Find the board option matching the tracked position's pool.

    Args:
        options: The enumerated board options.
        state: The threaded engine state carrying the held position.

    Returns:
        The matching option, or None when the held pool is not on the board.
    """
    position = state.position
    if position is None:
        return None
    for option in options:
        if option.pool_address == position.pool_address:
            return option
    return next(
        (option for option in options if option.token_address == position.token_address),
        None,
    )


def _option_for_held_inventory(
    options: Sequence[PoolBoardOption], state: PolicyState
) -> PoolBoardOption | None:
    """Find the board option matching the held inventory's pool.

    Args:
        options: The enumerated board options.
        state: The threaded engine state carrying the held inventory.

    Returns:
        The matching option, or None when the held pool is not on the board.
    """
    inventory = state.held_inventory
    if inventory is None:
        return None
    for option in options:
        if option.pool_address == inventory.pool_address:
            return option
    return next(
        (option for option in options if option.token_address == inventory.token_address),
        None,
    )


def _no_qualifying_hold(
    evaluations: Sequence[PoolEntryEvaluation], state: PolicyState
) -> PolicyOutcome:
    """Compose the flat hold verdict when no pool on the board qualifies.

    Args:
        evaluations: The board's complete entry-gate evaluations.
        state: The threaded engine state whose session facts carry forward.

    Returns:
        A hold outcome whose diagnostics name every pool's blocking reason.
    """
    if evaluations:
        reasons = "; ".join(
            f"{evaluation.symbol} "
            f"{evaluation.blocked_reason.value if evaluation.blocked_reason else 'blocked'}"
            for evaluation in evaluations
        )
        diagnostics = (f"No pool on the board qualified: {reasons}.",)
    else:
        diagnostics = ("No verified pools were enumerated on the board.",)
    return PolicyOutcome(
        decision=PolicyDecision(
            action=PolicyActionKind.HOLD,
            reason=PolicyReason.NO_QUALIFYING_POOL,
            diagnostics=diagnostics,
        ),
        next_state=state.model_copy(update={"position": None, "held_inventory": None}),
    )


def select_board(
    engine: PolicyEngine,
    state: PolicyState,
    options: Sequence[PoolBoardOption],
    reentry_blocked_until_by_symbol: Mapping[str, datetime],
    switch_margin_fraction: Decimal = DEFAULT_SWITCH_MARGIN_FRACTION,
) -> BoardSelection:
    """Select the board's verdict for one cycle over every enumerated pool.

    Precedence mirrors the engine's own: held inventory resolves before
    anything else, then the held position's own safety exits and maintenance,
    then the cross-pool switch discipline, then a fresh entry while flat.
    The board never enters while a position or unsold inventory exists.

    Args:
        engine: The locked per-pool policy engine.
        state: The threaded engine state, position and inventory included.
        options: The enumerated board options in deterministic order.
        reentry_blocked_until_by_symbol: The per-pool re-entry cooldowns.
        switch_margin_fraction: The relative APR switch margin, as a fraction.

    Returns:
        The complete selection with its board evidence.

    Raises:
        ValueError: If a held position or inventory names a pool that is not
            on the board, or the switch margin is negative.
    """
    evaluations = evaluate_pool_entries(engine, state, options, reentry_blocked_until_by_symbol)

    if state.held_inventory is not None:
        # Unsold stock resolves before any new exposure; the engine never
        # holds inventory and a position at once, and neither does the board.
        option = _option_for_held_inventory(options, state)
        if option is None:
            raise ValueError(
                "the held inventory's pool "
                f"{state.held_inventory.pool_address} is not on the enumerated board"
            )
        outcome = engine.decide(state, option.observation)
        return BoardSelection(
            outcome=outcome,
            evaluations=evaluations,
            selected_symbol=option.symbol,
            summary=(
                f"held inventory on {option.symbol} resolves before any board entry; "
                + _board_summary(evaluations, None)
            ),
        )

    if state.position is not None:
        option = _option_for_held_position(options, state)
        if option is None:
            raise ValueError(
                f"the tracked position's pool {state.position.pool_address} "
                "is not on the enumerated board"
            )
        held_outcome = engine.decide(state, option.observation)
        held_action = held_outcome.decision.action
        if held_action is not PolicyActionKind.HOLD:
            # Safety exits and maintenance on the held pool always win over
            # any switch consideration.
            return BoardSelection(
                outcome=held_outcome,
                evaluations=evaluations,
                selected_symbol=option.symbol,
                summary=(
                    f"held {option.symbol} verdict {held_action.value} takes precedence; "
                    + _board_summary(evaluations, option.symbol)
                ),
            )
        directive, note = evaluate_switch(
            engine,
            option.symbol,
            option.observation.emissions_apr,
            evaluations,
            switch_margin_fraction,
        )
        if directive is None:
            return BoardSelection(
                outcome=held_outcome,
                evaluations=evaluations,
                selected_symbol=option.symbol,
                summary=f"holding {option.symbol}: {note}; " + _board_summary(evaluations, None),
            )
        switch_outcome = PolicyOutcome(
            decision=_composed_switch_decision(directive),
            # The entry's own successor state already carries the new
            # position with the session facts; a voluntary switch arms no
            # re-entry cooldown, exactly like the engine's event exit.
            next_state=directive.entry_outcome.next_state.model_copy(
                update={"reentry_blocked_until": None}
            ),
        )
        return BoardSelection(
            outcome=switch_outcome,
            evaluations=evaluations,
            selected_symbol=directive.to_symbol,
            switch=directive,
            summary=note + "; " + _board_summary(evaluations, directive.to_symbol),
        )

    best = select_best_entry(evaluations)
    if best is not None and best.entry_outcome is not None:
        return BoardSelection(
            outcome=best.entry_outcome,
            evaluations=evaluations,
            selected_symbol=best.symbol,
            summary=_board_summary(evaluations, best.symbol),
        )
    return BoardSelection(
        outcome=_no_qualifying_hold(evaluations, state),
        evaluations=evaluations,
        selected_symbol=None,
        summary=_board_summary(evaluations, None),
    )
