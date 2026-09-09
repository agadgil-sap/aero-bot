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
from aero_bot.strategy import BoardListing, run_decision, run_selection
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

    def resolve_pool(self, symbol: str) -> tuple[PoolCandidate, int]:
        """Return the verified fixture pool and its snapshot block."""
        return make_candidate(), 123

    def enumerate_pools(self) -> tuple[tuple[BoardListing, ...], int]:
        """Return the verified fixture board and its snapshot block."""
        return (BoardListing(symbol="FIXc", pool=make_candidate()),), 123

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


def test_market_open_window_no_longer_blocks_entry_since_the_ruling() -> None:
    """A session window no longer gates: the entry verdict is the product.

    The captain's 2026-09-09 twenty-four-seven ruling: the B20 pools are
    continuous DeFi markets - nights and weekends are in scope - so the
    flat-window doctrine was removed. This fixture (Tuesday 09:40
    America/New_York, inside the old market-open window) previously held
    as event_window_flat; the doctrine test is inverted here with the
    ruling recorded as its provenance, and the window view stays in the
    report as informational evidence only.
    """
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
    assert report.outcome.decision.action is PolicyActionKind.ENTER
    assert report.outcome.decision.reason is PolicyReason.ENTRY_THRESHOLD_MET


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


def test_parse_reference_quotes_serves_both_forms() -> None:
    """The single form stays a quote; pairs parse into the per-symbol map."""
    from aero_bot.strategy import parse_reference_quotes

    assert parse_reference_quotes("") == {}
    assert parse_reference_quotes("318.5") == Decimal("318.5")
    assert parse_reference_quotes("AAPLc=318.5, FIXc=100") == {
        "AAPLc": Decimal("318.5"),
        "FIXc": Decimal("100"),
    }
    with pytest.raises(ValueError, match="positive"):
        parse_reference_quotes("0")
    with pytest.raises(ValueError, match="numbers"):
        parse_reference_quotes("AAPLc")
    with pytest.raises(ValueError, match="numbers"):
        parse_reference_quotes("AAPLc=abc")
    with pytest.raises(ValueError, match="SYMBOL=PRICE"):
        parse_reference_quotes("AAPLc=")
    with pytest.raises(ValueError, match="more than once"):
        parse_reference_quotes("A=1, A=2")


class SelectorStrategySources(FakeStrategySources):
    """Serve a two-pool board with one disqualified pool."""

    def __init__(self) -> None:
        """Configure the scripted board and shared reads."""
        super().__init__()
        second_token = "0xbb0000000000000000000078ee7ce2fe4908108c"  # noqa: S105
        self._second_pool = make_candidate(
            pool_address="0x2222222222222222222222222222222222222222",
            token1_address=second_token,
            emissions_per_second=2 * 4_494_371_922_759_724,
        )

    def enumerate_pools(self) -> tuple[tuple[BoardListing, ...], int]:
        """Return the two-pool board ordered by symbol."""
        return (
            BoardListing(symbol="AAAc", pool=make_candidate()),
            BoardListing(symbol="BBBc", pool=self._second_pool),
        ), 123

    def token_balance(self, token_address: str, owner_address: str) -> int:
        """Record the equity read and serve a flat ten-USDC Safe."""
        self.balance_reads.append(token_address)
        return 10_000_000 if token_address.lower().endswith("2913") else 0


def test_run_selection_picks_the_best_qualifying_pool() -> None:
    """The selector enters the higher-APR pool and reports the board."""
    sources = SelectorStrategySources()

    report = run_selection(
        sources,
        equity_usd=None,
        reference_prices={"AAAc": Decimal("100"), "BBBc": Decimal("100")},
        reference_age_seconds=0,
        safe_address="0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28",
        observed_at=QUIET_INSTANT,
    )

    assert report.selector_mode is True
    assert report.symbol == "BBBc"
    assert report.outcome.decision.action is PolicyActionKind.ENTER
    assert report.switch is None
    assert [evaluation.symbol for evaluation in report.board] == ["AAAc", "BBBc"]
    assert "selected BBBc" in report.board_summary
    # Both stock balances plus USDC were read once for the shared equity.
    assert len(sources.balance_reads) == 3


def test_run_selection_holds_when_no_pool_qualifies() -> None:
    """Without references the board holds naming each pool's reason."""
    sources = SelectorStrategySources()

    report = run_selection(
        sources,
        equity_usd=Decimal("200"),
        reference_prices={},
        reference_age_seconds=None,
        safe_address="0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28",
        observed_at=QUIET_INSTANT,
    )

    assert report.outcome.decision.action is PolicyActionKind.HOLD
    assert report.outcome.decision.reason is PolicyReason.NO_QUALIFYING_POOL
    assert "reference_stale" in report.outcome.decision.diagnostics[0]


def test_main_runs_the_selector_when_the_symbol_is_auto(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--symbol auto (and the default) run the cross-board selector."""
    from unittest.mock import patch

    from aero_bot.strategy import main

    with (
        patch(
            "aero_bot.strategy.LiveStrategySources",
            return_value=SelectorStrategySources(),
        ),
        patch("aero_bot.strategy.AuditStore") as audit_factory,
    ):
        exit_code = main(["--symbol", "auto", "--equity-usdc", "200"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "selector:" in output
    assert "event window (informational):" in output
    audit_factory.return_value.append.assert_called_once()
