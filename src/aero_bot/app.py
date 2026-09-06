"""FastAPI application factory and wallet-free dashboard routes."""

from html import escape
from importlib.resources import files
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from aero_bot.concentrated import (
    ConcentratedLiquidityAnalyzer,
    ConcentratedPositionAnalysis,
    ConcentratedPositionSnapshot,
)
from aero_bot.config import Settings
from aero_bot.domain import OpportunitySnapshot, RiskDecision, RiskPolicy
from aero_bot.oracles import (
    ChainlinkCoverageReport,
    OracleCoverageStatus,
    unavailable_chainlink_coverage,
)
from aero_bot.registry import B20RegistryResult, RegistryStatus, load_official_b20_registry
from aero_bot.risk import RiskEngine
from aero_bot.transactions import (
    AllowancePlanResult,
    ExactAllowanceRequest,
    PlanSimulationResult,
    TransactionCapabilities,
    TransactionPlanner,
    UnsignedTransactionPlan,
)
from aero_bot.venues import (
    AerodromeVenueAdapter,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
)

# The product version is shown in API metadata and machine-readable health output.
APP_VERSION = "0.1.0"
# Aerodrome is the sole explicitly enabled venue for the first release.
ENABLED_VENUE: Literal["aerodrome"] = "aerodrome"
# The Base mainnet chain identifier prevents ambiguity about the target network.
BASE_CHAIN_ID = 8453


class HealthResponse(BaseModel):
    """Describe the process and immutable safety boundary for health checks."""

    # Status indicates that the HTTP process is accepting requests.
    status: Literal["ok"]
    # Version identifies the running application build.
    version: str
    # Environment confirms that only the local operating mode is active.
    environment: Literal["local"]
    # Chain ID identifies Base mainnet as the only target network.
    chain_id: int
    # Enabled venues makes the strict venue boundary machine-readable.
    enabled_venues: tuple[Literal["aerodrome"], ...]
    # Execution mode confirms that signing and broadcasting are unavailable.
    execution_mode: Literal["simulation_only"]


def get_settings() -> Settings:
    """Load validated application settings from the local environment."""
    return Settings()


