"""Pin the hindsight scorer's truth rule, desk scores, and CLI contract."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aero_bot.advisor import (
    AdvisorAnomaly,
    AdvisorAnomalyAuditPayload,
    AdvisorBrief,
    AdvisorReportedAuditPayload,
    AdvisorWindowFacts,
    PositionVerdict,
    PositionView,
    ViewConfidence,
)
from aero_bot.hindsight import (
    DEFAULT_HINDSIGHT_HORIZON_HOURS,
    HINDSIGHT_REPORT_NAME,
    HINDSIGHT_REPORT_SCHEMA,
    HINDSIGHT_SERIES_MAX,
    HINDSIGHT_TRUTH_RULE,
    HINDSIGHT_VIEW_RULE,
    HindsightCalibration,
    HindsightDeskScore,
    HindsightReport,
    HindsightViewScore,
    ViewCounterfactual,
    ViewGrade,
    ViewGrades,
    ViewState,
    collect_reads,
    episode_verdicts,
    grade_corpus_views,
    resolve_corpus_dir,
    score_corpus,
    write_report,
)
from aero_bot.hindsight import (
    main as hindsight_main,
)
from aero_bot.teacher import (
    DEFAULT_TEACHER_CORPUS_DIR,
    TEACHER_CORPUS_DIR_ENV,
    TEACHER_EPISODE_SCHEMA,
    StudentWindowBrief,
    TeacherEpisode,
    TeacherSeatName,
    TeacherSeatOutcome,
    TeacherStream,
)

# One fixed time base keeps every horizon deterministic; NOW sits one
# minute past T0's 24-hour horizon so quiet verdicts are final.
T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 24, 20, 1, tzinfo=UTC)
HORIZON = 24.0


def facts_picture(
    *,
    equity: str | None = "100.0",
    day_start: str | None = "99.0",
    day_pnl: str | None = "1.0",
    halted: int = 0,
    committed: str | None = None,
    action: str | None = None,
) -> AdvisorWindowFacts:
    """Build one grounded window snapshot with deterministic economics."""
    return AdvisorWindowFacts(
        record_count=5,
        tracked_symbol="SPCXc",
        committed_usdc=committed,
        equity_usdc=equity,
        day_start_equity_usdc=day_start,
        day_pnl_usdc=day_pnl,
        halted_count=halted,
        latest_action=action,
    )


def calm_brief(flagged: bool = False) -> AdvisorBrief:
    """Build one accepted answer, optionally carrying a single anomaly."""
    anomalies: tuple[AdvisorAnomaly, ...] = ()
    if flagged:
        anomalies = (AdvisorAnomaly(label="equity divergence", confidence=0.8, rationale="drift"),)
    return AdvisorBrief(brief="a calm window reading", anomalies=anomalies)


def claude_outcome(
    *,
    outcome: str = "brief",
    brief: AdvisorBrief | None = None,
    model: str = "GLM 5.3",
) -> TeacherSeatOutcome:
    """Build the claude seat's outcome for one episode."""
    return TeacherSeatOutcome(
        seat=TeacherSeatName.CLAUDE,
        model=model,
        outcome=outcome,
        brief=brief if brief is not None else (calm_brief() if outcome == "brief" else None),
    )


def episode(
    at: datetime,
    *,
    stream: TeacherStream = TeacherStream.TACTICAL,
    facts: AdvisorWindowFacts | None = None,
    pulled: bool = True,
    seats: tuple[TeacherSeatOutcome, ...] = (),
    student: AdvisorReportedAuditPayload | None = None,
) -> TeacherEpisode:
    """Build one corpus episode with deterministic shape."""
    return TeacherEpisode(
        stream=stream,
        created_at=at,
        window_outcome="pulled" if pulled else "window_unreachable",
        facts=(facts if facts is not None else facts_picture()) if pulled else None,
        student=(
            StudentWindowBrief(created_at=at - timedelta(minutes=30), payload=student)
            if student is not None
            else None
        ),
        seats=seats,
    )


def desk_by_name(report: HindsightReport, desk: str) -> HindsightDeskScore:
    """Find one desk's score in a report."""
    return next(score for score in report.desks if score.desk == desk)


def scored(*episodes: TeacherEpisode) -> HindsightReport:
    """Score fixed episodes over the standard horizon and now."""
    return score_corpus(episodes, HORIZON, now=NOW)


