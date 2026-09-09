"""The decision-only strategy E2E surface: one live policy verdict per command.

This module assembles one complete ``PolicyObservation`` from live Base reads -
the same Sugar discovery, corrected emissions-APR convention, and depth
estimates the executor surfaces use - and folds it through the locked
``PolicyEngine`` exactly as the runtime would, then reports the typed verdict.
Nothing is built, signed, estimated for broadcast, or executed: the command's
product is the decision itself plus one ``policy_decision`` audit record.

Two honest gaps are visible by design rather than hidden: the real-market
reference quote is not wired live yet (pass ``--reference-price`` to exercise
complete verdicts; without it every entry blocks fail-closed as
``reference_stale``), and the fee APR stays at zero because a live fee-evidence
window needs the same price-path machinery the rehearsal reconstructs. Both are
post-reassessment scope.
"""

import argparse
import os
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.config import Settings
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.emissions_apr import aerodrome_display_emissions_apr
from aero_bot.executor import (
    DEFAULT_CANARY_SAFE_ADDRESS,
    SAFE_ADDRESS_ENV,
    ExecutionUnavailableError,
    ExecutorRpcBackend,
    LiveExecutionSources,
)
from aero_bot.history import price_usdc_per_stock
from aero_bot.known_pool import (
    persist_decision_pool_pin,
    resolve_known_pool_candidate,
)
from aero_bot.lp_pins import LpPoolPinStore
from aero_bot.lp_plan import estimate_in_range_depth_usdc
from aero_bot.policy import (
    LOCKED_POLICY_PARAMETERS,
    EventWindowView,
    PolicyEngine,
    PolicyObservation,
    PolicyOutcome,
    PolicyState,
    evaluate_event_window,
    load_event_calendar,
)
from aero_bot.registry import RegistryStatus
from aero_bot.venues import (
    BASE_USDC_ADDRESS,
    PoolCandidate,
    PoolDiscoveryStatus,
    aerodrome_contract_evidence,
)

# The Safe whose live balances default the equity input.
# One gwei is one billion wei; the observation carries gwei.
WEI_PER_GWEI = 1_000_000_000


class StrategyDecisionReport(BaseModel):
    """Carry one decision-only run's complete evidence and verdict."""

    # Frozen strict fields keep the verdict bound to its observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The registry-matched symbol the decision covers.
    symbol: str
    # The pool the observation came from.
    pool_address: EvmAddress
    # The snapshot block anchoring every live input.
    snapshot_block: int
    # When the observation assembled, timezone-aware.
    observed_at: datetime
    # The AMM price in USDC per stock at the snapshot.
    amm_price_usdc: Decimal
    # The corrected-convention emissions APR as a decimal fraction.
    emissions_apr: Decimal
    # The live AERO price the quote used.
    aero_price_usdc: Decimal
    # The executable in-range depth in USDC.
    pool_depth_usd: Decimal
    # The equity the decision sized against, in USDC.
    equity_usd: Decimal
    # The Base gas price in gwei; None when the read was unavailable.
    gas_price_gwei: Decimal | None
    # The injected real-market reference quote, when one was supplied.
    reference_price_usdc: Decimal | None
    # The active event-window view at the observation instant.
    event_window: EventWindowView
    # The engine's typed verdict with its diagnostics.
    outcome: PolicyOutcome
    # Explicit notes on the honest input gaps this run carried.
    input_notes: Annotated[tuple[str, ...], Field(min_length=1)]


class StrategyDecisionPayload(BaseModel):
    """Audit payload for one decision-only run; no credential-bearing fields."""

    # Frozen strict fields keep the audit bound to the reported verdict.
    model_config = ConfigDict(frozen=True)

    # Decision-only mode marker for the audit chain.
    mode: str = "decision_only"
    # The registry-matched symbol the decision covers.
    symbol: str
    # The pool the observation came from.
    pool_address: EvmAddress
    # The snapshot block anchoring every live input.
    snapshot_block: int
    # The chosen action, e.g. enter or hold.
    action: str
    # The stable primary reason for the action.
    reason: str
    # The corrected-convention emissions APR as a decimal fraction.
    emissions_apr: str
    # The AMM price in USDC per stock at the snapshot.
    amm_price_usdc: str


