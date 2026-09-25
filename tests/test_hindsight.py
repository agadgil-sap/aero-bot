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
)
from aero_bot.hindsight import (
    DEFAULT_HINDSIGHT_HORIZON_HOURS,
    HINDSIGHT_REPORT_NAME,
    HINDSIGHT_REPORT_SCHEMA,
    HINDSIGHT_SERIES_MAX,
    HINDSIGHT_TRUTH_RULE,
    HindsightCalibration,
    HindsightDeskScore,
    HindsightReport,
    collect_reads,
    episode_verdicts,
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
) -> AdvisorWindowFacts:
    """Build one grounded window snapshot with deterministic economics."""
    return AdvisorWindowFacts(
        record_count=5,
        tracked_symbol="SPCXc",
        equity_usdc=equity,
        day_start_equity_usdc=day_start,
        day_pnl_usdc=day_pnl,
        halted_count=halted,
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
