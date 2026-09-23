"""The hindsight scorer: replay the corpus against what actually followed.

The intelligence layer's third surface closes the loop the teacher harness
opened: episodes accumulate in the local corpus, and this scorer replays
them offline against the deterministic facts later episodes carry, scoring
every desk - both teacher seats and the student whose audited brief rides
each episode - on availability and anomaly calibration.

The corpus is self-contained truth: every pulled episode snapshots the
window facts at its timestamp, so the realized outcome of an earlier
episode is simply what later episodes observed. No live reads, no model
calls, no signing, no writes on the production box; the scored report under
the corpus's reports directory is the entire effect.

Discipline:

- **Deterministic truth only.** A read is scored against a stated rule any
  operator can recompute: within the horizon, did any later episode's facts
  show a negative day P&L or a higher halted-cycle count. Nothing is
  inferred from prose, and no model judges another model here.
- **Pending, never guessed.** A brief whose horizon contains no later
  facts is pending, not scored; absences are counted per typed reason and
  never scored at all (see ``docs/teacher.md``).
- **One uniform shape.** The report is one frozen schema-validated object
  (``hindsight_report/1``) rewritten atomically, so a month-old report
  explains itself: the horizon and the truth rule ride inside it.
"""

import argparse
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from aero_bot.advisor import AdvisorAnomaly, AdvisorBrief
from aero_bot.domain import IMMUTABLE_MODEL_CONFIG
from aero_bot.teacher import (
    DEFAULT_TEACHER_CORPUS_DIR,
    TEACHER_CORPUS_DIR_ENV,
    TEACHER_CORPUS_NAME,
    TEACHER_REPORT_DIR_NAME,
    TeacherEpisode,
    TeacherSeatName,
    TeacherStream,
    load_episodes,
)

# The report schema tag pinned into every scored report.
HINDSIGHT_REPORT_SCHEMA = "hindsight_report/1"
# The report file name inside the corpus's reports directory.
HINDSIGHT_REPORT_NAME = "hindsight_last.json"
# The default scoring horizon in hours: one full trading day of episodes.
DEFAULT_HINDSIGHT_HORIZON_HOURS = 24.0
# The bounded horizon range a manual run may request, in hours.
HINDSIGHT_HORIZON_BOUNDS = (1.0, 168.0)
# How many samples the scoreboard's bounded series carry.
HINDSIGHT_SERIES_MAX = 24
# The self-describing truth rule pinned into every report.
HINDSIGHT_TRUTH_RULE = (
    "a brief is scored against any later episode within the horizon whose "
    "facts show a negative day P&L or a higher halted-cycle count; a brief "
    "stays pending until its horizon has fully elapsed or a bad outcome is "
    "observed; absences are counted per typed reason and never scored"
)
# The teacher seats' absence labels that mean no question was asked.
UNASKED_REASONS = frozenset({"dark", "window_unreachable"})
# The streams whose briefs are window-grounded and therefore scoreable;
# news judges the outside world, which carries no follow-up truth here.
CALIBRATED_STREAMS = frozenset({TeacherStream.TACTICAL, TeacherStream.DAILY})


class HindsightCalibration(BaseModel):
    """Carry one desk's anomaly-flag counts against realized outcomes."""

    # Frozen strict fields keep one desk's counts immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # How many answered briefs had realized truth inside the horizon.
    scorable: Annotated[int, Field(ge=0)]
    # Flagged an anomaly and a bad outcome followed.
    flagged_bad: Annotated[int, Field(ge=0)]
    # Flagged an anomaly and nothing bad followed.
    flagged_quiet: Annotated[int, Field(ge=0)]
    # Flagged nothing and a bad outcome followed.
    unflagged_bad: Annotated[int, Field(ge=0)]
    # Flagged nothing and nothing bad followed.
    unflagged_quiet: Annotated[int, Field(ge=0)]
    # Answered briefs still waiting for their horizon to fill.
    pending: Annotated[int, Field(ge=0)]

    @property
    def answered(self) -> int:
        """Count every answered brief the calibration considered."""
        return (
            self.flagged_bad
            + self.flagged_quiet
            + self.unflagged_bad
            + self.unflagged_quiet
            + self.pending
        )


