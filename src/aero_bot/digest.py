"""The daily digest email: one bounded summary of the prior 24 hours.

The digest is the captain's 2026-10-05 email ruling made concrete: exactly
one email per day (the 09:00 Australia/Melbourne daily-report tick), composed
from the audit chain's own records, while ``AERO_BOT_ALERT_MODE=digest``
suppresses every immediate email path (see :mod:`aero_bot.alerts`).

Everything here is derived - never invented: every trading, action, reward,
and failure line restates a durable audit record inside the window, the
student's advice block restates the latest ``advisor_reported`` record with
its provenance and age, and the teacher block restates the bounded
one-way evidence artifact the Mac-side publisher writes beside the audit
store (firstmate 028's minimal transfer) - with the publish provenance,
per-episode timestamps, and an explicit missing, malformed, or stale marker
whenever the artifact is absent, unparseable, or older than the stale bound.
A failed send warns on stderr and never raises, exactly like the
per-cycle hook it replaces for this run.
"""

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, TextIO

from aero_bot.alerts import (
    AlertTransportError,
    build_alert_transport,
    parse_alert_config,
)
from aero_bot.audit import MAX_RECORDS_PER_READ, AuditEventType, AuditRecord, AuditStore

# The digest window: the prior twenty-four hours of audit history.
DIGEST_WINDOW_HOURS = 24
# Bounded backward paging over the chain: twelve pages of the store's own
# read cap cover 12,000 records - comfortably above a noisy day (~1,000)
# while never turning one email into an unbounded scan.
DIGEST_MAX_PAGES = 12
# The subject prefix distinguishing the digest from every suppressed email.
DIGEST_SUBJECT_PREFIX = "[aero-bot][DIGEST]"
# Display bounds: the failures and actions sections cap their line counts,
# and the student's brief truncates, so one digest stays on a few screens.
DIGEST_MAX_FAILURE_LINES = 40
DIGEST_MAX_ACTION_LINES = 60
DIGEST_BRIEF_MAX_CHARS = 600
# AERO's eighteen decimals, for raw reward units.
_AERO_DECIMALS = 18
# The display precision every token quantity renders at; identical to the
# per-cycle email's convention (six decimals, trailing zeros stripped).
_QUANTITY_QUANTUM = Decimal("0.000001")


class DigestRecordSource(Protocol):
    """Define the bounded read surface the digest composes from."""

    def read_records(self, limit: int = 100, *, offset: int = 0) -> tuple[AuditRecord, ...]:
        """Read a bounded ascending window of immutable audit records."""
        ...


class TeacherAdviceView:
    """Carry the teacher evidence's loaded state for the digest."""

    def __init__(self, document: object | None, state: str) -> None:
        """Bind the parsed document (when valid) and its state marker.

        Args:
            document: The validated artifact payload dict, or None.
            state: One of fresh, stale, missing, malformed - the honest
                marker the digest renders beside (or instead of) the advice.
        """
        self.document = document
        self.state = state


def _format_quantity(value: Decimal | None) -> str:
    """Render one token quantity at the email display precision."""
    if value is None:
        return "unmeasured"
    rendered = value.quantize(_QUANTITY_QUANTUM).normalize()
    if rendered == rendered.to_integral_value():
        rendered = rendered.to_integral_value()
    return f"{rendered}"


