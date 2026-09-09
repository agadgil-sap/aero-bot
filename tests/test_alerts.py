"""Pin the cycle's email alert hook over fake and scripted transports."""

import io
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from test_cycle import (
    MINT_TX_HASH,
    QUIET_INSTANT,
    make_runner,
    mint_receipt,
)

from aero_bot.alerts import (
    ALERT_FROM_ENV,
    ALERT_PROVIDER_ENV,
    ALERT_RESEND_API_KEY_ENV,
    ALERT_SMTP_HOST_ENV,
    ALERT_SMTP_PASSWORD_ENV,
    ALERT_SMTP_PORT_ENV,
    ALERT_SMTP_USER_ENV,
    ALERT_SUMMARY_EVERY_CYCLE_ENV,
    ALERT_TO_ENV,
    AlertProvider,
    AlertTransportError,
    EmailTransport,
    ResendHttpTransport,
    SmtpEmailTransport,
    build_alert_transport,
    compose_cycle_email,
    deliver_cycle_alerts,
    evaluate_alerts,
    parse_alert_config,
)
from aero_bot.cycle import CycleActionRecord, CycleMode, CycleReport
from aero_bot.cycle import CycleReconciliation as Reconciliation


class FakeTransport:
    """Record every email one cycle tries to send."""

    def __init__(self, failure: Exception | None = None) -> None:
        """Serve every send, or raise the scripted failure."""
        self.sent: list[tuple[str, str]] = []
        self.failure = failure

    def send(self, subject: str, body: str) -> None:
        """Record one delivery or raise the scripted failure."""
        if self.failure is not None:
            raise self.failure
        self.sent.append((subject, body))


SMTP_ENV = {
    ALERT_PROVIDER_ENV: "smtp",
    ALERT_FROM_ENV: "aero-bot@example.com",
    ALERT_TO_ENV: "captain@example.com, ops@example.com",
    ALERT_SMTP_HOST_ENV: "smtp.example.com",
    ALERT_SMTP_USER_ENV: "aero-bot@example.com",
    ALERT_SMTP_PASSWORD_ENV: "sealed-smtp-password",
}


def calm_report(*, usdc_units: int = 10_000_000, relayer_eth_wei: int = 10**15) -> CycleReport:
    """Build one calm completed cycle report for composition tests."""
    return CycleReport(
        started_at=QUIET_INSTANT,
        mode=CycleMode.LIVE,
        symbol="FIXc",
        reconciliation=Reconciliation(
            symbol="FIXc",
            safe_usdc_units=usdc_units,
            relayer_eth_wei=relayer_eth_wei,
            safe_stock_units=0,
            inventory_live_token_ids=(),
            inventory_empty_count=0,
            out_of_band="",
            diagnostics=(f"Safe holds {Decimal(usdc_units).scaleb(-6)} USDC",),
        ),
        decision_action="hold",
        decision_reason="open_in_range",
        decision_diagnostics=("position inside its range",),
        event_window="no active window",
        actions=(),
        pnl_vs_entry_usdc=Decimal("1.25"),
        fee_wei=0,
        input_notes=("fixture note",),
    )


def halted_report() -> CycleReport:
    """Build one halted cycle report carrying a refused action."""
    report = calm_report(usdc_units=1_000_000, relayer_eth_wei=10**14)
    return report.model_copy(
        update={
            "actions": (
                CycleActionRecord(
                    action="mint",
                    status="refused",
                    refusal_code="gas_price_above_cap",
                    diagnostic="the endpoint's gas price exceeds the cap",
                ),
            ),
            "fee_wei": 90_000,
            "halted_reason": "the mint action refused [gas_price_above_cap]",
        }
    )


