"""The risk manager: a deterministic counterparty desk over the corpus.

The intelligence layer's fifth surface is the separation-of-duties seat
the captain sealed as decision 3a: an independent desk that checks the
student and teacher desks' verdicts against the capital posture itself.
Where every other surface asks whether a desk's brief matched what
followed, this one asks whether the posture the briefs were read against
was even internally coherent - and whether any desk stayed quiet while it
was not.

The corpus is the audit store's snapshot trail: every grounded episode
carries the deterministic facts pulled from the audited cycle summaries
and the self-healing book at its timestamp. This desk replays those
snapshots offline - no live reads, no model calls, no signing, no writes
on the production box - against the locked posture arithmetic any operator
can recompute by hand:

- **Capital posture.** The day P&L must equal equity minus the day-start
  anchor (the exact identity the cycle reports compose) within one
  micro-USDC; anything else is a contradiction between the posture's own
  fields.
- **Halt state.** A marked drawdown of at least five percent of the
  day-start anchor crosses the daily-loss-halt line (the locked policy's
  latch fraction), and an entry-kind action observed later on the same
  anchor day violates the halt discipline, because the halt blocks new
  entries for the rest of that day.
- **Exposure versus caps.** Committed exposure above the 100 USDC hard
  ceiling breaches it, and an entry committing above eighty percent of
  current equity breaches the locked sizing fraction.
- **Contradictions.** A desk's accepted brief that carried no anomaly
  flags on an episode with any finding is a contradiction: the desk read a
  broken posture as quiet. Absences are never contradictions - an absent
  desk gave no verdict to contradict.

Every check is deterministic and fail-closed: malformed or non-finite
economics strings are absences, never guesses, and the findings land as
one frozen schema-validated report (``risk_manager_report/1``) atomically
rewritten beside the corpus. The upgrade loop consumes the same pure
audit (``audit_corpus_posture``) so its digest carries a fourth evidence
class - posture misses the student read as quiet - beside the teacher
divergences, without touching the execution path or the sealed teaching
block. Nothing this surface finds authorizes or blocks anything by
itself; the report is the entire effect, exactly like its siblings.
"""

import argparse
import json
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from aero_bot.advisor import AdvisorWindowFacts
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG
from aero_bot.hindsight import CountedReason, collect_reads, resolve_corpus_dir
from aero_bot.teacher import (
    TEACHER_CORPUS_NAME,
    TEACHER_REPORT_DIR_NAME,
    TeacherEpisode,
    TeacherSeatName,
    load_episodes,
)

# The report schema tag pinned into every audit report.
RISK_MANAGER_REPORT_SCHEMA = "risk_manager_report/1"
# The report file name inside the corpus's reports directory.
RISK_MANAGER_REPORT_NAME = "risk_manager_last.json"
# The locked daily-loss-halt latch fraction (docs/policy-engine.md).
DAILY_LOSS_HALT_FRACTION = Decimal("0.05")
# The locked sizing fraction at entry (docs/policy-engine.md).
POSITION_EQUITY_FRACTION = Decimal("0.80")
# The LP executor's unchanged hard exposure ceilings in USDC.
EXPOSURE_HARD_CAP_USDC = Decimal("100")
# The posture identity's tolerance: one micro-USDC absorbs serialization
# noise while never excusing a real contradiction.
POSTURE_TOLERANCE_USDC = Decimal("0.000001")
# The action kinds that commit new capital; the halt blocks exactly these
# while recentering stays armed as position maintenance.
ENTRY_ACTIONS = frozenset({"enter", "pool_switch"})
# How many findings and contradictions the report carries, and each
# detail's and brief snippet's bound.
RISK_FINDING_MAX = 20
RISK_DETAIL_MAX_CHARS = 300
RISK_BRIEF_MAX_CHARS = 600
RISK_FINDING_KINDS_PER_ENTRY = 5
# The New York session anchors the trading day (docs/policy-engine.md).
NEW_YORK = ZoneInfo("America/New_York")
# The self-describing posture rules pinned into every report.
RISK_POSTURE_RULES = (
    "every grounded episode is checked against the locked posture "
    "arithmetic: day P&L must equal equity minus the day-start anchor "
    "within one micro-USDC; a marked drawdown of at least five percent of "
    "the day-start anchor crosses the daily-loss-halt line; an entry-kind "
    "action observed later on the same anchor day violates the halt "
    "discipline; committed exposure above 100 USDC breaches the hard cap; "
    "an entry committing above eighty percent of equity breaches the "
    "sizing fraction; and a desk's accepted brief with no anomaly flags "
    "on an episode carrying any finding is a contradiction. Malformed or "
    "non-finite readings are absences, never guesses"
)


