"""Behavior tests for the rehearsal command's offline orchestration."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from aero_bot.history import (
    EmissionsAprHistory,
    EmissionsAprPoint,
    HistoryUnavailableError,
    PoolPricePath,
    PoolPricePoint,
)
from aero_bot.rehearsal import PoolRehearsalLedger
from aero_bot.rehearse import (
    PoolRehearsalFailure,
    RehearsalRunReport,
    RehearsalUnavailableError,
    anchor_staked_value_usd,
    build_argument_parser,
    main,
    print_report_summary,
    run_rehearsal,
    stock_token_address,
)
from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    BASE_USDC_ADDRESS,
    SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
    PoolCandidate,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
    PoolKind,
    VenueId,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
OTHER_B20_ADDRESS = "0x4f0000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
OTHER_POOL_ADDRESS = "0x3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b"
GAUGE_ADDRESS = "0x3111111111111111111111111111111111111111"
OTHER_GAUGE_ADDRESS = "0x3313131313131313131313131313131313131313"
# The fixture session is a Saturday noon UTC, clear of every session event window.
BASE_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
# Six-and-six decimals make the raw price equal the human price in fixtures.
FIXTURE_STOCK_DECIMALS = 6
FIXTURE_QUOTE_DECIMALS = 6
# The fixture raw active liquidity keeps the one-percent depth gate permissive.
DEFAULT_POOL_LIQUIDITY = 80_000_000_000_000
# The fixture gauge shares the pool's liquidity so staking shares stay readable.
DEFAULT_GAUGE_LIQUIDITY = DEFAULT_POOL_LIQUIDITY
# The fixture reward rate is one whole AERO per second.
DEFAULT_EMISSIONS_PER_SECOND = 10**18
# The documented AERO price assumption behind the fixture APR series.
FIXTURE_AERO_PRICE = Decimal("0.5")
# Every fixture swap moves a five-thousand USDC notional.
DEFAULT_SWAP_NOTIONAL_RAW = 5_000_000_000
# High-precision fixture math mirrors the replay's internal context.
MATH_PRECISION = 60


def make_candidate(
    pool_address: str = POOL_ADDRESS,
    token_address: str = B20_ADDRESS,
    gauge_address: str = GAUGE_ADDRESS,
    stock_is_token0: bool = True,
    sqrt_ratio: int = 1 << 96,
    staked_stock_raw: int = 3 * 10**FIXTURE_STOCK_DECIMALS,
    staked_quote_raw: int = 250 * 10**FIXTURE_QUOTE_DECIMALS,
    **overrides: object,
) -> PoolCandidate:
    """Build one accepted official B20/native-USDC Slipstream candidate.

    Args:
        pool_address: Fixture pool contract address.
        token_address: Fixture B20 stock token address.
        gauge_address: Fixture gauge contract address.
        stock_is_token0: True places the stock at token0, False at token1.
        sqrt_ratio: Raw Sugar snapshot square-root price.
        staked_stock_raw: Raw staked stock balance from the snapshot.
        staked_quote_raw: Raw staked USDC balance from the snapshot.
        **overrides: Candidate fields changed for one behavior test.

    Returns:
        A validated immutable pool candidate.
    """
    token0, token1 = (
        (token_address, BASE_USDC_ADDRESS)
        if stock_is_token0
        else (BASE_USDC_ADDRESS, token_address)
    )
    staked0, staked1 = (
        (staked_stock_raw, staked_quote_raw)
        if stock_is_token0
        else (staked_quote_raw, staked_stock_raw)
    )
    values: dict[str, Any] = {
        "pool_address": pool_address,
        "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "token0_address": token0,
        "token1_address": token1,
        "pool_kind": PoolKind.SLIPSTREAM,
        "tick_spacing": 10,
        "current_tick": -5,
        "sqrt_ratio": sqrt_ratio,
        "pool_fee_ppm": 500,
        "unstaked_fee_ppm": 100_000,
        "reserve0": 10**18,
        "reserve1": 5_000_000,
        "staked0": staked0,
        "staked1": staked1,
        "gauge_address": gauge_address,
        "gauge_liquidity": 9_999,
        "gauge_alive": True,
        "emissions_per_second": 4_494_371_922_759_724,
        "emissions_token_address": AERO_TOKEN_ADDRESS,
    }
    values.update(overrides)
    return PoolCandidate.model_validate(values)


def make_discovery(
    pools: tuple[PoolCandidate, ...],
    snapshot_block: int | None = 123,
    status: PoolDiscoveryStatus = PoolDiscoveryStatus.VERIFIED,
) -> PoolDiscoveryResult:
    """Build one adapter-shaped discovery result.

    Args:
        pools: Accepted pool candidates carried by the result.
        snapshot_block: Sugar snapshot pin block, or None for the no-observation shape.
        status: Discovery status the run must handle.

    Returns:
        A validated immutable discovery result.
    """
    return PoolDiscoveryResult(
        venue=VenueId.AERODROME,
        status=status,
        source=f"lp-sugar:fixture@block:{snapshot_block}",
        observed_at=BASE_TIME,
        snapshot_block=snapshot_block,
        pools=pools,
        diagnostics=("fixture summary",),
    )


def make_point(index: int, price: Decimal, step_seconds: int) -> PoolPricePoint:
    """Build one synthetic swap observation with a consistent raw witness.

    Args:
        index: Observation offset from the fixture base instant.
        price: Human USDC-per-stock price this swap settled at.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        An immutable price point with onchain-shaped evidence.
    """
    with localcontext() as decimal_context:
        decimal_context.prec = MATH_PRECISION
        # Equal six-and-six decimals make the raw price the human price.
        sqrt_ratio = int((price * Decimal(1 << 192)).sqrt())
        tick = int((price.ln() / Decimal("1.0001").ln()).to_integral_value())
    return PoolPricePoint(
        timestamp=BASE_TIME + timedelta(seconds=step_seconds * index),
        block_number=index,
        log_index=0,
        amount0=0,
        amount1=DEFAULT_SWAP_NOTIONAL_RAW,
        sqrt_ratio=sqrt_ratio,
        liquidity=DEFAULT_POOL_LIQUIDITY,
        tick=tick,
        price_usdc=price,
    )


def make_path(
    pool_address: str,
    token_address: str,
    prices: tuple[Decimal, ...],
    step_seconds: int = 60,
) -> PoolPricePath:
    """Build one synthetic price path from ordered prices.

    Args:
        pool_address: Fixture pool the path belongs to.
        token_address: Fixture B20 stock token in that pool.
        prices: Ordered human prices, one per reconstructed swap.
        step_seconds: Wall-clock seconds between observations.

    Returns:
        An immutable price path over the synthetic session.
    """
    return PoolPricePath(
        pool_address=pool_address,
        token_address=token_address,
        token_is_token0=True,
        token_decimals=FIXTURE_STOCK_DECIMALS,
        quote_decimals=FIXTURE_QUOTE_DECIMALS,
        from_block=0,
        to_block=max(len(prices) - 1, 0),
        observed_at=BASE_TIME,
        points=tuple(make_point(index, price, step_seconds) for index, price in enumerate(prices)),
    )


def make_emissions(
    pool_address: str,
    gauge_address: str,
    emissions_apr: Decimal = Decimal("3.0"),
    span_seconds: int = 120,
) -> EmissionsAprHistory:
    """Build one synthetic single-step emissions-APR series.

    Args:
        pool_address: Fixture pool the gauge belongs to.
        gauge_address: Fixture gauge whose liquidity the series describes.
        emissions_apr: Raw APR in Aerodrome's display convention.
        span_seconds: Seconds the single step spans.

    Returns:
        An immutable series labeled as an exact event fold.
    """
    return EmissionsAprHistory(
        pool_address=pool_address,
        gauge_address=gauge_address,
        from_block=0,
        to_block=10,
        anchor_block=10,
        observed_at=BASE_TIME,
        anchor_gauge_liquidity=DEFAULT_GAUGE_LIQUIDITY,
        anchor_staked_tvl_usd=Decimal("1000"),
        emissions_per_second=DEFAULT_EMISSIONS_PER_SECOND,
        aero_price_assumption_usd=FIXTURE_AERO_PRICE,
        reconstruction_mode="event_fold",
        starting_gauge_liquidity=DEFAULT_GAUGE_LIQUIDITY,
        steps=(
            EmissionsAprPoint(
                timestamp=BASE_TIME,
                gauge_liquidity=DEFAULT_GAUGE_LIQUIDITY,
                emissions_apr=emissions_apr,
            ),
        ),
    )


class FakeSources:
    """Serve deterministic offline rehearsal sources without any network."""

    def __init__(
        self,
        discovery: PoolDiscoveryResult,
        price_paths: dict[str, PoolPricePath],
        emissions_histories: dict[str, EmissionsAprHistory],
        decimals: dict[str, int],
        failing_pools: frozenset[str] = frozenset(),
    ) -> None:
        """Store the deterministic evidence and the failure injection.

        Args:
            discovery: The discovery result every run observes.
            price_paths: Reconstructed price path by pool address.
            emissions_histories: Reconstructed emissions series by pool address.
            decimals: Token decimal counts by normalized address.
            failing_pools: Pool addresses whose price-path read fails closed.
        """
        self._discovery = discovery
        self._price_paths = price_paths
        self._emissions_histories = emissions_histories
        # Decimal keys normalize on store so checksummed fixture spellings serve.
        self._decimals = {address.lower(): count for address, count in decimals.items()}
        self._failing_pools = failing_pools
        # Call recording lets tests assert the exact orchestration boundary.
        self.discover_calls = 0
        self.decimals_reads: list[str] = []
        self.price_path_calls: list[str] = []
        self.emissions_calls: list[tuple[str, int]] = []

    def discover_pools(self) -> PoolDiscoveryResult:
        """Return the fixture discovery result."""
        self.discover_calls += 1
        return self._discovery

    def read_token_decimals(self, token_address: str) -> int:
        """Return the fixture token's decimal count."""
        normalized = token_address.lower()
        self.decimals_reads.append(normalized)
        return self._decimals[normalized]

    def fetch_price_path(
        self, pool: PoolCandidate, token_decimals: int, quote_decimals: int
    ) -> PoolPricePath:
        """Return the pool's fixture price path, failing closed when injected.

        Raises:
            HistoryUnavailableError: For a pool named in failing_pools.
        """
        self.price_path_calls.append(pool.pool_address)
        if pool.pool_address in self._failing_pools:
            raise HistoryUnavailableError("fixture price path outage")
        return self._price_paths[pool.pool_address]

    def fetch_emissions_history(
        self, pool: PoolCandidate, anchor_block: int, anchor_staked_tvl_usd: Decimal
    ) -> EmissionsAprHistory:
        """Return the pool's fixture emissions series with the anchor evidence."""
        self.emissions_calls.append((pool.pool_address, anchor_block))
        return self._emissions_histories[pool.pool_address]