def captain_sample_report() -> CycleReport:
    """Rebuild the captain's first real dry-cycle email as a report.

    Every ugly rendering the captain flagged is present verbatim: the
    microsecond timestamp, the 18-decimal ETH balance, the 0E-8 stock
    zero, and the 28-digit USDC note.
    """
    return CycleReport(
        started_at=datetime(2026, 9, 9, 3, 15, 32, 294470, tzinfo=UTC),
        mode=CycleMode.DRY_RUN,
        symbol="AAPLc",
        reconciliation=Reconciliation(
            symbol="AAPLc",
            safe_usdc_units=8_921_957,
            relayer_eth_wei=880_720_593_895_162,
            safe_stock_units=0,
            inventory_live_token_ids=(),
            inventory_empty_count=0,
            held_stock_quantity=Decimal(0).scaleb(-8),
            out_of_band="",
            diagnostics=(
                "Safe holds 8.921957 USDC, relayer holds 0.000880720593895162 ETH, "
                "Safe holds 0E-8 stock",
            ),
        ),
        decision_action="hold",
        decision_reason="gas_gate_deferred",
        decision_diagnostics=("the endpoint's gas price exceeds the gate",),
        event_window="No scheduled or session event window is active.",
        actions=(),
        pnl_vs_entry_usdc=None,
        pnl_diagnostic="no tracked position",
        fee_wei=0,
        input_notes=(
            "equity defaulted to the Safe's live 8.921957000000000000000000000 USDC "
            "(8.921957 USDC plus stock valued at the snapshot price)",
            "fee APR stays zero because a live fee-evidence window needs the "
            "price-path machinery the rehearsal reconstructs",
            "oracle staleness is not yet wired live; the oracle-health layer is "
            "post-reassessment scope",
        ),
    )


class TestParseAlertConfig:
    """The environment-driven configuration and its secret discipline."""

    def test_no_provider_defaults_to_silent(self) -> None:
        """Without configuration, alerts stay off and nothing is required."""
        config = parse_alert_config({})
        assert config.provider is AlertProvider.NONE
        assert build_alert_transport(config) is None

    def test_unknown_provider_names_every_choice(self) -> None:
        """A bad provider value fails closed listing the legal values."""
        with pytest.raises(ValueError, match="smtp") as error:
            parse_alert_config({ALERT_PROVIDER_ENV: "carrier-pigeon"})
        assert "resend" in str(error.value)

    def test_smtp_configuration_parses_every_field(self) -> None:
        """A complete SMTP environment yields the full submission settings."""
        config = parse_alert_config({**SMTP_ENV, ALERT_SMTP_PORT_ENV: "465"})
        assert config.provider is AlertProvider.SMTP
        assert config.recipients == ("captain@example.com", "ops@example.com")
        assert config.relayer_eth_floor_wei == 500_000_000_000_000
        assert config.safe_usdc_floor_units == 5_000_000
        assert config.summary_every_cycle is True
        assert config.smtp is not None
        assert config.smtp.host == "smtp.example.com"
        assert config.smtp.port == 465

    def test_missing_variables_are_named_never_valued(self) -> None:
        """Missing required variables fail listing names, not secrets."""
        partial = {key: value for key, value in SMTP_ENV.items() if key != ALERT_SMTP_HOST_ENV}
        with pytest.raises(ValueError, match=ALERT_SMTP_HOST_ENV) as error:
            parse_alert_config(partial)
        assert "sealed-smtp-password" not in str(error.value)

    def test_resend_configuration_parses_and_defaults_the_url(self) -> None:
        """The Resend provider needs only its key plus sender and recipients."""
        config = parse_alert_config(
            {
                ALERT_PROVIDER_ENV: "resend",
                ALERT_FROM_ENV: "alerts@example.com",
                ALERT_TO_ENV: "captain@example.com",
                ALERT_RESEND_API_KEY_ENV: "re_sealed_key",
            }
        )
        assert config.provider is AlertProvider.RESEND
        assert config.resend is not None
        assert config.resend.url == "https://api.resend.com/emails"

    def test_threshold_overrides_and_bad_values(self) -> None:
        """Floors are overridable and malformed values refuse."""
        config = parse_alert_config(
            {
                ALERT_PROVIDER_ENV: "resend",
                ALERT_FROM_ENV: "a@example.com",
                ALERT_TO_ENV: "b@example.com",
                ALERT_RESEND_API_KEY_ENV: "k",
                "AERO_BOT_ALERT_RELAYER_ETH_FLOOR_WEI": "1000",
                "AERO_BOT_ALERT_SAFE_USDC_FLOOR_UNITS": "2000000",
            }
        )
        assert config.relayer_eth_floor_wei == 1000
        assert config.safe_usdc_floor_units == 2_000_000
        with pytest.raises(ValueError, match="non-negative"):
            parse_alert_config({"AERO_BOT_ALERT_RELAYER_ETH_FLOOR_WEI": "-1"})

    def test_summary_flag_understands_every_off_spelling(self) -> None:
        """Summaries disable on 0, false, and no alike."""
        for spelling in ("0", "false", "NO"):
            config = parse_alert_config({ALERT_SUMMARY_EVERY_CYCLE_ENV: spelling})
            assert config.summary_every_cycle is False


