"""Command-line rehearsal harness over live read-only Base evidence.

The ``aero-bot-rehearse`` command discovers the accepted B20/USDC pools
through LP Sugar, reconstructs each pool's swap-driven price path and gauge
emissions-APR series from read-only logs, replays the locked policy engine
over the reconstructed histories, and writes every deterministic per-pool
profit-and-loss ledger into one JSON report. All network reads are delegated
to injected sources, so the orchestration itself is pure and unit-tested
offline; only the command performs the live reads. Method and every
documented approximation live in docs/rehearsal.md.
"""

import argparse
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Protocol, Self

import httpx
from pydantic import BaseModel, Field, model_validator

from aero_bot.config import Settings
from aero_bot.domain import (
    IMMUTABLE_MODEL_CONFIG,
    EvmAddress,
    normalize_evm_address,
)
from aero_bot.executor import (
    ExecutionUnavailableError,
    ExecutorRpcBackend,
    ExecutorRpcRevertError,
)
from aero_bot.history import (
    MAX_BINARY_SEARCH_PROBES,
    MAX_HEADER_BATCH_SIZE,
    MAX_TOTAL_SWAP_EVENTS,
    REHEARSAL_LOOKBACK,
    EmissionsAprHistory,
    EventHistoryRpcBackend,
    HistoryUnavailableError,
    PoolPricePath,
    price_usdc_per_stock,
    staked_tvl_usd,
)
from aero_bot.registry import load_official_b20_registry
from aero_bot.rehearsal import (
    DEFAULT_GAS_PRICE_ASSUMPTION_GWEI,
    DEFAULT_SYNTHETIC_DISLOCATION_SCHEDULE,
    PoolRehearsalLedger,
    RehearsalAssumptions,
    WidthSelectionMode,
    replay_pool,
)
from aero_bot.sugar import LpSugarRpcBackend
from aero_bot.venues import (
    BASE_USDC_ADDRESS,
    AerodromeVenueAdapter,
    PoolCandidate,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
)

# One reconstruction reads at most every unique event block, the bounded
# binary search's probe blocks, and a handful of window anchors, so the
# rehearsal's cumulative header bound derives from those documented limits.
HEADER_BOUND_ANCHOR_ALLOWANCE = 16
REHEARSAL_HEADER_LOOKUP_BOUND = (
    MAX_TOTAL_SWAP_EVENTS + MAX_BINARY_SEARCH_PROBES + HEADER_BOUND_ANCHOR_ALLOWANCE
)
# The bundled default output report name, written into the working directory.
DEFAULT_OUTPUT_PATH = Path("rehearsal-ledgers.json")
# The native USDC quote identity, normalized once for pair-side resolution.
QUOTE_TOKEN_ADDRESS = normalize_evm_address(BASE_USDC_ADDRESS)


class RehearsalUnavailableError(RuntimeError):
    """Signal that a rehearsal run could not begin from trustworthy discovery."""


class PoolRehearsalFailure(BaseModel):
    """Record one pool's fail-closed rehearsal outcome with its diagnostic."""

    # Frozen strict fields keep the failure exactly as the run observed it.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The Slipstream pool whose rehearsal could not complete.
    pool_address: EvmAddress
    # The B20 stock token paired with native USDC in that pool.
    token_address: EvmAddress
    # The resolved stock symbol, carried for readable output.
    symbol: str
    # The fail-closed diagnostic explaining which read or replay failed.
    diagnostic: str


