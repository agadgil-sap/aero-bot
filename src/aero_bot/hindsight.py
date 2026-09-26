"""The hindsight scorer: replay the corpus against what actually followed.

The intelligence layer's third surface closes the loop the teacher harness
opened: episodes accumulate in the local corpus, and this scorer replays
them offline against the deterministic facts later episodes carry, scoring
every desk - both teacher seats and the student whose audited brief rides
each episode - on availability, anomaly calibration, and the conviction
layer's view grading.

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
- **Conviction is measured, not asserted.** A stated position view is
  graded against the same deterministic later facts under the report's
  own view rule; explicit declines, view gaps, and incoherent verdicts
  are counted, never graded, and the view-versus-policy counterfactual
  is computed only where the corpus prices it - never fabricated.
- **One uniform shape.** The report is one frozen schema-validated object
  (``hindsight_report/1``) rewritten atomically, so a month-old report
  explains itself: the horizon, the truth rule, and the view rule ride
  inside it.
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
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, cast

from pydantic import BaseModel, Field

from aero_bot.advisor import (
    AdvisorAnomaly,
    AdvisorBrief,
    AdvisorWindowFacts,
    PositionVerdict,
    PositionView,
    ViewConfidence,
)
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
# The self-describing rule every stated view is graded against, pinned
# into every report beside the anomaly truth rule.
HINDSIGHT_VIEW_RULE = (
    "a stated view is graded against the deterministic facts later "
    "episodes carry within the horizon: hold is right when the position "
    "stayed tracked through a drained quiet horizon and wrong when a bad "
    "outcome followed while it stayed tracked; exit is right when a bad "
    "outcome followed while the position stayed tracked and wrong when a "
    "quiet horizon drained with it still tracked; recenter is right when "
    "a recenter action followed while tracked and wrong when a quiet "
    "horizon drained with no recenter, ungradeable when a bad outcome "
    "followed with no recenter or the position left; enter is right when "
    "an entry followed with no bad outcome inside the window and wrong "
    "when any bad outcome fell inside it, ungradeable when a quiet "
    "horizon drained with no entry; a view whose position left before "
    "its horizon drained is ungradeable and a view whose horizon has not "
    "filled is pending, never guessed; explicit declines, missing views "
    "on positioned episodes, and verdicts incoherent with the facts are "
    "counted, never graded. The counterfactual is computed only where "
    "the corpus prices it: an exit view the policy declined is compared "
    "against the tracked position's committed-mark path over the same "
    "window - a falling mark means the view would have beaten the "
    "policy's stay, a rising mark means the stay won, a flat mark is "
    "equal - first order and blind to emissions, fees, gas, and "
    "slippage; a hold view the policy honored through a drained horizon "
    "is equal and a view that matched the policy's realized action is "
    "equal; every other comparison - a hold overridden by an exit, any "
    "recenter or enter view the policy declined - is uncomputable "
    "because the corpus never observed the counterfactual path"
)


class ViewGrade(StrEnum):
    """Name the four grades a stated view's correctness can take."""

    # The view's directional claim held against realized outcomes.
    RIGHT = "right"
    # The realized outcomes contradicted the claim.
    WRONG = "wrong"
    # The window settled but its outcomes cannot grade this verdict
    # (the position left, or trouble followed without the verdict's
    # matching action).
    UNGRADEABLE = "ungradeable"
    # The horizon has not filled; never guessed.
    PENDING = "pending"


class ViewCounterfactual(StrEnum):
    """Name the verdicts the view-versus-policy comparison can take."""

    # Acting on the view would have beaten the policy's actual choice.
    VIEW_BETTER = "view_better"
    # The policy's actual choice beat acting on the view.
    POLICY_BETTER = "policy_better"
    # No difference the store can price (identical paths or a flat
    # mark delta).
    EQUAL = "equal"
    # The corpus cannot support the comparison; never fabricated.
    UNCOMPUTABLE = "uncomputable"
    # The window has not settled; never guessed.
    PENDING = "pending"


