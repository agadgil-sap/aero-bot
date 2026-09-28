"""Behavior tests for the portfolio allocator."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aero_bot.allocator import (
    DEFAULT_MIN_POSITION_FLOOR_USDC,
    HARD_MAX_CONCURRENT_POSITIONS,
    PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC,
    DeferredReallocation,
    HeldPositionFact,
    PortfolioAllocation,
    PortfolioExclusionReason,
    PortfolioParameters,
    PortfolioRebalancePlan,
    PortfolioStepKind,
    allocate_portfolio,
    band_candidates,
    plan_portfolio_rebalance,
    rank_qualifying_pools,
    weighted_apr_of,
)
from aero_bot.domain import normalize_evm_address
from aero_bot.policy import (
    PolicyActionKind,
    PolicyDecision,
    PolicyEngine,
    PolicyObservation,
    PolicyOutcome,
    PolicyReason,
    PolicyState,
)
from aero_bot.ranging import RangingEvidence
from aero_bot.selector import PoolBoardOption, PoolEntryEvaluation, evaluate_pool_entries

# Fixture identities: distinct pools and tokens per board symbol.
AAA_TOKEN = normalize_evm_address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
BBB_TOKEN = normalize_evm_address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
CCC_TOKEN = normalize_evm_address("0xcccccccccccccccccccccccccccccccccccccccc")
AAA_POOL = normalize_evm_address("0x1111111111111111111111111111111111111111")
BBB_POOL = normalize_evm_address("0x2222222222222222222222222222222222222222")
CCC_POOL = normalize_evm_address("0x3333333333333333333333333333333333333333")
# A Saturday noon UTC session, clear of every session window.
BASE_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
# The passing entry shape mirrored from the selector tests: emissions
# over the floor, deep pool, fresh reference, cheap gas.
RANGING = RangingEvidence.model_validate(
    {
        "gauge_liquidity_raw": 80_000_000_000_000,
        "staked_tvl_usd": Decimal("100000"),
        "active_liquidity_raw": 80_000_000_000_000,
        "fee_window_seconds": 86_400,
        "fee_window_notional_usd": Decimal("1000000"),
        "pool_fee_ppm": 500,
        "realized_daily_volatility": Decimal("0.005"),
        "stock_decimals": 6,
        "quote_decimals": 6,
    }
)


def board_option(
    symbol: str, pool_address: str, token_address: str, **overrides: object
) -> PoolBoardOption:
    """Build one board option over a passing entry observation.

    Args:
        symbol: The registry-matched stock symbol.
        pool_address: The verified pool contract.
        token_address: The B20 stock token.
        **overrides: Observation fields changed for one behavior test.

    Returns:
        A validated immutable board option.
    """
    values: dict[str, object] = {
        "observed_at": BASE_TIME,
        "pool_address": pool_address,
        "token_address": token_address,
        "amm_price_usdc": Decimal("200"),
        "emissions_apr": Decimal("2.0"),
        "fee_apr": Decimal("0.5"),
        "pool_depth_usd": Decimal("50000"),
        "equity_usd": Decimal("1000"),
        "reference_price_usdc": Decimal("200"),
        "reference_age_seconds": 10,
        "gas_price_gwei": Decimal("0.002"),
        "ranging": RANGING,
    }
    values.update(overrides)
    return PoolBoardOption(
        symbol=symbol,
        pool_address=pool_address,
        token_address=token_address,
        observation=PolicyObservation.model_validate(values),
    )


def qualified_board() -> tuple[PoolBoardOption, ...]:
    """Build the three-pool scripted universe: BBBc best, CCCc sub-band."""
    return (
        board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
        board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("3.0")),
        board_option("CCCc", CCC_POOL, CCC_TOKEN, emissions_apr=Decimal("1.2")),
    )


def board_evaluations(
    options: tuple[PoolBoardOption, ...] | None = None,
    cooldowns: dict[str, datetime] | None = None,
) -> tuple[PoolEntryEvaluation, ...]:
    """Run the selector's entry-gate chain over the scripted board."""
    return evaluate_pool_entries(
        PolicyEngine(), PolicyState(), options or qualified_board(), cooldowns or {}
    )


def held_fact(
    symbol: str = "AAAc",
    *,
    token_id: int = 77,
    committed: Decimal = Decimal("100"),
    marked: Decimal | None = Decimal("100"),
    entered_hours_ago: float = 2.0,
    apr: Decimal = Decimal("1.2"),
) -> HeldPositionFact:
    """Build one held position fact two hours into its hold."""
    return HeldPositionFact(
        symbol=symbol,
        token_id=token_id,
        committed_usd=committed,
        marked_usd=marked,
        entered_at=BASE_TIME - timedelta(hours=entered_hours_ago),
        emissions_apr=apr,
    )


def hold_outcome() -> PolicyOutcome:
    """Build one in-range hold outcome for a held fold."""
    return PolicyOutcome(
        decision=PolicyDecision(
            action=PolicyActionKind.HOLD,
            reason=PolicyReason.OPEN_IN_RANGE,
            diagnostics=("fixture: the position holds in range",),
        ),
        next_state=PolicyState(),
    )


def action_outcome(action: PolicyActionKind, reason: PolicyReason) -> PolicyOutcome:
    """Build one typed action outcome for a held fold."""
    return PolicyOutcome(
        decision=PolicyDecision(
            action=action,
            reason=reason,
            diagnostics=(f"fixture: the engine ordered {action.value}",),
        ),
        next_state=PolicyState(),
    )