class RehearsalRunReport(BaseModel):
    """Collect one rehearsal run's per-pool ledgers and fail-closed failures."""

    # Frozen strict fields keep the whole run report immutable evidence.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The instant the run finished, for provenance only.
    generated_at: datetime
    # The discovery source string carrying the Sugar provenance and pin block.
    discovery_source: str
    # The block Sugar's enumeration was pinned to, anchoring the replay window.
    discovery_block: Annotated[int, Field(ge=0)]
    # How many pools discovery accepted before any pool filter was applied.
    discovered_pool_count: Annotated[int, Field(ge=0)]
    # The lookback window every reconstruction covered.
    lookback: timedelta
    # The documented AERO price assumption behind every ledger's emissions.
    aero_price_assumption_usd: Annotated[Decimal, Field(gt=0)]
    # The documented constant Base gas price behind every batch cost.
    gas_price_assumption_gwei: Annotated[Decimal, Field(gt=0)]
    # Whether the documented synthetic dislocation schedule overlay was applied.
    synthetic_stress: bool
    # How every entry and recenter range width was chosen in the replays.
    width_selection: WidthSelectionMode = WidthSelectionMode.DERIVED_FROM_TARGET
    # One deterministic ledger per pool that replayed end to end.
    ledgers: tuple[PoolRehearsalLedger, ...] = ()
    # One fail-closed diagnostic per pool whose reads or replay failed.
    failures: tuple[PoolRehearsalFailure, ...] = ()

    @model_validator(mode="after")
    def require_disjoint_pool_outcomes(self) -> Self:
        """Reject reports naming one pool twice or as both outcome kinds."""
        ledger_pools = [ledger.pool_address for ledger in self.ledgers]
        failure_pools = [failure.pool_address for failure in self.failures]
        if len(set(ledger_pools)) != len(ledger_pools):
            raise ValueError("ledgers must describe distinct pools")
        if len(set(failure_pools)) != len(failure_pools):
            raise ValueError("failures must describe distinct pools")
        if set(ledger_pools) & set(failure_pools):
            raise ValueError("one pool appears as both a ledger and a failure")
        return self


class RehearsalSources(Protocol):
    """Define the read-only live sources one rehearsal run consumes."""

    def discover_pools(self) -> PoolDiscoveryResult:
        """Return the accepted B20/USDC pools in one block-pinned snapshot."""
        ...

    def read_token_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count."""
        ...

    def fetch_price_path(
        self, pool: PoolCandidate, token_decimals: int, quote_decimals: int
    ) -> PoolPricePath:
        """Reconstruct the pool's price path over the configured window."""
        ...

    def fetch_emissions_history(
        self, pool: PoolCandidate, anchor_block: int, anchor_staked_tvl_usd: Decimal
    ) -> EmissionsAprHistory:
        """Reconstruct the pool's gauge emissions-APR series."""
        ...

    def read_aero_price_usdc(self, block_number: int) -> Decimal:
        """Read the AERO price in USDC pinned to one block."""
        ...


def stock_token_address(pool: PoolCandidate) -> EvmAddress:
    """Return the B20 stock side of one accepted native-USDC pair.

    Args:
        pool: Candidate whose pair the venue adapter already scoped to exactly
            native USDC plus one issuer-verified B20 token.

    Returns:
        The stock token's normalized contract address.

    Raises:
        ValueError: If neither pair side is native USDC.
    """
    if pool.token0_address == QUOTE_TOKEN_ADDRESS:
        return pool.token1_address
    if pool.token1_address == QUOTE_TOKEN_ADDRESS:
        return pool.token0_address
    raise ValueError(f"pool {pool.pool_address} does not pair native USDC with a B20 token")


