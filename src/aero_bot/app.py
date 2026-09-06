"""FastAPI application factory and wallet-free dashboard routes."""

from html import escape
from importlib.resources import files
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from aero_bot.config import Settings
from aero_bot.registry import B20RegistryResult, RegistryStatus, load_official_b20_registry
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
) -> FastAPI:
    """Create an application instance with explicit local safety metadata.

    Args:
        settings: Optional validated local configuration for this application instance.
        b20_registry: Optional preloaded official registry result for deterministic tests.
        pool_discovery: Optional preloaded Aerodrome discovery result for deterministic tests.

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

    @application.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        """Render the initial local dashboard with honest connector status."""
        return _dashboard_html(
            resolved_settings.app_name,
            resolved_registry,
            resolved_pool_discovery,
        )

    return application


def _dashboard_html(
    app_name: str,
    registry: B20RegistryResult,
    pool_discovery: PoolDiscoveryResult,
) -> str:
    """Build the dependency-free dashboard shell for the local first release.

    Args:
        app_name: Validated local product title.
        registry: Official B20 registry evidence or fail-closed diagnostic.
        pool_discovery: Validated Aerodrome pools or fail-closed diagnostic.

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
    return (
        dashboard_template.replace("{{APP_NAME}}", display_name)
        .replace("{{B20_TONE}}", registry_tone)
        .replace("{{B20_STATUS}}", registry_status)
        .replace("{{B20_DIAGNOSTIC}}", registry_diagnostic)
        .replace("{{POOL_TONE}}", pool_tone)
        .replace("{{POOL_STATUS}}", pool_status)
        .replace("{{POOL_DIAGNOSTIC}}", pool_diagnostic)
    )