class TestEpisodeVerdicts:
    """The public per-episode export the upgrade digest consumes."""

    def test_the_export_matches_the_scores_the_desks_receive(self) -> None:
        """Every grounded episode's verdict equals the internal truth."""
        episodes = (
            episode(T0, seats=(claude_outcome(brief=calm_brief(flagged=True)),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
            episode(
                T0 + timedelta(hours=4),
                facts=facts_picture(day_pnl="0.5"),
                stream=TeacherStream.DAILY,
            ),
            episode(T0 + timedelta(hours=6), pulled=False),
        )
        verdicts = episode_verdicts(episodes, HORIZON, now=NOW)
        # Only the grounded episodes appear, oldest first, in timestamp
        # order regardless of append order.
        assert tuple(episode_.created_at for episode_, _ in verdicts) == (
            episodes[0].created_at,
            episodes[1].created_at,
            episodes[2].created_at,
        )
        assert dict(verdicts)[episodes[0]] is True
        # Episode one's own horizon (T0 + 26h) has not drained at NOW, so
        # its quiet followers keep it pending; and the last grounded
        # episode has no followers at all - pending, never guessed.
        assert dict(verdicts)[episodes[1]] is None
        assert dict(verdicts)[episodes[2]] is None

    def test_an_open_horizon_stays_pending_in_the_export(self) -> None:
        """The export honors the same pending-never-guessed rule."""
        episodes = (
            episode(T0 + timedelta(hours=23), facts=facts_picture(day_pnl="0.5")),
            episode(T0 + timedelta(hours=25), facts=facts_picture(day_pnl="0.5")),
        )
        verdicts = episode_verdicts(episodes, HORIZON, now=NOW)
        assert dict(verdicts)[episodes[0]] is None


class TestTruthRule:
    """The deterministic follow-up verdicts, pinned end to end."""

    def test_a_flagged_read_with_a_losing_day_following_scores_flagged_bad(self) -> None:
        """An anomaly flag ahead of a negative day P&L observation hits."""
        report = scored(
            episode(T0, seats=(claude_outcome(brief=calm_brief(flagged=True)),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        calibration = desk_by_name(report, "claude").calibration
        assert calibration.flagged_bad == 1
        assert calibration.scorable == 1

    def test_a_flagged_read_with_a_new_halt_following_scores_flagged_bad(self) -> None:
        """A halt-count increment is the second half of the truth rule."""
        report = scored(
            episode(
                T0,
                facts=facts_picture(halted=0),
                seats=(claude_outcome(brief=calm_brief(flagged=True)),),
            ),
            episode(T0 + timedelta(hours=2), facts=facts_picture(halted=1)),
        )
        assert desk_by_name(report, "claude").calibration.flagged_bad == 1

    def test_a_flagged_read_with_a_quiet_horizon_scores_flagged_quiet(self) -> None:
        """A flag nothing bad confirms is an unconfirmed flag, not a hit."""
        report = scored(
            episode(T0, seats=(claude_outcome(brief=calm_brief(flagged=True)),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        assert desk_by_name(report, "claude").calibration.flagged_quiet == 1

    def test_an_unflagged_read_with_a_bad_outcome_scores_unflagged_bad(self) -> None:
        """A clean brief ahead of a losing observation is a miss."""
        report = scored(
            episode(T0, seats=(claude_outcome(),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-2.0")),
        )
        assert desk_by_name(report, "claude").calibration.unflagged_bad == 1

    def test_an_unflagged_read_with_a_quiet_horizon_scores_unflagged_quiet(self) -> None:
        """A clean brief ahead of a quiet horizon is a clean read."""
        report = scored(
            episode(T0, seats=(claude_outcome(),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture()),
        )
        assert desk_by_name(report, "claude").calibration.unflagged_quiet == 1

    def test_a_read_without_later_facts_stays_pending(self) -> None:
        """No later grounded episode means pending, never a guess."""
        report = scored(episode(T0, seats=(claude_outcome(),)))
        calibration = desk_by_name(report, "claude").calibration
        assert calibration.pending == 1
        assert calibration.scorable == 0

    def test_quiet_observations_do_not_score_before_the_horizon_elapses(self) -> None:
        """A quiet read inside an open horizon stays pending, not quiet."""
        report = score_corpus(
            (
                episode(T0, seats=(claude_outcome(brief=calm_brief(flagged=True)),)),
                episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
            ),
            HORIZON,
            now=T0 + timedelta(hours=3),
        )
        calibration = desk_by_name(report, "claude").calibration
        assert calibration.pending == 1
        assert calibration.flagged_quiet == 0

    def test_a_bad_outcome_scores_immediately_inside_an_open_horizon(self) -> None:
        """The horizon cannot hide a bad outcome already observed."""
        report = score_corpus(
            (
                episode(T0, seats=(claude_outcome(),)),
                episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-1.0")),
            ),
            HORIZON,
            now=T0 + timedelta(hours=3),
        )
        assert desk_by_name(report, "claude").calibration.unflagged_bad == 1

    def test_out_of_order_appends_still_find_their_followups(self) -> None:
        """Episodes timestamped at pass start can append out of order."""
        first = episode(T0, seats=(claude_outcome(),))
        second = episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-1.0"))
        third = episode(T0 + timedelta(hours=1), facts=facts_picture(day_pnl="0.5"))
        report = score_corpus((first, second, third), HORIZON, now=NOW)
        assert desk_by_name(report, "claude").calibration.unflagged_bad == 1
        assert report.scoreboard.latest_day_pnl_usdc == "-1.0"

    def test_non_finite_economics_never_become_truth(self) -> None:
        """NaN and infinite readings are absent, not quiet or bad."""
        report = scored(
            episode(T0, seats=(claude_outcome(brief=calm_brief(flagged=True)),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="nan")),
            episode(T0 + timedelta(hours=3), facts=facts_picture(day_pnl="-inf")),
        )
        calibration = desk_by_name(report, "claude").calibration
        assert calibration.flagged_quiet == 1
        assert calibration.flagged_bad == 0

    def test_a_bad_outcome_outside_the_horizon_leaves_the_read_pending(self) -> None:
        """The truth window is bounded by the horizon, not the corpus."""
        report = scored(
            episode(T0, seats=(claude_outcome(),)),
            episode(T0 + timedelta(hours=30), facts=facts_picture(day_pnl="-5.0")),
        )
        calibration = desk_by_name(report, "claude").calibration
        assert calibration.pending == 1
        assert calibration.flagged_bad == 0
        assert calibration.unflagged_bad == 0

    def test_a_malformed_day_pnl_reading_is_an_absence_not_a_bad_outcome(self) -> None:
        """A corrupt economics string never fabricates a truth signal."""
        report = scored(
            episode(T0, seats=(claude_outcome(brief=calm_brief(flagged=True)),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="not-a-number")),
        )
        calibration = desk_by_name(report, "claude").calibration
        assert calibration.flagged_quiet == 1

    def test_the_baseline_halt_count_is_the_scored_episodes_own(self) -> None:
        """A window already carrying halts only counts increments."""
        report = scored(
            episode(T0, facts=facts_picture(halted=2), seats=(claude_outcome(),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(halted=2)),
        )
        assert desk_by_name(report, "claude").calibration.unflagged_quiet == 1


class TestStreamScoping:
    """Calibration applies to window-grounded streams only."""

    def test_news_briefs_count_for_availability_but_never_calibration(self) -> None:
        """The outside world has no deterministic follow-up truth."""
        report = scored(
            episode(
                T0,
                stream=TeacherStream.NEWS,
                seats=(claude_outcome(brief=calm_brief(flagged=True)),),
            ),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-1.0")),
        )
        desk = desk_by_name(report, "claude")
        assert desk.asked == 1
        assert desk.briefs == 1
        assert desk.calibration == HindsightCalibration(
            scorable=0,
            flagged_bad=0,
            flagged_quiet=0,
            unflagged_bad=0,
            unflagged_quiet=0,
            pending=0,
        )

    def test_daily_briefs_are_calibrated_like_tactical(self) -> None:
        """The daily stream reviews the same window-grounded economics."""
        report = scored(
            episode(T0, stream=TeacherStream.DAILY, seats=(claude_outcome(),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-1.0")),
        )
        assert desk_by_name(report, "claude").calibration.unflagged_bad == 1


class TestAvailability:
    """Asked, answered, and absent are pinned to the typed catalog."""

    def test_an_unreachable_window_is_unasked_never_an_absence(self) -> None:
        """No grounded facts means no question was asked of any seat."""
        report = scored(
            episode(T0, pulled=False, seats=(claude_outcome(outcome="window_unreachable"),)),
        )
        desk = desk_by_name(report, "claude")
        assert desk.asked == 0
        assert desk.briefs == 0
        assert desk.absences == ()

    def test_a_dark_seat_is_unasked(self) -> None:
        """A disabled seat received no question."""
        report = scored(episode(T0, seats=(claude_outcome(outcome="dark"),)))
        assert desk_by_name(report, "claude").asked == 0

    def test_a_timeout_is_an_asked_absence_with_its_reason_counted(self) -> None:
        """A timed-out seat was asked and is counted as absent."""
        report = scored(episode(T0, seats=(claude_outcome(outcome="timeout"),)))
        desk = desk_by_name(report, "claude")
        assert desk.asked == 1
        assert desk.briefs == 0
        assert [(counted.reason, counted.count) for counted in desk.absences] == [("timeout", 1)]

    def test_absence_reasons_order_most_frequent_first(self) -> None:
        """Ties break alphabetically; frequency leads."""
        outcomes = (
            claude_outcome(outcome="timeout"),
            claude_outcome(outcome="timeout"),
            claude_outcome(outcome="cli_error"),
        )
        report = scored(
            *(
                episode(T0 + timedelta(minutes=30 * i), seats=(seat,))
                for i, seat in enumerate(outcomes)
            )
        )
        desk = desk_by_name(report, "claude")
        assert [(counted.reason, counted.count) for counted in desk.absences] == [
            ("timeout", 2),
            ("cli_error", 1),
        ]

    def test_a_seat_filtered_out_of_a_pass_is_unasked(self) -> None:
        """A pass the seat did not serve leaves no read behind."""
        episodes = (episode(T0, seats=()), episode(T0 + timedelta(hours=1)))
        reads = collect_reads(episodes, TeacherSeatName.CLAUDE)
        assert len(reads) == 2
        assert all(not read.asked for read in reads)

    def test_availability_is_the_brief_rate_over_asked_episodes(self) -> None:
        """The availability property is exact arithmetic."""
        outcomes = (claude_outcome(), claude_outcome(outcome="timeout"))
        report = scored(
            *(
                episode(T0 + timedelta(minutes=30 * i), seats=(seat,))
                for i, seat in enumerate(outcomes)
            )
        )
        desk = desk_by_name(report, "claude")
        assert desk.availability == pytest.approx(0.5)

    def test_availability_is_none_when_the_desk_was_never_asked(self) -> None:
        """Zero asked episodes divides by nothing."""
        report = scored()
        assert all(desk.availability is None for desk in report.desks)


class TestStudentDesk:
    """The student's audited brief scores as the third desk."""

    def test_a_student_brief_is_asked_answered_and_calibrated(self) -> None:
        """The student's anomalies project into the same shape."""
        payload = AdvisorReportedAuditPayload(
            outcome="brief",
            model="qwen3.6:35b-a3b",
            brief="calm student read",
            anomalies=(
                AdvisorAnomalyAuditPayload(
                    label="stale equity", confidence=0.9, rationale="fourth decimal"
                ),
            ),
        )
        report = scored(
            episode(T0, student=payload, seats=(claude_outcome(),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-1.0")),
        )
        desk = desk_by_name(report, "student")
        assert desk.model == "qwen3.6:35b-a3b"
        assert desk.asked == 1
        assert desk.briefs == 1
        assert desk.calibration.flagged_bad == 1

    def test_an_episode_without_a_student_observation_is_unasked(self) -> None:
        """No advisor record in the window is no question, no absence."""
        report = scored(episode(T0, seats=(claude_outcome(),)))
        desk = desk_by_name(report, "student")
        assert desk.asked == 0
        assert desk.absences == ()

    def test_a_dark_student_is_unasked(self) -> None:
        """The student's own dark pass was never a question."""
        payload = AdvisorReportedAuditPayload(outcome="dark", model="")
        report = scored(episode(T0, student=payload))
        assert desk_by_name(report, "student").asked == 0

    def test_a_timed_out_student_is_an_asked_absence(self) -> None:
        """The student's typed absence counts like any seat's."""
        payload = AdvisorReportedAuditPayload(outcome="timeout", model="")
        report = scored(episode(T0, student=payload))
        desk = desk_by_name(report, "student")
        assert desk.asked == 1
        assert [(counted.reason, counted.count) for counted in desk.absences] == [("timeout", 1)]


class TestScoreboard:
    """The report's economics context is composed from the corpus."""

    def test_the_scoreboard_carries_the_latest_grounded_economics(self) -> None:
        """Latest means the chronologically last grounded episode."""
        report = scored(
            episode(
                T0, facts=facts_picture(equity="100.0", day_pnl="1.0"), seats=(claude_outcome(),)
            ),
            episode(T0 + timedelta(hours=2), facts=facts_picture(equity="102.5", day_pnl="3.5")),
        )
        scoreboard = report.scoreboard
        assert scoreboard.latest_equity_usdc == "102.5"
        assert scoreboard.latest_day_pnl_usdc == "3.5"
        assert scoreboard.latest_day_start_equity_usdc == "99.0"

    def test_series_run_oldest_first_and_skip_absent_samples(self) -> None:
        """Absent readings leave no hole in the bounded series."""
        report = scored(
            episode(T0, facts=facts_picture(equity="100.0", day_pnl="1.0")),
            episode(T0 + timedelta(hours=1), facts=facts_picture(equity=None, day_pnl=None)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(equity="101.0", day_pnl="2.0")),
        )
        assert report.scoreboard.equity_samples == ("100.0", "101.0")
        assert report.scoreboard.day_pnl_samples == ("1.0", "2.0")

    def test_series_are_bounded_to_the_pinned_tail(self) -> None:
        """Only the most recent samples ride the report."""
        episodes = tuple(
            episode(
                T0 + timedelta(minutes=30 * index), facts=facts_picture(equity=str(100 + index))
            )
            for index in range(HINDSIGHT_SERIES_MAX + 5)
        )
        report = scored(*episodes)
        assert len(report.scoreboard.equity_samples) == HINDSIGHT_SERIES_MAX
        assert report.scoreboard.equity_samples[-1] == str(100 + HINDSIGHT_SERIES_MAX + 4)

    def test_an_empty_corpus_scores_honestly_to_zero_everywhere(self) -> None:
        """No episodes means no desks asked and no scoreboard claims."""
        report = scored()
        assert report.episode_count == 0
        assert report.grounded_episode_count == 0
        assert report.scoreboard.latest_equity_usdc is None
        assert report.desks and all(desk.asked == 0 for desk in report.desks)

    def test_ungrounded_episodes_do_not_enter_the_timeline(self) -> None:
        """Only pulled episodes count toward the grounded timeline."""
        report = scored(
            episode(T0, pulled=False, seats=(claude_outcome(outcome="window_unreachable"),)),
            episode(T0 + timedelta(hours=1), seats=(claude_outcome(),)),
        )
        assert report.episode_count == 2
        assert report.grounded_episode_count == 1


class TestReportShape:
    """The report is one frozen self-describing object."""

    def test_the_report_pins_its_schema_horizon_and_truth_rule(self) -> None:
        """A month-old report explains its own scoring rule."""
        report = scored(episode(T0, seats=(claude_outcome(),)))
        assert report.schema_version == HINDSIGHT_REPORT_SCHEMA
        assert report.horizon_hours == HORIZON
        assert report.truth_rule == HINDSIGHT_TRUTH_RULE
        assert "stays pending until its horizon has fully elapsed" in HINDSIGHT_TRUTH_RULE
        assert "negative day P&L" in HINDSIGHT_TRUTH_RULE

    def test_the_desks_order_teacher_seats_first_then_the_student(self) -> None:
        """The scoreboard's rows are stable."""
        report = scored(episode(T0, seats=(claude_outcome(),)))
        assert [desk.desk for desk in report.desks] == ["claude", "codex", "student"]

    def test_the_model_tag_comes_from_the_latest_answer(self) -> None:
        """The desk's model is its most recent non-empty tag."""
        outcomes = (claude_outcome(model="old-model"), claude_outcome(model="GLM 5.3"))
        report = scored(
            *(
                episode(T0 + timedelta(minutes=30 * i), seats=(seat,))
                for i, seat in enumerate(outcomes)
            )
        )
        assert desk_by_name(report, "claude").model == "GLM 5.3"


class TestCorpusWriting:
    """The scored report lands beside the corpus, atomically."""

    def test_write_report_rewrites_the_pinned_file_atomically(self, tmp_path: Path) -> None:
        """The report exists, parses, and leaves no temporary behind."""
        report = scored(episode(T0, seats=(claude_outcome(),)))
        path = write_report(report, tmp_path)
        assert path == tmp_path / "reports" / HINDSIGHT_REPORT_NAME
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == HINDSIGHT_REPORT_SCHEMA
        assert not (tmp_path / "reports" / "hindsight_last.json.tmp").exists()

    def test_the_cli_reads_the_corpus_and_writes_the_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A seeded corpus scores through the CLI end to end."""
        corpus = tmp_path / "teacher"
        corpus.mkdir()
        first = episode(T0, seats=(claude_outcome(),))
        second = episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-1.0"))
        with (corpus / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(first.model_dump_json() + "\n")
            handle.write(second.model_dump_json() + "\n")
        code = hindsight_main(["--corpus-dir", str(corpus), "--json"])
        captured = capsys.readouterr()
        assert code == 0
        assert '"schema_version": "hindsight_report/1"' in captured.out
        assert (corpus / "reports" / HINDSIGHT_REPORT_NAME).exists()

    def test_the_human_summary_names_every_desk_and_the_truth_rule(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The default printout is the daily report a human reads."""
        corpus = tmp_path / "teacher"
        corpus.mkdir()
        with (corpus / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(episode(T0, seats=(claude_outcome(),)).model_dump_json() + "\n")
            handle.write(
                episode(
                    T0 + timedelta(hours=1),
                    seats=(claude_outcome(outcome="timeout"),),
                ).model_dump_json()
                + "\n"
            )
        code = hindsight_main(["--corpus-dir", str(corpus)])
        captured = capsys.readouterr()
        assert code == 0
        assert "hindsight report" in captured.out
        assert "claude [GLM 5.3]: 1/2 briefs" in captured.out
        assert "absent timeout: 1" in captured.out
        assert "scoreboard: equity 100.0 USDC" in captured.out
        assert "truth:" in captured.out

    def test_an_empty_or_missing_corpus_exits_zero(self, tmp_path: Path) -> None:
        """An honest empty report is a clean pass."""
        code = hindsight_main(["--corpus-dir", str(tmp_path / "absent")])
        assert code == 0
        assert (tmp_path / "absent" / "reports" / HINDSIGHT_REPORT_NAME).exists()

    def test_an_unwritable_corpus_directory_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corpus directory that cannot hold reports fails honestly."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        code = hindsight_main(["--corpus-dir", str(blocker)])
        captured = capsys.readouterr()
        assert code == 1
        assert "could not be written" in captured.err

    def test_an_unreadable_corpus_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Invalid UTF-8 in the corpus is a typed failure, not a traceback."""
        corpus = tmp_path / "teacher"
        corpus.mkdir()
        (corpus / "corpus.jsonl").write_bytes(b"\xff\xfe not utf-8")
        code = hindsight_main(["--corpus-dir", str(corpus)])
        captured = capsys.readouterr()
        assert code == 1
        assert "could not be read" in captured.err


def stated_view(
    verdict: PositionVerdict = PositionVerdict.HOLD,
    confidence: ViewConfidence = ViewConfidence.HIGH,
) -> PositionView:
    """Build one stated view citing the locked policy."""
    return PositionView(
        verdict=verdict,
        confidence=confidence,
        reason="in range with emissions above the locked floor",
    )


def viewed_brief(
    verdict: PositionVerdict = PositionVerdict.HOLD,
    confidence: ViewConfidence = ViewConfidence.HIGH,
    *,
    declined: bool = False,
) -> AdvisorBrief:
    """Build one accepted answer carrying a view or a decline."""
    return AdvisorBrief(
        brief="a calm window reading",
        anomalies=(),
        view=None if declined else stated_view(verdict, confidence),
        view_declined="the window is too stale to defend a verdict" if declined else None,
    )


def flat_picture(
    *,
    day_pnl: str | None = "0.0",
    tracked: str | None = None,
    committed: str | None = None,
    halted: int = 0,
    action: str | None = None,
) -> AdvisorWindowFacts:
    """Build one window snapshot with the position fields given."""
    return AdvisorWindowFacts(
        record_count=5,
        tracked_symbol=tracked,
        committed_usdc=committed,
        equity_usdc="100.0",
        day_start_equity_usdc="100.0",
        day_pnl_usdc=day_pnl,
        halted_count=halted,
        latest_action=action,
    )


def claude_views(
    *episodes_views: AdvisorBrief | None,
) -> tuple[TeacherSeatOutcome, ...]:
    """Build the claude seat's outcome per episode, briefs in order."""
    return tuple(
        TeacherSeatOutcome(
            seat=TeacherSeatName.CLAUDE,
            model="GLM 5.3",
            outcome="brief" if brief is not None else "timeout",
            brief=brief,
        )
        for brief in episodes_views
    )


def desk_view_score(report: HindsightReport, desk: str = "claude") -> HindsightViewScore:
    """Find one desk's view score in a report."""
    return desk_by_name(report, desk).views


class TestViewGrading:
    """The conviction layer: stated views graded against realized outcomes."""

    def test_hold_is_right_over_a_drained_quiet_horizon(self) -> None:
        """A stay that stayed safe through a filled horizon grades right."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.HOLD))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.stated == 1
        assert views.right == 1
        assert views.wrong == 0
        assert views.ungradeable == 0
        assert views.pending == 0

    def test_hold_is_wrong_when_bad_follows_while_held(self) -> None:
        """A stay that ate a realized bad outcome grades wrong."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.HOLD))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.wrong == 1
        assert views.right == 0

    def test_hold_is_ungradeable_when_the_position_leaves(self) -> None:
        """A stay the policy overrode is never graded from what followed."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.HOLD))),
            episode(
                T0 + timedelta(hours=2),
                facts=flat_picture(day_pnl="0.5"),
            ),
        )
        views = desk_view_score(scored(*episodes))
        assert views.ungradeable == 1
        assert views.right == 0
        assert views.wrong == 0

    def test_hold_stays_pending_over_an_open_horizon(self) -> None:
        """A quiet stay inside an unfilled horizon is never guessed."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.HOLD))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(score_corpus(episodes, HORIZON, now=T0 + timedelta(hours=3)))
        assert views.pending == 1
        assert views.right == 0

    def test_exit_is_right_when_bad_follows_while_held(self) -> None:
        """A leave claim vindicated by realized trouble grades right."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.EXIT))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.right == 1

    def test_exit_is_wrong_over_a_drained_quiet_horizon(self) -> None:
        """A leave claim the quiet horizon contradicts grades wrong."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.EXIT))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.wrong == 1

    def test_recenter_is_right_when_a_recenter_action_follows(self) -> None:
        """A maintenance claim the policy's own recenter vindicates grades right."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.RECENTER))),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(day_pnl="0.5", action="recenter"),
            ),
        )
        views = desk_view_score(scored(*episodes))
        assert views.right == 1

    def test_recenter_is_wrong_over_a_quiet_horizon_with_no_recenter(self) -> None:
        """A maintenance claim nothing acted on through a quiet horizon grades wrong."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.RECENTER))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.wrong == 1

    def test_recenter_is_ungradeable_when_bad_follows_with_no_recenter(self) -> None:
        """Trouble without maintenance cannot be attributed to the range."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.RECENTER))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.ungradeable == 1
        assert views.wrong == 0

    def test_enter_is_right_when_an_entry_follows_and_nothing_bad_fell(self) -> None:
        """A commit claim vindicated by a quiet realized entry grades right."""
        episodes = (
            episode(
                T0,
                facts=flat_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.ENTER)),
            ),
            episode(
                T0 + timedelta(hours=2),
                facts=flat_picture(tracked="SPCXc", committed="80.0"),
            ),
        )
        views = desk_view_score(scored(*episodes))
        assert views.right == 1

    def test_enter_is_wrong_when_bad_falls_inside_the_window(self) -> None:
        """A commit claim into a bleeding book grades wrong."""
        episodes = (
            episode(
                T0,
                facts=flat_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.ENTER)),
            ),
            episode(T0 + timedelta(hours=2), facts=flat_picture(day_pnl="-0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.wrong == 1

    def test_enter_is_ungradeable_over_a_quiet_flat_horizon(self) -> None:
        """A missed opportunity the store cannot price grades ungradeable."""
        episodes = (
            episode(
                T0,
                facts=flat_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.ENTER)),
            ),
            episode(T0 + timedelta(hours=2), facts=flat_picture(day_pnl="0.0")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.ungradeable == 1

    def test_enter_is_right_only_once_the_horizon_drains(self) -> None:
        """A realized entry inside an open horizon stays pending."""
        episodes = (
            episode(
                T0,
                facts=flat_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.ENTER)),
            ),
            episode(
                T0 + timedelta(hours=2),
                facts=flat_picture(tracked="SPCXc", committed="80.0"),
            ),
        )
        views = desk_view_score(score_corpus(episodes, HORIZON, now=T0 + timedelta(hours=3)))
        assert views.pending == 1
        assert views.right == 0


class TestViewHonesty:
    """Declines, gaps, and incoherent verdicts are counted, never graded."""

    def test_an_explicit_decline_is_counted_never_graded(self) -> None:
        """A desk that cannot form a view says so and is not scored."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(declined=True))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.declined == 1
        assert views.stated == 0
        assert views.right == 0 and views.wrong == 0 and views.pending == 0

    def test_a_positioned_episode_without_a_view_counts_missing(self) -> None:
        """An answered brief over a tracked position with no view is a gap."""
        episodes = (
            episode(T0, seats=claude_views(calm_brief())),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.missing == 1
        assert views.stated == 0

    def test_a_flat_episode_without_a_view_counts_nothing(self) -> None:
        """The view is optional while no position is tracked."""
        episodes = (
            episode(T0, facts=flat_picture(), seats=claude_views(calm_brief())),
            episode(T0 + timedelta(hours=2), facts=flat_picture()),
        )
        views = desk_view_score(scored(*episodes))
        assert views.missing == 0
        assert views.stated == 0
        assert views.declined == 0

    def test_an_enter_verdict_while_tracked_is_incoherent(self) -> None:
        """A verdict contradicting the episode's own facts is counted."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.ENTER))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.incoherent == 1
        assert views.stated == 0

    def test_a_hold_verdict_while_flat_is_incoherent(self) -> None:
        """The flat-book mirror of the same incoherence."""
        episodes = (
            episode(
                T0,
                facts=flat_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.HOLD)),
            ),
            episode(T0 + timedelta(hours=2), facts=flat_picture()),
        )
        views = desk_view_score(scored(*episodes))
        assert views.incoherent == 1

    def test_news_episodes_never_score_views(self) -> None:
        """The outside-world stream carries no deterministic view truth."""
        episodes = (
            episode(
                T0,
                stream=TeacherStream.NEWS,
                seats=claude_views(viewed_brief(PositionVerdict.HOLD)),
            ),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.stated == 0
        assert views.missing == 0

    def test_absent_seats_never_score_views(self) -> None:
        """A typed absence gave no view and is never a gap."""
        episodes = (
            episode(T0, seats=claude_views(None)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.missing == 0
        assert views.stated == 0

    def test_the_student_view_rides_the_audit_payload(self) -> None:
        """The student desk's stated view projects from its audited answer."""
        payload = AdvisorReportedAuditPayload(
            outcome="brief",
            model="qwen3.6:35b-a3b",
            brief="the student read",
            view=stated_view(PositionVerdict.EXIT, ViewConfidence.LOW),
        )
        episodes = (
            episode(T0, student=payload),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        views = desk_view_score(scored(*episodes), desk="student")
        assert views.stated == 1
        assert views.right == 1


class TestViewCalibration:
    """The confidence bands: high must be right more often than low."""

    @staticmethod
    def _band_corpus(
        *,
        early_bad: bool,
        late_bad: bool,
        early_confidence: ViewConfidence,
        late_confidence: ViewConfidence,
        early_count: int = 2,
        late_count: int = 2,
    ) -> tuple[TeacherEpisode, ...]:
        """Build two view families whose windows cannot pollute each other.

        The early family's windows close before the late family's
        episodes begin, so each family's truth stays its own; the early
        quiet windows drain under the fixed clock while the late bad
        windows decide the moment their bad follower lands.
        """
        episodes: list[TeacherEpisode] = []
        for index in range(early_count):
            at = T0 + timedelta(hours=4 * index)
            episodes.append(
                episode(
                    at, seats=claude_views(viewed_brief(PositionVerdict.EXIT, early_confidence))
                )
            )
            episodes.append(
                episode(
                    at + timedelta(hours=2),
                    facts=facts_picture(day_pnl="-0.5" if early_bad else "0.5"),
                )
            )
        for index in range(late_count):
            at = T0 + timedelta(hours=30 + 4 * index)
            episodes.append(
                episode(at, seats=claude_views(viewed_brief(PositionVerdict.EXIT, late_confidence)))
            )
            episodes.append(
                episode(
                    at + timedelta(hours=2),
                    facts=facts_picture(day_pnl="-0.5" if late_bad else "0.5"),
                )
            )
        return tuple(episodes)

    def test_high_right_more_often_than_low_is_calibrated(self) -> None:
        """Right highs against wrong lows calibrate the bands."""
        episodes = self._band_corpus(
            early_bad=False,
            late_bad=True,
            early_confidence=ViewConfidence.LOW,
            late_confidence=ViewConfidence.HIGH,
        )
        views = desk_view_score(score_corpus(episodes, HORIZON, now=T0 + timedelta(hours=48)))
        assert views.calibration == "calibrated"
        bands = {band.band: band for band in views.bands}
        assert bands["high"].right == 2
        assert bands["low"].wrong == 2

    def test_low_right_more_often_than_high_is_miscalibrated(self) -> None:
        """The inverted ordering fails the calibration requirement."""
        episodes = self._band_corpus(
            early_bad=False,
            late_bad=True,
            early_confidence=ViewConfidence.HIGH,
            late_confidence=ViewConfidence.LOW,
        )
        views = desk_view_score(score_corpus(episodes, HORIZON, now=T0 + timedelta(hours=48)))
        assert views.calibration == "miscalibrated"

    def test_one_decided_band_is_insufficient(self) -> None:
        """Calibration needs decided views in both extreme bands."""
        episodes = self._band_corpus(
            early_bad=False,
            late_bad=True,
            early_confidence=ViewConfidence.HIGH,
            late_confidence=ViewConfidence.HIGH,
            early_count=0,
            late_count=1,
        )
        views = desk_view_score(score_corpus(episodes, HORIZON, now=T0 + timedelta(hours=48)))
        assert views.calibration == "insufficient"


class TestViewCounterfactual:
    """The view-versus-policy comparison, computed only where priced."""

    def _exit_window(
        self,
        *,
        committed: str,
        later_committed: str | None,
        later_pnl: str = "0.5",
    ) -> tuple[TeacherEpisode, ...]:
        """Build one exit view over a tracked mark path."""
        return (
            episode(
                T0,
                facts=facts_picture(committed=committed),
                seats=claude_views(viewed_brief(PositionVerdict.EXIT)),
            ),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(
                    committed=later_committed,
                    day_pnl=later_pnl,
                ),
            ),
        )

    def test_a_declined_exit_over_a_falling_mark_would_have_beaten_the_stay(self) -> None:
        """The one computable family: the committed-mark path prices it."""
        episodes = self._exit_window(committed="80.0", later_committed="70.0")
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.view_better == 1
        assert views.counterfactuals.policy_better == 0

    def test_a_declined_exit_over_a_rising_mark_loses_to_the_stay(self) -> None:
        """The rising mark rewards the policy's actual choice."""
        episodes = self._exit_window(committed="70.0", later_committed="80.0")
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.policy_better == 1

    def test_a_flat_mark_delta_is_equal(self) -> None:
        """An unchanged committed mark prices no difference."""
        episodes = self._exit_window(committed="75.0", later_committed="75.0")
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.equal == 1

    def test_missing_mark_readings_are_uncomputable(self) -> None:
        """A path the store never observed is never fabricated."""
        episodes = (
            episode(
                T0,
                facts=facts_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.EXIT)),
            ),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(day_pnl="0.5"),
            ),
        )
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.uncomputable == 1

    def test_an_exit_window_that_has_not_settled_stays_pending(self) -> None:
        """The comparison waits for the window like the grade does."""
        episodes = self._exit_window(committed="80.0", later_committed="70.0")
        views = desk_view_score(score_corpus(episodes, HORIZON, now=T0 + timedelta(hours=3)))
        assert views.counterfactuals.pending == 1
        assert views.counterfactuals.view_better == 0

    def test_a_hold_the_policy_honored_is_equal(self) -> None:
        """Acting on a hold the policy took is the identical path."""
        episodes = (
            episode(
                T0,
                facts=facts_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.HOLD)),
            ),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.equal == 1

    def test_a_hold_overridden_by_an_exit_is_uncomputable(self) -> None:
        """The corpus never observed the kept-position path."""
        episodes = (
            episode(
                T0,
                facts=facts_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.HOLD)),
            ),
            episode(T0 + timedelta(hours=2), facts=flat_picture(day_pnl="0.5")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.uncomputable == 1

    def test_a_recenter_the_policy_performed_is_equal(self) -> None:
        """A matching maintenance action prices as identical."""
        episodes = (
            episode(
                T0,
                facts=facts_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.RECENTER)),
            ),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(day_pnl="0.5", action="recenter"),
            ),
        )
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.equal == 1

    def test_an_enter_the_policy_declined_is_uncomputable(self) -> None:
        """The committed path the view would have taken is unobserved."""
        episodes = (
            episode(
                T0,
                facts=flat_picture(),
                seats=claude_views(viewed_brief(PositionVerdict.ENTER)),
            ),
            episode(T0 + timedelta(hours=2), facts=flat_picture(day_pnl="0.0")),
        )
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.uncomputable == 1

    def test_the_exit_mark_window_ends_at_the_position_leave(self) -> None:
        """A late switch prices the held-too-long path, not what followed."""
        episodes = (
            episode(
                T0,
                facts=facts_picture(committed="80.0"),
                seats=claude_views(viewed_brief(PositionVerdict.EXIT)),
            ),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(committed="60.0"),
            ),
            episode(
                T0 + timedelta(hours=4),
                facts=flat_picture(tracked="MSTRc", committed="90.0", day_pnl="5.0"),
            ),
        )
        views = desk_view_score(scored(*episodes))
        assert views.counterfactuals.view_better == 1


class TestViewBackwardCompatibility:
    """Older corpora and reports stay valid beside the conviction layer."""

    def test_view_free_corpora_parse_and_measure_their_gaps(self) -> None:
        """A corpus recorded before the layer parses and measures honestly.

        Old briefs over a tracked position count as view gaps - the
        honest measurement, never a silently ignored field - while the
        pre-existing calibration axis is untouched.
        """
        episodes = (
            episode(T0, seats=(claude_outcome(),)),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        report = scored(*episodes)
        claude = desk_by_name(report, "claude")
        assert claude.views.stated == 0
        assert claude.views.missing == 1
        assert claude.calibration.unflagged_bad == 1

    def test_view_free_reports_parse_with_defaulted_view_fields(self) -> None:
        """A report written before the layer validates with its defaults."""
        legacy = {
            "schema_version": HINDSIGHT_REPORT_SCHEMA,
            "created_at": T0.isoformat(),
            "horizon_hours": HORIZON,
            "episode_count": 1,
            "grounded_episode_count": 1,
            "desks": [],
        }
        report = HindsightReport.model_validate(legacy)
        assert report.view_rule == HINDSIGHT_VIEW_RULE
        assert report.truth_rule == HINDSIGHT_TRUTH_RULE

    def test_corpus_episodes_without_views_round_trip(self) -> None:
        """Old corpus lines revalidate after the schema extension."""
        first = episode(T0, seats=(claude_outcome(),))
        document = json.loads(first.model_dump_json())
        assert "view" in json.loads(json.dumps(document))["seats"][0]["brief"]
        revalidated = TeacherEpisode.model_validate(document)
        assert revalidated.seats[0].brief is not None
        assert revalidated.seats[0].brief.view is None


class TestViewReportShape:
    """The scored report carries the view axis beside the calibration."""

    def test_the_report_pins_its_view_rule(self) -> None:
        """Every scored report explains its own view grading rule."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.HOLD))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        report = scored(*episodes)
        assert report.view_rule == HINDSIGHT_VIEW_RULE

    def test_every_desk_carries_a_view_score(self) -> None:
        """Teacher seats and the student all carry the view axis."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.HOLD))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        report = scored(*episodes)
        assert all(desk.views is not None for desk in report.desks)
        assert desk_by_name(report, "claude").views.right == 1

    def test_the_grades_export_joins_per_episode_by_desk(self) -> None:
        """The digest-facing export keys entries by episode identity."""
        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.EXIT))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="-0.5")),
        )
        grades = grade_corpus_views(episodes, HORIZON, now=T0 + timedelta(hours=3))
        assert isinstance(grades, ViewGrades)
        by_desk = grades.by_episode(episodes[0])
        entry = by_desk["claude"]
        assert entry.state is ViewState.STATED
        # The bad follower decides the grade immediately while the
        # comparison waits for the window to settle.
        assert entry.grade is ViewGrade.RIGHT
        assert entry.counterfactual is ViewCounterfactual.PENDING

    def test_the_human_summary_prints_the_view_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The printed summary names the view axis and its bands."""
        from aero_bot.hindsight import print_report

        episodes = (
            episode(T0, seats=claude_views(viewed_brief(PositionVerdict.HOLD))),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        print_report(scored(*episodes))
        summary = capsys.readouterr().out
        assert "views: 1 stated" in summary
        assert "bands" in summary
        assert "counterfactuals" in summary
        assert "view rule:" in summary


class TestCliContract:
    """The command's flags and resolution rules are pinned."""

    def test_the_default_horizon_is_one_day(self, tmp_path: Path) -> None:
        """The default horizon is the pinned trading day."""
        assert DEFAULT_HINDSIGHT_HORIZON_HOURS == 24.0

    def test_horizon_bounds_are_enforced(self, tmp_path: Path) -> None:
        """Out-of-range horizons are usage errors."""
        with pytest.raises(SystemExit) as raised:
            hindsight_main(["--corpus-dir", str(tmp_path), "--horizon-hours", "0.5"])
        assert raised.value.code == 2
        with pytest.raises(SystemExit) as raised:
            hindsight_main(["--corpus-dir", str(tmp_path), "--horizon-hours", "200"])
        assert raised.value.code == 2

    def test_the_corpus_dir_resolves_the_shared_environment_override(self) -> None:
        """The teacher's corpus override governs the scorer too."""
        override = str(Path.home() / "state-override" / "teacher")
        assert resolve_corpus_dir({TEACHER_CORPUS_DIR_ENV: override}) == Path(override)
        assert resolve_corpus_dir({}) == DEFAULT_TEACHER_CORPUS_DIR

    def test_the_environment_override_feeds_the_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AERO_BOT_TEACHER_CORPUS_DIR reaches the CLI without flags."""
        corpus = tmp_path / "env-corpus"
        corpus.mkdir()
        monkeypatch.setenv(TEACHER_CORPUS_DIR_ENV, str(corpus))
        with (corpus / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(episode(T0, seats=(claude_outcome(),)).model_dump_json() + "\n")
        assert hindsight_main([]) == 0
        assert (corpus / "reports" / HINDSIGHT_REPORT_NAME).exists()

    def test_corpus_lines_stay_the_teacher_schema(self) -> None:
        """The episodes this suite writes validate as the corpus schema."""
        first = episode(T0, seats=(claude_outcome(),))
        assert first.schema_version == TEACHER_EPISODE_SCHEMA
        assert TeacherEpisode.model_validate(json.loads(first.model_dump_json()))