def _parse_decimal(raw: object) -> Decimal | None:
    """Parse one payload field as a Decimal, tolerating every absence."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _format_aero(raw_units: Decimal | None) -> str:
    """Render raw eighteen-decimal AERO units at the display precision."""
    if raw_units is None:
        return "unmeasured"
    return _format_quantity(raw_units.scaleb(-_AERO_DECIMALS))


def _payload(record: AuditRecord) -> dict[str, object]:
    """Decode one record's canonical payload JSON tolerantly."""
    try:
        decoded = json.loads(record.payload_json)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _stamp(moment: datetime) -> str:
    """Render one audit timestamp at minute precision, UTC."""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def load_teacher_advice(
    path: Path,
    now: datetime,
    stale_hours: int = 24,
    max_bytes: int = 16_384,
) -> TeacherAdviceView:
    """Load and validate the published teacher-advice artifact.

    Args:
        path: The artifact path beside the audit store.
        now: The composition instant, for the stale marker.
        stale_hours: The age bound past which the advice is stale.
        max_bytes: The size bound past which the artifact is discarded.

    Returns:
        The view: a validated document with state fresh or stale, or an
        empty document with state missing or malformed. Every failure is
        honest and bounded - nothing raises, and the caller renders the
        state marker verbatim.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return TeacherAdviceView(None, "missing")
    if not raw or len(raw) > max_bytes:
        return TeacherAdviceView(None, "malformed")
    try:
        document = json.loads(raw)
    except ValueError:
        return TeacherAdviceView(None, "malformed")
    if not isinstance(document, dict) or document.get("schema_version") != "teacher_advice/1":
        return TeacherAdviceView(None, "malformed")
    episodes = document.get("episodes")
    if not isinstance(episodes, list):
        return TeacherAdviceView(None, "malformed")
    stamps: list[datetime] = []
    for episode in episodes:
        if not isinstance(episode, dict):
            return TeacherAdviceView(None, "malformed")
        stamp = episode.get("created_at")
        if not isinstance(stamp, str):
            return TeacherAdviceView(None, "malformed")
        try:
            stamps.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        except ValueError:
            return TeacherAdviceView(None, "malformed")
    if stamps and now - max(stamps) > timedelta(hours=stale_hours):
        return TeacherAdviceView(document, "stale")
    return TeacherAdviceView(document, "fresh")


def collect_digest_records(
    store: DigestRecordSource,
    now: datetime,
    window_hours: int = DIGEST_WINDOW_HOURS,
) -> tuple[AuditRecord, ...]:
    """Read the audit window the digest composes from, newest-page-first.

    Args:
        store: The audit store to read.
        now: The timezone-aware composition instant.
        window_hours: The window's length in hours.

    Returns:
        Ascending records whose timestamps fall inside the window; the read
        stops at the first page fully older than the cutoff or the page
        bound, so a quiet or noisy day costs the same bounded scan.
    """
    cutoff = now - timedelta(hours=window_hours)
    total = 0
    collected: list[AuditRecord] = []
    for _ in range(DIGEST_MAX_PAGES):
        page = store.read_records(limit=MAX_RECORDS_PER_READ, offset=total)
        if not page:
            break
        total += len(page)
        in_window = [record for record in page if record.created_at >= cutoff]
        collected.extend(in_window)
        if len(in_window) < len(page):
            break
    collected.sort(key=lambda record: record.sequence)
    return tuple(collected)


def _compose_failures(records: tuple[AuditRecord, ...]) -> tuple[list[str], int]:
    """Restate every persisted failure class inside the window."""
    lines: list[str] = []
    for record in records:
        payload = _payload(record)
        if record.event_type is AuditEventType.CYCLE_REPORTED:
            reason = payload.get("halted_reason")
            if isinstance(reason, str) and reason:
                lines.append(f"halted cycle {_stamp(record.created_at)}: {reason}")
        elif record.event_type is AuditEventType.LP_EXECUTE_FAILED:
            lines.append(
                f"reverted delivery {_stamp(record.created_at)}: "
                f"{payload.get('action', 'action')} {payload.get('transaction_hash', '')}"
            )
        elif record.event_type is AuditEventType.EXECUTION_FAILED:
            lines.append(
                f"reverted delivery {_stamp(record.created_at)}: "
                f"{payload.get('role', 'role')} {payload.get('transaction_hash', '')}"
            )
        elif record.event_type is AuditEventType.LP_EXECUTE_BROADCAST_UNKNOWN:
            lines.append(
                f"broadcast unknown {_stamp(record.created_at)}: "
                f"{payload.get('action', 'action')} {payload.get('transaction_hash', '')}"
            )
        elif record.event_type is AuditEventType.LP_REFUSED:
            code = payload.get("code", "")
            message = payload.get("message", "")
            symbol = payload.get("symbol") or ""
            suffix = f" for {symbol}" if symbol else ""
            lines.append(
                f"refused {_stamp(record.created_at)}: {payload.get('action', 'action')}"
                f"{suffix} [{code}] {message}"
            )
    hidden = max(0, len(lines) - DIGEST_MAX_FAILURE_LINES)
    return lines[:DIGEST_MAX_FAILURE_LINES], hidden


def _compose_trading(records: tuple[AuditRecord, ...]) -> list[str]:
    """Restate the window's decision flow and equity path."""
    cycles = [record for record in records if record.event_type is AuditEventType.CYCLE_REPORTED]
    lines: list[str] = []
    if not cycles:
        return ["no cycle ran in the window"]
    decisions: dict[str, int] = {}
    for record in cycles:
        payload = _payload(record)
        action = payload.get("action")
        if isinstance(action, str) and action:
            decisions[action] = decisions.get(action, 0) + 1
    breakdown = ", ".join(f"{count} {action}" for action, count in sorted(decisions.items()))
    live = sum(1 for record in cycles if _payload(record).get("mode") == "live")
    lines.append(f"cycles: {len(cycles)} ({live} live, {len(cycles) - live} dry): {breakdown}")
    equities = [
        value
        for value in (_parse_decimal(_payload(record).get("equity_usdc")) for record in cycles)
        if value is not None
    ]
    if equities:
        lines.append(
            f"equity: {_format_quantity(equities[0])} -> {_format_quantity(equities[-1])} "
            f"(latest {_format_quantity(equities[-1])} USDC)"
        )
    day_pnls = [
        value
        for value in (_parse_decimal(_payload(record).get("day_pnl_usdc")) for record in cycles)
        if value is not None
    ]
    if day_pnls:
        lines.append(
            f"day P&L marks in window: min {_format_quantity(min(day_pnls))}, "
            f"max {_format_quantity(max(day_pnls))} USDC"
        )
    latest = _payload(cycles[-1])
    token_id = latest.get("tracked_token_id")
    position_value = _parse_decimal(latest.get("position_value_usdc"))
    if token_id is not None:
        lines.append(
            f"tracked position: NFT {token_id} valued {_format_quantity(position_value)} USDC"
        )
    else:
        lines.append("tracked position: none (flat book at the latest cycle)")
    return lines