class TestParameterCeilings:
    """The locked bounds reject any sealed configuration above them."""

    def test_count_cap_hard_ceiling_is_ten(self) -> None:
        """No configuration may exceed the captain's ten positions."""
        with pytest.raises(ValueError, match="hard.*ceiling of 10"):
            PortfolioParameters(max_concurrent_positions=11)
        assert HARD_MAX_CONCURRENT_POSITIONS == 10
        PortfolioParameters(max_concurrent_positions=10)

    def test_min_size_stays_inside_the_total_cap(self) -> None:
        """A minimum above the total cap can never fund anything."""
        with pytest.raises(ValueError, match="exceeds the hard"):
            PortfolioParameters(min_position_usdc=Decimal("1500"))

    def test_the_floor_stays_positive_and_under_the_configured_minimum(self) -> None:
        """A floor above the configured minimum would raise the bound."""
        with pytest.raises(ValueError, match="greater than 0"):
            PortfolioParameters(min_position_floor_usdc=Decimal("0"))
        with pytest.raises(ValueError, match="never raises the minimum"):
            PortfolioParameters(min_position_floor_usdc=Decimal("90"))
        # The floor may equal the minimum: the coherence rule degenerates
        # to the old behavior, which is coherent at every equity.
        PortfolioParameters(min_position_floor_usdc=Decimal("80"))
        assert PortfolioParameters().min_position_floor_usdc == DEFAULT_MIN_POSITION_FLOOR_USDC
        assert Decimal("30") == DEFAULT_MIN_POSITION_FLOOR_USDC  # the shipped default

    def test_fractions_stay_in_their_intervals(self) -> None:
        """The concentration and band fractions cap at one."""
        with pytest.raises(ValueError, match="concentration"):
            PortfolioParameters(concentration_cap_fraction=Decimal("1.5"))
        with pytest.raises(ValueError, match="tier_band"):
            PortfolioParameters(tier_band_fraction=Decimal("1.5"))
        with pytest.raises(ValueError, match="non-negative"):
            PortfolioParameters(switch_margin_fraction=Decimal("-0.1"))

    def test_total_cap_never_exceeds_the_pilot_ceiling(self) -> None:
        """The portfolio cap mirrors the executor's 1000 USDC ceiling."""
        with pytest.raises(ValueError, match="hard ceiling"):
            PortfolioParameters(total_exposure_cap_usdc=Decimal("2000"))
        assert Decimal("1000") == PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC


class TestParameterCoherence:
    """The gnhf 36 rule: the minimum and the concentration cap cohere.

    The two bounds were mutually unsatisfiable below 80 / 0.35 = 228.57
    USDC equity: the clamp forced every tranche under the minimum, so a
    book at the captain's trial scale could never deploy. The effective
    minimum is max(floor, min(configured minimum, concentration clamp))
    with a hard gas-efficiency floor (default 30 USDC). These tests pin
    the three regimes, the exact boundary crossings, and the allocation
    behavior at each boundary size.
    """

    # The defaults: 80 minimum, 0.35 concentration fraction, 30 floor.
    PARAMETERS = PortfolioParameters()
    # The equity where the clamp crosses the configured minimum:
    # 80 / 0.35 = 228.571428... USDC.
    MINIMUM_CROSSING_EQUITY = Decimal("1600") / Decimal("7")
    # The equity where the clamp crosses the hard floor:
    # 30 / 0.35 = 85.714285... USDC.
    FLOOR_CROSSING_EQUITY = Decimal("600") / Decimal("7")

    def test_the_three_regimes_of_the_effective_minimum(self) -> None:
        """The configured minimum, the clamp, and the floor each govern."""
        parameters = self.PARAMETERS
        # A rich book: the clamp (350) sits above 80, so 80 governs.
        assert parameters.effective_minimum_position_usdc(Decimal("1000")) == Decimal("80")
        assert "the configured minimum governs" in parameters.describe_effective_minimum(
            Decimal("1000")
        )
        # The captain's trial scale: the clamp (36.75) governs over 30.
        assert parameters.effective_minimum_position_usdc(Decimal("105")) == Decimal("36.75")
        assert "the concentration clamp governs" in parameters.describe_effective_minimum(
            Decimal("105")
        )
        # A tiny book: the clamp (29.75) sits below the 30 floor, so the
        # floor holds the minimum - and the allocation stays cash at it.
        assert parameters.effective_minimum_position_usdc(Decimal("85")) == Decimal("30")
        assert "the hard floor governs" in parameters.describe_effective_minimum(Decimal("85"))
        # The derivation names every bound and the arithmetic.
        assert parameters.describe_effective_minimum(Decimal("105")) == (
            "effective minimum min(80 configured, 36.75 concentration clamp on "
            "equity 105) floored at 30 = 36.75; the concentration clamp governs "
            "under the configured minimum"
        )

    def test_the_exact_boundary_crossings(self) -> None:
        """The clamp crosses the minimum at 228.57 and the floor at 85.71."""
        parameters = self.PARAMETERS
        at_crossing = parameters.effective_minimum_position_usdc(self.MINIMUM_CROSSING_EQUITY)
        assert at_crossing == Decimal("80")
        below_crossing = parameters.effective_minimum_position_usdc(
            self.MINIMUM_CROSSING_EQUITY - Decimal("0.01")
        )
        assert below_crossing < Decimal("80")  # the clamp governs just below
        assert parameters.effective_minimum_position_usdc(self.FLOOR_CROSSING_EQUITY) == Decimal(
            "30"
        )
        just_under = parameters.effective_minimum_position_usdc(
            self.FLOOR_CROSSING_EQUITY - Decimal("0.01")
        )
        assert just_under == Decimal("30")  # the floor holds the boundary
        assert just_under > Decimal("30") - Decimal("0.01")

    def test_the_boundary_sizes_fund_at_the_binding_bound(self) -> None:
        """A single-pool book funds at the clamp just under the crossing.

        At equity 228 (clamp 79.80, under the 80 minimum) the tranche
        funds at 79.80 - the exact size the old bound pair refused; at
        equity 230 (clamp 80.50, above the minimum) the configured 80 is
        the effective minimum and the tranche funds at the 80.50 target.
        """
        board = (board_option("BBBc", BBB_POOL, BBB_TOKEN),)
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, PolicyState(), board, {})
        below = allocate_portfolio(
            engine,
            PolicyState(),
            evaluations,
            {},
            held=(),
            cash_usdc=Decimal("228"),
            equity_usdc=Decimal("228"),
        )
        assert [tranche.symbol for tranche in below.tranches] == ["BBBc"]
        assert below.tranches[0].budget_usd == Decimal("79.80")
        assert self.PARAMETERS.effective_minimum_position_usdc(Decimal("228")) == Decimal("79.80")
        above = allocate_portfolio(
            engine,
            PolicyState(),
            evaluations,
            {},
            held=(),
            cash_usdc=Decimal("230"),
            equity_usdc=Decimal("230"),
        )
        assert [tranche.symbol for tranche in above.tranches] == ["BBBc"]
        assert above.tranches[0].budget_usd == Decimal("80.50")
        assert self.PARAMETERS.effective_minimum_position_usdc(Decimal("230")) == Decimal("80")

    def test_a_lower_floor_lets_a_tiny_book_deploy_at_its_clamp(self) -> None:
        """The floor is sealed-env configurable: floor 10 funds 29.75."""
        parameters = PortfolioParameters(min_position_floor_usdc=Decimal("10"))
        assert parameters.effective_minimum_position_usdc(Decimal("85")) == Decimal("29.75")
        board = (board_option("BBBc", BBB_POOL, BBB_TOKEN),)
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, PolicyState(), board, {})
        allocation = allocate_portfolio(
            engine,
            PolicyState(),
            evaluations,
            {},
            held=(),
            cash_usdc=Decimal("85"),
            equity_usdc=Decimal("85"),
            parameters=parameters,
        )
        assert [tranche.symbol for tranche in allocation.tranches] == ["BBBc"]
        assert allocation.tranches[0].budget_usd == Decimal("29.75")

    def test_sub_effective_minimum_freed_capital_names_the_derivation(self) -> None:
        """A reallocation below the effective minimum states it fully."""
        held = (
            held_fact("AAAc", apr=Decimal("1.2"), committed=Decimal("40"), marked=Decimal("40")),
        )
        plan = plan_portfolio_rebalance(
            PolicyEngine(),
            allocate_portfolio(
                PolicyEngine(),
                PolicyState(),
                board_evaluations(),
                {},
                held=held,
                cash_usdc=Decimal("0"),
                equity_usdc=Decimal("1000"),
            ),
            board_evaluations(),
            PolicyState(),
            {},
            held,
            {"AAAc": hold_outcome()},
            Decimal("0"),
            Decimal("1000"),
        )
        deferred = next(
            item
            for item in plan.deferred
            if item.reason is PortfolioExclusionReason.REALLOCATION_TOO_SMALL
        )
        assert "below the effective minimum 80 USDC" in deferred.detail
        assert "floored at 30 = 80" in deferred.detail
        assert "the configured minimum governs" in deferred.detail