class CountedReason(BaseModel):
    """Carry one stable absence reason and how often it occurred."""

    # Frozen strict fields keep one count immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The stable absence reason as recorded in the corpus.
    reason: str
    # How many asked episodes ended in this absence.
    count: Annotated[int, Field(ge=1)]


class HindsightDeskScore(BaseModel):
    """Carry one desk's availability and calibration over the corpus."""

    # Frozen strict fields keep one desk's score immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The desk's stable name: a teacher seat or the student.
    desk: str
    # The latest model tag the desk answered under, else empty.
    model: str
    # How many episodes actually offered the desk a question.
    asked: Annotated[int, Field(ge=0)]
    # How many of those the desk answered with a brief.
    briefs: Annotated[int, Field(ge=0)]
    # Every scored absence reason and its count, most frequent first.
    absences: tuple[CountedReason, ...] = ()
    # The anomaly-flag calibration over window-grounded streams; news
    # episodes count for availability only because the outside world has
    # no deterministic follow-up truth in this corpus.
    calibration: HindsightCalibration

    @property
    def availability(self) -> float | None:
        """Report the desk's brief rate over asked episodes, else None."""
        if self.asked == 0:
            return None
        return self.briefs / self.asked


class HindsightScoreboard(BaseModel):
    """Carry the corpus's latest economics for the report's context."""

    # Frozen strict fields keep one scoreboard immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The latest observed portfolio equity, else None.
    latest_equity_usdc: str | None = None
    # The latest observed day-start anchor, else None.
    latest_day_start_equity_usdc: str | None = None
    # The latest observed day P&L, else None.
    latest_day_pnl_usdc: str | None = None
    # The day P&L samples across the corpus, oldest first, bounded.
    day_pnl_samples: tuple[str, ...] = ()
    # The equity samples across the corpus, oldest first, bounded.
    equity_samples: tuple[str, ...] = ()


class HindsightReport(BaseModel):
    """Carry one complete hindsight scoring pass."""

    # Frozen strict fields keep one report immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The report schema tag.
    schema_version: str = HINDSIGHT_REPORT_SCHEMA
    # When the scoring pass ran.
    created_at: datetime
    # The scoring horizon actually applied.
    horizon_hours: Annotated[float, Field(gt=0.0)]
    # The self-describing rule the calibration scored against.
    truth_rule: str = HINDSIGHT_TRUTH_RULE
    # Every episode the corpus loaded, all streams.
    episode_count: Annotated[int, Field(ge=0)]
    # How many episodes carried pulled facts (the scoreable timeline).
    grounded_episode_count: Annotated[int, Field(ge=0)]
    # One score per desk, teacher seats first then the student.
    desks: tuple[HindsightDeskScore, ...] = ()
    # The latest economics and bounded series for context.
    scoreboard: HindsightScoreboard = HindsightScoreboard()


@dataclass(frozen=True)
class DeskRead:
    """Carry one desk's outcome on one episode."""

    # The episode the read belongs to.
    episode: TeacherEpisode
    # Whether the desk actually received the question.
    asked: bool
    # The accepted brief, else None.
    brief: AdvisorBrief | None
    # The stable outcome label as recorded in the corpus.
    outcome: str
    # The model tag the desk ran under, else empty.
    model: str


def _student_read(episode: TeacherEpisode) -> DeskRead:
    """Project one episode's student observation into a desk read.

    Args:
        episode: The episode whose pull carried the student's latest
            audited answer.

    Returns:
        The student desk's read; an episode without a student observation
        is unasked, never absent - the student surface simply had not run
        inside the window.
    """
    student = episode.student
    if student is None:
        return DeskRead(episode, asked=False, brief=None, outcome="", model="")
    payload = student.payload
    brief: AdvisorBrief | None = None
    if payload.outcome == "brief" and payload.brief:
        brief = AdvisorBrief(
            brief=payload.brief,
            anomalies=tuple(
                AdvisorAnomaly(
                    label=anomaly.label,
                    confidence=anomaly.confidence,
                    rationale=anomaly.rationale,
                )
                for anomaly in payload.anomalies
            ),
        )
    return DeskRead(
        episode,
        asked=payload.outcome not in UNASKED_REASONS,
        brief=brief,
        outcome=payload.outcome,
        model=payload.model,
    )