class ViewState(StrEnum):
    """Name what one desk's brief carried on a positioned episode."""

    # A coherent stated view.
    STATED = "stated"
    # An explicit no-view declaration.
    DECLINED = "declined"
    # A positioned episode answered with neither a view nor a decline.
    MISSING = "missing"
    # A stated verdict incoherent with the episode's own facts.
    INCOHERENT = "incoherent"


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


class ViewBandScore(BaseModel):
    """Carry one confidence band's decided-view counts."""

    # Frozen strict fields keep one band's counts immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The band's stable name (low, medium, high).
    band: str
    # Decided views in this band that came back right.
    right: Annotated[int, Field(ge=0)]
    # Decided views in this band that came back wrong.
    wrong: Annotated[int, Field(ge=0)]

    @property
    def decided(self) -> int:
        """Count this band's decided views."""
        return self.right + self.wrong

    @property
    def right_rate(self) -> float | None:
        """Report the band's right rate over decided views, else None."""
        decided = self.decided
        return self.right / decided if decided else None


class ViewCounterfactualScore(BaseModel):
    """Carry one desk's view-versus-policy comparison counts."""

    # Frozen strict fields keep one desk's counts immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Windows where acting on the view would have beaten the policy.
    view_better: Annotated[int, Field(ge=0)] = 0
    # Windows where the policy's actual choice beat the view.
    policy_better: Annotated[int, Field(ge=0)] = 0
    # Windows the store prices as identical (matched actions, flat marks).
    equal: Annotated[int, Field(ge=0)] = 0
    # Windows the corpus cannot support; never fabricated.
    uncomputable: Annotated[int, Field(ge=0)] = 0
    # Windows still waiting to settle; never guessed.
    pending: Annotated[int, Field(ge=0)] = 0

    @property
    def decided(self) -> int:
        """Count every settled comparison."""
        return self.view_better + self.policy_better + self.equal + self.uncomputable


class HindsightViewScore(BaseModel):
    """Carry one desk's stated-view grading over the corpus."""

    # Frozen strict fields keep one desk's view score immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Coherent stated views entering grading.
    stated: Annotated[int, Field(ge=0)] = 0
    # Explicit no-view declarations; honest, never graded.
    declined: Annotated[int, Field(ge=0)] = 0
    # Positioned episodes answered with neither a view nor a decline.
    missing: Annotated[int, Field(ge=0)] = 0
    # Stated verdicts incoherent with the episode's own facts.
    incoherent: Annotated[int, Field(ge=0)] = 0
    # Stated views graded right against realized outcomes.
    right: Annotated[int, Field(ge=0)] = 0
    # Stated views graded wrong against realized outcomes.
    wrong: Annotated[int, Field(ge=0)] = 0
    # Settled windows whose outcomes cannot grade the verdict.
    ungradeable: Annotated[int, Field(ge=0)] = 0
    # Views still waiting for their horizon; never guessed.
    pending: Annotated[int, Field(ge=0)] = 0
    # The decided counts per confidence band, low through high.
    bands: tuple[ViewBandScore, ...] = ()
    # The view-versus-policy comparison counts.
    counterfactuals: ViewCounterfactualScore = ViewCounterfactualScore()

    @property
    def calibration(self) -> str:
        """Verdict on the confidence bands' ordering.

        High-confidence views must be right more often than low-
        confidence ones; the verdict needs at least one decided view in
        each of the low and high bands, else the corpus is insufficient.
        """
        by_band = {band.band: band for band in self.bands}
        low = by_band.get(ViewConfidence.LOW.value)
        high = by_band.get(ViewConfidence.HIGH.value)
        if low is None or high is None or not low.decided or not high.decided:
            return "insufficient"
        # The decided guards above make both rates real numbers.
        high_rate = cast(float, high.right_rate)
        low_rate = cast(float, low.right_rate)
        return "calibrated" if high_rate > low_rate else "miscalibrated"


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
    # The stated-view grading over the same window-grounded streams;
    # defaults empty so older reports and view-free corpora stay valid.
    views: HindsightViewScore = HindsightViewScore()

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
    # The self-describing rule the stated views were graded against;
    # defaults onto older reports that predate the conviction layer.
    view_rule: str = HINDSIGHT_VIEW_RULE
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
            view=payload.view,
            view_declined=payload.view_declined,
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


