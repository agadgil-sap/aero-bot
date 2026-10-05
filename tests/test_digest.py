"""Pin the daily digest: suppression, composition, and the daily tick wiring."""

import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from test_alerts import SMTP_ENV, FakeTransport, calm_report, halted_report

from aero_bot.alerts import (
    ALERT_MODE_ENV,
    AlertMode,
    deliver_cycle_alerts,
    parse_alert_config,
)
from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.cycle import CycleReportPayload
from aero_bot.digest import (
    DIGEST_MAX_FAILURE_LINES,
    DIGEST_SUBJECT_PREFIX,
    collect_digest_records,
    compose_daily_digest,
    deliver_daily_digest,
)
from aero_bot.executor import ExecutionMode, ExecutionReceiptPayload, ExecutionSentPayload
from aero_bot.executor import ExecutionRole as SwapRole
from aero_bot.lp_executor import (
    LpCollectPlannedPayload,
    LpExecuteReceiptPayload,
    LpExecuteSentPayload,
    LpExecutionRole,
    LpRefusedPayload,
)

NOW = datetime(2026, 10, 6, 22, 0, 0, tzinfo=UTC)
TEN_HOURS_AGO = NOW - timedelta(hours=10)
THIRTY_HOURS_AGO = NOW - timedelta(hours=30)

RESEND_ENV = {
    **SMTP_ENV,
    "AERO_BOT_ALERT_PROVIDER": "resend",
    "AERO_BOT_ALERT_RESEND_API_KEY": "sealed-resend-key",
}


def cycle_payload(**overrides: object) -> CycleReportPayload:
    """Build one cycle summary payload with calm defaults."""
    fields: dict[str, object] = {
        "mode": "live",
        "symbol": "AAPLc",
        "action": "hold",
        "reason": "open_in_range",
        "equity_usdc": "105.37",
        "day_start_equity_usdc": "105.37",
        "day_pnl_usdc": "0.01",
        "halted_reason": "",
    }
    fields.update(overrides)
    return CycleReportPayload.model_validate(fields)


class AdvisorPayload(BaseModel):
    """One advisor summary shaped exactly like the production record."""

    outcome: str
    model: str = ""
    brief: str = ""
    latency_ms: int = 0
    window_records: int = 1


def seeded_digest_store(tmp_path: Path) -> AuditStore:
    """Build one audit chain covering every digest section and the cutoff."""
    store = AuditStore(tmp_path / "audit" / "audit.sqlite3")
    store.append(
        AuditEventType.CYCLE_REPORTED,
        cycle_payload(action="enter", reason="entry_threshold_met"),
        TEN_HOURS_AGO - timedelta(hours=1),
    )
    store.append(
        AuditEventType.CYCLE_REPORTED,
        cycle_payload(mode="dry_run", action="hold"),
        TEN_HOURS_AGO,
    )
    # One refused mint, then its confirmed delivery pair, then a collect.
    store.append(
        AuditEventType.LP_REFUSED,
        LpRefusedPayload(
            action="mint",
            mode=ExecutionMode.EXECUTE,
            code="depth_gate",
            message="executable depth below the gate",
            symbol="METAc",
        ),
        TEN_HOURS_AGO + timedelta(minutes=5),
    )
    store.append(
        AuditEventType.LP_EXECUTE_SENT,
        LpExecuteSentPayload(
            action="mint",
            role=LpExecutionRole.BALANCING_SWAP,
            safe_tx_hash="0x" + "a" * 64,
            transaction_hash="0x" + "b" * 64,
            nonce=2512,
            relayer_address="0x" + "1" * 40,
            safe_address="0x" + "2" * 40,
        ),
        TEN_HOURS_AGO + timedelta(minutes=6),
    )
    store.append(
        AuditEventType.LP_EXECUTE_CONFIRMED,
        LpExecuteReceiptPayload(
            outcome="confirmed",
            action="mint",
            role=LpExecutionRole.BALANCING_SWAP,
            safe_tx_hash="0x" + "a" * 64,
            transaction_hash="0x" + "b" * 64,
            block_number=52_202_165,
            gas_used=313_288,
            effective_gas_price_wei=6_000_000,
            inclusion_ms=1500,
        ),
        TEN_HOURS_AGO + timedelta(minutes=7),
    )
    store.append(
        AuditEventType.LP_COLLECT_PLANNED,
        LpCollectPlannedPayload(
            mode=ExecutionMode.EXECUTE,
            symbol="AAPLc",
            pool_address="0x" + "3" * 40,
            nfpm_address="0x" + "4" * 40,
            gauge_address="0x" + "5" * 40,
            token_id=7_634_175,
            staked=True,
            accrued_aero_earned_units=10_059_442_624_909_639,
        ),
        TEN_HOURS_AGO + timedelta(minutes=8),
    )
    store.append(
        AuditEventType.ADVISOR_REPORTED,
        AdvisorPayload(
            outcome="brief",
            model="qwen3.6:35b-a3b",
            brief="Benign wait state; position in range; nothing demands eyes.",
            latency_ms=9_235,
        ),
        TEN_HOURS_AGO + timedelta(minutes=9),
    )
    # The record a halted cycle leaves, plus one outside the window.
    store.append(
        AuditEventType.CYCLE_REPORTED,
        cycle_payload(halted_reason="daily_loss_halt", equity_usdc="99.10"),
        TEN_HOURS_AGO + timedelta(minutes=10),
    )
    store.append(
        AuditEventType.CYCLE_REPORTED,
        cycle_payload(action="exit", equity_usdc="98.00"),
        THIRTY_HOURS_AGO,
    )
    return store