def anchor_staked_value_usd(
    pool: PoolCandidate,
    token_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Value the pool's gauge-staked balances at the Sugar snapshot price.

    The snapshot's square-root price converts into the pool's human price and
    both staked token balances value at that price, which anchors the
    emissions-APR convention exactly as the history module documents.

    Args:
        pool: Candidate whose Sugar snapshot supplies price and staked balances.
        token_decimals: Decimal count of the B20 stock token.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The staked value in USDC at the snapshot block.

    Raises:
        ValueError: If the pair carries no USDC side or the snapshot price is
            not positive.
    """
    stock = stock_token_address(pool)
    stock_is_token0 = pool.token0_address == stock
    snapshot_price = price_usdc_per_stock(
        pool.sqrt_ratio, stock_is_token0, token_decimals, quote_decimals
    )
    staked_stock_raw = pool.staked0 if stock_is_token0 else pool.staked1
    staked_quote_raw = pool.staked1 if stock_is_token0 else pool.staked0
    return staked_tvl_usd(
        staked_quote_raw, quote_decimals, staked_stock_raw, token_decimals, snapshot_price
    )


class LiveRehearsalSources:
    """Compose the read-only live backends one rehearsal run consumes."""

    def __init__(
        self,
        rpc_url: str,
        sugar_address: str,
        b20_addresses: frozenset[str],
        lookback: timedelta,
        aero_price_assumption_usd: Decimal | None,
        header_batch_size: int = MAX_HEADER_BATCH_SIZE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure the live read-only sources for one run.

        Args:
            rpc_url: Base JSON-RPC endpoint used exclusively for read-only calls.
            sugar_address: LP Sugar contract address anchoring pool discovery.
            b20_addresses: Issuer-verified B20 contracts allowed for pairing.
            lookback: Reconstruction window every pool covers.
            aero_price_assumption_usd: Optional documented AERO price override;
                absent means the per-run live read at the anchor block sets it.
            header_batch_size: Block-header reads grouped per JSON-RPC batch.
            transport: Optional injected HTTP transport for deterministic tests.
            sleep: Injected delay function used for backoff and politeness waits.
        """
        self._rpc_url = rpc_url
        self._sugar_address = sugar_address
        self._b20_addresses = b20_addresses
        self._lookback = lookback
        self._aero_price_assumption_usd = aero_price_assumption_usd
        self._history = EventHistoryRpcBackend(
            rpc_url=rpc_url,
            max_block_header_lookups=REHEARSAL_HEADER_LOOKUP_BOUND,
            header_batch_size=header_batch_size,
            transport=transport,
            sleep=sleep,
        )
        # Token decimals are pure metadata, so one read per token is cached.
        self._decimals_cache: dict[str, int] = {}
        self._transport = transport
        self._sleep = sleep

    def discover_pools(self) -> PoolDiscoveryResult:
        """Return the accepted B20/USDC pools in one block-pinned snapshot.

        Returns:
            The venue adapter's verified discovery result.

        Raises:
            PoolDiscoveryUnavailableError: If the Sugar enumeration cannot
                complete with bounded retries.
        """
        backend = LpSugarRpcBackend(
            rpc_url=self._rpc_url,
            sugar_address=self._sugar_address,
            transport=self._transport,
            sleep=self._sleep,
        )
        return AerodromeVenueAdapter(backend).discover_pools(self._b20_addresses)

    def read_token_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count, cached per token.

        Args:
            token_address: ERC20 contract whose decimals() is read once.

        Returns:
            The token's decimal count.

        Raises:
            HistoryUnavailableError: If the read cannot complete or is malformed.
        """
        normalized = normalize_evm_address(token_address)
        cached = self._decimals_cache.get(normalized)
        if cached is not None:
            return cached
        decimals = self._history.read_erc20_decimals(normalized)
        self._decimals_cache[normalized] = decimals
        return decimals

    def fetch_price_path(
        self, pool: PoolCandidate, token_decimals: int, quote_decimals: int
    ) -> PoolPricePath:
        """Reconstruct the pool's price path over the configured window.

        Args:
            pool: Candidate whose pool contract supplies the log filter.
            token_decimals: Decimal count of the B20 stock token.
            quote_decimals: Decimal count of the USDC quote token.

        Returns:
            The immutable ordered price path over the lookback window.

        Raises:
            HistoryUnavailableError: If any read cannot complete with
                bounded retries or any decoded evidence is malformed.
        """
        return self._history.fetch_price_path(
            pool_address=pool.pool_address,
            token_address=stock_token_address(pool),
            token0_address=pool.token0_address,
            token1_address=pool.token1_address,
            token_decimals=token_decimals,
            quote_decimals=quote_decimals,
            lookback=self._lookback,
        )

    def fetch_emissions_history(
        self, pool: PoolCandidate, anchor_block: int, anchor_staked_tvl_usd: Decimal
    ) -> EmissionsAprHistory:
        """Reconstruct the pool's gauge emissions-APR series.

        Args:
            pool: Candidate whose gauge and snapshot state anchor the series.
            anchor_block: The Sugar snapshot block pinning the window's end.
            anchor_staked_tvl_usd: Staked value in USDC at the anchor block.

        Returns:
            The immutable stepwise emissions-APR series.

        Raises:
            HistoryUnavailableError: If any read cannot complete with bounded
                retries or any decoded evidence is malformed.
        """
        if pool.gauge_address is None:
            # The venue adapter accepts only live gauges; this branch is defensive.
            raise HistoryUnavailableError(
                f"pool {pool.pool_address} carries no gauge for emissions reconstruction"
            )
        if self._aero_price_assumption_usd is None:
            # No operator override: resolve once, live, at the anchor block.
            self.read_aero_price_usdc(anchor_block)
        assert self._aero_price_assumption_usd is not None  # noqa: S101 - resolved above
        return self._history.fetch_emissions_apr_history(
            pool_address=pool.pool_address,
            gauge_address=pool.gauge_address,
            anchor_block=anchor_block,
            anchor_gauge_liquidity=pool.gauge_liquidity,
            anchor_staked_tvl_usd=anchor_staked_tvl_usd,
            emissions_per_second=pool.emissions_per_second,
            aero_price_assumption_usd=self._aero_price_assumption_usd,
            lookback=self._lookback,
        )

    def read_aero_price_usdc(self, block_number: int) -> Decimal:
        """Read the AERO price in USDC pinned to one block.

        Args:
            block_number: The block pinning the reserve read.

        Returns:
            The USDC price of one whole AERO token at that block.

        Raises:
            ExecutionUnavailableError: If the read cannot complete.
            ExecutorRpcRevertError: If a call reverts on-chain.
            ValueError: If the pool or a reserve is absent.
        """
        price = ExecutorRpcBackend(
            rpc_url=self._rpc_url, transport=self._transport, sleep=self._sleep
        ).fetch_aero_price_usdc(hex(block_number))
        # The per-run resolution becomes the assumption every pool's
        # emissions-history reconstruction replays under, unless the operator
        # overrode the price explicitly.
        self._aero_price_assumption_usd = price
        return price


def run_rehearsal(
    sources: RehearsalSources,
    symbols_by_token_address: Mapping[str, str],
    lookback: timedelta,
    aero_price_assumption_usd: Decimal | None,
    gas_price_assumption_gwei: Decimal,
    pool_addresses: frozenset[str] = frozenset(),
    apply_synthetic_stress: bool = True,
    width_selection: WidthSelectionMode = WidthSelectionMode.DERIVED_FROM_TARGET,
    progress: Callable[[str], None] | None = None,
) -> RehearsalRunReport:
    """Rehearse every discovered pool and assemble the run report.

    Args:
        sources: The read-only sources performing every network read.
        symbols_by_token_address: Stock symbol by normalized B20 token address.
        lookback: The reconstruction window every pool covers.
        aero_price_assumption_usd: Optional documented AERO price override;
            absent means the price is read live from Aerodrome's own
                       USDC/AERO pool at the anchor block, failing closed.
        gas_price_assumption_gwei: The documented constant Base gas price.
        pool_addresses: Optional normalized pool filter; every address must
            match a discovered pool.
        apply_synthetic_stress: Whether the documented synthetic dislocation
            schedule overlays the reference path.
        width_selection: Whether widths derive from the target-yield solver
            or sit at the locked ceiling as the v1 baseline.
        progress: Optional sink receiving one line per pool stage.

    Returns:
        The immutable run report with one ledger or failure per pool.

    Raises:
        RehearsalUnavailableError: If discovery does not verify, accepts no
            pools, carries no snapshot block, or the pool filter names a pool
            outside the verified discovery.
    """
    report_progress = progress if progress is not None else (lambda message: None)
    discovery = sources.discover_pools()
    if discovery.status is not PoolDiscoveryStatus.VERIFIED:
        raise RehearsalUnavailableError(
            "pool discovery did not verify: " + " ".join(discovery.diagnostics)
        )
    if discovery.snapshot_block is None:
        raise RehearsalUnavailableError(
            "verified discovery carried no snapshot block for the emissions anchor"
        )
    anchor_block = discovery.snapshot_block
    if aero_price_assumption_usd is None:
        try:
            aero_price_assumption_usd = sources.read_aero_price_usdc(anchor_block)
            report_progress(
                f"AERO price read live at anchor block {anchor_block}: "
                f"{aero_price_assumption_usd} USDC"
            )
        except (ExecutorRpcRevertError, ExecutionUnavailableError, ValueError) as error:
            raise RehearsalUnavailableError(
                f"the live AERO price read at anchor block {anchor_block} failed: {error}; "
                "pass --aero-price to assume a price explicitly"
            ) from error
    pools_by_address = {pool.pool_address: pool for pool in discovery.pools}
    if not pools_by_address:
        raise RehearsalUnavailableError(
            "pool discovery verified but accepted no B20/USDC pools; nothing to rehearse"
        )
    if pool_addresses:
        unknown_addresses = pool_addresses - pools_by_address.keys()
        if unknown_addresses:
            raise RehearsalUnavailableError(
                "pool filter names pools outside the verified discovery: "
                + ", ".join(sorted(unknown_addresses))
            )
        selected = [pools_by_address[address] for address in sorted(pool_addresses)]
    else:
        # Address order keeps multi-pool runs deterministic regardless of source order.
        selected = [pools_by_address[address] for address in sorted(pools_by_address)]
    ledgers: list[PoolRehearsalLedger] = []
    failures: list[PoolRehearsalFailure] = []
    for pool in selected:
        stock = stock_token_address(pool)
        symbol = symbols_by_token_address.get(stock, "")
        if not symbol:
            failures.append(
                PoolRehearsalFailure(
                    pool_address=pool.pool_address,
                    token_address=stock,
                    symbol="unknown",
                    diagnostic="the official B20 registry carries no symbol for the stock token",
                )
            )
            continue
        try:
            report_progress(f"{symbol}: reading token decimals")
            token_decimals = sources.read_token_decimals(stock)
            quote_decimals = sources.read_token_decimals(QUOTE_TOKEN_ADDRESS)
            report_progress(f"{symbol}: reconstructing the price path")
            price_path = sources.fetch_price_path(pool, token_decimals, quote_decimals)
            report_progress(f"{symbol}: reconstructing the emissions history")
            emissions_history = sources.fetch_emissions_history(
                pool,
                anchor_block,
                anchor_staked_value_usd(pool, token_decimals, quote_decimals),
            )
            report_progress(f"{symbol}: replaying the policy")
            ledger = replay_pool(
                price_path=price_path,
                emissions_history=emissions_history,
                symbol=symbol,
                assumptions=RehearsalAssumptions(
                    aero_price_assumption_usd=aero_price_assumption_usd,
                    gas_price_assumption_gwei=gas_price_assumption_gwei,
                    pool_fee_ppm=pool.pool_fee_ppm,
                ),
                schedule=(
                    DEFAULT_SYNTHETIC_DISLOCATION_SCHEDULE if apply_synthetic_stress else None
                ),
                width_selection=width_selection,
            )
        except (HistoryUnavailableError, ValueError) as error:
            report_progress(f"{symbol}: failed closed - {error}")
            failures.append(
                PoolRehearsalFailure(
                    pool_address=pool.pool_address,
                    token_address=stock,
                    symbol=symbol,
                    diagnostic=str(error),
                )
            )
            continue
        report_progress(
            f"{symbol}: {ledger.observation_count} observations, pnl {ledger.pnl_usd} USDC"
        )
        ledgers.append(ledger)
    return RehearsalRunReport(
        generated_at=datetime.now(UTC),
        discovery_source=discovery.source,
        discovery_block=anchor_block,
        discovered_pool_count=len(discovery.pools),
        lookback=lookback,
        aero_price_assumption_usd=aero_price_assumption_usd,
        gas_price_assumption_gwei=gas_price_assumption_gwei,
        synthetic_stress=apply_synthetic_stress,
        width_selection=width_selection,
        ledgers=tuple(ledgers),
        failures=tuple(failures),
    )


def _format_ledger_window(ledger: PoolRehearsalLedger) -> str:
    """Format one ledger's replay window for the summary line.

    Args:
        ledger: The ledger whose window bounds are formatted.

    Returns:
        A readable inclusive window range, or a placeholder for empty paths.
    """
    if ledger.window_start is None or ledger.window_end is None:
        return "empty window"
    return f"{ledger.window_start:%Y-%m-%d %H:%M}..{ledger.window_end:%Y-%m-%d %H:%M} UTC"


def print_report_summary(report: RehearsalRunReport, output_path: Path) -> None:
    """Print one human-readable summary line per rehearsed pool.

    Args:
        report: The run report whose outcomes are summarized.
        output_path: The path the full JSON report was written to.
    """
    for ledger in report.ledgers:
        counts = ledger.action_counts
        print(
            f"{ledger.symbol}: window {_format_ledger_window(ledger)} | "
            f"observations {ledger.observation_count} | "
            f"final equity {ledger.final_equity_usd:.2f} USDC | "
            f"pnl {ledger.pnl_usd:.2f} ({ledger.return_fraction * 100:.2f}%) | "
            f"entries {counts.entries} recenters {counts.recenters} "
            f"stops {counts.stop_outs} dilution {counts.dilution_exits} "
            f"events {counts.event_exits} dislocation {counts.dislocation_exits} "
            f"stale-low {counts.stale_low_burns} defensive {counts.defensive_exits} "
            f"gas-deferrals {counts.gas_deferrals} | "
            f"fees {ledger.fees_accrued_usd:.4f} AERO {ledger.aero_accrued_units:.4f} | "
            f"impact {ledger.total_swap_impact_cost_usd:.4f} "
            f"gas {ledger.total_gas_cost_usd:.4f} | "
            f"{ledger.emissions_reconstruction_mode}/{ledger.reference_mode}",
            flush=True,
        )
    for failure in report.failures:
        print(f"{failure.symbol}: FAILED - {failure.diagnostic}", flush=True)
    print(
        f"wrote {len(report.ledgers)} ledgers and {len(report.failures)} failures to {output_path}",
        flush=True,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the rehearsal command's argument parser.

    Returns:
        The configured parser for the aero-bot-rehearse command.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-rehearse",
        description=(
            "Replay the locked emissions-farming policy over reconstructed onchain "
            "history and write per-pool P&L ledgers as one JSON report."
        ),
    )
    parser.add_argument(
        "--lookback-days",
        type=Decimal,
        default=Decimal(REHEARSAL_LOOKBACK.days),
        help=(
            "Days of onchain history reconstructed per pool "
            f"(default: {REHEARSAL_LOOKBACK.days}). Multi-week windows take "
            "hours against the public RPC."
        ),
    )
    parser.add_argument(
        "--aero-price",
        type=Decimal,
        default=None,
        help=(
            "USD price override assumed per accrued AERO for reproducibility; "
            "absent means the price is read live from Aerodrome's own "
            "USDC/AERO pool at the anchor block (fails closed)."
        ),
    )
    parser.add_argument(
        "--gas-price-gwei",
        type=Decimal,
        default=DEFAULT_GAS_PRICE_ASSUMPTION_GWEI,
        help="Constant Base gas price in gwei behind every batch cost (default: 0.001).",
    )
    parser.add_argument(
        "--pool",
        action="append",
        default=[],
        dest="pool_addresses",
        help="Pool contract address to rehearse; repeatable. Default: every discovered pool.",
    )
    parser.add_argument(
        "--no-synthetic-stress",
        action="store_false",
        dest="synthetic_stress",
        help=(
            "Replay against the plain AMM-equals-reference path without the "
            "documented synthetic dislocation episodes."
        ),
    )
    parser.add_argument(
        "--fixed-ceiling-width",
        action="store_true",
        dest="fixed_ceiling_width",
        help=(
            "Run the v1 baseline: every entry and recenter uses the locked "
            "0.3-percent ceiling width instead of the target-yield-derived solve."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Path of the JSON report written at the end (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--header-batch-size",
        type=int,
        default=MAX_HEADER_BATCH_SIZE,
        help=(
            "Block-header reads grouped per JSON-RPC batch "
            f"(default: {MAX_HEADER_BATCH_SIZE}, the public endpoint's cap)."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one rehearsal from the command line.

    The RPC endpoint and Sugar address come from the application settings, so
    the documented environment overrides apply unchanged.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero when every selected pool produced a ledger.
    """
    settings = Settings()
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.lookback_days <= 0:
        parser.error("--lookback-days must be positive")
    if arguments.aero_price is not None and arguments.aero_price <= 0:
        parser.error("--aero-price must be positive")
    if arguments.gas_price_gwei <= 0:
        parser.error("--gas-price-gwei must be positive")
    if not 1 <= arguments.header_batch_size <= MAX_HEADER_BATCH_SIZE:
        parser.error(f"--header-batch-size must be between 1 and {MAX_HEADER_BATCH_SIZE}")
    lookback = timedelta(days=float(arguments.lookback_days))
    registry = load_official_b20_registry()
    symbols_by_token_address = {
        normalize_evm_address(asset.address): asset.symbol for asset in registry.assets
    }
    sources = LiveRehearsalSources(
        rpc_url=settings.base_rpc_url,
        sugar_address=settings.lp_sugar_address,
        b20_addresses=frozenset(asset.address for asset in registry.assets),
        lookback=lookback,
        aero_price_assumption_usd=arguments.aero_price,
        header_batch_size=arguments.header_batch_size,
    )
    try:
        report = run_rehearsal(
            sources=sources,
            symbols_by_token_address=symbols_by_token_address,
            lookback=lookback,
            aero_price_assumption_usd=arguments.aero_price,
            gas_price_assumption_gwei=arguments.gas_price_gwei,
            pool_addresses=frozenset(
                normalize_evm_address(address) for address in arguments.pool_addresses
            ),
            apply_synthetic_stress=arguments.synthetic_stress,
            width_selection=(
                WidthSelectionMode.FIXED_CEILING_BASELINE
                if arguments.fixed_ceiling_width
                else WidthSelectionMode.DERIVED_FROM_TARGET
            ),
            progress=lambda message: print(message, flush=True),
        )
    except RehearsalUnavailableError as error:
        print(f"rehearsal unavailable: {error}", file=sys.stderr, flush=True)
        return 1
    # A nested operator-supplied path is created rather than crashing the run.
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print_report_summary(report, arguments.output)
    return 0 if report.ledgers and not report.failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