class TestEvaluateAlerts:
    """The alert derivation over one report."""

    def test_calm_report_has_no_alerts(self) -> None:
        """A calm healthy cycle with funded balances alerts on nothing."""
        config = parse_alert_config({})
        assert evaluate_alerts(calm_report(), config) == ()

    def test_balance_floors_alert_with_both_values_quoted(self) -> None:
        """A drained gas tank and low working capital each alert."""
        config = parse_alert_config({})
        alerts = evaluate_alerts(halted_report(), config)
        assert any("relayer ETH" in line for line in alerts)
        assert any("Safe USDC" in line for line in alerts)

    def test_refused_actions_alert_with_their_catalog_code(self) -> None:
        """A refused action surfaces its code and diagnostic."""
        config = parse_alert_config(
            {
                "AERO_BOT_ALERT_RELAYER_ETH_FLOOR_WEI": "0",
                "AERO_BOT_ALERT_SAFE_USDC_FLOOR_UNITS": "0",
            }
        )
        alerts = evaluate_alerts(halted_report(), config)
        assert any("gas_price_above_cap" in line for line in alerts)

    def test_out_of_band_leads_the_alerts(self) -> None:
        """An out-of-band condition is the first alert line."""
        report = calm_report().model_copy(
            update={
                "reconciliation": calm_report().reconciliation.model_copy(
                    update={"out_of_band": "custody moved elsewhere"}
                )
            }
        )
        config = parse_alert_config({})
        alerts = evaluate_alerts(report, config)
        assert alerts[0].startswith("out of band")