def collect_reads(
    episodes: Sequence[TeacherEpisode], desk: TeacherSeatName | None
) -> tuple[DeskRead, ...]:
    """Project every episode into one desk's reads, oldest first.

    Args:
        episodes: The corpus's episodes, oldest first.
        desk: The teacher seat to project, else None for the student.

    Returns:
        One read per episode; a pass the seat was filtered out of is
        unasked, never absent.
    """
    if desk is None:
        return tuple(_student_read(episode) for episode in episodes)
    reads: list[DeskRead] = []
    for episode in episodes:
        outcome = next((entry for entry in episode.seats if entry.seat is desk), None)
        if outcome is None:
            reads.append(DeskRead(episode, asked=False, brief=None, outcome="", model=""))
            continue
        reads.append(
            DeskRead(
                episode=episode,
                asked=outcome.outcome not in UNASKED_REASONS,
                brief=outcome.brief,
                outcome=outcome.outcome,
                model=outcome.model,
            )
        )
    return tuple(reads)


def _parse_usdc(value: str | None) -> float | None:
    """Parse one facts string field into a float, honestly.

    Args:
        value: The facts field's string form, else None.

    Returns:
        The parsed float, else None when absent or malformed; a malformed
        reading is an absence, never a guess.
    """
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    # NaN and infinities parse but carry no honest economics: a NaN
    # comparison is silently False and -inf would fabricate a bad outcome.
    return parsed if math.isfinite(parsed) else None


def _bad_outcome_followed(
    episode: TeacherEpisode,
    grounded_tail: Sequence[TeacherEpisode],
    horizon: timedelta,
    now: datetime,
) -> bool | None:
    """Decide whether a bad outcome followed one grounded episode.

    Args:
        episode: The scored episode; its facts define the baseline.
        grounded_tail: Every chronologically later grounded episode; each
            is checked against the horizon here.
        horizon: How far ahead the truth may be observed.
        now: The scoring pass's reference time; a quiet verdict requires
            the horizon to have fully elapsed, because more truth may
            still arrive.

    Returns:
        True when any later grounded episode inside the horizon observed
        a negative day P&L or a higher halted-cycle count, False when the
        horizon has fully elapsed with only quiet observations, None
        while the read stays pending (no later grounded episode inside
        the horizon, or the horizon has not yet elapsed).
    """
    baseline = episode.facts
    if baseline is None:
        return None
    limit = episode.created_at + horizon
    observed = False
    for follower in grounded_tail:
        facts = follower.facts
        if facts is None:
            continue
        if not episode.created_at < follower.created_at <= limit:
            continue
        observed = True
        day_pnl = _parse_usdc(facts.day_pnl_usdc)
        if day_pnl is not None and day_pnl < 0.0:
            return True
        if facts.halted_count > baseline.halted_count:
            return True
    if not observed:
        return None
    # Quiet so far: quiet is only final once the horizon has drained.
    if now < limit:
        return None
    return False


def _score_desk(
    desk: str,
    reads: Sequence[DeskRead],
    verdicts: Mapping[int, bool | None],
) -> HindsightDeskScore:
    """Score one desk's reads into availability and calibration counts.

    Args:
        desk: The desk's stable name.
        reads: The desk's projected reads, oldest first.
        verdicts: The per-episode bad-outcome verdicts keyed by episode
            object identity.

    Returns:
        The desk's complete score.
    """
    asked = tuple(read for read in reads if read.asked)
    answered = tuple(read for read in asked if read.brief is not None)
    counter: Counter[str] = Counter(read.outcome for read in asked if read.brief is None)
    absences = tuple(
        CountedReason(reason=reason, count=count)
        for reason, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )
    counts = {"flagged_bad": 0, "flagged_quiet": 0, "unflagged_bad": 0, "unflagged_quiet": 0}
    scorable = 0
    pending = 0
    for read in answered:
        if read.episode.stream not in CALIBRATED_STREAMS:
            continue
        verdict = verdicts.get(id(read.episode))
        if verdict is None:
            pending += 1
            continue
        scorable += 1
        flagged = bool(read.brief is not None and read.brief.anomalies)
        if verdict:
            counts["flagged_bad" if flagged else "unflagged_bad"] += 1
        else:
            counts["flagged_quiet" if flagged else "unflagged_quiet"] += 1
    model = ""
    for source in (answered, asked):
        for read in reversed(tuple(source)):
            if read.model:
                model = read.model
                break
        if model:
            break
    return HindsightDeskScore(
        desk=desk,
        model=model,
        asked=len(asked),
        briefs=len(answered),
        absences=absences,
        calibration=HindsightCalibration(
            scorable=scorable,
            flagged_bad=counts["flagged_bad"],
            flagged_quiet=counts["flagged_quiet"],
            unflagged_bad=counts["unflagged_bad"],
            unflagged_quiet=counts["unflagged_quiet"],
            pending=pending,
        ),
    )