class TestTierConstruction:
    """Tiered allocation across sparse and rich boards."""

    def test_rich_board_funds_weight_proportional_tiers(self) -> None:
        """The top APR earns the largest tranche; shares follow weights."""
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
        )
        symbols = [tranche.symbol for tranche in allocation.tranches]
        assert symbols == ["BBBc", "AAAc"]  # ranked, CCCc cut by the band
        top, second = allocation.tranches
        assert top.budget_usd == Decimal("240")  # 3.0/5.0 of 400
        assert second.budget_usd == Decimal("160")  # 2.0/5.0 of 400
        assert top.tier_rank == 1 and second.tier_rank == 2
        assert allocation.cash_residual_usdc == Decimal("0")
        assert allocation.projected_committed_usdc == Decimal("400")

    def test_sparse_board_funds_one_tranche_with_cash_residual(self) -> None:
        """One qualifying pool deploys alone and the rest stays cash."""
        options = (board_option("BBBc", BBB_POOL, BBB_TOKEN),)
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(options),
            {},
            held=(),
            cash_usdc=Decimal("500"),
            equity_usdc=Decimal("2000"),
        )
        assert len(allocation.tranches) == 1
        assert allocation.tranches[0].symbol == "BBBc"
        assert allocation.tranches[0].budget_usd == Decimal("500")
        assert allocation.cash_residual_usdc == Decimal("0")

    def test_below_band_pool_is_excluded_with_typed_reason(self) -> None:
        """A qualifying pool under half the top's APR earns no tranche."""
        options = (
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("4.0")),
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("CCCc", CCC_POOL, CCC_TOKEN, emissions_apr=Decimal("1.8")),
        )
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(options),
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
        )
        excluded = {item.symbol: item.reason for item in allocation.excluded}
        assert excluded["CCCc"] is PortfolioExclusionReason.BELOW_TIER_BAND
        assert "CCCc" not in [tranche.symbol for tranche in allocation.tranches]
        details = {item.symbol: item.detail for item in allocation.excluded}
        assert "tier band floor 2.00" in details["CCCc"]

    def test_concentration_cap_clamps_the_top_tier(self) -> None:
        """No tranche exceeds thirty-five percent of book equity."""
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("400"),
        )
        top, second = allocation.tranches
        assert top.budget_usd == Decimal("140")  # 0.35 * 400
        assert second.budget_usd == Decimal("140")  # also clamped
        assert allocation.cash_residual_usdc == Decimal("120")
        clamped = {item.symbol for item in allocation.excluded}
        assert clamped == {"BBBc", "AAAc"}
        reasons = {item.symbol: item.reason for item in allocation.excluded}
        assert reasons["BBBc"] is PortfolioExclusionReason.CONCENTRATION_CAP_CLAMPED

    def test_ties_break_on_the_symbol(self) -> None:
        """Equal weighted APRs rank lexicographically."""
        options = (
            board_option("BBBc", BBB_POOL, BBB_TOKEN),
            board_option("AAAc", AAA_POOL, AAA_TOKEN),
        )
        ranked = rank_qualifying_pools(board_evaluations(options))
        assert [evaluation.symbol for evaluation, _ in ranked] == ["AAAc", "BBBc"]