class TestComposeCycleEmail:
    """The email composition."""

    def test_calm_subject_and_body_pin_the_readable_layout(self) -> None:
        """A calm email pins its subject and the full one-screen body."""
        subject, body = compose_cycle_email(calm_report(), ())
        assert subject == "[aero-bot] FIXc cycle hold (open_in_range)"
        assert body == (
            "Aero Bot cycle report - FIXc\n"
            "============================\n"
            "\n"
            "mode:         live\n"
            "started:      2026-09-08 20:30:00 UTC\n"
            "decision:     hold (open_in_range)\n"
            "event window: no active window\n"
            "\n"
            "--- state " + "-" * 56 + "\n"
            "\n"
            "  Safe:     10 USDC, 0 FIXc\n"
            "  relayer:  0.001 ETH\n"
            "\n"
            "--- outcome " + "-" * 54 + "\n"
            "\n"
            "  pnl vs entry:  1.25 USDC\n"
            "  gas spent:     0 wei\n"
            "\n"
            "--- notes " + "-" * 56 + "\n"
            "\n"
            "  - fixture note\n"
        )

    def test_captain_sample_renders_cleanly(self) -> None:
        """The captain's flagged email renders readable, fact for fact."""
        subject, body = compose_cycle_email(captain_sample_report(), ())
        assert subject == "[aero-bot] AAPLc cycle hold (gas_gate_deferred)"
        assert body == (
            "Aero Bot cycle report - AAPLc\n"
            "=============================\n"
            "\n"
            "mode:         dry_run\n"
            "started:      2026-09-09 03:15:32 UTC\n"
            "decision:     hold (gas_gate_deferred)\n"
            "event window: No scheduled or session event window is active.\n"
            "\n"
            "--- state " + "-" * 56 + "\n"
            "\n"
            "  Safe:     8.921957 USDC, 0 AAPLc\n"
            "  relayer:  0.000881 ETH\n"
            "\n"
            "--- outcome " + "-" * 54 + "\n"
            "\n"
            "  pnl vs entry:  unavailable (no tracked position)\n"
            "  gas spent:     0 wei\n"
            "\n"
            "--- notes " + "-" * 56 + "\n"
            "\n"
            "  - equity defaulted to the Safe's live 8.921957 USDC "
            "(8.921957 USDC plus stock valued at the snapshot price)\n"
            "  - fee APR stays zero because a live fee-evidence window needs the "
            "price-path machinery the rehearsal reconstructs\n"
            "  - oracle staleness is not yet wired live; the oracle-health layer is "
            "post-reassessment scope\n"
        )
        # The flagged noise is gone without losing a single disclosure.
        assert "0E-8" not in body
        assert "294470" not in body
        assert "8.921957000000000000000000000" not in body

    def test_alert_subject_is_truncated_and_prefixed(self) -> None:
        """An alerting email leads its subject with the first alert."""
        long_out_of_band = "x" * 200
        report = calm_report().model_copy(
            update={
                "reconciliation": calm_report().reconciliation.model_copy(
                    update={"out_of_band": long_out_of_band}
                )
            }
        )
        subject, _ = compose_cycle_email(report, (f"out of band: {long_out_of_band}",))
        assert subject.startswith("[aero-bot][ALERT] FIXc")
        assert len(subject) <= 120

    def test_body_carries_actions_hashes_and_notes(self) -> None:
        """Actions, transaction hashes, and input notes all render."""
        report = calm_report().model_copy(
            update={
                "actions": (
                    CycleActionRecord(
                        action="mint",
                        status="completed",
                        transaction_hashes=(MINT_TX_HASH,),
                        fee_wei=90_000,
                    ),
                )
            }
        )
        _, body = compose_cycle_email(report, ())
        assert "  mint: completed" in body
        assert f"    tx {MINT_TX_HASH}" in body
        assert "  - fixture note" in body

    def test_refused_cycle_renders_alerts_actions_and_the_halt(self) -> None:
        """An alerting email carries every section the halt deserves."""
        alerts = (
            "action mint refused [gas_price_above_cap]: the endpoint's gas price exceeds the cap",
        )
        subject, body = compose_cycle_email(halted_report(), alerts)
        assert subject.startswith("[aero-bot][ALERT] FIXc")
        assert "--- alerts " + "-" * 55 in body
        assert "  ! " + alerts[0] in body
        assert "--- actions " + "-" * 54 in body
        assert "  mint: refused [gas_price_above_cap]" in body
        assert "    the endpoint's gas price exceeds the cap" in body
        assert "  halted:        the mint action refused [gas_price_above_cap]" in body

    def test_unread_relayer_keeps_the_honest_line_without_a_zero_row(self) -> None:
        """A run without a relayer address reports unread, never a zero."""
        report = calm_report().model_copy(
            update={
                "reconciliation": calm_report().reconciliation.model_copy(
                    update={
                        "relayer_eth_wei": 0,
                        "diagnostics": (
                            "relayer ETH unread: no relayer address configured for this run",
                            "Safe holds 10 USDC, Safe holds 0 stock",
                        ),
                    }
                )
            }
        )
        _, body = compose_cycle_email(report, ())
        assert "  relayer ETH unread: no relayer address configured for this run" in body
        assert "relayer:" not in body

    def test_prose_numbers_render_without_noise_or_lost_precision_claims(self) -> None:
        """Overlong decimals humanize and dust reads as below the precision."""
        report = calm_report().model_copy(
            update={
                "reconciliation": calm_report().reconciliation.model_copy(
                    update={
                        "diagnostics": (
                            "tracked position 5703026 (staked) valued "
                            "9.123456789012345678901234 USDC against 9 committed",
                            "Safe holds 10 USDC",
                        ),
                    }
                ),
                "pnl_vs_entry_usdc": Decimal("1.250000000000000000000000"),
                "input_notes": (
                    "the relayer tank drifted to 0.000000000123 ETH overnight",
                    "fixture note",
                ),
            }
        )
        _, body = compose_cycle_email(report, ())
        assert "valued 9.123457 USDC against 9 committed" in body
        assert "  pnl vs entry:  1.25 USDC" in body
        assert "drifted to <0.000001 ETH overnight" in body