def _grounded_timeline(episodes: Sequence[TeacherEpisode]) -> list[TeacherEpisode]:
    """Sort the fact-carrying episodes into the chronological timeline.

    Args:
        episodes: The corpus's episodes in any order.

    Returns:
        The grounded episodes oldest first; episodes carry ``created_at`` at
        pass start but append at pass end, so overlapping passes can land
        out of timestamp order - file order is never chronology.
    """
    return sorted(
        (episode for episode in episodes if episode.facts is not None),
        key=lambda episode: episode.created_at,
    )


def episode_verdicts(
    episodes: Sequence[TeacherEpisode],
    horizon_hours: float,
    now: datetime | None = None,
) -> tuple[tuple[TeacherEpisode, bool | None], ...]:
    """Compute every grounded episode's bad-outcome verdict, oldest first.

    The verdict is the truth rule applied to one episode: did any later
    grounded episode within the horizon observe a negative day P&L or a
    higher halted-cycle count than the episode's own baseline. This is the
    public export the upgrade loop's divergence digest consumes; the
    scorer's own desk scores derive from the same computation.

    Args:
        episodes: The corpus's episodes, oldest first.
        horizon_hours: How many hours ahead a read may be scored against.
        now: The pass's reference time; None reads the clock.

    Returns:
        One ``(episode, verdict)`` pair per grounded episode in
        chronological order: True when a bad outcome followed inside the
        horizon, False when the horizon drained with only quiet
        observations, None while the read stays pending.
    """
    moment = now if now is not None else datetime.now(UTC)
    horizon = timedelta(hours=horizon_hours)
    grounded = _grounded_timeline(episodes)
    return tuple(
        (episode, _bad_outcome_followed(episode, grounded[index + 1 :], horizon, moment))
        for index, episode in enumerate(grounded)
    )


@dataclass(frozen=True)
class ViewGradeEntry:
    """Carry one desk's stated view on one episode with its grades."""

    # The grounded episode the view was stated on.
    episode: TeacherEpisode
    # The desk's stable name: a teacher seat or the student.
    desk: str
    # The stated view itself.
    view: PositionView | None
    # What the brief carried: stated, declined, missing, or incoherent.
    state: ViewState
    # The correctness grade, None for every ungraded state.
    grade: ViewGrade | None
    # The view-versus-policy comparison, None for every ungraded state.
    counterfactual: ViewCounterfactual | None


@dataclass(frozen=True)
class ViewGrades:
    """Carry every desk's view grading over the corpus, joinable per episode."""

    # Every entry in chronological order.
    entries: tuple[ViewGradeEntry, ...] = ()

    def by_episode(self, episode: TeacherEpisode) -> Mapping[str, ViewGradeEntry]:
        """Join the entries back to one episode, keyed by desk name.

        Args:
            episode: The corpus episode to look up.

        Returns:
            The desk-name-to-entry mapping for that episode; keyed the
            same way the posture audit's per-episode join keys, over the
            same loader objects.
        """
        return {entry.desk: entry for entry in self.entries if entry.episode is episode}

    def for_desk(self, desk: str) -> tuple[ViewGradeEntry, ...]:
        """List one desk's entries in chronological order.

        Args:
            desk: The desk's stable name.

        Returns:
            The desk's entries; empty when the desk stated nothing.
        """
        return tuple(entry for entry in self.entries if entry.desk == desk)