class TestCountAsOutput:
    """The deployed count emerges under the bounds."""

    def test_held_slots_consume_the_count_bound(self) -> None:
        """Held positions come off the ceiling before new tiers fund."""
        parameters = PortfolioParameters(max_concurrent_positions=2)
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(held_fact("DDDc", token_id=1, apr=Decimal("1.6")),),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
            parameters=parameters,
        )
        assert [tranche.symbol for tranche in allocation.tranches] == ["BBBc"]
        excluded = {item.symbol: item.reason for item in allocation.excluded}
        assert excluded["AAAc"] is PortfolioExclusionReason.MAX_POSITIONS_REACHED

    def test_full_book_deploys_nothing(self) -> None:
        """A book at the count bound keeps every candidate as cash."""
        parameters = PortfolioParameters(max_concurrent_positions=1)
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(held_fact("DDDc", token_id=1, apr=Decimal("1.6")),),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
            parameters=parameters,
        )
        assert allocation.tranches == ()
        assert allocation.cash_residual_usdc == Decimal("400")
        assert "count bound" in allocation.summary

    def test_below_minimum_target_stays_cash(self) -> None:
        """A tier target under eighty USDC never deploys."""
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("150"),
            equity_usdc=Decimal("1500"),
        )
        # Weights 3/5 and 2/5 of 150: 90 funds the top; the second share
        # floors to 80 but only 60 remains, so it stays cash.
        assert [tranche.symbol for tranche in allocation.tranches] == ["BBBc"]
        assert allocation.tranches[0].budget_usd == Decimal("90")
        excluded = {item.symbol: item.reason for item in allocation.excluded}
        assert excluded["AAAc"] is PortfolioExclusionReason.INSUFFICIENT_CASH
        assert allocation.cash_residual_usdc == Decimal("60")

    def test_cash_never_deploys_past_the_budget(self) -> None:
        """A later tier cannot spend cash an earlier tier consumed."""
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("250"),
            equity_usdc=Decimal("1500"),
        )
        # 3/5 of 250 = 150 funds; 2/5 = 100 also fits, so both deploy.
        assert len(allocation.tranches) == 2
        tight = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("180"),
            equity_usdc=Decimal("1500"),
        )
        # 3/5 of 180 = 108 funds; the second share floors to 80 but only
        # 72 remains, so it stays cash rather than deploying a stub.
        assert [tranche.symbol for tranche in tight.tranches] == ["BBBc"]
        assert tight.cash_residual_usdc == Decimal("72")

    def test_total_cap_headroom_bounds_deployment(self) -> None:
        """Committed capital comes off the deployable budget first."""
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(held_fact("DDDc", token_id=1, committed=Decimal("900"), marked=Decimal("900")),),
            cash_usdc=Decimal("500"),
            equity_usdc=Decimal("1400"),
        )
        # Headroom is 100 USDC; the top tier takes its minimum-sized
        # share of it and the second share no longer fits.
        assert [tranche.symbol for tranche in allocation.tranches] == ["BBBc"]
        assert allocation.tranches[0].budget_usd == Decimal("80")
        assert allocation.deployable_usdc == Decimal("100")
        assert allocation.projected_committed_usdc == Decimal("980")

    def test_halted_book_funds_no_entries(self) -> None:
        """The daily loss halt blocks every tranche, engine-judged."""
        halted = PolicyState(day=BASE_TIME.date(), halted_day=BASE_TIME.date())
        allocation = allocate_portfolio(
            PolicyEngine(),
            halted,
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
        )
        assert allocation.tranches == ()
        reasons = {item.symbol: item.reason for item in allocation.excluded}
        assert reasons["BBBc"] is PortfolioExclusionReason.ENTRY_GATE_REFUSED
        details = {item.symbol: item.detail for item in allocation.excluded}
        assert "daily_loss_halt_active" in details["BBBc"]

    def test_pending_inventory_blocks_entries(self) -> None:
        """Unsold stock keeps the whole board in cash."""
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
            inventory_pending=True,
        )
        assert allocation.tranches == ()
        assert allocation.cash_residual_usdc == Decimal("400")
        reasons = [item.reason for item in allocation.excluded]
        assert PortfolioExclusionReason.INVENTORY_UNWIND_PENDING in reasons


class TestDisciplineWeighting:
    """Measured in-range discipline weights the tier ranking."""

    def test_weight_reorders_the_tiers(self) -> None:
        """A disciplined second-best pool outranks the raw APR leader."""
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(),
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
            discipline_by_symbol={"AAAc": Decimal("1.0"), "BBBc": Decimal("0.5")},
        )
        # Weighted: AAAc 2.0, BBBc 1.5, CCCc 1.2 - the band reshapes.
        assert [tranche.symbol for tranche in allocation.tranches] == ["AAAc", "BBBc"]

    def test_weight_out_of_bounds_refuses(self) -> None:
        """A discipline weight outside [0, 1] never ranks anything."""
        with pytest.raises(ValueError, match="must sit in \\[0, 1\\]"):
            rank_qualifying_pools(board_evaluations(), {"AAAc": Decimal("1.5")})
        with pytest.raises(ValueError, match="must sit in \\[0, 1\\]"):
            weighted_apr_of(Decimal("2"), {"AAAc": Decimal("-0.1")}, "AAAc")

    def test_unmeasured_names_rank_on_raw_apr(self) -> None:
        """Names without a measurement keep the selector ranking."""
        assert weighted_apr_of(Decimal("2.0"), {}, "AAAc") == Decimal("2.0")

    def test_band_candidates_cut_held_symbols(self) -> None:
        """Held symbols never compete for fresh tiers."""
        candidates = band_candidates(board_evaluations(), {"BBBc"}, PortfolioParameters())
        assert [evaluation.symbol for evaluation, _ in candidates] == ["AAAc"]