class StrategySources(Protocol):
    """Define the live reads one decision-only run consumes."""

    def resolve_pool(self, symbol: str) -> tuple[PoolCandidate, int]:
        """Return the symbol's verified pool with its snapshot block.

        Args:
            symbol: The registry-matched stock symbol, like AAPLc.

        Returns:
            The verified B20/USDC pool candidate for the symbol and the
            block pinning its snapshot.
        """
        ...

    def registry_paused(self) -> bool:
        """Return whether the official B20 registry is not verified."""
        ...

    def symbol_address(self, symbol: str) -> str | None:
        """Resolve one registry symbol to its B20 contract address."""
        ...

    def token_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count."""
        ...

    def aero_price(self, block_number: int) -> Decimal:
        """Read the live AERO price pinned to one block."""
        ...

    def gas_price_gwei(self) -> Decimal | None:
        """Read the Base gas price in gwei; None when unavailable."""
        ...

    def token_balance(self, token_address: str, owner_address: str) -> int:
        """Read one ERC20 balance."""
        ...


class LiveStrategySources:
    """Compose the live reads one decision-only run consumes."""

    def __init__(
        self,
        rpc_url: str,
        sugar_address: str,
        transport: httpx.BaseTransport | None = None,
        pool_pin_store: LpPoolPinStore | None = None,
        progress: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the discovery and RPC sources.

        Args:
            rpc_url: Base JSON-RPC endpoint for reads.
            sugar_address: LP Sugar contract anchoring discovery.
            transport: Optional injected HTTP transport for tests.
            pool_pin_store: Optional local store of Sugar-verified pool
                identities arming the known-pool fast path.
            progress: Optional callback receiving one human-readable line per
                enumerated page and per retried request during a Sugar sweep.
            sleep: Optional injected delay for the RPC backend's politeness
                pacing and retry backoff.
            timer: Optional injected monotonic clock for the same backend.
        """
        self._execution_sources = LiveExecutionSources(
            rpc_url=rpc_url,
            sugar_address=sugar_address,
            transport=transport,
            progress=progress,
        )
        self._rpc = ExecutorRpcBackend(
            rpc_url=rpc_url,
            transport=transport,
            progress=progress,
            sleep=sleep,
            timer=timer,
        )
        self._pool_pin_store = pool_pin_store
        self._progress = progress

    def resolve_pool(self, symbol: str) -> tuple[PoolCandidate, int]:
        """Return the symbol's verified pool with its snapshot block.

        A persisted pool pin takes the known-pool fast path: the identity is
        re-verified live against the pool contract's own views, the factory
        is re-checked against the official allowlist, the gauge's kill
        switch is read through the factory's Voter, and the live state is
        read at one freshly pinned block - a handful of reads instead of a
        full Sugar enumeration. Any mismatch or unreadable view falls back
        to the full sweep, which re-verifies everything the slow way and
        refreshes the pin. Without a usable pin the sweep itself runs and
        its verified result is persisted so the next run is fast.

        Args:
            symbol: The registry-matched stock symbol, like AAPLc.

        Returns:
            The verified B20/USDC pool candidate for the symbol and the
            block pinning its snapshot.

        Raises:
            ExecutionUnavailableError: If discovery cannot complete or does
                not verify.
            ValueError: If the symbol is unknown or has no discovered pool.
        """
        registry = self._execution_sources.load_registry()
        listing = next(
            (asset for asset in registry.assets if asset.symbol.lower() == symbol.strip().lower()),
            None,
        )
        if listing is None:
            raise ValueError(f"symbol {symbol!r} is not in the official B20 registry")
        if self._pool_pin_store is not None:
            pin = self._pool_pin_store.load().get(listing.symbol.strip().lower())
            if pin is not None:
                try:
                    candidate, block = resolve_known_pool_candidate(
                        self._rpc, pin, listing, aerodrome_contract_evidence()
                    )
                except (ExecutionUnavailableError, ValueError) as error:
                    if self._progress is not None:
                        self._progress(f"known-pool fast path fell back to full discovery: {error}")
                else:
                    return candidate, block
        result = self._execution_sources.discover_pools()
        if result.status is not PoolDiscoveryStatus.VERIFIED:
            raise ExecutionUnavailableError(
                "pool discovery did not verify: " + " ".join(result.diagnostics)
            )
        if result.snapshot_block is None:
            raise ExecutionUnavailableError("verified discovery carried no snapshot block")
        normalized_token = listing.address.lower()
        normalized_usdc = BASE_USDC_ADDRESS.lower()
        pool = next(
            (
                candidate
                for candidate in result.pools
                if normalized_usdc
                in (candidate.token0_address.lower(), candidate.token1_address.lower())
                and normalized_token
                in (candidate.token0_address.lower(), candidate.token1_address.lower())
            ),
            None,
        )
        if pool is None:
            raise ValueError(f"symbol {symbol!r} has no discovered B20/USDC pool")
        if self._pool_pin_store is not None:
            persist_decision_pool_pin(
                self._pool_pin_store,
                listing,
                pool,
                self._execution_sources.read_token_decimals(listing.address),
                result.snapshot_block,
                datetime.now(UTC),
                result.source,
            )
        return pool, result.snapshot_block

    def registry_paused(self) -> bool:
        """Return whether the official B20 registry is not verified."""
        registry = self._execution_sources.load_registry()
        return registry.status is not RegistryStatus.VERIFIED

    def symbol_address(self, symbol: str) -> str | None:
        """Resolve one registry symbol to its B20 contract address."""
        registry = self._execution_sources.load_registry()
        listing = next(
            (asset for asset in registry.assets if asset.symbol.lower() == symbol.strip().lower()),
            None,
        )
        return listing.address if listing is not None else None

    def token_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count."""
        return self._execution_sources.read_token_decimals(token_address)

    def aero_price(self, block_number: int) -> Decimal:
        """Read the live AERO price pinned to one block."""
        return self._rpc.fetch_aero_price_usdc(hex(block_number))

    def gas_price_gwei(self) -> Decimal | None:
        """Read the endpoint's gas price in gwei; None when unavailable."""
        try:
            return Decimal(self._rpc.fetch_gas_price()) / Decimal(WEI_PER_GWEI)
        except ExecutionUnavailableError:
            return None

    def token_balance(self, token_address: str, owner_address: str) -> int:
        """Read one ERC20 balance for the equity default."""
        return self._rpc.fetch_token_balance(token_address, owner_address)


