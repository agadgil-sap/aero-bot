"""Shared pytest configuration for asynchronous HTTP behavior tests."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from _pytest.monkeypatch import MonkeyPatch

from aero_bot.history import PoolPricePoint
from aero_bot.ranging import RangingEvidence, raw_tick_for_human_price

# The shared evidence fixture's anchor instant: a Saturday clear of windows.
EVIDENCE_BASE_TIME = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def healthy_trailing_path(
    spot: Decimal,
    wiggle_ticks: int = 3,
    count: int = 30,
    step_seconds: int = 600,
    start: datetime | None = None,
    end_before: datetime | None = None,
) -> tuple[PoolPricePoint, ...]:
    """Build one measured trailing window wandering a few ticks around a spot.

    Args:
        spot: The center price the window wanders around.
        wiggle_ticks: The wander radius in raw ticks.
        count: The number of observed points.
        step_seconds: Wall-clock seconds between observations.
        start: The window's first timestamp; None uses the shared base.
        end_before: An observation instant the window must END one step
            before, so the newest measured point is fresh at the decision.

    Returns:
        Points whose prices oscillate inside a few-tick band of the spot, so
        every in-band adaptive candidate measures a high dwell share.
    """
    ratio = Decimal("1.0001")
    prices = [
        spot * ratio ** Decimal((index % (2 * wiggle_ticks + 1)) - wiggle_ticks)
        for index in range(count)
    ]
    if end_before is not None:
        first = end_before - timedelta(seconds=step_seconds * count)
    elif start is not None:
        first = start
    else:
        # The window ends one step after its anchor, which callers place
        # just before their observation instants.
        first = EVIDENCE_BASE_TIME
    return tuple(
        PoolPricePoint(
            timestamp=first + timedelta(seconds=step_seconds * index),
            block_number=index,
            log_index=0,
            amount0=0,
            amount1=5_000_000_000,
            sqrt_ratio=1 << 96,
            liquidity=80_000_000_000_000,
            tick=0,
            price_usdc=price,
        )
        for index, price in enumerate(prices)
    )


def healthy_ranging_evidence(
    spot: Decimal,
    observed_at: datetime | None = None,
    **overrides: object,
) -> RangingEvidence:
    """Build one solvable shared ranging-evidence fixture around a spot.

    Args:
        spot: The price the observation carries; the window wanders around it.
        observed_at: The decision instant; the window ends one step before
            it so the newest measured point is fresh. None anchors at the
            shared base instant.
        **overrides: Evidence fields changed for one behavior test.

    Returns:
        A validated evidence set - a thin concentrated five-hundred-USDC
        staked book at the quiet measured regime - whose adaptive solve
        lands on a clearly positive best candidate at floor-scale APRs.
    """
    values: dict[str, object] = {
        "gauge_liquidity_raw": 35_000_000_000,
        "staked_tvl_usd": Decimal("500"),
        "active_liquidity_raw": 80_000_000_000_000,
        "fee_window_seconds": 86_400,
        "fee_window_notional_usd": Decimal("1000000"),
        "pool_fee_ppm": 500,
        "realized_daily_volatility": Decimal("0.001"),
        "trailing_path": healthy_trailing_path(spot, end_before=observed_at),
        "stock_decimals": 6,
        "quote_decimals": 6,
        # A coherent raw grid anchor for the spot price at the default
        # token1 orientation; pool-anchored callers override both fields
        # from the resolved pool snapshot.
        "pool_tick_raw": raw_tick_for_human_price(spot, False, 6, 6),
        "stock_is_token0": False,
    }
    if overrides.get("pool_tick_raw") is not None and overrides.get("stock_is_token0") is None:
        # A raw tick without an orientation cannot anchor; derive both from
        # the spot at the default orientation.
        overrides["stock_is_token0"] = False
    values.update(overrides)
    return RangingEvidence.model_validate(values)


@pytest.fixture(autouse=True)
def isolate_audit_storage(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep application audit writes inside each test's private temporary directory.

    Args:
        monkeypatch: Pytest environment mutation helper restored after the test.
        tmp_path: Per-test private filesystem location.

    Yields:
        Control after configuring isolated application persistence.
    """
    # Dedicated child lets AuditStore create and secure its own final parent directory.
    database_path = tmp_path / "aero-bot-audit" / "audit.sqlite3"
    # Environment override exercises the same settings boundary used by local operators.
    monkeypatch.setenv("AERO_BOT_AUDIT_DATABASE_PATH", str(database_path))
    yield


@pytest.fixture
def anyio_backend() -> str:
    """Use asyncio because it is the production ASGI server's event-loop backend."""
    return "asyncio"