def _compose_actions(records: tuple[AuditRecord, ...]) -> tuple[list[str], int]:
    """Restate every broadcast delivery in the window with its outcome."""
    outcomes: dict[str, str] = {}
    for record in records:
        if record.event_type in (
            AuditEventType.LP_EXECUTE_CONFIRMED,
            AuditEventType.EXECUTION_CONFIRMED,
        ):
            raw_hash = _payload(record).get("transaction_hash", "")
            if isinstance(raw_hash, str):
                outcomes[raw_hash] = "confirmed"
        elif record.event_type in (
            AuditEventType.LP_EXECUTE_FAILED,
            AuditEventType.EXECUTION_FAILED,
        ):
            raw_hash = _payload(record).get("transaction_hash", "")
            if isinstance(raw_hash, str):
                outcomes[raw_hash] = "reverted"
    lines: list[str] = []
    for record in records:
        if record.event_type not in (
            AuditEventType.LP_EXECUTE_SENT,
            AuditEventType.EXECUTION_SENT,
        ):
            continue
        payload = _payload(record)
        name = str(payload.get("action") or payload.get("role") or "action")
        digest_hash = str(payload.get("transaction_hash", ""))
        outcome = outcomes.get(digest_hash, "unknown outcome")
        lines.append(f"{name} {_stamp(record.created_at)}: {digest_hash} ({outcome})")
    hidden = max(0, len(lines) - DIGEST_MAX_ACTION_LINES)
    return lines[:DIGEST_MAX_ACTION_LINES], hidden