def assemble_observation(
    sources: StrategySources,
    symbol: str,
    pool: PoolCandidate,
    snapshot_block: int,
    observed_at: datetime,
    equity_usd: Decimal | None,
    reference_price_usdc: Decimal | None,
    reference_age_seconds: int | None,
    safe_address: str,
) -> tuple[PolicyObservation, Decimal, Decimal, tuple[str, ...]]:
    """Assemble one complete policy observation from live pool state.

    Args:
        sources: The live reads backing every input.
        symbol: The registry-matched stock symbol.
        pool: The discovered candidate the decision applies to.
        snapshot_block: The block pinning the discovery snapshot.
        observed_at: The assembly instant, timezone-aware.
        equity_usd: Optional equity override; the default is the Safe's live
            USDC plus stock value at the snapshot price.
        reference_price_usdc: Optional injected real-market quote.
        reference_age_seconds: Age of the injected quote; defaults to zero.
        safe_address: The Safe whose balances default the equity input.

    Returns:
        The observation, the corrected emissions APR, the live AERO price,
        and the honest input notes the report should carry.

    Raises:
        ValueError: If any input leaves the observation undefined.
    """
    notes: list[str] = []
    normalized_usdc = BASE_USDC_ADDRESS.lower()
    stock_token = (
        pool.token1_address
        if pool.token0_address.lower() == normalized_usdc
        else pool.token0_address
    )
    stock_is_token0 = pool.token0_address.lower() != normalized_usdc
    decimals = sources.token_decimals(stock_token)
    price = price_usdc_per_stock(pool.sqrt_ratio, stock_is_token0, decimals, 6)
    aero_price = sources.aero_price(snapshot_block)
    emissions_apr = aerodrome_display_emissions_apr(
        pool.emissions_per_second,
        aero_price,
        pool.staked0,
        pool.staked1,
        stock_is_token0,
        decimals,
        6,
        price,
    )
    depth = estimate_in_range_depth_usdc(
        pool.sqrt_ratio,
        pool.tick_spacing,
        pool.current_tick,
        pool.pool_active_liquidity,
        stock_is_token0,
        decimals,
        6,
    )
    if equity_usd is None:
        usdc_balance = Decimal(sources.token_balance(BASE_USDC_ADDRESS, safe_address)) / Decimal(
            10**6
        )
        stock_balance = Decimal(sources.token_balance(stock_token, safe_address)) / Decimal(
            10**decimals
        )
        equity_usd = +(usdc_balance + stock_balance * price)
        notes.append(
            f"equity defaulted to the Safe's live {equity_usd} USDC "
            f"({usdc_balance} USDC plus stock valued at the snapshot price)"
        )
    if reference_price_usdc is None:
        notes.append(
            "no live real-market reference quote is wired yet; entries block "
            "fail-closed as reference_stale unless --reference-price injects one"
        )
    notes.append(
        "fee APR stays zero because a live fee-evidence window needs the "
        "price-path machinery the rehearsal reconstructs"
    )
    notes.append(
        "oracle staleness is not yet wired live; the oracle-health layer is post-reassessment scope"
    )
    observation = PolicyObservation(
        observed_at=observed_at,
        pool_address=pool.pool_address,
        token_address=stock_token,
        amm_price_usdc=price,
        emissions_apr=emissions_apr,
        fee_apr=Decimal("0"),
        pool_depth_usd=depth,
        equity_usd=equity_usd,
        reference_price_usdc=reference_price_usdc,
        reference_age_seconds=reference_age_seconds,
        oracle_stale=False,
        registry_paused=sources.registry_paused(),
        gas_price_gwei=sources.gas_price_gwei(),
        ranging=None,
    )
    return observation, emissions_apr, aero_price, tuple(notes)