def one_pool_fixture(
    pool_address: str = POOL_ADDRESS,
    token_address: str = B20_ADDRESS,
    gauge_address: str = GAUGE_ADDRESS,
    failing_pools: frozenset[str] = frozenset(),
    prices: tuple[Decimal, ...] = (Decimal("100"), Decimal("100"), Decimal("100")),
    step_seconds: int = 60,
) -> FakeSources:
    """Build offline sources serving exactly one accepted pool.

    Args:
        pool_address: Fixture pool contract address.
        token_address: Fixture B20 stock token in that pool.
        gauge_address: Fixture gauge for the emissions series.
        failing_pools: Pool addresses whose price-path read fails closed.
        prices: Ordered synthetic prices for the reconstructed path.
        step_seconds: Wall-clock seconds between path observations.

    Returns:
        Deterministic offline sources for one rehearsal run.
    """
    candidate = make_candidate(
        pool_address=pool_address,
        token_address=token_address,
        gauge_address=gauge_address,
    )
    return FakeSources(
        discovery=make_discovery((candidate,)),
        price_paths={pool_address: make_path(pool_address, token_address, prices, step_seconds)},
        emissions_histories={pool_address: make_emissions(pool_address, gauge_address)},
        decimals={
            token_address: FIXTURE_STOCK_DECIMALS,
            BASE_USDC_ADDRESS: FIXTURE_QUOTE_DECIMALS,
        },
        failing_pools=failing_pools,
    )