class TestDigestModeSuppression:
    """Digest routing suppresses every immediate email path."""

    def test_the_mode_variable_parses_both_values(self) -> None:
        """per_cycle stays the default; digest is explicit; junk refuses."""
        assert parse_alert_config(SMTP_ENV).mode is AlertMode.PER_CYCLE
        assert parse_alert_config({**SMTP_ENV, ALERT_MODE_ENV: "digest"}).mode is AlertMode.DIGEST
        with pytest.raises(ValueError, match="AERO_BOT_ALERT_MODE"):
            parse_alert_config({**SMTP_ENV, ALERT_MODE_ENV: "weekly"})

    def test_digest_mode_suppresses_the_cycle_summary(self) -> None:
        """A calm cycle that would email stays silent under digest."""
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert (
                deliver_cycle_alerts(calm_report(), {**SMTP_ENV, ALERT_MODE_ENV: "digest"}) is False
            )
        assert transport.sent == []

    def test_digest_mode_suppresses_the_alert_email_too(self) -> None:
        """Even a halted cycle never emails immediately under digest."""
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert (
                deliver_cycle_alerts(halted_report(), {**SMTP_ENV, ALERT_MODE_ENV: "digest"})
                is False
            )
        assert transport.sent == []

    def test_per_cycle_mode_emails_unchanged(self) -> None:
        """The shipped default keeps its immediate behavior."""
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert deliver_cycle_alerts(calm_report(), SMTP_ENV) is True
        assert len(transport.sent) == 1

    def test_the_watchtower_paths_are_suppressed_under_digest(self) -> None:
        """Range trips and fail-safe notices stay silent under digest."""
        from aero_bot.watchtower import (
            deliver_watchtower_notice,
            deliver_watchtower_trigger,
        )

        env = {**SMTP_ENV, ALERT_MODE_ENV: "digest"}
        with patch("aero_bot.watchtower.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert deliver_watchtower_trigger(calm_report(), "upper edge", env) is False
            assert deliver_watchtower_notice("watchtower armed", "body", env) is False
        assert transport.sent == []


class TestWindowCollection:
    """The bounded window read carries exactly the prior day."""

    def test_records_outside_the_window_are_excluded(self, tmp_path: Path) -> None:
        """The 30-hour-old record never reaches the digest."""
        store = seeded_digest_store(tmp_path)
        records = collect_digest_records(store, NOW)
        stamps = {record.created_at for record in records}
        assert THIRTY_HOURS_AGO not in stamps
        assert TEN_HOURS_AGO in stamps

    def test_the_read_stops_at_the_page_bound(self) -> None:
        """A source older than the window ends the scan at its first page."""

        class BoundedSource:
            def __init__(self) -> None:
                self.reads = 0

            def read_records(self, limit: int = 100, *, offset: int = 0) -> tuple[()]:
                self.reads += 1
                assert limit == 1000
                return ()

        source = BoundedSource()
        assert collect_digest_records(source, NOW) == ()
        assert source.reads == 1


class TestDigestComposition:
    """Every digest line restates durable evidence or an honest absence."""

    def test_the_full_digest_carries_every_section(self, tmp_path: Path) -> None:
        """The seeded day composes into one bounded email."""
        records = collect_digest_records(seeded_digest_store(tmp_path), NOW)
        subject, body = compose_daily_digest(records, NOW)
        assert subject.startswith(DIGEST_SUBJECT_PREFIX)
        assert "failure line(s)" in subject
        assert "halted cycle" in body and "daily_loss_halt" in body
        assert "refused" in body and "depth_gate" in body
        assert "mint" in body and "0x" + "b" * 64 in body and "(confirmed)" in body
        assert "reward claims planned: 1" in body
        assert "student advice: 1 brief(s) of 1 pass(es)" in body
        assert "qwen3.6:35b-a3b" in body
        assert "Benign wait state" in body
        assert "MISSING BY DESIGN" in body
        assert "teacher seats run on the operator's Mac" in body
        assert "equity:" in body
        assert "exit" not in body.split("== trading ==")[1].split("==")[0]

    def test_an_empty_window_states_every_absence(self) -> None:
        """No records compose into explicit absence lines, never silence."""
        subject, body = compose_daily_digest((), NOW)
        assert "clean window" in subject
        assert "no cycle ran in the window" in body
        assert "no broadcasts in the window" in body
        assert "student advice: NONE in the window" in body

    def test_the_student_block_reports_typed_absences(self, tmp_path: Path) -> None:
        """Absence outcomes tally beside the latest brief."""
        store = AuditStore(tmp_path / "audit" / "audit.sqlite3")
        store.append(
            AuditEventType.ADVISOR_REPORTED,
            AdvisorPayload(outcome="timeout"),
            NOW - timedelta(hours=2),
        )
        store.append(
            AuditEventType.ADVISOR_REPORTED,
            AdvisorPayload(outcome="unreachable"),
            NOW - timedelta(hours=1),
        )
        records = collect_digest_records(store, NOW)
        _, body = compose_daily_digest(records, NOW)
        assert "0 brief(s) of 2 pass(es)" in body
        assert "1 timeout, 1 unreachable" in body
        assert "outcome unreachable" in body

    def test_failure_and_action_sections_bound_their_lines(self, tmp_path: Path) -> None:
        """A noisy day caps its sections and says how much it hid."""
        store = AuditStore(tmp_path / "audit" / "audit.sqlite3")
        for index in range(DIGEST_MAX_FAILURE_LINES + 3):
            store.append(
                AuditEventType.LP_EXECUTE_FAILED,
                LpExecuteReceiptPayload(
                    outcome="failed",
                    action="mint",
                    role=LpExecutionRole.MINT,
                    safe_tx_hash="0x" + "c" * 64,
                    transaction_hash=f"0x{index:064x}",
                    block_number=1,
                    gas_used=1,
                    effective_gas_price_wei=1,
                    inclusion_ms=1,
                    diagnostic="reverted",
                ),
                NOW - timedelta(minutes=index + 1),
            )
        records = collect_digest_records(store, NOW)
        subject, body = compose_daily_digest(records, NOW)
        assert "(+3 more)" in subject
        assert f"bounded at {DIGEST_MAX_FAILURE_LINES}" in body

    def test_swap_executor_deliveries_join_their_outcomes(self, tmp_path: Path) -> None:
        """The aero-swap path's broadcasts render with their receipts."""
        store = AuditStore(tmp_path / "audit" / "audit.sqlite3")
        store.append(
            AuditEventType.EXECUTION_SENT,
            ExecutionSentPayload(
                role=SwapRole.SWAP,
                safe_tx_hash="0x" + "d" * 64,
                transaction_hash="0x" + "e" * 64,
                relayer_address="0x" + "6" * 40,
                safe_address="0x" + "7" * 40,
            ),
            NOW - timedelta(hours=1),
        )
        store.append(
            AuditEventType.EXECUTION_FAILED,
            ExecutionReceiptPayload(
                outcome="failed",
                role=SwapRole.SWAP,
                safe_tx_hash="0x" + "d" * 64,
                transaction_hash="0x" + "e" * 64,
                block_number=9,
                gas_used=9,
                effective_gas_price_wei=9,
                inclusion_duration_ms=Decimal("9"),
                diagnostic="slippage",
            ),
            NOW - timedelta(hours=1) + timedelta(minutes=1),
        )
        records = collect_digest_records(store, NOW)
        _, body = compose_daily_digest(records, NOW)
        assert "0x" + "e" * 64 in body
        assert "(reverted)" in body
        assert "reverted delivery" in body


class TestDigestDelivery:
    """The digest send rides the sealed transport with honest failures."""

    def test_the_digest_sends_through_the_configured_transport(self, tmp_path: Path) -> None:
        """A configured provider receives exactly one digest email."""
        store = seeded_digest_store(tmp_path)
        with patch("aero_bot.digest.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert deliver_daily_digest(store, RESEND_ENV, now=NOW) is True
        assert len(transport.sent) == 1
        subject, body = transport.sent[0]
        assert subject.startswith(DIGEST_SUBJECT_PREFIX)
        assert "== failures ==" in body

    def test_provider_none_stays_silent(self, tmp_path: Path) -> None:
        """A silent configuration sends no digest."""
        store = seeded_digest_store(tmp_path)
        assert deliver_daily_digest(store, {}, now=NOW) is False

    def test_an_unreadable_store_warns_without_raising(self, tmp_path: Path) -> None:
        """A missing database degrades to one stderr warning."""

        class BrokenStore:
            def read_records(self, limit: int = 100, *, offset: int = 0) -> tuple[()]:
                raise OSError("disk unavailable")

        errors = io.StringIO()
        assert (
            deliver_daily_digest(BrokenStore(), RESEND_ENV, error_stream=errors, now=NOW) is False
        )
        assert "disk unavailable" in errors.getvalue()

    def test_a_transport_failure_warns_without_raising(self, tmp_path: Path) -> None:
        """A rejected send degrades to one stderr warning."""
        store = seeded_digest_store(tmp_path)
        from aero_bot.alerts import AlertTransportError

        errors = io.StringIO()
        with patch("aero_bot.digest.build_alert_transport") as builder:
            builder.return_value = FakeTransport(failure=AlertTransportError("provider refused"))
            assert deliver_daily_digest(store, RESEND_ENV, error_stream=errors, now=NOW) is False
        assert "provider refused" in errors.getvalue()


class TestDailyTickWiring:
    """The shipped daily tick carries the digest flag and a DST-true clock."""

    def test_the_daily_report_unit_sends_the_digest(self) -> None:
        """ExecStart carries --daily-digest beside the dry selector cycle."""
        unit = (
            Path(__file__).parent.parent / "deploy" / "systemd" / "aero-bot-daily-report.service"
        ).read_text()
        assert (
            "ExecStart=/opt/aero-bot/.venv/bin/aero-bot-cycle --symbol auto "
            "--dry-run --json --daily-digest" in unit
        )

    def test_the_timer_stays_nine_am_melbourne_across_dst(self) -> None:
        """OnCalendar names Australia/Melbourne so systemd holds local time."""
        timer = (
            Path(__file__).parent.parent / "deploy" / "systemd" / "aero-bot-daily-report.timer"
        ).read_text()
        assert "OnCalendar=*-*-* 09:00:00 Australia/Melbourne" in timer

    def test_the_cycle_cli_accepts_the_digest_flag(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The cycle parser carries --daily-digest in its usage."""
        from aero_bot.cycle import main

        with pytest.raises(SystemExit) as raised:
            main(["--help"])
        assert raised.value.code == 0
        assert "--daily-digest" in capsys.readouterr().out