def run_decision(
    sources: LiveStrategySources | StrategySources,
    symbol: str,
    equity_usd: Decimal | None,
    reference_price_usdc: Decimal | None,
    reference_age_seconds: int | None,
    safe_address: str,
    observed_at: datetime | None = None,
) -> StrategyDecisionReport:
    """Run one decision-only verdict for one symbol.

    Args:
        sources: The live reads backing every input.
        symbol: The registry-matched stock symbol, like AAPLc.
        equity_usd: Optional equity override.
        reference_price_usdc: Optional injected real-market quote.
        reference_age_seconds: Age of the injected quote.
        safe_address: The Safe whose balances default the equity input.
        observed_at: Optional fixed instant for tests; defaults to now.

    Returns:
        The complete decision report.

    Raises:
        ValueError: If the symbol has no discovered pool.
        ExecutionUnavailableError: If discovery or a read fails.
    """
    pool, snapshot_block = sources.resolve_pool(symbol)
    token = sources.symbol_address(symbol)
    if token is None:
        raise ValueError(f"symbol {symbol!r} is not in the official B20 registry")
    now = observed_at if observed_at is not None else datetime.now(UTC)
    observation, emissions_apr, aero_price, notes = assemble_observation(
        sources,
        symbol,
        pool,
        snapshot_block,
        now,
        equity_usd,
        reference_price_usdc,
        reference_age_seconds,
        safe_address,
    )
    engine = PolicyEngine(LOCKED_POLICY_PARAMETERS, load_event_calendar())
    window = evaluate_event_window(
        observation.observed_at, observation.token_address, engine.calendar
    )
    outcome = engine.decide(PolicyState(), observation)
    return StrategyDecisionReport(
        symbol=symbol,
        pool_address=pool.pool_address,
        snapshot_block=snapshot_block,
        observed_at=observation.observed_at,
        amm_price_usdc=observation.amm_price_usdc,
        emissions_apr=emissions_apr,
        aero_price_usdc=aero_price,
        pool_depth_usd=observation.pool_depth_usd,
        equity_usd=observation.equity_usd,
        gas_price_gwei=observation.gas_price_gwei,
        reference_price_usdc=reference_price_usdc,
        event_window=window,
        outcome=outcome,
        input_notes=notes,
    )