def two_pool_fixture(failing_pools: frozenset[str] = frozenset()) -> FakeSources:
    """Build offline sources serving the primary and secondary fixture pools.

    Args:
        failing_pools: Pool addresses whose price-path read fails closed.

    Returns:
        Deterministic offline sources for a two-pool rehearsal run.
    """
    return FakeSources(
        discovery=make_discovery(
            (
                make_candidate(),
                make_candidate(
                    pool_address=OTHER_POOL_ADDRESS,
                    token_address=OTHER_B20_ADDRESS,
                    gauge_address=OTHER_GAUGE_ADDRESS,
                ),
            )
        ),
        price_paths={
            POOL_ADDRESS: make_path(POOL_ADDRESS, B20_ADDRESS, (Decimal("100"),) * 3),
            OTHER_POOL_ADDRESS: make_path(
                OTHER_POOL_ADDRESS, OTHER_B20_ADDRESS, (Decimal("100"),) * 3
            ),
        },
        emissions_histories={
            POOL_ADDRESS: make_emissions(POOL_ADDRESS, GAUGE_ADDRESS),
            OTHER_POOL_ADDRESS: make_emissions(OTHER_POOL_ADDRESS, OTHER_GAUGE_ADDRESS),
        },
        decimals={
            B20_ADDRESS: FIXTURE_STOCK_DECIMALS,
            OTHER_B20_ADDRESS: FIXTURE_STOCK_DECIMALS,
            BASE_USDC_ADDRESS: FIXTURE_QUOTE_DECIMALS,
        },
        failing_pools=failing_pools,
    )


