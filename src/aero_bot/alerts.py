"""Email alerts for the decision cycle: per-cycle summaries and threshold warnings.

The alert hook turns every cycle report into at most one email: the per-cycle
performance summary (position state, P&L vs entry, gas spent) whenever
summaries are enabled, and - always, whatever the summary setting - an
alerting email when anything in the cycle deserves eyes: an out-of-band
condition, a halted cycle, a refused or failed action, or a balance below
its floor (the relayer's ETH gas tank, the Safe's USDC working capital).

Credentials live only in the environment, sealed by the process supervisor:
a provider-agnostic SMTP transport (``AERO_BOT_ALERT_PROVIDER=smtp``) reads
its host, port, user, and password from ``AERO_BOT_ALERT_SMTP_*`` variables,
and a Resend-style HTTP transport (``AERO_BOT_ALERT_PROVIDER=resend``) reads
its bearer key from ``AERO_BOT_ALERT_RESEND_API_KEY``. Nothing
secrets-shaped is stored in the repository, logged, or echoed in errors:
configuration problems name the missing variable, never any value, and
transport failures surface as a warning line on stderr without ever
crashing the cycle they describe.
"""

import os
import re
import smtplib
import ssl
import sys
from collections.abc import Mapping
from datetime import UTC
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from enum import StrEnum
from typing import Protocol, TextIO, runtime_checkable

import httpx

from aero_bot.cycle import CycleReport

# Environment variable selecting the alert transport: smtp, resend, or none.
ALERT_PROVIDER_ENV = "AERO_BOT_ALERT_PROVIDER"
# Environment variables carrying the shared sender and recipients.
ALERT_FROM_ENV = "AERO_BOT_ALERT_FROM"
ALERT_TO_ENV = "AERO_BOT_ALERT_TO"
# SMTP transport configuration.
ALERT_SMTP_HOST_ENV = "AERO_BOT_ALERT_SMTP_HOST"
ALERT_SMTP_PORT_ENV = "AERO_BOT_ALERT_SMTP_PORT"
ALERT_SMTP_USER_ENV = "AERO_BOT_ALERT_SMTP_USER"
ALERT_SMTP_PASSWORD_ENV = "AERO_BOT_ALERT_SMTP_PASSWORD"  # noqa: S105 - a variable name, not a secret
# Resend-style HTTP transport configuration.
ALERT_RESEND_API_KEY_ENV = "AERO_BOT_ALERT_RESEND_API_KEY"
ALERT_RESEND_URL_ENV = "AERO_BOT_ALERT_RESEND_URL"
# Whether every cycle sends its summary email; alerts always send.
ALERT_SUMMARY_EVERY_CYCLE_ENV = "AERO_BOT_ALERT_SUMMARY_EVERY_CYCLE"
# Balance-floor alert thresholds, overridable from the environment.
ALERT_RELAYER_ETH_FLOOR_WEI_ENV = "AERO_BOT_ALERT_RELAYER_ETH_FLOOR_WEI"
ALERT_SAFE_USDC_FLOOR_UNITS_ENV = "AERO_BOT_ALERT_SAFE_USDC_FLOOR_UNITS"
# The default Resend endpoint; any Resend-style API shares the shape.
DEFAULT_RESEND_URL = "https://api.resend.com/emails"
# The default SMTP submission port with STARTTLS.
DEFAULT_SMTP_PORT = 587
# Default alert floors: half a milli-ETH of relayer gas headroom above the
# executor's 0.0002-ETH broadcast floor, and five USDC of Safe working
# capital for a pilot funded around twenty.
DEFAULT_RELAYER_ETH_FLOOR_WEI = 500_000_000_000_000
DEFAULT_SAFE_USDC_FLOOR_UNITS = 5_000_000
# One email bound: the server gets its seconds, the cycle gets its exit.
SMTP_TIMEOUT_SECONDS = 30.0
HTTP_TIMEOUT_SECONDS = 20.0