class TestRebalanceTriggers:
    """Portfolio reallocation under the generalized switch margin."""

    def _plan(
        self,
        held: tuple[HeldPositionFact, ...],
        held_outcomes: dict[str, PolicyOutcome],
        *,
        cash: Decimal = Decimal("0"),
        equity: Decimal = Decimal("1000"),
        parameters: PortfolioParameters | None = None,
        evaluations: tuple[PoolEntryEvaluation, ...] | None = None,
        base_state: PolicyState | None = None,
        inventory_symbol: str | None = None,
        inventory_outcome: PolicyOutcome | None = None,
        now: datetime | None = BASE_TIME,
    ) -> "PortfolioRebalancePlan":
        """Allocate and plan over the scripted board in one pass."""
        engine = PolicyEngine()
        resolved_evaluations = evaluations if evaluations is not None else board_evaluations()
        resolved_state = base_state or PolicyState()
        allocation = allocate_portfolio(
            engine,
            resolved_state,
            resolved_evaluations,
            {},
            held=held,
            cash_usdc=cash,
            equity_usdc=equity,
            parameters=parameters,
        )
        return plan_portfolio_rebalance(
            engine,
            allocation,
            resolved_evaluations,
            resolved_state,
            {},
            held,
            held_outcomes,
            cash,
            equity,
            inventory_symbol=inventory_symbol,
            inventory_outcome=inventory_outcome,
            parameters=parameters,
            now=now,
        )

    def test_decayed_pool_reallocates_to_the_better_candidate(self) -> None:
        """A held APR decayed past the margin swaps into the candidate."""
        held = (held_fact("AAAc", apr=Decimal("1.2")),)
        plan = self._plan(held, {"AAAc": hold_outcome()})
        assert len(plan.steps) == 1
        step = plan.steps[0]
        assert step.kind is PortfolioStepKind.REALLOCATE
        assert step.symbol == "AAAc" and step.to_symbol == "BBBc"
        assert step.outcome.decision.action is PolicyActionKind.POOL_SWITCH
        assert step.outcome.decision.size_usd == Decimal("100")  # the freed scale
        assert plan.projected_committed_usdc == Decimal("100")
        assert plan.projected_position_count == 1

    def test_margin_not_met_keeps_the_held_pool(self) -> None:
        """A candidate inside the margin never churns a funded position."""
        held = (held_fact("AAAc", apr=Decimal("2.6")),)
        plan = self._plan(held, {"AAAc": hold_outcome()})
        assert plan.steps == ()
        assert plan.projected_position_count == 1

    def test_minimum_hold_window_defers_reallocation(self) -> None:
        """A fresh position runs its hour before any voluntary swap."""
        held = (held_fact("AAAc", apr=Decimal("1.2"), entered_hours_ago=0.2),)
        plan = self._plan(held, {"AAAc": hold_outcome()})
        assert plan.steps == ()
        assert plan.deferred[0].reason is PortfolioExclusionReason.REALLOCATION_MIN_HOLD_ACTIVE

    def test_sub_minimum_freed_capital_keeps_the_held_pool(self) -> None:
        """Freed capital under the minimum never churns into a stub."""
        held = (
            held_fact("AAAc", apr=Decimal("1.2"), committed=Decimal("40"), marked=Decimal("40")),
        )
        parameters = PortfolioParameters()
        plan = self._plan(held, {"AAAc": hold_outcome()}, parameters=parameters)
        assert plan.steps == ()
        assert plan.deferred[0].reason is PortfolioExclusionReason.REALLOCATION_TOO_SMALL

    def test_safety_exit_precedes_entries_and_reallocation(self) -> None:
        """Exits order first; entries fund from the freed capital."""
        held = (
            held_fact("AAAc", apr=Decimal("1.2")),
            held_fact("ZZZc", token_id=78, apr=Decimal("2.9")),
        )
        outcomes = {
            "AAAc": hold_outcome(),
            "ZZZc": action_outcome(PolicyActionKind.STOP_OUT, PolicyReason.DOWNSIDE_STOP_TRIGGERED),
        }
        plan = self._plan(held, outcomes, cash=Decimal("300"), equity=Decimal("1300"))
        kinds = [step.kind for step in plan.steps]
        # The stop-out frees ZZZc first; the reallocation swaps AAAc into
        # BBBc; the entry funds CCCc-adjacent leftovers from cash.
        assert kinds[0] is PortfolioStepKind.POSITION_ACTION
        assert plan.steps[0].symbol == "ZZZc"
        assert plan.steps[0].outcome.decision.action is PolicyActionKind.STOP_OUT
        assert any(step.kind is PortfolioStepKind.REALLOCATE for step in plan.steps)
        assert kinds[-1] is PortfolioStepKind.ENTER or all(
            step.kind is not PortfolioStepKind.ENTER for step in plan.steps
        )
        # The invariant: committed never projects above the total cap.
        assert plan.projected_committed_usdc <= PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC

    def test_recenter_step_plans_as_position_action(self) -> None:
        """A held fold's recenter rides the plan as its own step."""
        held = (held_fact("AAAc", apr=Decimal("3.0")),)
        outcomes = {
            "AAAc": action_outcome(PolicyActionKind.RECENTER, PolicyReason.RECENTER_WAIT_ELAPSED)
        }
        plan = self._plan(held, outcomes, cash=Decimal("300"))
        step = plan.steps[0]
        assert step.kind is PortfolioStepKind.POSITION_ACTION
        assert step.symbol == "AAAc" and step.token_id == 77
        assert step.outcome.decision.action is PolicyActionKind.RECENTER

    def test_inventory_resolution_orders_before_maintenance(self) -> None:
        """The held-inventory fold's action rides right after safety exits."""
        held = (held_fact("AAAc", apr=Decimal("3.0")),)
        sell = action_outcome(
            PolicyActionKind.SELL_INVENTORY, PolicyReason.INVENTORY_FLAT_WINDOW_SELL
        )
        plan = self._plan(
            held,
            {"AAAc": hold_outcome()},
            inventory_symbol="QQQc",
            inventory_outcome=sell,
        )
        assert plan.steps[0].kind is PortfolioStepKind.INVENTORY_ACTION
        assert plan.steps[0].symbol == "QQQc"

    def test_reallocation_claims_its_target(self) -> None:
        """No fresh entry double-funds a reallocation's target."""
        held = (held_fact("AAAc", apr=Decimal("1.2")),)
        plan = self._plan(
            held, {"AAAc": hold_outcome()}, cash=Decimal("400"), equity=Decimal("1200")
        )
        targets = {
            step.to_symbol for step in plan.steps if step.kind is PortfolioStepKind.REALLOCATE
        }
        entered = {step.symbol for step in plan.steps if step.kind is PortfolioStepKind.ENTER}
        assert targets and not (targets & entered)

    def test_full_book_still_rotates_decayed_names(self) -> None:
        """A book at the count bound swaps one-for-one, cap intact."""
        held = tuple(
            held_fact(symbol, token_id=index, apr=Decimal("1.2"), committed=Decimal("90"))
            for index, symbol in enumerate(("AAAc", "DDDc", "EEEc"), start=1)
        )
        parameters = PortfolioParameters(max_concurrent_positions=3)
        plan = self._plan(
            held,
            {fact.symbol: hold_outcome() for fact in held},
            parameters=parameters,
        )
        reallocations = [step for step in plan.steps if step.kind is PortfolioStepKind.REALLOCATE]
        assert len(reallocations) == 1
        assert plan.projected_position_count == 3
        assert plan.projected_committed_usdc <= PORTFOLIO_TOTAL_EXPOSURE_CAP_USDC

    def test_missing_fold_refuses(self) -> None:
        """A held position without its engine fold is a plan bug."""
        held = (held_fact("AAAc"),)
        with pytest.raises(ValueError, match="carries no engine fold"):
            self._plan(held, {})

    def test_over_cap_projection_refuses(self) -> None:
        """The projection guard refuses a plan above the total cap."""
        engine = PolicyEngine()
        evaluations = board_evaluations()
        tranche_allocation = allocate_portfolio(
            engine,
            PolicyState(),
            evaluations,
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
        )
        assert tranche_allocation.tranches
        # Forge an allocation whose tranches sum past a tightened cap;
        # the planner's invariant must refuse to sequence it.
        inflated = tranche_allocation.model_copy(
            update={
                "tranches": (
                    tranche_allocation.tranches[0].model_copy(
                        update={"budget_usd": Decimal("200")}
                    ),
                )
            }
        )
        with pytest.raises(ValueError, match="USDC total cap"):
            plan_portfolio_rebalance(
                engine,
                inflated,
                evaluations,
                PolicyState(),
                {},
                held=(),
                held_outcomes={},
                cash_usdc=Decimal("400"),
                equity_usdc=Decimal("1200"),
                parameters=PortfolioParameters(total_exposure_cap_usdc=Decimal("150")),
            )

    def test_deferred_reallocation_is_typed(self) -> None:
        """Every considered-and-refused swap carries its catalog reason."""
        held = (held_fact("AAAc", apr=Decimal("1.2"), entered_hours_ago=0.1),)
        plan = self._plan(held, {"AAAc": hold_outcome()})
        assert all(
            isinstance(item, DeferredReallocation)
            and item.reason in tuple(PortfolioExclusionReason)
            for item in plan.deferred
        )


