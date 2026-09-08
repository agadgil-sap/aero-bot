"""Pin the decision-only strategy E2E surface."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_lp_executor import (
    B20_ADDRESS,
    POOL_ADDRESS,
    make_candidate,
)

from aero_bot.policy import PolicyActionKind, PolicyReason
from aero_bot.strategy import run_decision
from aero_bot.venues import PoolCandidate

# A fixture instant well inside a weekday market-open window in New York:
# 2026-09-08 is a Tuesday; 13:40 UTC is 09:40 America/New_York.
MARKET_OPEN_INSTANT = datetime(2026, 9, 8, 13, 40, tzinfo=UTC)
# The same Tuesday afternoon New York time, outside every session window.
QUIET_INSTANT = datetime(2026, 9, 8, 20, 30, tzinfo=UTC)


class FakeStrategySources:
    """Serve deterministic discovery and reads without any network."""

    def __init__(
        self, *, aero_price: Decimal = Decimal("0.6"), gas_gwei: Decimal | None = Decimal("0.001")
    ) -> None:
        """Configure the fixture pool with verified defaults."""
        self._aero_price = aero_price
        self._gas_gwei = gas_gwei
        self.balance_reads: list[str] = []

    def discover(self) -> tuple[tuple[PoolCandidate, ...], int]:
        """Return the verified fixture pool and its snapshot block."""
        return (make_candidate(),), 123

    def registry_paused(self) -> bool:
        """The fixture registry is verified."""
        return False

    def symbol_address(self, symbol: str) -> str | None:
        """Resolve the fixture FIXc symbol."""
        return B20_ADDRESS if symbol.lower() == "fixc" else None

    def token_decimals(self, token_address: str) -> int:
        """The fixture stock carries eight decimals."""
        return 8

    def aero_price(self, block_number: int) -> Decimal:
        """Return the fixture AERO price."""
        return self._aero_price

    def gas_price_gwei(self) -> Decimal | None:
        """Return the fixture gas price."""
        return self._gas_gwei

    def token_balance(self, token_address: str, owner_address: str) -> int:
        """Record the equity read and serve a flat ten-USDC Safe."""
        self.balance_reads.append(token_address)
        return 10_000_000 if token_address.lower().endswith("2913") else 0


def test_enter_verdict_with_an_injected_reference_quote() -> None:
    """A quiet window with a fresh reference quote produces a full ENTER."""
    sources = FakeStrategySources()

    report = run_decision(
        sources,
        "FIXc",
        equity_usd=Decimal("200"),
        reference_price_usdc=Decimal("100"),
        reference_age_seconds=0,
        safe_address="0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28",
        observed_at=QUIET_INSTANT,
    )

    assert report.outcome.decision.action is PolicyActionKind.ENTER
    assert report.outcome.decision.reason is PolicyReason.ENTRY_THRESHOLD_MET
    # The corrected convention clears the 150 percent entry gate.
    assert report.emissions_apr > Decimal("1.5")
    assert report.pool_address == POOL_ADDRESS
    assert report.event_window.active is False
    assert report.gas_price_gwei == Decimal("0.001")


def test_missing_reference_blocks_entries_fail_closed() -> None:
    """Without a reference quote the verdict holds as reference_stale."""
    sources = FakeStrategySources()

    report = run_decision(
        sources,
        "FIXc",
        equity_usd=Decimal("200"),
        reference_price_usdc=None,
        reference_age_seconds=None,
        safe_address="0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28",
        observed_at=QUIET_INSTANT,
    )

    assert report.outcome.decision.action is PolicyActionKind.HOLD
    assert report.outcome.decision.reason is PolicyReason.REFERENCE_STALE
    assert any("reference_stale" in note for note in report.input_notes)


def test_flat_verdict_inside_the_market_open_window_is_correct() -> None:
    """A session window holds the policy flat; that verdict is the doctrine."""
    sources = FakeStrategySources()

    report = run_decision(
        sources,
        "FIXc",
        equity_usd=Decimal("200"),
        reference_price_usdc=Decimal("100"),
        reference_age_seconds=0,
        safe_address="0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28",
        observed_at=MARKET_OPEN_INSTANT,
    )

    assert report.event_window.active is True
    assert "market open" in report.event_window.description
    assert report.outcome.decision.action is PolicyActionKind.HOLD
    assert report.outcome.decision.reason is PolicyReason.EVENT_WINDOW_FLAT


def test_equity_defaults_to_the_safe_live_balances() -> None:
    """Without an override the equity is the Safe's live token value."""
    sources = FakeStrategySources()

    report = run_decision(
        sources,
        "FIXc",
        equity_usd=None,
        reference_price_usdc=Decimal("100"),
        reference_age_seconds=0,
        safe_address="0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28",
        observed_at=QUIET_INSTANT,
    )

    assert report.equity_usd == Decimal("10")
    assert len(sources.balance_reads) == 2


def test_unknown_symbol_refuses_the_run() -> None:
    """A symbol outside the registry refuses rather than guessing a pool."""
    sources = FakeStrategySources()

    with pytest.raises(ValueError, match="not in the official B20 registry"):
        run_decision(
            sources,
            "NOPEc",
            equity_usd=Decimal("200"),
            reference_price_usdc=None,
            reference_age_seconds=None,
            safe_address="0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28",
            observed_at=QUIET_INSTANT,
        )


def test_main_prints_the_verdict_and_audits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI prints the human verdict; the audit chain gains one record."""
    from unittest.mock import patch

    from aero_bot.strategy import main

    with (
        patch(
            "aero_bot.strategy.LiveStrategySources",
            return_value=FakeStrategySources(),
        ),
        patch("aero_bot.strategy.AuditStore") as audit_factory,
    ):
        exit_code = main(["--symbol", "FIXc", "--equity-usdc", "200"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "verdict:" in output
    audit_factory.return_value.append.assert_called_once()