# The display precision every token quantity in an email renders at: plain
# decimal notation, at most six fractional digits, trailing zeros stripped.
# Six decimals keeps USDC (a six-decimal token) exact and reads ETH and stock
# holdings at full pilot significance without 28-digit Decimal noise.
_QUANTITY_QUANTUM = Decimal("0.000001")
# Decimal tokens that read badly in email prose: Python's scientific
# notation (like 0E-8) and any decimal longer than the display precision.
# Short decimals, integers, hashes, and addresses never match, so sentence
# wording around a rewritten token is preserved verbatim.
_AWKWARD_DECIMAL = re.compile(r"(?<![\w.])(?:\d+(?:\.\d+)?[eE][+-]?\d+|\d+\.\d{7,})(?![\w.])")
# The width every dashed section separator fills to; one calm email stays
# on a single screen.
_SECTION_WIDTH = 66


class AlertTransportError(RuntimeError):
    """Signal that one alert email could not be delivered."""


class AlertProvider(StrEnum):
    """Identify the configured alert transport."""

    # No emails at all: the default until the operator wires a provider.
    NONE = "none"
    # Provider-agnostic SMTP with STARTTLS login.
    SMTP = "smtp"
    # Resend-style HTTP API with a bearer key.
    RESEND = "resend"


@runtime_checkable
class EmailTransport(Protocol):
    """Define the boundary every alert transport implements."""

    def send(self, subject: str, body: str) -> None:
        """Deliver one email or raise ``AlertTransportError``.

        Args:
            subject: The email's subject line.
            body: The email's plain-text body.
        """
        ...