def score_corpus(
    episodes: Sequence[TeacherEpisode],
    horizon_hours: float,
    now: datetime | None = None,
) -> HindsightReport:
    """Score every desk over the corpus against what actually followed.

    Args:
        episodes: The corpus's episodes, oldest first.
        horizon_hours: How many hours ahead a read may be scored against.
        now: The pass's reference time; None reads the clock.

    Returns:
        The complete scored report.
    """
    moment = now if now is not None else datetime.now(UTC)
    horizon = timedelta(hours=horizon_hours)
    # Episodes carry created_at at pass start but append at pass end, so
    # overlapping passes can land out of timestamp order; the timeline is
    # the grounded episodes sorted by their own clocks, never file order.
    grounded = sorted(
        (episode for episode in episodes if episode.facts is not None),
        key=lambda episode: episode.created_at,
    )
    verdicts: dict[int, bool | None] = {}
    for index, episode in enumerate(grounded):
        verdicts[id(episode)] = _bad_outcome_followed(
            episode, grounded[index + 1 :], horizon, moment
        )
    desks: list[HindsightDeskScore] = []
    for seat in TeacherSeatName:
        desks.append(_score_desk(seat.value, collect_reads(episodes, seat), verdicts))
    desks.append(_score_desk("student", collect_reads(episodes, None), verdicts))
    scoreboard = HindsightScoreboard()
    latest = grounded[-1].facts if grounded else None
    if latest is not None:
        day_samples = tuple(
            facts.day_pnl_usdc
            for facts in (episode.facts for episode in grounded)
            if facts is not None and facts.day_pnl_usdc is not None
        )
        equity_samples = tuple(
            facts.equity_usdc
            for facts in (episode.facts for episode in grounded)
            if facts is not None and facts.equity_usdc is not None
        )
        scoreboard = HindsightScoreboard(
            latest_equity_usdc=latest.equity_usdc,
            latest_day_start_equity_usdc=latest.day_start_equity_usdc,
            latest_day_pnl_usdc=latest.day_pnl_usdc,
            day_pnl_samples=day_samples[-HINDSIGHT_SERIES_MAX:],
            equity_samples=equity_samples[-HINDSIGHT_SERIES_MAX:],
        )
    return HindsightReport(
        created_at=moment,
        horizon_hours=horizon_hours,
        episode_count=len(episodes),
        grounded_episode_count=len(grounded),
        desks=tuple(desks),
        scoreboard=scoreboard,
    )