class TestDeliverCycleAlerts:
    """The delivery orchestration over the fake transport."""

    def test_provider_none_sends_nothing(self) -> None:
        """A silent configuration never builds a transport."""
        assert deliver_cycle_alerts(calm_report(), {}) is False

    def test_summary_email_sends_on_a_calm_cycle(self) -> None:
        """A configured provider emails the per-cycle summary."""
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert deliver_cycle_alerts(calm_report(), SMTP_ENV) is True
        assert len(transport.sent) == 1
        subject, body = transport.sent[0]
        assert subject.startswith("[aero-bot] FIXc")
        assert "  pnl vs entry:  1.25 USDC" in body

    def test_disabled_summaries_still_send_alerts(self) -> None:
        """Alerts override the summary-off setting."""
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert (
                deliver_cycle_alerts(
                    halted_report(), {**SMTP_ENV, ALERT_SUMMARY_EVERY_CYCLE_ENV: "0"}
                )
                is True
            )
        subject, _ = transport.sent[0]
        assert subject.startswith("[aero-bot][ALERT]")

    def test_disabled_summaries_skip_calm_cycles(self) -> None:
        """With summaries off, a calm cycle sends nothing."""
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert (
                deliver_cycle_alerts(
                    calm_report(), {**SMTP_ENV, ALERT_SUMMARY_EVERY_CYCLE_ENV: "0"}
                )
                is False
            )
        assert transport.sent == []

    def test_transport_failure_warns_without_raising(self) -> None:
        """A failed delivery warns on the stream and returns False."""
        errors = io.StringIO()
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            builder.return_value = FakeTransport(failure=AlertTransportError("server unreachable"))
            assert deliver_cycle_alerts(calm_report(), SMTP_ENV, error_stream=errors) is False
        assert "server unreachable" in errors.getvalue()

    def test_misconfiguration_names_the_variable(self) -> None:
        """A broken configuration warns naming the variable, never values."""
        errors = io.StringIO()
        partial = {key: value for key, value in SMTP_ENV.items() if key != ALERT_SMTP_HOST_ENV}
        assert deliver_cycle_alerts(calm_report(), partial, error_stream=errors) is False
        assert ALERT_SMTP_HOST_ENV in errors.getvalue()
        assert "sealed-smtp-password" not in errors.getvalue()