class SmtpEmailTransport:
    """Deliver alert emails through any STARTTLS SMTP provider."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        sender: str,
        recipients: tuple[str, ...],
    ) -> None:
        """Configure one SMTP submission boundary.

        Args:
            host: The SMTP server hostname.
            port: The STARTTLS submission port, 587 by default.
            user: The SMTP authentication user.
            password: The SMTP password; held only for the login call and
                never logged or echoed.
            sender: The envelope From address.
            recipients: The alert recipients, at least one.
        """
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._sender = sender
        self._recipients = recipients

    def send(self, subject: str, body: str) -> None:
        """Deliver one email over STARTTLS.

        Args:
            subject: The email's subject line.
            body: The email's plain-text body.

        Raises:
            AlertTransportError: If the server exchange fails; the message
                never echoes the password.
        """
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(self._user, self._password)
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            raise AlertTransportError(
                f"the SMTP alert delivery to {self._host}:{self._port} failed: {error}"
            ) from error


class ResendHttpTransport:
    """Deliver alert emails through a Resend-style HTTP API."""

    def __init__(
        self,
        api_key: str,
        sender: str,
        recipients: tuple[str, ...],
        url: str = DEFAULT_RESEND_URL,
        client: httpx.Client | None = None,
    ) -> None:
        """Configure one bearer-keyed HTTP boundary.

        Args:
            api_key: The bearer key; held only for the Authorization header
                and never logged or echoed.
            sender: The From address the API sends as.
            recipients: The alert recipients, at least one.
            url: The API endpoint; any Resend-shaped service works.
            client: Optional injected HTTP client for deterministic tests.
        """
        self._api_key = api_key
        self._sender = sender
        self._recipients = recipients
        self._url = url
        self._client = client

    def send(self, subject: str, body: str) -> None:
        """Deliver one email through the HTTP API.

        Args:
            subject: The email's subject line.
            body: The email's plain-text body.

        Raises:
            AlertTransportError: If the request fails or answers nonzero.
        """
        request = httpx.Request(
            "POST",
            self._url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "from": self._sender,
                "to": list(self._recipients),
                "subject": subject,
                "text": body,
            },
        )
        try:
            if self._client is not None:
                response = self._client.send(request)
            else:
                with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    response = client.send(request)
        except httpx.HTTPError as error:
            raise AlertTransportError(
                f"the HTTP alert delivery to {self._url} failed: {error}"
            ) from error
        if response.status_code // 100 != 2:
            raise AlertTransportError(
                f"the HTTP alert delivery to {self._url} answered {response.status_code}"
            )


class AlertConfig:
    """Carry the environment-driven alert configuration; values stay in env."""

    def __init__(
        self,
        provider: AlertProvider,
        sender: str,
        recipients: tuple[str, ...],
        summary_every_cycle: bool,
        relayer_eth_floor_wei: int,
        safe_usdc_floor_units: int,
        smtp: "SmtpSettings | None" = None,
        resend: "ResendSettings | None" = None,
    ) -> None:
        """Bind the validated alert configuration.

        Args:
            provider: The selected transport, none by default.
            sender: The From address for every alert.
            recipients: The alert recipients, at least one.
            summary_every_cycle: Whether calm cycles email their summary.
            relayer_eth_floor_wei: The relayer ETH alert floor in wei.
            safe_usdc_floor_units: The Safe USDC alert floor in raw units.
            smtp: SMTP settings when the provider is smtp.
            resend: Resend settings when the provider is resend.
        """
        self.provider = provider
        self.sender = sender
        self.recipients = recipients
        self.summary_every_cycle = summary_every_cycle
        self.relayer_eth_floor_wei = relayer_eth_floor_wei
        self.safe_usdc_floor_units = safe_usdc_floor_units
        self.smtp = smtp
        self.resend = resend


class SmtpSettings:
    """Carry the SMTP transport settings; the password stays memory-only."""

    def __init__(self, host: str, port: int, user: str, password: str) -> None:
        """Bind the SMTP submission settings.

        Args:
            host: The SMTP server hostname.
            port: The STARTTLS submission port.
            user: The SMTP authentication user.
            password: The SMTP password, never logged or echoed.
        """
        self.host = host
        self.port = port
        self.user = user
        self.password = password


class ResendSettings:
    """Carry the Resend-style transport settings; the key stays memory-only."""

    def __init__(self, api_key: str, url: str) -> None:
        """Bind the HTTP transport settings.

        Args:
            api_key: The bearer key, never logged or echoed.
            url: The API endpoint URL.
        """
        self.api_key = api_key
        self.url = url


def _positive_int(value: str, variable: str) -> int:
    """Parse one strictly positive integer environment value."""
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{variable} must be positive, not {value!r}")
    return parsed


def _non_negative_int(value: str, variable: str) -> int:
    """Parse one non-negative integer environment value."""
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{variable} must be non-negative, not {value!r}")
    return parsed


def parse_alert_config(environ: Mapping[str, str] | None = None) -> AlertConfig:
    """Build the alert configuration from the process environment.

    Args:
        environ: The process environment; None reads the live one. Secrets
            are read here and nowhere else, and problems name variables,
            never values.

    Returns:
        The validated configuration; provider none needs no credentials.

    Raises:
        ValueError: If an active provider lacks its required variables or a
            threshold is malformed. The message names the missing variable
            only - never any secret value.
    """
    resolved = os.environ if environ is None else environ
    raw_provider = resolved.get(ALERT_PROVIDER_ENV, AlertProvider.NONE.value).strip().lower()
    try:
        provider = AlertProvider(raw_provider)
    except ValueError:
        raise ValueError(
            f"{ALERT_PROVIDER_ENV} must be one of "
            f"{{{','.join(member.value for member in AlertProvider)}}}, "
            f"not {raw_provider!r}"
        ) from None
    raw_floor_eth = resolved.get(ALERT_RELAYER_ETH_FLOOR_WEI_ENV, "").strip()
    raw_floor_usdc = resolved.get(ALERT_SAFE_USDC_FLOOR_UNITS_ENV, "").strip()
    config = AlertConfig(
        provider=provider,
        sender=resolved.get(ALERT_FROM_ENV, "").strip(),
        recipients=tuple(
            dict.fromkeys(
                recipient.strip()
                for recipient in resolved.get(ALERT_TO_ENV, "").split(",")
                if recipient.strip()
            )
        ),
        summary_every_cycle=resolved.get(ALERT_SUMMARY_EVERY_CYCLE_ENV, "1").strip().lower()
        not in {"0", "false", "no"},
        relayer_eth_floor_wei=(
            _non_negative_int(raw_floor_eth, ALERT_RELAYER_ETH_FLOOR_WEI_ENV)
            if raw_floor_eth
            else DEFAULT_RELAYER_ETH_FLOOR_WEI
        ),
        safe_usdc_floor_units=(
            _non_negative_int(raw_floor_usdc, ALERT_SAFE_USDC_FLOOR_UNITS_ENV)
            if raw_floor_usdc
            else DEFAULT_SAFE_USDC_FLOOR_UNITS
        ),
    )
    if provider is AlertProvider.NONE:
        return config
    missing = [
        (ALERT_FROM_ENV, config.sender),
        (ALERT_TO_ENV, config.recipients),
    ]
    missing_names = [name for name, value in missing if not value]
    if provider is AlertProvider.SMTP:
        host = resolved.get(ALERT_SMTP_HOST_ENV, "").strip()
        user = resolved.get(ALERT_SMTP_USER_ENV, "").strip()
        password = resolved.get(ALERT_SMTP_PASSWORD_ENV, "")
        raw_port = resolved.get(ALERT_SMTP_PORT_ENV, "").strip()
        port = _positive_int(raw_port, ALERT_SMTP_PORT_ENV) if raw_port else DEFAULT_SMTP_PORT
        missing_names.extend(
            name
            for name, value in (
                (ALERT_SMTP_HOST_ENV, host),
                (ALERT_SMTP_USER_ENV, user),
                (ALERT_SMTP_PASSWORD_ENV, password),
            )
            if not value
        )
        if missing_names:
            raise ValueError(
                f"the smtp alert provider is missing required variables: {', '.join(missing_names)}"
            )
        config.smtp = SmtpSettings(host=host, port=port, user=user, password=password)
        return config
    api_key = resolved.get(ALERT_RESEND_API_KEY_ENV, "")
    url = resolved.get(ALERT_RESEND_URL_ENV, "").strip() or DEFAULT_RESEND_URL
    if not api_key:
        missing_names.append(ALERT_RESEND_API_KEY_ENV)
    if missing_names:
        raise ValueError(
            f"the resend alert provider is missing required variables: {', '.join(missing_names)}"
        )
    config.resend = ResendSettings(api_key=api_key, url=url)
    return config


def build_alert_transport(config: AlertConfig) -> EmailTransport | None:
    """Build the configured transport, or None when alerts are off.

    Args:
        config: The validated alert configuration.

    Returns:
        The transport boundary, or None for provider none.
    """
    if config.provider is AlertProvider.NONE:
        return None
    if config.provider is AlertProvider.SMTP and config.smtp is not None:
        return SmtpEmailTransport(
            host=config.smtp.host,
            port=config.smtp.port,
            user=config.smtp.user,
            password=config.smtp.password,
            sender=config.sender,
            recipients=config.recipients,
        )
    if config.provider is AlertProvider.RESEND and config.resend is not None:
        return ResendHttpTransport(
            api_key=config.resend.api_key,
            sender=config.sender,
            recipients=config.recipients,
            url=config.resend.url,
        )
    return None


def evaluate_alerts(report: CycleReport, config: AlertConfig) -> tuple[str, ...]:
    """Derive every alert line one cycle's report deserves.

    Args:
        report: The complete cycle report being examined.
        config: The configuration carrying the balance floors.

    Returns:
        The alert lines in fixed order: out-of-band first, then the halt,
        every refused or failed action, and the balance floors.
    """
    alerts: list[str] = []
    reconciliation = report.reconciliation
    if reconciliation.out_of_band:
        alerts.append(f"out of band: {reconciliation.out_of_band}")
    if report.halted_reason and not reconciliation.out_of_band:
        alerts.append(f"cycle halted: {report.halted_reason}")
    for action in report.actions:
        if action.status == "refused":
            alerts.append(
                f"action {action.action} refused [{action.refusal_code}]: {action.diagnostic}"
            )
        elif action.status == "failed":
            alerts.append(f"action {action.action} failed: {action.diagnostic}")
    if (
        reconciliation.relayer_eth_wei > 0
        and reconciliation.relayer_eth_wei < config.relayer_eth_floor_wei
    ):
        alerts.append(
            f"relayer ETH {reconciliation.relayer_eth_wei} wei is below the "
            f"{config.relayer_eth_floor_wei}-wei alert floor; top up the gas tank"
        )
    if reconciliation.safe_usdc_units < config.safe_usdc_floor_units:
        alerts.append(
            f"Safe USDC {reconciliation.safe_usdc_units} raw units is below the "
            f"{config.safe_usdc_floor_units}-unit alert floor; working capital is low"
        )
    return tuple(alerts)


def _format_quantity(value: Decimal) -> str:
    """Render one token quantity for humans: plain, at most six decimals.

    Args:
        value: The quantity as a scaled Decimal, any exponent.

    Returns:
        The readable plain-decimal rendering; a nonzero quantity below the
        display precision renders as ``<0.000001`` (never a silent zero).
    """
    if value == 0:
        return "0"
    if value.adjusted() > 21:
        # Absurdly large for a token quantity; keep every digit, plainly.
        return format(value, "f")
    quantized = value.quantize(_QUANTITY_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized == 0:
        return "<0.000001" if value > 0 else ">-0.000001"
    return format(quantized, "f").rstrip("0").rstrip(".")


def _humanize_numbers(text: str) -> str:
    """Rewrite every awkward decimal token in one prose line for humans.

    Args:
        text: A diagnostic, alert, action, or note line as produced.

    Returns:
        The same line with scientific notation and overlong decimals
        rendered at the display precision; every word around a rewritten
        token is kept verbatim.
    """
    return _AWKWARD_DECIMAL.sub(lambda match: _format_quantity(Decimal(match.group(0))), text)


def _section(title: str) -> str:
    """Build one dashed separator line carrying its section title."""
    return f"--- {title} ".ljust(_SECTION_WIDTH, "-")


def _state_lines(report: CycleReport) -> list[str]:
    """Render the reconciliation as scannable balance rows plus its story.

    The structured rows rebuild the reconciliation's closing balance
    summary from the raw fields so the numbers carry units and the stock
    carries its symbol; every other diagnostic line follows verbatim
    (numbers humanized), so no honest observation is dropped.

    Args:
        report: The cycle report whose reconciliation is rendered.

    Returns:
        The indented state lines, in report order after the balances.
    """
    reconciliation = report.reconciliation
    # The one unread case: a run without a relayer address reports zero wei
    # and says so; that honest line stays and no zero row is invented.
    relayer_readable = not any(
        line.startswith("relayer ETH unread") for line in reconciliation.diagnostics
    )
    safe_usdc = Decimal(reconciliation.safe_usdc_units).scaleb(-6)
    lines = [
        f"  {'Safe:':<10}{_format_quantity(safe_usdc)} USDC, "
        f"{_format_quantity(reconciliation.held_stock_quantity)} {report.symbol}"
    ]
    if relayer_readable:
        relayer_eth = Decimal(reconciliation.relayer_eth_wei).scaleb(-18)
        lines.append(f"  {'relayer:':<10}{_format_quantity(relayer_eth)} ETH")
    for line in reconciliation.diagnostics:
        # The closing "Safe holds ..." summary is the balance row rebuilt
        # above from the same raw fields; matching its exact producer
        # prefix keeps every other diagnostic line flowing through.
        if line.startswith("Safe holds "):
            continue
        lines.append(f"  {_humanize_numbers(line)}")
    return lines


def compose_cycle_email(report: CycleReport, alerts: tuple[str, ...]) -> tuple[str, str]:
    """Compose one cycle's email subject and plain-text body.

    The body is one scannable screen: a titled header over the cycle's
    mode, start (UTC, microsecond-free), decision, and event window, then
    dashed sections for the reconciled state, any alerts and actions, the
    outcome (P&L vs entry, gas spent, a halt when one happened), and the
    honest-input notes as a bullet list. Quantities render at the display
    precision and the stock carries its symbol, but every fact and every
    disclosure keeps its wording.

    Args:
        report: The complete cycle report being summarized.
        alerts: The evaluated alert lines, empty on a calm cycle.

    Returns:
        The subject line and the plain-text body.
    """
    if alerts:
        subject = f"[aero-bot][ALERT] {report.symbol} cycle {report.decision_action}: {alerts[0]}"
        if len(subject) > 120:
            subject = subject[:117] + "..."
    else:
        subject = (
            f"[aero-bot] {report.symbol} cycle {report.decision_action} ({report.decision_reason})"
        )
    title = f"Aero Bot cycle report - {report.symbol}"
    started = report.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines: list[str] = [
        title,
        "=" * len(title),
        "",
        f"{'mode:':<14}{report.mode.value}",
        f"{'started:':<14}{started}",
        f"{'decision:':<14}{report.decision_action} ({report.decision_reason})",
        f"{'event window:':<14}{report.event_window}",
        "",
        _section("state"),
        "",
        *_state_lines(report),
    ]
    if alerts:
        lines.extend(["", _section("alerts"), ""])
        lines.extend(f"  ! {_humanize_numbers(line)}" for line in alerts)
    if report.actions:
        lines.extend(["", _section("actions"), ""])
        for action in report.actions:
            suffix = f" [{action.refusal_code}]" if action.refusal_code else ""
            lines.append(f"  {action.action}: {action.status}{suffix}")
            for transaction_hash in action.transaction_hashes:
                lines.append(f"    tx {transaction_hash}")
            if action.diagnostic:
                lines.append(f"    {_humanize_numbers(action.diagnostic)}")
    lines.extend(["", _section("outcome"), ""])
    if report.pnl_vs_entry_usdc is not None:
        pnl = f"{_format_quantity(report.pnl_vs_entry_usdc)} USDC"
    else:
        pnl = f"unavailable ({_humanize_numbers(report.pnl_diagnostic)})"
    lines.append(f"  {'pnl vs entry:':<15}{pnl}")
    lines.append(f"  {'gas spent:':<15}{report.fee_wei} wei")
    if report.halted_reason:
        lines.append(f"  {'halted:':<15}{report.halted_reason}")
    if report.input_notes:
        lines.extend(["", _section("notes"), ""])
        lines.extend(f"  - {_humanize_numbers(note)}" for note in report.input_notes)
    return subject, "\n".join(lines) + "\n"


def deliver_cycle_alerts(
    report: CycleReport,
    environ: Mapping[str, str] | None = None,
    error_stream: TextIO | None = None,
) -> bool:
    """Deliver one cycle's email; a transport failure never crashes the cycle.

    Args:
        report: The complete cycle report to summarize and alert on.
        environ: The environment carrying the alert configuration; None
            reads the live process environment.
        error_stream: Where delivery warnings land; stderr by default.

    Returns:
        Whether an email was sent. Provider none, calm cycles with summaries
        disabled, and failed deliveries all return False.
    """
    resolved_environ = os.environ if environ is None else environ
    stream = error_stream if error_stream is not None else sys.stderr
    try:
        config = parse_alert_config(resolved_environ)
    except ValueError as error:
        print(f"email alerts are misconfigured: {error}", file=stream)
        return False
    transport = build_alert_transport(config)
    if transport is None:
        return False
    alerts = evaluate_alerts(report, config)
    if not alerts and not config.summary_every_cycle:
        return False
    subject, body = compose_cycle_email(report, alerts)
    try:
        transport.send(subject, body)
    except AlertTransportError as error:
        print(f"email alert delivery failed: {error}", file=stream)
        return False
    return True


__all__ = [
    "ALERT_PROVIDER_ENV",
    "AlertConfig",
    "AlertProvider",
    "AlertTransportError",
    "EmailTransport",
    "ResendHttpTransport",
    "ResendSettings",
    "SmtpEmailTransport",
    "SmtpSettings",
    "build_alert_transport",
    "compose_cycle_email",
    "deliver_cycle_alerts",
    "evaluate_alerts",
    "parse_alert_config",
]