def run_fixture(
    sources: FakeSources,
    symbols: dict[str, str] | None = None,
    **overrides: object,
) -> RehearsalRunReport:
    """Run one rehearsal against fixture sources with default arguments.

    Args:
        sources: The offline fixture sources driving the run.
        symbols: Symbol map override; defaults to both fixture tokens.
        **overrides: run_rehearsal argument overrides for one behavior test.

    Returns:
        The assembled run report.
    """
    arguments: dict[str, Any] = {
        "sources": sources,
        "symbols_by_token_address": symbols
        if symbols is not None
        else {
            B20_ADDRESS: "FIXc",
            OTHER_B20_ADDRESS: "OTHERc",
        },
        "lookback": timedelta(days=1),
        "aero_price_assumption_usd": FIXTURE_AERO_PRICE,
        "gas_price_assumption_gwei": Decimal("0.001"),
        "apply_synthetic_stress": False,
    }
    arguments.update(overrides)
    return run_rehearsal(**arguments)


def run_main_with_sources(arguments: list[str], sources: FakeSources) -> int:
    """Run the command against fixture sources and the fixture token registry.

    Args:
        arguments: Command-line arguments handed to the command.
        sources: Offline fixture sources serving every live read.

    Returns:
        The command's process exit code.
    """
    registry = SimpleNamespace(
        assets=(
            SimpleNamespace(address=B20_ADDRESS, symbol="FIXc"),
            SimpleNamespace(address=OTHER_B20_ADDRESS, symbol="OTHERc"),
        )
    )
    with (
        patch("aero_bot.rehearse.LiveRehearsalSources", return_value=sources),
        patch("aero_bot.rehearse.load_official_b20_registry", return_value=registry),
    ):
        return main(arguments)


def test_stock_token_address_resolves_the_non_usdc_side() -> None:
    """The stock side is whichever pair member is not native USDC."""
    stock_first = make_candidate()
    stock_second = make_candidate(stock_is_token0=False)
    assert stock_token_address(stock_first) == B20_ADDRESS
    assert stock_token_address(stock_second) == B20_ADDRESS
    # A pair without native USDC cannot reach the rehearsal and fails closed.
    orphan = make_candidate(
        token0_address=OTHER_B20_ADDRESS,
        token1_address=B20_ADDRESS,
    )
    with pytest.raises(ValueError, match="does not pair native USDC"):
        stock_token_address(orphan)


def test_anchor_staked_value_usd_prices_both_pair_orderings() -> None:
    """Staked value is exact under either token ordering at the snapshot price."""
    # sqrt_ratio 2^96 with eight-and-six decimals prices one stock at 100 USDC.
    stock_first = make_candidate(
        sqrt_ratio=1 << 96,
        stock_is_token0=True,
        staked_stock_raw=3 * 10**8,
        staked_quote_raw=250 * 10**6,
    )
    stock_second = make_candidate(
        sqrt_ratio=1 << 96,
        stock_is_token0=False,
        staked_stock_raw=3 * 10**8,
        staked_quote_raw=250 * 10**6,
    )
    assert anchor_staked_value_usd(stock_first, 8, 6) == Decimal("550")
    assert anchor_staked_value_usd(stock_second, 8, 6) == Decimal("550")