def _compose_rewards(records: tuple[AuditRecord, ...]) -> list[str]:
    """Restate the window's reward claims and the latest yield attribution."""
    lines: list[str] = []
    accrued_raw = Decimal("0")
    collects = 0
    for record in records:
        if record.event_type is not AuditEventType.LP_COLLECT_PLANNED:
            continue
        collects += 1
        earned = _parse_decimal(_payload(record).get("accrued_aero_earned_units"))
        if earned is not None:
            accrued_raw += earned
    if collects:
        lines.append(
            f"reward claims planned: {collects}, accrued earned beside them "
            f"{_format_quantity(accrued_raw.scaleb(-_AERO_DECIMALS))} AERO"
        )
    else:
        lines.append("reward claims planned: none in the window")
    cycles = [record for record in records if record.event_type is AuditEventType.CYCLE_REPORTED]
    if cycles:
        latest = _payload(cycles[-1])
        rewards_usdc = _parse_decimal(latest.get("yield_aero_rewards_usdc"))
        rewards_units = _parse_decimal(latest.get("yield_aero_rewards_units"))
        fees = _parse_decimal(latest.get("yield_fees_earned_usdc"))
        unclaimed = _parse_decimal(latest.get("unclaimed_aero_value_usdc"))
        lines.append(
            "latest attribution: AERO rewards "
            f"{_format_quantity(rewards_usdc)} USDC "
            f"({_format_aero(rewards_units)} AERO), "
            f"computed fees {_format_quantity(fees)} USDC"
        )
        lines.append(
            f"unclaimed AERO value at the latest cycle: {_format_quantity(unclaimed)} USDC"
        )
    return lines


def _compose_student_advice(records: tuple[AuditRecord, ...], now: datetime) -> list[str]:
    """Restate the latest student brief with provenance and explicit staleness."""
    advisories = [
        record for record in records if record.event_type is AuditEventType.ADVISOR_REPORTED
    ]
    if not advisories:
        return [
            "student advice: NONE in the window - no advisor pass reported; "
            "the seat's own audit trail names why on the box",
        ]
    passes = len(advisories)
    absences: dict[str, int] = {}
    briefed = 0
    for record in advisories:
        payload = _payload(record)
        outcome = payload.get("outcome")
        if isinstance(outcome, str) and outcome == "brief":
            briefed += 1
        elif isinstance(outcome, str) and outcome:
            absences[outcome] = absences.get(outcome, 0) + 1
    latest_record = advisories[-1]
    latest = _payload(latest_record)
    age_hours = (now - latest_record.created_at).total_seconds() / 3600
    lines = [f"student advice: {briefed} brief(s) of {passes} pass(es) in the window"]
    if absences:
        tally = ", ".join(f"{count} {reason}" for reason, count in sorted(absences.items()))
        lines.append(f"  absences: {tally}")
    model = latest.get("model", "")
    latency = latest.get("latency_ms", 0)
    lines.append(
        f"  latest pass {_stamp(latest_record.created_at)} ({age_hours:.1f}h old) "
        f"by {model or 'unknown model'} in {latency} ms: outcome {latest.get('outcome', 'unknown')}"
    )
    brief = latest.get("brief")
    if isinstance(brief, str) and brief:
        clipped = (
            brief
            if len(brief) <= DIGEST_BRIEF_MAX_CHARS
            else brief[:DIGEST_BRIEF_MAX_CHARS] + "..."
        )
        lines.append(f"  brief: {clipped}")
    view = latest.get("view")
    if isinstance(view, dict):
        lines.append(f"  position view: {view.get('verdict', 'unstated')}")
    elif isinstance(latest.get("view_declined"), str) and latest.get("view_declined"):
        lines.append(f"  position view: declined ({latest.get('view_declined')})")
    return lines