class RiskFindingKind:
    """Namespace the stable finding kinds the audit can report."""

    # The posture's own fields disagree: equity minus day-start anchor is
    # not the recorded day P&L.
    DAY_PNL_CONTRADICTION = "day_pnl_contradiction"
    # The marked drawdown crossed the five-percent daily-loss-halt line.
    HALT_THRESHOLD_BREACHED = "halt_threshold_breached"
    # An entry-kind action followed a same-anchor-day halt-line crossing.
    HALT_DISCIPLINE_VIOLATION = "halt_discipline_violation"
    # Committed exposure sits above the 100 USDC hard ceiling.
    HARD_CAP_BREACH = "hard_cap_breach"
    # An entry committed above eighty percent of current equity.
    SIZING_CAP_BREACH = "sizing_cap_breach"


class RiskFinding(BaseModel):
    """Carry one deterministic posture finding on one grounded episode."""

    # Frozen strict fields keep one finding immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # When the episode carrying the finding ran.
    created_at: datetime
    # The stream the episode served.
    stream: str
    # The stable finding kind.
    kind: str
    # One bounded human line carrying the exact numbers.
    detail: str
    # The posture snapshot the finding read, verbatim strings.
    equity_usdc: str | None = None
    day_start_equity_usdc: str | None = None
    day_pnl_usdc: str | None = None
    committed_usdc: str | None = None
    # The episode's halted-cycle baseline.
    halted_count: Annotated[int, Field(ge=0)] = 0


class DeskContradiction(BaseModel):
    """Carry one desk's quiet verdict on a posture the audit flagged."""

    # Frozen strict fields keep one contradiction immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # When the episode carrying the finding ran.
    created_at: datetime
    # The stream the episode served.
    stream: str
    # The desk that read a flagged posture as quiet: a teacher seat or
    # the student.
    desk: str
    # The model tag the desk answered under, else empty.
    model: str
    # The finding kinds the desk stayed quiet over, bounded.
    finding_kinds: tuple[str, ...]
    # The desk's quiet brief, bounded.
    brief: str


class RiskManagerReport(BaseModel):
    """Carry one complete risk-manager pass."""

    # Frozen strict fields keep one report immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The report schema tag.
    schema_version: str = RISK_MANAGER_REPORT_SCHEMA
    # When the audit pass ran.
    created_at: datetime
    # The self-describing posture rules the audit applied.
    posture_rules: str = RISK_POSTURE_RULES
    # Every episode the corpus loaded, all streams.
    episode_count: Annotated[int, Field(ge=0)]
    # How many episodes carried pulled facts (the audited timeline).
    grounded_episode_count: Annotated[int, Field(ge=0)]
    # Every finding kind and its count over the whole corpus, most
    # frequent first; the counts are the complete truth while the entries
    # below stay bounded.
    finding_counts: tuple[CountedReason, ...] = ()
    # Every contradicting desk and its count, most frequent first.
    contradiction_counts: tuple[CountedReason, ...] = ()
    # The most recent findings, bounded.
    findings: tuple[RiskFinding, ...] = ()
    # The most recent contradictions, bounded.
    contradictions: tuple[DeskContradiction, ...] = ()