class TestSmtpTransport:
    """The SMTP submission boundary over a scripted server."""

    def test_starttls_login_and_send_message(self) -> None:
        """The exchange is STARTTLS, login, send_message, quit."""
        exchanges: list[tuple[str, object]] = []

        class ScriptedSmtp:
            def __init__(self, host: str, port: int, timeout: float) -> None:
                exchanges.append(("connect", (host, port, timeout)))

            def __enter__(self) -> "ScriptedSmtp":
                return self

            def __exit__(self, *args: object) -> None:
                exchanges.append(("quit", None))

            def starttls(self, context: object | None = None) -> None:
                exchanges.append(("starttls", context is not None))

            def login(self, user: str, password: str) -> None:
                exchanges.append(("login", user))

            def send_message(self, message: object) -> None:
                exchanges.append(("send", message))

        transport = SmtpEmailTransport(
            host="smtp.example.com",
            port=587,
            user="aero-bot@example.com",
            password="sealed",  # noqa: S106 - a fixture value, never real
            sender="aero-bot@example.com",
            recipients=("captain@example.com",),
        )
        with patch("aero_bot.alerts.smtplib.SMTP", ScriptedSmtp):
            transport.send("subject line", "body text")

        assert [name for name, _ in exchanges] == [
            "connect",
            "starttls",
            "login",
            "send",
            "quit",
        ]
        from email.message import EmailMessage

        message = cast(EmailMessage, exchanges[3][1])
        assert message["Subject"] == "subject line"
        assert message["From"] == "aero-bot@example.com"
        assert message["To"] == "captain@example.com"

    def test_server_failure_raises_without_echoing_the_password(self) -> None:
        """An SMTP failure raises the transport error, password-free."""

        class RefusingSmtp:
            def __init__(self, host: str, port: int, timeout: float) -> None: ...

            def __enter__(self) -> "RefusingSmtp":
                return self

            def __exit__(self, *args: object) -> None: ...

            def starttls(self, context: object | None = None) -> None:
                raise OSError("connection refused")

        transport = SmtpEmailTransport(
            host="smtp.example.com",
            port=587,
            user="aero-bot@example.com",
            password="sealed-password",  # noqa: S106 - a fixture value, never real
            sender="aero-bot@example.com",
            recipients=("captain@example.com",),
        )
        with (
            patch("aero_bot.alerts.smtplib.SMTP", RefusingSmtp),
            pytest.raises(AlertTransportError, match="connection refused") as error,
        ):
            transport.send("subject", "body")
        assert "sealed-password" not in str(error.value)


class TestResendTransport:
    """The HTTP boundary over a scripted client."""

    def test_bearer_post_with_the_resend_shape(self) -> None:
        """The request carries the bearer key and the Resend JSON shape."""
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "email-1"})

        transport = ResendHttpTransport(
            api_key="re_sealed",
            sender="alerts@example.com",
            recipients=("captain@example.com",),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        transport.send("cycle subject", "cycle body")

        assert len(requests) == 1
        request = requests[0]
        assert request.url == "https://api.resend.com/emails"
        assert request.headers["Authorization"] == "Bearer re_sealed"
        payload = cast("dict[str, object]", request.read().decode("utf-8"))
        assert "cycle subject" in str(payload)

    def test_nonzero_answers_raise_the_transport_error(self) -> None:
        """A 4xx answer raises without leaking the key."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"message": "invalid from"})

        transport = ResendHttpTransport(
            api_key="re_sealed",
            sender="alerts@example.com",
            recipients=("captain@example.com",),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(AlertTransportError, match="422") as error:
            transport.send("subject", "body")
        assert "re_sealed" not in str(error.value)


class TestProtocolConformance:
    """Both real transports satisfy the protocol the deliverer accepts."""

    def test_both_transports_implement_the_boundary(self) -> None:
        """The concrete transports are structural EmailTransports."""
        smtp = SmtpEmailTransport("h", 587, "u", "p", "from@example.com", ("to@example.com",))
        resend = ResendHttpTransport("k", "from@example.com", ("to@example.com",))
        assert isinstance(smtp, EmailTransport)
        assert isinstance(resend, EmailTransport)


class TestCycleWiring:
    """The cycle CLI delivers its email after the report."""

    def test_live_cycle_summary_flows_to_the_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A completed cycle over scripted state emails its summary."""
        for key, value in SMTP_ENV.items():
            monkeypatch.setenv(key, value)
        runner, _, _, _ = make_runner(tmp_path)
        report = runner.run(CycleMode.DRY_RUN)
        with patch("aero_bot.alerts.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            from aero_bot.alerts import deliver_cycle_alerts

            assert deliver_cycle_alerts(report) is True
        subject, body = transport.sent[0]
        assert subject.startswith("[aero-bot] FIXc")
        assert "  gas spent:     0 wei" in body


def test_quiet_instant_fixture_stays_aware() -> None:
    """The shared fixture instant remains timezone-aware for reports."""
    assert QUIET_INSTANT.tzinfo is UTC
    assert mint_receipt(1)["logs"]