def _compose_teacher_advice(advice: TeacherAdviceView, now: datetime) -> list[str]:
    """Restate the published teacher evidence with provenance and markers."""
    if advice.document is None:
        reason = {
            "missing": "no published evidence artifact exists on this box",
            "malformed": "the published artifact is unparseable or oversized and was discarded",
        }.get(advice.state, advice.state)
        return [
            f"teacher advice: {advice.state.upper()} - {reason}; the Mac-side",
            "  publisher's next pass replaces it (never invented here)",
        ]
    document = advice.document
    if not isinstance(document, dict):
        return [
            "teacher advice: MALFORMED - the published artifact is unparseable "
            "or oversized and was discarded",
        ]
    lines: list[str] = []
    marker = " (STALE - newest episode beyond the stale bound)" if advice.state == "stale" else ""
    generated = document.get("generated_at", "unknown")
    lines.append(f"teacher advice: published artifact generated {generated}{marker}")
    absences = document.get("absence_counts")
    if isinstance(absences, dict) and absences:
        tally = ", ".join(f"{count} {reason}" for reason, count in sorted(absences.items()))
        lines.append(f"  typed absences across the selected episodes: {tally}")
    episodes = document.get("episodes")
    if not isinstance(episodes, list):
        episodes = []
    for episode in episodes[-DIGEST_MAX_ACTION_LINES // 10 or 1 :]:
        if not isinstance(episode, dict):
            continue
        stamp = str(episode.get("created_at", "unknown"))[:16].replace("T", " ")
        lines.append(f"  episode {stamp} ({episode.get('stream', 'unknown')} stream):")
        seats = episode.get("seats")
        if not isinstance(seats, list):
            continue
        for seat in seats:
            if not isinstance(seat, dict):
                continue
            head = (
                f"    {seat.get('seat', '?')} ({seat.get('model', '?')}): "
                f"{seat.get('outcome', 'unknown')}"
            )
            lines.append(head)
            brief = seat.get("brief")
            if isinstance(brief, str) and brief:
                lines.append(f"      brief: {_clip_text(brief, DIGEST_BRIEF_MAX_CHARS)}")
            if isinstance(seat.get("view_verdict"), str) and seat.get("view_verdict"):
                lines.append(f"      position view: {seat.get('view_verdict')}")
            elif isinstance(seat.get("view_declined"), str) and seat.get("view_declined"):
                lines.append(f"      position view: declined ({seat.get('view_declined')})")
    if not episodes:
        lines.append("  the artifact carries no episodes (a quiet corpus)")
    return lines


def _clip_text(text: str, limit: int) -> str:
    """Cap one excerpt with an explicit ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def compose_daily_digest(
    records: tuple[AuditRecord, ...],
    now: datetime,
    window_hours: int = DIGEST_WINDOW_HOURS,
    advice: TeacherAdviceView | None = None,
) -> tuple[str, str]:
    """Compose the daily digest email from the audit window.

    Args:
        records: The window's audit records, any order (composition sorts).
        now: The timezone-aware composition instant.
        window_hours: The window's length in hours, for the header.
        advice: The loaded teacher-advice view; None renders the missing
            marker (no artifact was even attempted).

    Returns:
        The (subject, body) pair; every line restates durable audit
        evidence or states an explicit honest absence.
    """
    cutoff = now - timedelta(hours=window_hours)
    ordered = tuple(sorted(records, key=lambda record: record.sequence))
    failure_lines, failure_hidden = _compose_failures(ordered)
    action_lines, action_hidden = _compose_actions(ordered)

    headline = f"{len(failure_lines)} failure line(s)" if failure_lines else "clean window"
    subject = f"{DIGEST_SUBJECT_PREFIX} {window_hours}h to {_stamp(now)} - {headline}" + (
        f" (+{failure_hidden} more)" if failure_hidden else ""
    )

    lines: list[str] = [
        f"Aero Bot daily digest - prior {window_hours} hours",
        f"window: {_stamp(cutoff)} -> {_stamp(now)} ({len(ordered)} audit records)",
        "",
        "== failures ==",
    ]
    lines.extend(f"  - {line}" for line in failure_lines)
    if not failure_lines:
        lines.append("  - no halted cycles, reverted deliveries, or refusals in the window")
    if failure_hidden:
        lines.append(f"  ... and {failure_hidden} more (bounded at {DIGEST_MAX_FAILURE_LINES})")
    lines.extend(["", "== trading =="])
    lines.extend(f"  {line}" for line in _compose_trading(ordered))
    lines.extend(["", "== actions =="])
    lines.extend(f"  - {line}" for line in action_lines)
    if not action_lines:
        lines.append("  - no broadcasts in the window")
    if action_hidden:
        lines.append(f"  ... and {action_hidden} more (bounded at {DIGEST_MAX_ACTION_LINES})")
    lines.extend(["", "== rewards =="])
    lines.extend(f"  {line}" for line in _compose_rewards(ordered))
    lines.extend(["", "== student advice (shadow advisor) =="])
    lines.extend(f"  {line}" for line in _compose_student_advice(ordered, now))
    lines.extend(["", "== teacher advice (Mac-side seats) =="])
    lines.extend(
        f"  {line}"
        for line in _compose_teacher_advice(
            advice if advice is not None else TeacherAdviceView(None, "missing"), now
        )
    )
    lines.extend(
        [
            "",
            "digest routing: immediate per-cycle and event emails are suppressed;",
            "local logging, the audit chain, and every protection are unchanged.",
        ]
    )
    return subject, "\n".join(lines) + "\n"


def deliver_daily_digest(
    store: DigestRecordSource,
    environ: Mapping[str, str] | None = None,
    error_stream: TextIO | None = None,
    now: datetime | None = None,
    advice_path: Path | None = None,
) -> bool:
    """Deliver one daily digest email; a failure never raises.

    Args:
        store: The audit store the window reads.
        environ: The environment carrying the alert configuration; None
            reads the live process environment.
        error_stream: Where delivery warnings land; stderr by default.
        now: The composition instant; None reads the wall clock.
        advice_path: The published teacher-advice artifact path; None
            keeps the missing marker. An absent, malformed, or stale
            artifact renders its honest marker and never suppresses the
            send.

    Returns:
        Whether the digest email was sent. Provider none, digest-mode
        misrouting, an unreadable window, and failed deliveries all warn
        (where warranted) and return False.
    """
    stream = error_stream if error_stream is not None else sys.stderr
    try:
        config = parse_alert_config(environ)
    except ValueError as error:
        print(f"email alerts are misconfigured: {error}", file=stream)
        return False
    transport = build_alert_transport(config)
    if transport is None:
        return False
    moment = now if now is not None else datetime.now(UTC)
    try:
        records = collect_digest_records(store, moment)
    except (OSError, ValueError) as error:
        print(f"daily digest could not read the audit window: {error}", file=stream)
        return False
    advice_view = (
        load_teacher_advice(advice_path, moment)
        if advice_path is not None
        else TeacherAdviceView(None, "missing")
    )
    subject, body = compose_daily_digest(records, moment, advice=advice_view)
    try:
        transport.send(subject, body)
    except AlertTransportError as error:
        print(f"daily digest delivery failed: {error}", file=stream)
        return False
    return True


def open_digest_store(database_path: Path | str) -> DigestRecordSource:
    """Open the audit store for the digest's bounded reads."""
    return AuditStore(Path(database_path))


__all__ = [
    "DIGEST_MAX_ACTION_LINES",
    "DIGEST_MAX_FAILURE_LINES",
    "DIGEST_MAX_PAGES",
    "DIGEST_SUBJECT_PREFIX",
    "DIGEST_WINDOW_HOURS",
    "TeacherAdviceView",
    "collect_digest_records",
    "compose_daily_digest",
    "deliver_daily_digest",
    "load_teacher_advice",
    "open_digest_store",
]