def test_run_rehearsal_produces_one_ordered_ledger_per_pool() -> None:
    """Every discovered pool is rehearsed once, ordered by pool address."""
    sources = two_pool_fixture()

    report = run_fixture(sources)

    # The busier address sorts first, keeping multi-pool runs deterministic.
    assert [ledger.pool_address for ledger in report.ledgers] == [
        POOL_ADDRESS,
        OTHER_POOL_ADDRESS,
    ]
    assert [ledger.symbol for ledger in report.ledgers] == ["FIXc", "OTHERc"]
    assert report.discovery_block == 123
    assert report.discovered_pool_count == 2
    assert report.synthetic_stress is False
    assert report.aero_price_assumption_usd == FIXTURE_AERO_PRICE
    assert report.gas_price_assumption_gwei == Decimal("0.001")
    # Each ledger carries its own pool's fee tier and resolved observations.
    assert all(ledger.assumptions.pool_fee_ppm == 500 for ledger in report.ledgers)
    assert all(ledger.observation_count == 3 for ledger in report.ledgers)
    # The emissions anchor is pinned at the discovery snapshot block.
    assert sources.emissions_calls == [
        (POOL_ADDRESS, 123),
        (OTHER_POOL_ADDRESS, 123),
    ]


def test_run_rehearsal_pool_filter_selects_only_known_pools() -> None:
    """A pool filter rehearses exactly the named pools and rejects typos."""
    sources = one_pool_fixture()

    filtered = run_fixture(sources, pool_addresses=frozenset({POOL_ADDRESS}))

    assert [ledger.pool_address for ledger in filtered.ledgers] == [POOL_ADDRESS]
    assert sources.price_path_calls == [POOL_ADDRESS]
    with pytest.raises(RehearsalUnavailableError, match="outside the verified discovery"):
        run_fixture(sources, pool_addresses=frozenset({OTHER_POOL_ADDRESS}))


def test_run_rehearsal_requires_verified_discovery() -> None:
    """Unverified discovery is fail-closed evidence, never an empty rehearsal."""
    sources = FakeSources(
        discovery=make_discovery((), status=PoolDiscoveryStatus.UNAVAILABLE),
        price_paths={},
        emissions_histories={},
        decimals={},
    )
    with pytest.raises(RehearsalUnavailableError, match="did not verify: fixture summary"):
        run_fixture(sources)


def test_run_rehearsal_requires_a_snapshot_block() -> None:
    """A verified result without a pin block cannot anchor the emissions fold."""
    sources = FakeSources(
        discovery=make_discovery((make_candidate(),), snapshot_block=None),
        price_paths={POOL_ADDRESS: make_path(POOL_ADDRESS, B20_ADDRESS, (Decimal("100"),))},
        emissions_histories={POOL_ADDRESS: make_emissions(POOL_ADDRESS, GAUGE_ADDRESS)},
        decimals={},
    )
    with pytest.raises(RehearsalUnavailableError, match="no snapshot block"):
        run_fixture(sources)


def test_run_rehearsal_requires_discovered_pools() -> None:
    """A verified but empty discovery proves nothing and fails closed."""
    sources = FakeSources(
        discovery=make_discovery(()),
        price_paths={},
        emissions_histories={},
        decimals={},
    )
    with pytest.raises(RehearsalUnavailableError, match="accepted no B20/USDC pools"):
        run_fixture(sources)


def test_run_rehearsal_records_failures_and_continues() -> None:
    """One pool's failed read is recorded while the remaining pools rehearse."""
    sources = two_pool_fixture(failing_pools=frozenset({POOL_ADDRESS}))

    report = run_fixture(sources)

    assert [ledger.pool_address for ledger in report.ledgers] == [OTHER_POOL_ADDRESS]
    assert len(report.failures) == 1
    assert report.failures[0].pool_address == POOL_ADDRESS
    assert report.failures[0].symbol == "FIXc"
    assert "fixture price path outage" in report.failures[0].diagnostic


def test_run_rehearsal_fails_pools_without_registry_symbols() -> None:
    """A stock token missing from the registry map fails that pool only."""
    sources = one_pool_fixture()

    report = run_fixture(sources, symbols={})

    assert report.ledgers == ()
    assert len(report.failures) == 1
    assert "no symbol" in report.failures[0].diagnostic