def _print_report(report: StrategyDecisionReport) -> None:
    """Print one decision report's human summary.

    Args:
        report: The decision-only report being printed.
    """
    decision = report.outcome.decision
    print(
        f"{report.symbol} pool {report.pool_address} at block {report.snapshot_block}, "
        f"observed {report.observed_at.isoformat()}"
    )
    print(
        f"amm price {report.amm_price_usdc} USDC per stock, emissions APR "
        f"{Decimal(100) * report.emissions_apr:.2f}% (AERO {report.aero_price_usdc:.4f}), "
        f"depth {report.pool_depth_usd} USDC, equity {report.equity_usd} USDC"
    )
    print(
        f"event window: {report.event_window.description}"
        + ("" if not report.event_window.active else " (flat verdict is correct behavior)")
    )
    print(f"verdict: {decision.action.value} ({decision.reason.value})")
    for diagnostic in decision.diagnostics:
        print(f"  - {diagnostic}")
    for note in report.input_notes:
        print(f"  note: {note}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one decision-only strategy verdict.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on any verdict (a hold is a decision, not
        a failure), one when live inputs cannot be assembled.
    """
    settings = Settings()
    parser = argparse.ArgumentParser(
        prog="aero-bot-decide",
        description=(
            "Run one complete policy-engine verdict from live Base reads; "
            "decision-only - nothing is built, signed, or broadcast."
        ),
    )
    parser.add_argument("--symbol", required=True, help="Registry symbol like AAPLc.")
    parser.add_argument(
        "--equity-usdc",
        type=Decimal,
        default=None,
        help=(
            "Equity the decision sizes against; defaults to the Safe's live "
            "USDC plus stock value at the snapshot price."
        ),
    )
    parser.add_argument(
        "--reference-price",
        type=Decimal,
        default=None,
        help=(
            "Optional injected real-market quote in USDC per stock; without it "
            "entries block fail-closed as reference_stale because no live "
            "reference feed is wired yet."
        ),
    )
    parser.add_argument(
        "--reference-age-seconds",
        type=int,
        default=0,
        help="Age of the injected reference quote in seconds (default: 0).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete report as JSON instead of a summary.",
    )
    arguments = parser.parse_args(argv)
    if arguments.equity_usdc is not None and arguments.equity_usdc <= 0:
        parser.error("--equity-usdc must be positive")
    if arguments.reference_price is not None and arguments.reference_price <= 0:
        parser.error("--reference-price must be positive")
    if arguments.reference_age_seconds < 0:
        parser.error("--reference-age-seconds must be non-negative")
    safe_address = normalize_evm_address(
        os.environ.get(SAFE_ADDRESS_ENV, DEFAULT_CANARY_SAFE_ADDRESS)
    )
    sources = LiveStrategySources(
        rpc_url=settings.base_rpc_url,
        sugar_address=settings.lp_sugar_address,
        pool_pin_store=LpPoolPinStore(settings.lp_pool_pins_path),
        progress=lambda line: print(line, file=sys.stderr),
    )
    try:
        report = run_decision(
            sources,
            arguments.symbol,
            arguments.equity_usdc,
            arguments.reference_price,
            arguments.reference_age_seconds,
            safe_address,
        )
    except (ExecutionUnavailableError, ValueError) as error:
        print(f"decision unavailable: {error}", file=sys.stderr)
        return 1
    try:
        audit_store = AuditStore(settings.audit_database_path)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"the audit store is unavailable: {error}", file=sys.stderr)
        return 1
    decision = report.outcome.decision
    audit_store.append(
        AuditEventType.POLICY_DECISION,
        StrategyDecisionPayload(
            symbol=report.symbol,
            pool_address=report.pool_address,
            snapshot_block=report.snapshot_block,
            action=decision.action.value,
            reason=decision.reason.value,
            emissions_apr=str(report.emissions_apr),
            amm_price_usdc=str(report.amm_price_usdc),
        ),
        report.observed_at,
    )
    if arguments.json:
        print(report.model_dump_json(indent=2))
    else:
        _print_report(report)
    return 0