@dataclass(frozen=True)
class PostureAudit:
    """Carry the complete deterministic posture audit over the corpus."""

    # How many grounded episodes the audit considered.
    grounded_episode_count: int
    # Every finding in chronological order.
    findings: tuple[RiskFinding, ...]
    # The findings grouped by episode object identity, so consumers (the
    # upgrade digest) can join them back to the desks' reads; keyed the
    # same way the digest's verdict mapping keys, over the same loader
    # objects.
    findings_by_episode: Mapping[int, tuple[RiskFinding, ...]] = field(default_factory=dict)
    # Every contradiction in chronological order.
    contradictions: tuple[DeskContradiction, ...] = ()

    def kinds_for(self, episode: TeacherEpisode) -> tuple[str, ...]:
        """List the stable finding kinds recorded on one episode.

        Args:
            episode: The corpus episode to look up.

        Returns:
            The episode's finding kinds in recorded order, bounded to
            five; an unflagged episode yields nothing.
        """
        kinds = tuple(finding.kind for finding in self.findings_by_episode.get(id(episode), ()))
        return kinds[:RISK_FINDING_KINDS_PER_ENTRY]


def _parse_usdc(value: str | None) -> Decimal | None:
    """Parse one facts string field into a Decimal, honestly.

    Args:
        value: The facts field's string form, else None.

    Returns:
        The parsed finite Decimal, else None when absent, malformed, or
        non-finite; a NaN or infinite reading is an absence, never a
        guess, exactly like the hindsight scorer's float discipline.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _collapse(text: str, limit: int) -> str:
    """Collapse and bound one detail or brief string.

    Args:
        text: The source text.
        limit: The maximum characters to keep.

    Returns:
        The whitespace-collapsed text truncated to its first ``limit``
        characters; entries stay single-line and bounded.
    """
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def _anchor_key(episode: TeacherEpisode, day_start: str) -> tuple[object, str]:
    """Key one episode's anchor day for the halt-discipline state.

    Args:
        episode: The grounded episode.
        day_start: The episode's parsed day-start anchor string.

    Returns:
        The (New York session date, anchor) pair; two episodes share an
        anchor day only when their trading-day dates and day-start
        anchors both match, so an identical equity on two different days
        never aliases into one halt window.
    """
    return (episode.created_at.astimezone(NEW_YORK).date(), day_start)


def _posture_findings(
    facts: AdvisorWindowFacts, episode: TeacherEpisode
) -> tuple[RiskFinding, ...]:
    """Compute one grounded facts snapshot's posture findings.

    Args:
        facts: The episode's composed facts, never None here.
        episode: The grounded episode carrying the facts (its timestamp
            and stream stamp every finding).

    Returns:
        The episode's findings in stable check order; an internally
        coherent posture yields nothing.
    """
    equity = _parse_usdc(facts.equity_usdc)
    day_start = _parse_usdc(facts.day_start_equity_usdc)
    day_pnl = _parse_usdc(facts.day_pnl_usdc)
    committed = _parse_usdc(facts.committed_usdc)
    findings: list[RiskFinding] = []

    def _record(kind: str, detail: str) -> None:
        findings.append(
            RiskFinding(
                created_at=episode.created_at,
                stream=episode.stream.value,
                kind=kind,
                detail=_collapse(detail, RISK_DETAIL_MAX_CHARS),
                equity_usdc=facts.equity_usdc,
                day_start_equity_usdc=facts.day_start_equity_usdc,
                day_pnl_usdc=facts.day_pnl_usdc,
                committed_usdc=facts.committed_usdc,
                halted_count=facts.halted_count,
            )
        )

    # Capital posture: the day P&L must compose from the equity pair.
    if equity is not None and day_start is not None and day_pnl is not None:
        composed = equity - day_start
        if abs(composed - day_pnl) > POSTURE_TOLERANCE_USDC:
            _record(
                RiskFindingKind.DAY_PNL_CONTRADICTION,
                f"equity {facts.equity_usdc} minus day-start "
                f"{facts.day_start_equity_usdc} is {composed} but day P&L "
                f"reads {facts.day_pnl_usdc}",
            )
    # Halt state: the five-percent marked-drawdown latch line.
    drawdown_fraction: Decimal | None = None
    if equity is not None and day_start is not None and day_start > 0:
        drawdown_fraction = (day_start - equity) / day_start
        if drawdown_fraction >= DAILY_LOSS_HALT_FRACTION:
            _record(
                RiskFindingKind.HALT_THRESHOLD_BREACHED,
                f"marked drawdown {drawdown_fraction:.6f} of day-start "
                f"{facts.day_start_equity_usdc} crosses the five-percent "
                f"daily-loss-halt line at equity {facts.equity_usdc}",
            )
    # Exposure versus caps: the hard ceiling reads on its own.
    if committed is not None and committed > EXPOSURE_HARD_CAP_USDC:
        _record(
            RiskFindingKind.HARD_CAP_BREACH,
            f"committed {facts.committed_usdc} exceeds the 100 USDC hard exposure ceiling",
        )
    # Exposure versus caps: the eighty-percent sizing bound applies at
    # entry-kind actions, the moments capital commits.
    action = facts.latest_action
    if (
        action in ENTRY_ACTIONS
        and committed is not None
        and equity is not None
        and equity > 0
        and committed > equity * POSITION_EQUITY_FRACTION + POSTURE_TOLERANCE_USDC
    ):
        bound = equity * POSITION_EQUITY_FRACTION
        _record(
            RiskFindingKind.SIZING_CAP_BREACH,
            f"entry action {action} committed {facts.committed_usdc} above "
            f"the eighty-percent sizing bound {bound} of equity "
            f"{facts.equity_usdc}",
        )
    return tuple(findings)


def audit_corpus_posture(
    episodes: Sequence[TeacherEpisode],
) -> PostureAudit:
    """Audit every grounded episode's posture and the desks' quiet reads.

    The audit replays the corpus's fact snapshots in chronological order
    (episodes append at pass end while timestamping at pass start, so
    file order is never chronology) and applies the locked posture
    arithmetic: the day-P&L identity, the five-percent halt line and its
    entry discipline across the same anchor day, and the hard and sizing
    caps. Every desk that answered an episode carrying any finding with a
    brief free of anomaly flags is recorded as a contradiction; absences
    gave no verdict and are never contradictions.

    Args:
        episodes: The corpus's episodes in any order.

    Returns:
        The complete audit with findings, the per-episode finding join,
        and the desk contradictions, all in chronological order.
    """
    grounded: list[tuple[TeacherEpisode, AdvisorWindowFacts]] = []
    for episode in episodes:
        facts = episode.facts
        if facts is not None:
            grounded.append((episode, facts))
    grounded.sort(key=lambda pair: pair[0].created_at)
    findings: list[RiskFinding] = []
    findings_by_episode: dict[int, tuple[RiskFinding, ...]] = {}
    contradictions: list[DeskContradiction] = []
    # Anchor days whose halt line was observed crossed; the halt latches
    # for the rest of that day, so any later entry-kind action on the
    # same anchor violates the discipline.
    halted_anchors: set[tuple[object, str]] = set()
    for episode, facts in grounded:
        anchor = (
            _anchor_key(episode, facts.day_start_equity_usdc)
            if facts.day_start_equity_usdc is not None
            else None
        )
        episode_findings: list[RiskFinding] = list(_posture_findings(facts, episode))
        if anchor is not None and anchor in halted_anchors and facts.latest_action in ENTRY_ACTIONS:
            episode_findings.append(
                RiskFinding(
                    created_at=episode.created_at,
                    stream=episode.stream.value,
                    kind=RiskFindingKind.HALT_DISCIPLINE_VIOLATION,
                    detail=_collapse(
                        f"entry action {facts.latest_action} on anchor day-start "
                        f"{facts.day_start_equity_usdc} after the halt line was "
                        "crossed; the daily loss halt blocks new entries for "
                        "the rest of the day",
                        RISK_DETAIL_MAX_CHARS,
                    ),
                    equity_usdc=facts.equity_usdc,
                    day_start_equity_usdc=facts.day_start_equity_usdc,
                    day_pnl_usdc=facts.day_pnl_usdc,
                    committed_usdc=facts.committed_usdc,
                    halted_count=facts.halted_count,
                )
            )
        if anchor is not None and any(
            finding.kind == RiskFindingKind.HALT_THRESHOLD_BREACHED for finding in episode_findings
        ):
            halted_anchors.add(anchor)
        if episode_findings:
            findings.extend(episode_findings)
            findings_by_episode[id(episode)] = tuple(episode_findings)
            kinds = tuple(finding.kind for finding in episode_findings)[
                :RISK_FINDING_KINDS_PER_ENTRY
            ]
            # The desks' verdicts: every answered brief with no anomaly
            # flags read a flagged posture as quiet.
            contradictions.extend(_quiet_contradictions(episode, kinds))
    return PostureAudit(
        grounded_episode_count=len(grounded),
        findings=tuple(findings),
        findings_by_episode=findings_by_episode,
        contradictions=tuple(contradictions),
    )


def _quiet_contradictions(
    episode: TeacherEpisode, kinds: tuple[str, ...]
) -> tuple[DeskContradiction, ...]:
    """Record every desk that answered a flagged episode quietly.

    Args:
        episode: The grounded episode carrying posture findings.
        kinds: The episode's stable finding kinds, bounded.

    Returns:
        One contradiction per desk whose accepted brief carried no
        anomaly flags; the student desk is checked first, then each
        teacher seat in seat order.
    """
    desks: list[tuple[str, TeacherSeatName | None]] = [("student", None)]
    desks.extend((seat.value, seat) for seat in TeacherSeatName)
    recorded: list[DeskContradiction] = []
    for name, seat in desks:
        read = collect_reads((episode,), seat)[0]
        if read.brief is None or read.brief.anomalies:
            continue
        recorded.append(
            DeskContradiction(
                created_at=episode.created_at,
                stream=episode.stream.value,
                desk=name,
                model=read.model,
                finding_kinds=kinds,
                brief=_collapse(read.brief.brief, RISK_BRIEF_MAX_CHARS),
            )
        )
    return tuple(recorded)


def compose_report(
    audit: PostureAudit,
    episode_count: int,
    now: datetime,
) -> RiskManagerReport:
    """Compose the frozen report from one complete audit.

    Args:
        audit: The complete posture audit.
        episode_count: How many episodes the corpus loaded, all streams.
        now: The pass's reference time.

    Returns:
        The schema-validated report carrying the full counts and the
        bounded most-recent entries.
    """
    finding_counter: Counter[str] = Counter(finding.kind for finding in audit.findings)
    contradiction_counter: Counter[str] = Counter(
        contradiction.desk for contradiction in audit.contradictions
    )
    return RiskManagerReport(
        created_at=now,
        episode_count=episode_count,
        grounded_episode_count=audit.grounded_episode_count,
        finding_counts=tuple(
            CountedReason(reason=reason, count=count)
            for reason, count in sorted(
                finding_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        contradiction_counts=tuple(
            CountedReason(reason=reason, count=count)
            for reason, count in sorted(
                contradiction_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        findings=tuple(sorted(audit.findings, key=lambda finding: finding.created_at))[
            -RISK_FINDING_MAX:
        ],
        contradictions=tuple(sorted(audit.contradictions, key=lambda entry: entry.created_at))[
            -RISK_FINDING_MAX:
        ],
    )


def write_report(report: RiskManagerReport, corpus_dir: Path) -> Path:
    """Rewrite the audit report atomically beside the corpus.

    Args:
        report: The complete audit report.
        corpus_dir: The corpus state directory.

    Returns:
        The report path written.

    Raises:
        OSError: The reports directory could not be written.
    """
    report_dir = corpus_dir / TEACHER_REPORT_DIR_NAME
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / RISK_MANAGER_REPORT_NAME
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(json.loads(report.model_dump_json()), indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, report_path)
    return report_path


def print_report(report: RiskManagerReport) -> None:
    """Print the report's human summary to stdout."""
    print(
        f"risk-manager report {report.created_at.isoformat()} over "
        f"{report.episode_count} episodes ({report.grounded_episode_count} grounded)"
    )
    if not report.finding_counts and not report.contradiction_counts:
        print("  posture clean: no findings, no contradictions")
    for counted in report.finding_counts:
        print(f"  finding {counted.reason}: {counted.count}")
    for counted in report.contradiction_counts:
        print(f"  quiet-on-finding {counted.reason}: {counted.count}")
    for finding in report.findings:
        print(f"  {finding.created_at.isoformat()} [{finding.stream}] {finding.kind}")
        print(f"    {finding.detail}")
    for contradiction in report.contradictions:
        model = f" [{contradiction.model}]" if contradiction.model else ""
        print(
            f"  {contradiction.created_at.isoformat()} "
            f"{contradiction.desk}{model} quiet over "
            f"{', '.join(contradiction.finding_kinds)}"
        )
    print(f"  rules: {report.posture_rules}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the risk-manager audit pass.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on a produced report (findings are
        honest observations, not failures; an empty corpus audits to
        zero everywhere), one on corpus or write failures.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-risk-manager",
        description=(
            "Audit the teacher corpus's posture snapshots as the "
            "independent counterparty desk: verify the capital-posture "
            "arithmetic, the halt state and its entry discipline, and "
            "exposure against the locked caps, and record every desk "
            "whose accepted brief stayed quiet over a flagged posture. "
            "The audit is deterministic and offline; the report beside "
            "the corpus is the entire effect - advisory only, no live "
            "reads, no signing, no writes on the production box, and "
            "nothing a desk says authorizes anything by itself."
        ),
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help="The corpus state directory; default resolves the shared "
        "environment override or the standard state path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report JSON instead of the human summary.",
    )
    arguments = parser.parse_args(argv)
    corpus_dir = arguments.corpus_dir or resolve_corpus_dir(os.environ)
    try:
        episodes = load_episodes(corpus_dir / TEACHER_CORPUS_NAME)
        audit = audit_corpus_posture(episodes)
        report = compose_report(audit, len(episodes), datetime.now(UTC))
        report_path = write_report(report, corpus_dir)
    except OSError as error:
        print(f"the risk-manager report could not be written: {error}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as error:
        # A corpus the audit cannot even read is a typed failure, never a
        # traceback.
        print(f"the corpus could not be read: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(json.loads(report.model_dump_json()), indent=2))
    else:
        print_report(report)
        print(f"report: {report_path}")
    return 0


__all__ = [
    "DAILY_LOSS_HALT_FRACTION",
    "DeskContradiction",
    "ENTRY_ACTIONS",
    "EXPOSURE_HARD_CAP_USDC",
    "NEW_YORK",
    "POSITION_EQUITY_FRACTION",
    "POSTURE_TOLERANCE_USDC",
    "PostureAudit",
    "RISK_BRIEF_MAX_CHARS",
    "RISK_DETAIL_MAX_CHARS",
    "RISK_FINDING_KINDS_PER_ENTRY",
    "RISK_FINDING_MAX",
    "RISK_MANAGER_REPORT_NAME",
    "RISK_MANAGER_REPORT_SCHEMA",
    "RISK_POSTURE_RULES",
    "RiskFinding",
    "RiskFindingKind",
    "RiskManagerReport",
    "audit_corpus_posture",
    "compose_report",
    "main",
    "print_report",
    "write_report",
]