def _parse_mark(value: str | None) -> Decimal | None:
    """Parse one committed-mark string into a finite Decimal, honestly.

    Args:
        value: The facts field's string form, else None.

    Returns:
        The parsed finite Decimal, else None when absent, malformed, or
        non-finite; exactly the risk manager's posture discipline.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class _ViewWindowScan:
    """Carry the realized facts one stated view is graded against."""

    # Whether the horizon has fully elapsed at scoring time.
    drained: bool
    # Whether any in-window follower was observed at all; quiet grades
    # need observations, mirroring the anomaly truth rule.
    observed: bool
    # Whether the tracked symbol left (book flat or switched) inside the
    # window; the tracked position's own path ends there.
    position_left: bool
    # Whether a bad outcome was observed before any leave (tracked
    # views) - the same bad truth the anomaly calibration scores.
    bad_while_held: bool
    # Whether any bad outcome fell inside the window (flat views).
    bad_in_window: bool
    # Whether a recenter action was observed before any leave.
    recenter_seen: bool
    # Whether a position appeared in the window (enter views).
    entry_seen: bool
    # The last valid committed-mark reading while the same symbol stayed
    # tracked, else None; the exit counterfactual's terminal reading.
    final_committed: str | None


def _scan_view_window(
    facts: AdvisorWindowFacts,
    created_at: datetime,
    grounded_tail: Sequence[TeacherEpisode],
    horizon: timedelta,
    now: datetime,
) -> _ViewWindowScan:
    """Scan the later grounded episodes one stated view grades against.

    Args:
        facts: The episode's composed facts; they define the baseline
            symbol, halt count, and committed mark.
        created_at: The episode's timestamp.
        grounded_tail: Every chronologically later grounded episode.
        horizon: How far ahead the truth may be observed.
        now: The scoring pass's reference time.

    Returns:
        The realized window facts; malformed and non-finite economics
        are absences, never guesses, throughout.
    """
    symbol = facts.tracked_symbol
    limit = created_at + horizon
    drained = now >= limit
    observed = False
    position_left = False
    bad_while_held = False
    bad_in_window = False
    recenter_seen = False
    entry_seen = False
    final_committed: str | None = None
    for follower in grounded_tail:
        follower_facts = follower.facts
        if follower_facts is None:
            continue
        if not created_at < follower.created_at <= limit:
            continue
        observed = True
        day_pnl = _parse_usdc(follower_facts.day_pnl_usdc)
        bad = (day_pnl is not None and day_pnl < 0.0) or (
            follower_facts.halted_count > facts.halted_count
        )
        if symbol is None:
            if follower_facts.tracked_symbol is not None:
                entry_seen = True
            if bad:
                bad_in_window = True
            continue
        if follower_facts.tracked_symbol != symbol:
            # The tracked position's own path ends here: later facts
            # describe a different book and never grade the stay.
            position_left = True
            break
        if bad:
            bad_while_held = True
        if follower_facts.latest_action == "recenter":
            recenter_seen = True
        if _parse_mark(follower_facts.committed_usdc) is not None:
            final_committed = follower_facts.committed_usdc
    return _ViewWindowScan(
        drained=drained,
        observed=observed,
        position_left=position_left,
        bad_while_held=bad_while_held,
        bad_in_window=bad_in_window,
        recenter_seen=recenter_seen,
        entry_seen=entry_seen,
        final_committed=final_committed,
    )


def _grade_stated_view(
    view: PositionView, scan: _ViewWindowScan, facts: AdvisorWindowFacts
) -> tuple[ViewGrade, ViewCounterfactual]:
    """Grade one coherent stated view and its policy comparison.

    Args:
        view: The stated view.
        scan: The realized window facts the view grades against.
        facts: The episode's own composed facts; the committed mark
            anchors the exit counterfactual.

    Returns:
        The correctness grade and the counterfactual verdict, per the
        rule pinned into every report (``HINDSIGHT_VIEW_RULE``).
    """
    if view.verdict is PositionVerdict.HOLD:
        if scan.bad_while_held:
            return ViewGrade.WRONG, ViewCounterfactual.PENDING
        if scan.position_left:
            return ViewGrade.UNGRADEABLE, ViewCounterfactual.UNCOMPUTABLE
        if scan.drained and scan.observed:
            return ViewGrade.RIGHT, ViewCounterfactual.EQUAL
        return ViewGrade.PENDING, ViewCounterfactual.PENDING
    if view.verdict is PositionVerdict.EXIT:
        if scan.bad_while_held:
            grade = ViewGrade.RIGHT
        elif scan.position_left:
            grade = ViewGrade.UNGRADEABLE
        elif scan.drained and scan.observed:
            grade = ViewGrade.WRONG
        else:
            grade = ViewGrade.PENDING
        if not (scan.drained or scan.position_left):
            # The window is still accruing truth; the comparison waits
            # for settlement even when the bad outcome already decided
            # the grade.
            return grade, ViewCounterfactual.PENDING
        # The one computable comparison: the committed-mark path of the
        # stay the policy actually chose, first order and blind to
        # emissions, fees, gas, and slippage.
        baseline_mark = _parse_mark(facts.committed_usdc)
        final_mark = _parse_mark(scan.final_committed)
        if baseline_mark is None or final_mark is None:
            return grade, ViewCounterfactual.UNCOMPUTABLE
        delta = final_mark - baseline_mark
        if delta < 0:
            return grade, ViewCounterfactual.VIEW_BETTER
        if delta > 0:
            return grade, ViewCounterfactual.POLICY_BETTER
        return grade, ViewCounterfactual.EQUAL
    if view.verdict is PositionVerdict.RECENTER:
        if scan.recenter_seen:
            return ViewGrade.RIGHT, ViewCounterfactual.EQUAL
        if scan.position_left:
            return ViewGrade.UNGRADEABLE, ViewCounterfactual.UNCOMPUTABLE
        if scan.drained and scan.observed:
            if scan.bad_while_held:
                return ViewGrade.UNGRADEABLE, ViewCounterfactual.UNCOMPUTABLE
            return ViewGrade.WRONG, ViewCounterfactual.UNCOMPUTABLE
        return ViewGrade.PENDING, ViewCounterfactual.PENDING
    # The enter verdict: stated while flat, graded over the whole window.
    if scan.bad_in_window:
        return ViewGrade.WRONG, ViewCounterfactual.PENDING
    if scan.entry_seen:
        if scan.drained:
            return ViewGrade.RIGHT, ViewCounterfactual.EQUAL
        return ViewGrade.PENDING, ViewCounterfactual.PENDING
    if scan.drained and scan.observed:
        return ViewGrade.UNGRADEABLE, ViewCounterfactual.UNCOMPUTABLE
    return ViewGrade.PENDING, ViewCounterfactual.PENDING


def _view_state(read: DeskRead) -> tuple[ViewState, PositionView | None] | None:
    """Classify one desk read's view carriage on a positioned-or-flat episode.

    Args:
        read: The desk's projected read.

    Returns:
        The read's view state and the stated view when present, else
        None when the episode carries no brief, sits outside the
        calibrated streams, or offers no view surface (a flat book the
        brief read cleanly).
    """
    episode = read.episode
    brief = read.brief
    facts = episode.facts
    if brief is None or facts is None or episode.stream not in CALIBRATED_STREAMS:
        return None
    tracked = facts.tracked_symbol is not None
    if brief.view is not None:
        verdict = brief.view.verdict
        coherent = (
            verdict is PositionVerdict.ENTER
            if not tracked
            else verdict is not PositionVerdict.ENTER
        )
        return (ViewState.INCOHERENT if not coherent else ViewState.STATED, brief.view)
    if brief.view_declined is not None:
        return ViewState.DECLINED, None
    if tracked:
        return ViewState.MISSING, None
    # A flat book with no view: the view was optional, nothing to count.
    return None


def grade_corpus_views(
    episodes: Sequence[TeacherEpisode],
    horizon_hours: float,
    now: datetime | None = None,
) -> ViewGrades:
    """Grade every desk's stated views over the corpus.

    The conviction layer's truth pass: every answered brief on a
    window-grounded episode carries either a stated view, an explicit
    decline, or (on a positioned episode) a measurable gap. Stated views
    are graded against the deterministic facts later episodes carry
    within the horizon, their confidence bands bucketed for calibration,
    and their policy comparison marked computable only where the corpus
    prices it (see ``HINDSIGHT_VIEW_RULE``). No model judges another
    model; every grade is recomputable by hand.

    Args:
        episodes: The corpus's episodes, oldest first.
        horizon_hours: How many hours ahead a view may be graded against.
        now: The pass's reference time; None reads the clock.

    Returns:
        Every desk's view entries in chronological order, joinable per
        episode for the upgrade digest's conviction class.
    """
    moment = now if now is not None else datetime.now(UTC)
    horizon = timedelta(hours=horizon_hours)
    grounded = _grounded_timeline(episodes)
    desks: list[tuple[str, TeacherSeatName | None]] = [
        (seat.value, seat) for seat in TeacherSeatName
    ]
    desks.append(("student", None))
    entries: list[ViewGradeEntry] = []
    for index, episode in enumerate(grounded):
        facts = episode.facts
        if facts is None:
            # The grounded timeline guarantees this never trips.
            continue
        tail = grounded[index + 1 :]
        for name, seat in desks:
            read = collect_reads((episode,), seat)[0]
            classified = _view_state(read)
            if classified is None:
                continue
            state, view = classified
            grade: ViewGrade | None = None
            counterfactual: ViewCounterfactual | None = None
            if state is ViewState.STATED and view is not None:
                scan = _scan_view_window(facts, episode.created_at, tail, horizon, moment)
                grade, counterfactual = _grade_stated_view(view, scan, facts)
            entries.append(
                ViewGradeEntry(
                    episode=episode,
                    desk=name,
                    view=view,
                    state=state,
                    grade=grade,
                    counterfactual=counterfactual,
                )
            )
    entries.sort(key=lambda entry: entry.episode.created_at)
    return ViewGrades(entries=tuple(entries))


def _compose_view_score(entries: Sequence[ViewGradeEntry]) -> HindsightViewScore:
    """Aggregate one desk's view entries into the frozen score.

    Args:
        entries: One desk's chronological entries.

    Returns:
        The counts, bands, and counterfactuals; decided views alone feed
        the bands, and every state feeds its own count.
    """
    counts = dict.fromkeys(ViewState, 0)
    grades = dict.fromkeys(ViewGrade, 0)
    band_counts = {
        band: {"right": 0, "wrong": 0}
        for band in (ViewConfidence.LOW, ViewConfidence.MEDIUM, ViewConfidence.HIGH)
    }
    comparisons = dict.fromkeys(ViewCounterfactual, 0)
    for entry in entries:
        counts[entry.state] += 1
        if entry.grade is not None:
            grades[entry.grade] += 1
        if entry.counterfactual is not None:
            comparisons[entry.counterfactual] += 1
        if entry.state is ViewState.STATED and entry.grade in (ViewGrade.RIGHT, ViewGrade.WRONG):
            # The stated state always carries its view; the static type
            # cannot see that construction.
            view = cast(PositionView, entry.view)
            band_counts[view.confidence][
                "right" if entry.grade is ViewGrade.RIGHT else "wrong"
            ] += 1
    bands = tuple(
        ViewBandScore(band=band.value, right=tallies["right"], wrong=tallies["wrong"])
        for band, tallies in band_counts.items()
    )
    return HindsightViewScore(
        stated=counts[ViewState.STATED],
        declined=counts[ViewState.DECLINED],
        missing=counts[ViewState.MISSING],
        incoherent=counts[ViewState.INCOHERENT],
        right=grades[ViewGrade.RIGHT],
        wrong=grades[ViewGrade.WRONG],
        ungradeable=grades[ViewGrade.UNGRADEABLE],
        pending=grades[ViewGrade.PENDING],
        bands=bands,
        counterfactuals=ViewCounterfactualScore(
            view_better=comparisons[ViewCounterfactual.VIEW_BETTER],
            policy_better=comparisons[ViewCounterfactual.POLICY_BETTER],
            equal=comparisons[ViewCounterfactual.EQUAL],
            uncomputable=comparisons[ViewCounterfactual.UNCOMPUTABLE],
            pending=comparisons[ViewCounterfactual.PENDING],
        ),
    )


def _score_desk(
    desk: str,
    reads: Sequence[DeskRead],
    verdicts: Mapping[int, bool | None],
    view_entries: Sequence[ViewGradeEntry] = (),
) -> HindsightDeskScore:
    """Score one desk's reads into availability and calibration counts.

    Args:
        desk: The desk's stable name.
        reads: The desk's projected reads, oldest first.
        verdicts: The per-episode bad-outcome verdicts keyed by episode
            object identity.
        view_entries: The desk's view grading entries from
            :func:`grade_corpus_views`, else empty for a view-free score.

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
        views=_compose_view_score(view_entries),
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
    # One shared computation feeds both the desk scores and any consumer
    # (the upgrade digest) asking for the same truth.
    scored = episode_verdicts(episodes, horizon_hours, moment)
    grounded = [episode for episode, _ in scored]
    verdicts: dict[int, bool | None] = {id(episode): verdict for episode, verdict in scored}
    view_grades = grade_corpus_views(episodes, horizon_hours, moment)
    desks: list[HindsightDeskScore] = []
    for seat in TeacherSeatName:
        desks.append(
            _score_desk(
                seat.value,
                collect_reads(episodes, seat),
                verdicts,
                view_grades.for_desk(seat.value),
            )
        )
    desks.append(
        _score_desk(
            "student", collect_reads(episodes, None), verdicts, view_grades.for_desk("student")
        )
    )
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
        views = desk.views
        if views.stated or views.declined or views.missing or views.incoherent:
            bands = ", ".join(f"{band.band} {band.right}/{band.decided}" for band in views.bands)
            comparisons = views.counterfactuals
            print(
                f"    views: {views.stated} stated "
                f"({views.right} right, {views.wrong} wrong, "
                f"{views.ungradeable} ungradeable, {views.pending} pending), "
                f"{views.declined} declined, {views.missing} missing, "
                f"{views.incoherent} incoherent; bands {bands} "
                f"({views.calibration}); counterfactuals "
                f"{comparisons.view_better} view-better, "
                f"{comparisons.policy_better} policy-better, "
                f"{comparisons.equal} equal, "
                f"{comparisons.uncomputable} uncomputable, "
                f"{comparisons.pending} pending"
            )
    scoreboard = report.scoreboard
    if scoreboard.latest_equity_usdc is not None:
        day_pnl = scoreboard.latest_day_pnl_usdc
        pnl_note = f", day P&L {day_pnl}" if day_pnl is not None else ""
        print(f"  scoreboard: equity {scoreboard.latest_equity_usdc} USDC{pnl_note}")
    print(f"  truth: {report.truth_rule}")
    print(f"  view rule: {report.view_rule}")


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
    "HINDSIGHT_VIEW_RULE",
    "HindsightCalibration",
    "HindsightDeskScore",
    "HindsightReport",
    "HindsightScoreboard",
    "HindsightViewScore",
    "UNASKED_REASONS",
    "ViewBandScore",
    "ViewCounterfactual",
    "ViewCounterfactualScore",
    "ViewGrade",
    "ViewGradeEntry",
    "ViewGrades",
    "ViewState",
    "collect_reads",
    "episode_verdicts",
    "grade_corpus_views",
    "main",
    "print_report",
    "resolve_corpus_dir",
    "score_corpus",
    "write_report",
]