def create_app(
    settings: Settings | None = None,
    b20_registry: B20RegistryResult | None = None,
    pool_discovery: PoolDiscoveryResult | None = None,
    oracle_coverage: ChainlinkCoverageReport | None = None,
    transaction_planner: TransactionPlanner | None = None,
    risk_engine: RiskEngine | None = None,
) -> FastAPI:
    """Create an application instance with explicit local safety metadata.

    Args:
        settings: Optional validated local configuration for this application instance.
        b20_registry: Optional preloaded official registry result for deterministic tests.
        pool_discovery: Optional preloaded Aerodrome discovery result for deterministic tests.
        oracle_coverage: Optional preloaded Chainlink coverage for deterministic tests.
        transaction_planner: Optional policy-bound wallet-free transaction planner.
        risk_engine: Optional explicit deterministic policy engine for this application.

    Returns:
        A configured FastAPI application with dashboard and diagnostic routes.
    """
    # The resolved settings allow tests and local launch code to share the same factory.
    resolved_settings = settings or get_settings()
    # The resolved registry validates packaged official evidence without making a network request.
    resolved_registry = b20_registry or load_official_b20_registry()
    # Only verified issuer records are supplied to the strict venue adapter.
    official_b20_addresses = frozenset(asset.address for asset in resolved_registry.assets)
    # Without an injected live result, the adapter emits an explicit no-RPC diagnostic.
    resolved_pool_discovery = pool_discovery or AerodromeVenueAdapter().discover_pools(
        official_b20_addresses
    )
    # Without injected observations, oracle output explicitly reports the missing trust inputs.
    resolved_oracle_coverage = oracle_coverage or unavailable_chainlink_coverage(
        expected_assets=len(official_b20_addresses)
    )
    # Default transaction policy is emergency-halted with no targets or live backend.
    resolved_transaction_planner = transaction_planner or TransactionPlanner()
    # Pure Decimal analysis is stateless and shares no wallet or network capability.
    concentrated_analyzer = ConcentratedLiquidityAnalyzer()
    # Default policy is emergency-halted with empty token and pool allowlists.
    resolved_risk_engine = risk_engine or RiskEngine(RiskPolicy())
    # The FastAPI instance owns this process's routes and OpenAPI metadata.
    application = FastAPI(
        title=resolved_settings.app_name,
        version=APP_VERSION,
        description="Wallet-free Aerodrome LP analysis on Base",
    )
    # Storing immutable settings on app state makes the active configuration inspectable.
    application.state.settings = resolved_settings
    # Storing the immutable result keeps every route on one coherent evidence snapshot.
    application.state.b20_registry = resolved_registry
    # Storing pool evidence gives every route the same all-or-nothing discovery outcome.
    application.state.pool_discovery = resolved_pool_discovery
    # Storing oracle evidence keeps API and dashboard health claims on one snapshot.
    application.state.oracle_coverage = resolved_oracle_coverage
    # Storing the planner keeps all requests behind one immutable safety policy.
    application.state.transaction_planner = resolved_transaction_planner
    # Storing the engine keeps every request behind the same immutable risk policy.
    application.state.risk_engine = resolved_risk_engine

    @application.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        """Return process health and the active execution constraints."""
        return HealthResponse(
            status="ok",
            version=APP_VERSION,
            environment=resolved_settings.environment,
            chain_id=BASE_CHAIN_ID,
            enabled_venues=(ENABLED_VENUE,),
            execution_mode="simulation_only",
        )

    @application.get("/api/registry/b20", response_model=B20RegistryResult)
    def official_b20_registry() -> B20RegistryResult:
        """Return verified Coinbase-issued B20 identities or fail-closed diagnostics."""
        return resolved_registry

    @application.get("/api/venues/aerodrome/pools", response_model=PoolDiscoveryResult)
    def aerodrome_pools() -> PoolDiscoveryResult:
        """Return validated B20/USDC pools or an evidence-backed discovery diagnostic."""
        return resolved_pool_discovery

    @application.get("/api/oracles/chainlink", response_model=ChainlinkCoverageReport)
    def chainlink_oracle_coverage() -> ChainlinkCoverageReport:
        """Return evaluated B20 feed health or an evidence-backed connector diagnostic."""
        return resolved_oracle_coverage

    @application.get("/api/transactions/capabilities", response_model=TransactionCapabilities)
    def transaction_capabilities() -> TransactionCapabilities:
        """Return immutable wallet, signing, broadcast, and simulation capabilities."""
        return resolved_transaction_planner.capabilities()

    @application.post("/api/transactions/plan/exact-allowance", response_model=AllowancePlanResult)
    def plan_exact_allowance(request: ExactAllowanceRequest) -> AllowancePlanResult:
        """Plan an allowlisted exact approval without wallet access or signing."""
        return resolved_transaction_planner.plan_exact_allowance(request)

    @application.post("/api/transactions/simulate", response_model=PlanSimulationResult)
    def simulate_transaction_plan(plan: UnsignedTransactionPlan) -> PlanSimulationResult:
        """Revalidate and submit an unsigned plan to read-only simulation only."""
        return resolved_transaction_planner.simulate(plan)

    @application.post(
        "/api/analysis/concentrated-position", response_model=ConcentratedPositionAnalysis
    )
    def analyze_concentrated_position(
        snapshot: ConcentratedPositionSnapshot,
    ) -> ConcentratedPositionAnalysis:
        """Calculate deterministic Slipstream inventory and loss evidence."""
        return concentrated_analyzer.analyze(snapshot)

    @application.post("/api/risk/evaluate", response_model=RiskDecision)
    def evaluate_risk(snapshot: OpportunitySnapshot) -> RiskDecision:
        """Return a deterministic hold or eligible decision without execution."""
        return resolved_risk_engine.evaluate(snapshot)

    @application.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        """Render the initial local dashboard with honest connector status."""
        return _dashboard_html(
            resolved_settings.app_name,
            resolved_registry,
            resolved_pool_discovery,
            resolved_oracle_coverage,
            resolved_transaction_planner.capabilities(),
        )

    return application