def resolve_corpus_dir(environ: Mapping[str, str]) -> Path:
    """Resolve the corpus directory from the shared environment override.

    Args:
        environ: The environment mapping to consult.

    Returns:
        The override from the teacher's shared variable when set, else the
        default corpus directory.
    """
    override = environ.get(TEACHER_CORPUS_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_TEACHER_CORPUS_DIR


def write_report(report: HindsightReport, corpus_dir: Path) -> Path:
    """Rewrite the scored report atomically beside the corpus.

    Args:
        report: The complete scored report.
        corpus_dir: The corpus state directory.

    Returns:
        The report path written.

    Raises:
        OSError: The reports directory could not be written.
    """
    report_dir = corpus_dir / TEACHER_REPORT_DIR_NAME
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / HINDSIGHT_REPORT_NAME
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(json.loads(report.model_dump_json()), indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, report_path)
    return report_path


def print_report(report: HindsightReport) -> None:
    """Print the report's human summary to stdout."""
    print(
        f"hindsight report {report.created_at.isoformat()} "
        f"horizon {report.horizon_hours:g}h "
        f"over {report.episode_count} episodes "
        f"({report.grounded_episode_count} grounded)"
    )
    for desk in report.desks:
        model = f" [{desk.model}]" if desk.model else ""
        availability = (
            f", availability {desk.availability:.2f}" if desk.availability is not None else ""
        )
        print(f"  {desk.desk}{model}: {desk.briefs}/{desk.asked} briefs{availability}")
        for counted in desk.absences:
            print(f"    absent {counted.reason}: {counted.count}")
        calibration = desk.calibration
        if calibration.answered:
            print(
                f"    calibration: {calibration.scorable} scorable "
                f"({calibration.flagged_bad} flagged-bad, "
                f"{calibration.flagged_quiet} flagged-quiet, "
                f"{calibration.unflagged_bad} unflagged-bad, "
                f"{calibration.unflagged_quiet} unflagged-quiet), "
                f"{calibration.pending} pending"
            )
    scoreboard = report.scoreboard
    if scoreboard.latest_equity_usdc is not None:
        day_pnl = scoreboard.latest_day_pnl_usdc
        pnl_note = f", day P&L {day_pnl}" if day_pnl is not None else ""
        print(f"  scoreboard: equity {scoreboard.latest_equity_usdc} USDC{pnl_note}")
    print(f"  truth: {report.truth_rule}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the hindsight scorer.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on a produced report (an empty corpus
        scores honestly to zero everywhere), one on usage or write
        failures.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-hindsight",
        description=(
            "Replay the teacher corpus against what actually followed: "
            "score every desk (both teacher seats and the student) on "
            "availability and anomaly calibration against deterministic "
            "later facts, and rewrite the scored report beside the "
            "corpus. Offline and advisory only - no live reads, no model "
            "calls, no writes on the production box."
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
        "--horizon-hours",
        type=float,
        default=DEFAULT_HINDSIGHT_HORIZON_HOURS,
        help="How many hours ahead a brief may be scored against "
        f"(default {DEFAULT_HINDSIGHT_HORIZON_HOURS:g}, bounded "
        f"{HINDSIGHT_HORIZON_BOUNDS[0]:g}-{HINDSIGHT_HORIZON_BOUNDS[1]:g}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report JSON instead of the human summary.",
    )
    arguments = parser.parse_args(argv)
    low, high = HINDSIGHT_HORIZON_BOUNDS
    if not low <= arguments.horizon_hours <= high:
        parser.error(f"--horizon-hours must be {low:g} to {high:g}")
    corpus_dir = arguments.corpus_dir or resolve_corpus_dir(os.environ)
    try:
        episodes = load_episodes(corpus_dir / TEACHER_CORPUS_NAME)
        report = score_corpus(episodes, arguments.horizon_hours)
        report_path = write_report(report, corpus_dir)
    except OSError as error:
        print(f"the hindsight report could not be written: {error}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as error:
        # A corpus the scorer cannot even read is a typed failure, never
        # a traceback.
        print(f"the corpus could not be read: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(json.loads(report.model_dump_json()), indent=2))
    else:
        print_report(report)
        print(f"report: {report_path}")
    return 0


__all__ = [
    "CALIBRATED_STREAMS",
    "CountedReason",
    "DEFAULT_HINDSIGHT_HORIZON_HOURS",
    "HINDSIGHT_HORIZON_BOUNDS",
    "HINDSIGHT_REPORT_NAME",
    "HINDSIGHT_REPORT_SCHEMA",
    "HINDSIGHT_SERIES_MAX",
    "HINDSIGHT_TRUTH_RULE",
    "HindsightCalibration",
    "HindsightDeskScore",
    "HindsightReport",
    "HindsightScoreboard",
    "UNASKED_REASONS",
    "collect_reads",
    "main",
    "print_report",
    "resolve_corpus_dir",
    "score_corpus",
    "write_report",
]