class TestEntryGateTransparencyAndScaledLatches:
    """The gnhf 34 fixes: named refusing gates and unspoofed halt latches.

    The overnight production evidence: 113 clean unhalted cycles sat flat
    at 102.26 USDC while MSTRc (qualifying APR 69-81) was excluded every
    cycle with the opaque label ``entry_gate_refused``. The cause: the
    allocator's tranche re-derivation scaled the observation's equity to
    the tranche's sizing basis (46.15 for a 36.92 tranche) while the
    session state anchored the day-start equity at 102.57 - the scaled
    equity read as a 55 percent drawdown and the daily loss halt refused
    every fresh entry. These tests pin the fix with the exact production
    numbers.
    """

    # The live night's session facts, verbatim from the audit chain.
    LIVE_DAY_START = Decimal("102.5739724972593502724458080")
    LIVE_PEAK = Decimal("105.3181507845350552886065412")
    LIVE_CASH = Decimal("102.263507")
    LIVE_EQUITY = Decimal("105.4951978105427944343744845")
    LIVE_MSTRC_APR = Decimal("69.590101247023764134591907279181621479093244490011647158019")

    def live_session(self) -> PolicyState:
        """Build the live night's session state: same day, healthy anchors."""
        day = BASE_TIME.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
        return PolicyState(
            day=day,
            day_start_equity_usd=self.LIVE_DAY_START,
            peak_equity_usd=self.LIVE_PEAK,
            halted_day=None,
            position=None,
            held_inventory=None,
            reentry_blocked_until=None,
        )

    def live_board(self) -> tuple[PoolBoardOption, ...]:
        """Build the live night's board: MSTRc atop two out-of-band pools."""
        return (
            board_option(
                "MSTRc",
                "0x" + "1" * 40,
                "0x" + "2" * 40,
                emissions_apr=self.LIVE_MSTRC_APR,
                equity_usd=self.LIVE_EQUITY,
            ),
            board_option(
                "SNDKc",
                "0x" + "3" * 40,
                "0x" + "4" * 40,
                emissions_apr=Decimal("15.29"),
                equity_usd=self.LIVE_EQUITY,
            ),
            board_option(
                "METAc",
                "0x" + "5" * 40,
                "0x" + "6" * 40,
                emissions_apr=Decimal("10.59"),
                equity_usd=self.LIVE_EQUITY,
            ),
        )

    def test_the_live_flat_night_now_funds_at_the_coherent_minimum(self) -> None:
        """The gnhf 36 flip: the exact night that sat flat now deploys.

        With the day-start anchor at 102.57 and the tranche's scaled basis
        at 46.15, the old code latched ``daily_loss_halt_active`` on every
        tranche (gnhf 34 fixed that), and the book then sat flat on the
        honest bound conflict - the thirty-five percent concentration
        bound (36.92) below the eighty-USDC minimum (the state gnhf 35
        pinned). The coherence rule resolves it: the effective minimum
        at this equity IS the concentration clamp (36.92), so MSTRc
        funds at exactly the locked per-name bound and the residual
        stays cash.
        """
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, self.live_session(), self.live_board(), {})
        assert next(e for e in evaluations if e.symbol == "MSTRc").qualifies
        allocation = allocate_portfolio(
            engine,
            self.live_session(),
            evaluations,
            {},
            held=(),
            cash_usdc=self.LIVE_CASH,
            equity_usdc=self.LIVE_EQUITY,
        )
        assert [tranche.symbol for tranche in allocation.tranches] == ["MSTRc"]
        tranche = allocation.tranches[0]
        # The concentration bound equals the effective minimum here.
        assert tranche.budget_usd == Decimal("36.92331923368997805203106958")
        assert tranche.budget_usd >= PortfolioParameters().effective_minimum_position_usdc(
            self.LIVE_EQUITY
        )
        assert allocation.cash_residual_usdc == Decimal("65.34018776631002194796893042")
        # The clamp note still rides: the tier target was clamped, and
        # the funded size names the per-name bound it clamped to.
        clamped = next(
            item
            for item in allocation.excluded
            if item.reason is PortfolioExclusionReason.CONCENTRATION_CAP_CLAMPED
        )
        assert clamped.symbol == "MSTRc"
        assert "the tranche still funds" in clamped.detail

    def test_a_large_healthy_book_funds_despite_the_anchor_above_the_basis(self) -> None:
        """The exact old failure shape now funds: anchor 950, tranche 350.

        Before the fix the 350 tranche's scaled basis (437.50) read as a
        54 percent drawdown against the 950 anchor and the halt refused
        the entry; the concentration cap alone should bind here.
        """
        board = (
            board_option("MSTRc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("69.59")),
            board_option("SNDKc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("15.29")),
        )
        session = PolicyState(
            day=BASE_TIME.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date(),
            day_start_equity_usd=Decimal("950"),
            peak_equity_usd=Decimal("1000"),
        )
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, session, board, {})
        allocation = allocate_portfolio(
            engine,
            session,
            evaluations,
            {},
            held=(),
            cash_usdc=Decimal("1000"),
            equity_usdc=Decimal("1000"),
        )
        assert [tranche.symbol for tranche in allocation.tranches] == ["MSTRc"]
        assert allocation.tranches[0].budget_usd == Decimal("350")

    def test_a_real_gate_refusal_names_the_gate_and_the_forgone_income(self) -> None:
        """A tranche-scale gas deferral surfaces the engine gate by name."""
        board = (
            board_option(
                "MSTRc",
                AAA_POOL,
                AAA_TOKEN,
                emissions_apr=Decimal("1.6"),
                gas_price_gwei=Decimal("0.05"),
            ),
        )
        session = PolicyState(
            day=BASE_TIME.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date(),
            day_start_equity_usd=Decimal("1000"),
            peak_equity_usd=Decimal("1000"),
        )
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, session, board, {})
        allocation = allocate_portfolio(
            engine,
            session,
            evaluations,
            {},
            held=(),
            cash_usdc=Decimal("1000"),
            equity_usdc=Decimal("1000"),
        )
        refused = next(item for item in allocation.excluded if item.symbol == "MSTRc")
        assert refused.reason is PortfolioExclusionReason.ENTRY_GATE_REFUSED
        assert refused.gate == "gas_gate_deferred"
        assert refused.cause == "gas_gate_deferred"
        assert "Estimated batch cost" in refused.detail
        assert "income forgone about" in refused.detail
        assert refused.forgone_income_usdc_per_day is not None
        assert "MSTRc (entry_gate_refused: gas_gate_deferred)" in allocation.summary

    def test_the_gate_chain_trace_rides_the_allocation(self) -> None:
        """The trace names the top pool at the tranche basis it was judged at."""
        board = (
            board_option("MSTRc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("69.59")),
            board_option("SNDKc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("15.29")),
        )
        session = PolicyState(
            day=BASE_TIME.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date(),
            day_start_equity_usd=Decimal("1000"),
        )
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, session, board, {})
        allocation = allocate_portfolio(
            engine,
            session,
            evaluations,
            {},
            held=(),
            cash_usdc=Decimal("1000"),
            equity_usdc=Decimal("1000"),
        )
        assert allocation.gate_trace_symbol == "MSTRc"
        assert allocation.gate_trace_basis.startswith("entry gate chain for MSTRc at the")
        assert "tranche basis" in allocation.gate_trace_basis
        assert "portfolio equity 1000" in allocation.gate_trace_basis
        halt_line = next(
            text for text in allocation.gate_trace if text.startswith("gate daily_loss_halt:")
        )
        assert ": PASS" in halt_line

    def test_the_summary_exclusion_line_names_the_cause_for_every_reason(self) -> None:
        """Below-band exclusions carry their measured bound in the summary."""
        options = (
            board_option("BBBc", BBB_POOL, BBB_TOKEN, emissions_apr=Decimal("4.0")),
            board_option("AAAc", AAA_POOL, AAA_TOKEN, emissions_apr=Decimal("2.0")),
            board_option("CCCc", CCC_POOL, CCC_TOKEN, emissions_apr=Decimal("1.8")),
        )
        allocation = allocate_portfolio(
            PolicyEngine(),
            PolicyState(),
            board_evaluations(options),
            {},
            held=(),
            cash_usdc=Decimal("400"),
            equity_usdc=Decimal("1200"),
        )
        assert "CCCc (below_tier_band: weighted" in allocation.summary
        assert "below band floor" in allocation.summary


class TestLiveSndkcNight:
    """The gnhf 35 live reproduction, flipped by the gnhf 36 coherence rule.

    The second production evidence pass, verbatim from the VM cycle at
    2026-09-28T05:30Z and the on-chain decomposition at block 51893582:
    SNDKc topped the board at a qualifying APR of 347.907... read as a
    decimal fraction in Aerodrome's displayed convention - 34,791 percent,
    the per-cell concentration number whose denominator (a 5,078-USDC
    current-cell staked value) collapsed overnight while the reward
    stream held. Every protective gate PASSED; the book stayed flat on
    the honest bound conflict - the 35-percent concentration bound
    (36.86 USDC of a 105.30 book) below the 80-USDC minimum. The gnhf 36
    coherence rule makes those bounds satisfiable: the effective minimum
    at this equity IS the clamp, so the same morning's board funds its
    top name at the locked per-name bound.
    """

    LIVE_CASH = Decimal("102.263507")
    LIVE_EQUITY = Decimal("105.3026673647274842033170387")
    LIVE_SNDKC_APR = Decimal("347.907697331990933767452574132184551184787645543487960275680")
    LIVE_CONCENTRATION_BOUND = Decimal("36.85593357765461947116096354")

    def session(self) -> PolicyState:
        """Build the morning's session state: healthy, no halt."""
        day = BASE_TIME.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
        return PolicyState(
            day=day,
            day_start_equity_usd=Decimal("102.263507"),
            peak_equity_usd=Decimal("105.3181507845350552886065412"),
        )

    def board(self) -> tuple[PoolBoardOption, ...]:
        """Build the morning's board: SNDKc far above the band."""
        return (
            board_option(
                "SNDKc",
                "0x" + "3" * 40,
                "0x" + "4" * 40,
                emissions_apr=self.LIVE_SNDKC_APR,
                equity_usd=self.LIVE_EQUITY,
            ),
            board_option(
                "MSTRc",
                "0x" + "1" * 40,
                "0x" + "2" * 40,
                emissions_apr=Decimal("153.5826647602950643433296005"),
                equity_usd=self.LIVE_EQUITY,
            ),
        )

    def allocation(self) -> PortfolioAllocation:
        """Run the allocator over the morning's board at the live basis."""
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, self.session(), self.board(), {})
        return allocate_portfolio(
            engine,
            self.session(),
            evaluations,
            {},
            held=(),
            cash_usdc=self.LIVE_CASH,
            equity_usdc=self.LIVE_EQUITY,
        )

    def test_the_morning_funds_at_the_effective_minimum(self) -> None:
        """The deployment unblock: the funded tranche clears the coherent bound.

        The gnhf 36 verification the captain's brief demanded: a fixture
        decision at the exact 105-equity production basis showing the
        funded tranche sitting at the effective minimum (36.86, the
        clamp) rather than refused against the unsatisfiable 80.
        """
        allocation = self.allocation()
        assert [tranche.symbol for tranche in allocation.tranches] == ["SNDKc"]
        tranche = allocation.tranches[0]
        assert tranche.budget_usd == self.LIVE_CONCENTRATION_BOUND
        effective = PortfolioParameters().effective_minimum_position_usdc(self.LIVE_EQUITY)
        assert effective == self.LIVE_CONCENTRATION_BOUND
        assert tranche.budget_usd >= effective
        assert allocation.cash_residual_usdc == Decimal("65.40757342234538052883903646")
        # MSTRc stays below the band; nothing is force-deployed past it.
        mstrc = next(item for item in allocation.excluded if item.symbol == "MSTRc")
        assert mstrc.reason is PortfolioExclusionReason.BELOW_TIER_BAND
        assert (
            f"allocation funds 1 tranche(s) [SNDKc {self.LIVE_CONCENTRATION_BOUND}]"
            in allocation.summary
        )

    def test_the_degenerate_book_stays_cash_below_the_floor(self) -> None:
        """A book whose clamp sits under the hard floor never breaches the cap.

        The chosen degenerate behavior, stated explicitly: when the
        concentration clamp (29.75 at equity 85) sits below the 30-USDC
        floor, the effective minimum holds at the floor and the book
        stays cash - the floor bounds the coherence rule from below, it
        never licenses breaching the locked per-name concentration cap.
        The exclusion carries the full derivation and the percent-
        annotated forgone income (the gnhf 34-35 evidence style).
        """
        equity = Decimal("85")
        engine = PolicyEngine()
        evaluations = evaluate_pool_entries(engine, self.session(), self.board(), {})
        allocation = allocate_portfolio(
            engine,
            self.session(),
            evaluations,
            {},
            held=(),
            cash_usdc=equity,
            equity_usdc=equity,
        )
        assert allocation.tranches == ()
        sndkc = next(item for item in allocation.excluded if item.symbol == "SNDKc")
        assert sndkc.reason is PortfolioExclusionReason.BELOW_MIN_POSITION_SIZE
        assert (
            "SNDKc (below_min_position_size: size 29.750000 below the effective minimum "
            "30 after the 29.75 concentration clamp)" in allocation.summary
        )
        assert "that locked bound sits below the effective minimum 30 USDC" in sndkc.detail
        assert "the book stays cash rather than breach the concentration cap" in sndkc.detail
        assert "floored at 30 = 30; the hard floor governs" in sndkc.detail
        assert "income forgone about" in sndkc.detail
        assert "(about 34,791 percent)" in sndkc.detail

    def test_the_full_gate_chain_rides_the_allocation_all_pass(self) -> None:
        """The eight-gate chain shows every protective gate passing.

        The determination the escalation asked for: no gate refuses on bad
        data - the flat book was the sizing bound conflict alone, and the
        trace proves it cycle over cycle at the exact tranche basis that
        now funds.
        """
        allocation = self.allocation()
        assert allocation.gate_trace_symbol == "SNDKc"
        assert allocation.gate_trace_basis.startswith("entry gate chain for SNDKc at the")
        assert f"{self.LIVE_CONCENTRATION_BOUND} USDC tranche basis" in allocation.gate_trace_basis
        assert f"portfolio equity {self.LIVE_EQUITY}" in allocation.gate_trace_basis
        assert len(allocation.gate_trace) == 8
        assert all(": PASS" in line for line in allocation.gate_trace)
        emissions_line = next(
            line for line in allocation.gate_trace if line.startswith("gate emissions_floor:")
        )
        assert "(about 34,791 percent)" in emissions_line
        assert "1.5 (about 150 percent)" in emissions_line