def test_run_rehearsal_applies_the_synthetic_stress_schedule() -> None:
    """The documented stress overlay labels the ledger's reference mode."""
    # A two-and-a-quarter-hour window keeps the schedule's episodes disjoint.
    sources = one_pool_fixture(
        prices=(Decimal("100"),) * 4,
        step_seconds=45 * 60,
    )

    report = run_fixture(sources, apply_synthetic_stress=True)

    assert report.synthetic_stress is True
    assert report.ledgers[0].reference_mode == "synthetic_stress"


def test_report_rejects_drifted_pool_outcomes() -> None:
    """A report never names one pool twice or as both outcome kinds."""
    ledger_kwargs: dict[str, Any] = {
        "pool_address": POOL_ADDRESS,
        "token_address": B20_ADDRESS,
        "symbol": "FIXc",
        "replayed_at": BASE_TIME,
        "emissions_reconstruction_mode": "event_fold",
        "reference_mode": "amm_equals_reference",
        "assumptions": {
            "aero_price_assumption_usd": FIXTURE_AERO_PRICE,
            "gas_price_assumption_gwei": Decimal("0.001"),
            "starting_equity_usd": Decimal("200"),
            "pool_fee_ppm": 500,
        },
        "assumption_labels": ("fixture label",),
        "observation_count": 0,
        "starting_equity_usd": Decimal("200"),
        "final_equity_usd": Decimal("200"),
        "final_cash_usd": Decimal("200"),
        "pnl_usd": Decimal("0"),
        "return_fraction": Decimal("0"),
        "time_open_seconds": 0,
        "time_in_range_seconds": 0,
        "action_counts": {},
        "actions": (),
    }
    failure = PoolRehearsalFailure(
        pool_address=POOL_ADDRESS,
        token_address=B20_ADDRESS,
        symbol="FIXc",
        diagnostic="fixture",
    )
    with pytest.raises(ValidationError, match="distinct pools"):
        RehearsalRunReport(
            generated_at=BASE_TIME,
            discovery_source="fixture",
            discovery_block=123,
            discovered_pool_count=1,
            lookback=timedelta(days=1),
            aero_price_assumption_usd=FIXTURE_AERO_PRICE,
            gas_price_assumption_gwei=Decimal("0.001"),
            synthetic_stress=False,
            ledgers=(),
            failures=(failure, failure),
        )
    ledger = PoolRehearsalLedger(**ledger_kwargs)
    with pytest.raises(ValidationError, match="ledgers must describe distinct pools"):
        RehearsalRunReport(
            generated_at=BASE_TIME,
            discovery_source="fixture",
            discovery_block=123,
            discovered_pool_count=2,
            lookback=timedelta(days=1),
            aero_price_assumption_usd=FIXTURE_AERO_PRICE,
            gas_price_assumption_gwei=Decimal("0.001"),
            synthetic_stress=False,
            ledgers=(ledger, ledger),
            failures=(),
        )
    with pytest.raises(ValidationError, match="both a ledger and a failure"):
        RehearsalRunReport(
            generated_at=BASE_TIME,
            discovery_source="fixture",
            discovery_block=123,
            discovered_pool_count=1,
            lookback=timedelta(days=1),
            aero_price_assumption_usd=FIXTURE_AERO_PRICE,
            gas_price_assumption_gwei=Decimal("0.001"),
            synthetic_stress=False,
            ledgers=(ledger,),
            failures=(failure,),
        )


def test_run_rehearsal_replays_quiet_pools_to_empty_ledgers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pool with no swaps in the window still yields a zero-observation ledger."""
    sources = one_pool_fixture(prices=())

    report = run_fixture(sources)

    assert len(report.ledgers) == 1
    assert report.ledgers[0].observation_count == 0
    print_report_summary(report, Path("fixture-ledgers.json"))
    summary = capsys.readouterr().out
    assert "empty window" in summary
    assert "wrote 1 ledgers and 0 failures" in summary


def test_main_writes_a_round_tripping_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command writes one JSON report that round-trips as the typed model."""
    # The default synthetic stress schedule needs a window its episodes fit in.
    sources = one_pool_fixture(prices=(Decimal("100"),) * 4, step_seconds=45 * 60)
    output = tmp_path / "ledgers.json"
    exit_code = run_main_with_sources(["--output", str(output), "--lookback-days", "2"], sources)

    assert exit_code == 0
    report = RehearsalRunReport.model_validate_json(output.read_text(encoding="utf-8"))
    assert [ledger.pool_address for ledger in report.ledgers] == [POOL_ADDRESS]
    assert report.lookback == timedelta(days=2)
    summary = capsys.readouterr().out
    assert "FIXc" in summary
    assert f"wrote 1 ledgers and 0 failures to {output}" in summary