def _dashboard_html(
    app_name: str,
    registry: B20RegistryResult,
    pool_discovery: PoolDiscoveryResult,
    oracle_coverage: ChainlinkCoverageReport,
    transaction_capabilities: TransactionCapabilities,
) -> str:
    """Build the dependency-free dashboard shell for the local first release.

    Args:
        app_name: Validated local product title.
        registry: Official B20 registry evidence or fail-closed diagnostic.
        pool_discovery: Validated Aerodrome pools or fail-closed diagnostic.
        oracle_coverage: Chainlink B20 coverage evidence or fail-closed diagnostic.
        transaction_capabilities: Immutable wallet-free capability declaration.

    Returns:
        A complete HTML document with escaped dynamic evidence.
    """
    # Escaping protects the HTML document even when a local operator customizes the title.
    display_name = escape(app_name)
    # The packaged template keeps presentation markup separate from application behavior.
    dashboard_template = files("aero_bot").joinpath("dashboard.html").read_text(encoding="utf-8")
    # Verified evidence receives a positive visual status while failures remain visibly pending.
    registry_tone = "good" if registry.status is RegistryStatus.VERIFIED else "pending"
    # The short status label supports scanning without hiding the detailed diagnostic.
    registry_status = "Source verified" if registry.status is RegistryStatus.VERIFIED else "Blocked"
    # Escaping the diagnostic protects HTML if a local filesystem error includes special characters.
    registry_diagnostic = escape(registry.diagnostic)
    # Verified live pools receive a positive status while missing or rejected data stays blocked.
    pool_tone = "good" if pool_discovery.status is PoolDiscoveryStatus.VERIFIED else "pending"
    # The visible label never describes unavailable observations as connected.
    pool_status = (
        f"{len(pool_discovery.pools)} pools verified"
        if pool_discovery.status is PoolDiscoveryStatus.VERIFIED
        else "Discovery blocked"
    )
    # Combining ordered diagnostics gives the operator complete escaped evidence.
    pool_diagnostic = escape(" ".join(pool_discovery.diagnostics))
    # Only a fully verified coverage snapshot receives a positive visual status.
    oracle_tone = "good" if oracle_coverage.status is OracleCoverageStatus.VERIFIED else "pending"
    # The count label makes zero configured feeds visible without implying a transient error.
    oracle_status = (
        f"{oracle_coverage.healthy_feeds} of {oracle_coverage.configured_feeds} feeds healthy"
        if oracle_coverage.status is OracleCoverageStatus.VERIFIED
        else "Health unavailable"
    )
    # Ordered diagnostics preserve the evidence behind the coverage outcome.
    oracle_diagnostic = escape(" ".join(oracle_coverage.diagnostics))
    # Escaping preserves the HTML boundary if a future backend supplies local diagnostic text.
    transaction_diagnostic = escape(transaction_capabilities.diagnostic)
    # Backend status names unavailable simulation without weakening local planning guarantees.
    simulation_status = (
        "Read-only backend configured"
        if transaction_capabilities.simulation_backend_configured
        else "Simulation backend unavailable"
    )
    return (
        dashboard_template.replace("{{APP_NAME}}", display_name)
        .replace("{{B20_TONE}}", registry_tone)
        .replace("{{B20_STATUS}}", registry_status)
        .replace("{{B20_DIAGNOSTIC}}", registry_diagnostic)
        .replace("{{POOL_TONE}}", pool_tone)
        .replace("{{POOL_STATUS}}", pool_status)
        .replace("{{POOL_DIAGNOSTIC}}", pool_diagnostic)
        .replace("{{ORACLE_TONE}}", oracle_tone)
        .replace("{{ORACLE_STATUS}}", oracle_status)
        .replace("{{ORACLE_DIAGNOSTIC}}", oracle_diagnostic)
        .replace("{{SIMULATION_STATUS}}", simulation_status)
        .replace("{{TRANSACTION_DIAGNOSTIC}}", transaction_diagnostic)
    )