def test_main_passes_settings_and_filters_to_the_sources(tmp_path: Path) -> None:
    """The command threads the pool filter and configured endpoints through."""
    sources = one_pool_fixture()
    output = tmp_path / "ledgers.json"
    settings = type(
        "Settings",
        (),
        {"base_rpc_url": "https://fixture.example", "lp_sugar_address": "0xabc"},
    )()
    registry = SimpleNamespace(
        assets=(
            SimpleNamespace(address=B20_ADDRESS, symbol="FIXc"),
            SimpleNamespace(address=OTHER_B20_ADDRESS, symbol="OTHERc"),
        )
    )
    with (
        patch("aero_bot.rehearse.LiveRehearsalSources", return_value=sources) as factory,
        patch("aero_bot.rehearse.Settings", return_value=settings),
        patch("aero_bot.rehearse.load_official_b20_registry", return_value=registry),
    ):
        exit_code = main(
            [
                "--output",
                str(output),
                "--no-synthetic-stress",
                "--pool",
                # An uppercased body with the lowercase 0x prefix stays valid.
                "0x" + POOL_ADDRESS[2:].upper(),
            ]
        )

    assert exit_code == 0
    # The factory received the configured endpoint and Sugar identity.
    assert factory.call_count == 1
    assert factory.call_args.kwargs["rpc_url"] == "https://fixture.example"
    assert factory.call_args.kwargs["sugar_address"] == "0xabc"
    # A checksummed pool filter normalizes to the discovered pool.
    assert sources.price_path_calls == [POOL_ADDRESS]


def test_main_exits_nonzero_when_discovery_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unverifiable discovery exits nonzero with the diagnostic on stderr."""
    sources = FakeSources(
        discovery=make_discovery((), status=PoolDiscoveryStatus.UNAVAILABLE),
        price_paths={},
        emissions_histories={},
        decimals={},
    )
    output = tmp_path / "ledgers.json"
    exit_code = run_main_with_sources(["--output", str(output)], sources)

    assert exit_code == 1
    assert "rehearsal unavailable" in capsys.readouterr().err
    assert not output.exists()


def test_main_exits_nonzero_on_partial_pool_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing pool still writes the report but exits nonzero."""
    sources = one_pool_fixture(failing_pools=frozenset({POOL_ADDRESS}))
    output = tmp_path / "ledgers.json"
    exit_code = run_main_with_sources(["--output", str(output)], sources)

    assert exit_code == 1
    report = RehearsalRunReport.model_validate_json(output.read_text(encoding="utf-8"))
    assert report.ledgers == ()
    assert "fixture price path outage" in report.failures[0].diagnostic
    assert "FAILED" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("arguments", "diagnostic"),
    [
        (["--lookback-days", "0"], "lookback-days must be positive"),
        (["--aero-price", "0"], "aero-price must be positive"),
        (["--gas-price-gwei", "0"], "gas-price-gwei must be positive"),
        (["--header-batch-size", "11"], "header-batch-size must be between"),
        (["--header-batch-size", "0"], "header-batch-size must be between"),
    ],
)
def test_argument_validation_rejects_invalid_values(
    arguments: list[str], diagnostic: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid numeric arguments exit with the documented diagnostic."""
    with pytest.raises(SystemExit) as caught:
        main(arguments + ["--output", "/dev/null"])
    assert caught.value.code == 2
    assert diagnostic in capsys.readouterr().err


def test_argument_parser_defaults_match_the_documented_assumptions() -> None:
    """The parser defaults mirror the locked rehearsal assumptions."""
    arguments = build_argument_parser().parse_args([])
    assert arguments.lookback_days == Decimal(21)
    assert arguments.aero_price == Decimal("0.50")
    assert arguments.gas_price_gwei == Decimal("0.001")
    assert arguments.pool_addresses == []
    assert arguments.synthetic_stress is True
    assert arguments.output == Path("rehearsal-ledgers.json")
    assert arguments.header_batch_size == 10
