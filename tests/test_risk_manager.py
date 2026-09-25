"""Pin the risk manager's posture audit, contradiction checks, and CLI."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aero_bot.advisor import (
    AdvisorAnomaly,
    AdvisorAnomalyAuditPayload,
    AdvisorBrief,
    AdvisorReportedAuditPayload,
    AdvisorWindowFacts,
)
from aero_bot.risk_manager import (
    DAILY_LOSS_HALT_FRACTION,
    RISK_FINDING_MAX,
    RISK_MANAGER_REPORT_NAME,
    RISK_MANAGER_REPORT_SCHEMA,
    RISK_POSTURE_RULES,
    PostureAudit,
    RiskFindingKind,
    audit_corpus_posture,
    compose_report,
    main,
    write_report,
)
from aero_bot.teacher import (
    TEACHER_CORPUS_DIR_ENV,
    StudentWindowBrief,
    TeacherEpisode,
    TeacherSeatName,
    TeacherSeatOutcome,
    TeacherStream,
)

# One fixed time base inside the New York trading day 2026-09-23.
T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)


def facts_picture(
    *,
    equity: str | None = "100.0",
    day_start: str | None = "100.0",
    day_pnl: str | None = None,
    committed: str | None = None,
    action: str | None = None,
    halted: int = 0,
) -> AdvisorWindowFacts:
    """Build one grounded window snapshot with the given posture fields."""
    return AdvisorWindowFacts(
        record_count=5,
        tracked_symbol="SPCXc" if committed is not None else None,
        committed_usdc=committed,
        equity_usdc=equity,
        day_start_equity_usdc=day_start,
        day_pnl_usdc=day_pnl,
        halted_count=halted,
        latest_action=action,
    )


def brief_with(labels: tuple[str, ...]) -> AdvisorBrief:
    """Build one accepted answer carrying the given anomaly labels."""
    return AdvisorBrief(
        brief="a window reading worth auditing",
        anomalies=tuple(
            AdvisorAnomaly(label=label, confidence=0.8, rationale="deterministic")
            for label in labels
        ),
    )


def student_payload(*, brief: AdvisorBrief | None) -> AdvisorReportedAuditPayload:
    """Build one audited student answer, absent when the brief is None."""
    return AdvisorReportedAuditPayload(
        outcome="brief" if brief is not None else "malformed_json",
        model="qwen3.6:35b-a3b",
        brief=brief.brief if brief is not None else "",
        anomalies=tuple(
            AdvisorAnomalyAuditPayload(
                label=anomaly.label, confidence=anomaly.confidence, rationale=anomaly.rationale
            )
            for anomaly in (brief.anomalies if brief is not None else ())
        ),
        latency_ms=1000,
        window_records=5,
    )


def episode(
    at: datetime,
    *,
    stream: TeacherStream = TeacherStream.TACTICAL,
    facts: AdvisorWindowFacts | None = None,
    seats: tuple[TeacherSeatOutcome, ...] = (),
    student: AdvisorBrief | None = None,
    student_absent: bool = False,
) -> TeacherEpisode:
    """Build one corpus episode with deterministic shape."""
    resolved = facts if facts is not None else facts_picture()
    payload = None if student_absent else student_payload(brief=student)
    return TeacherEpisode(
        stream=stream,
        created_at=at,
        window_outcome="pulled",
        facts=resolved,
        student=(
            StudentWindowBrief(created_at=at - timedelta(minutes=30), payload=payload)
            if payload is not None
            else None
        ),
        seats=seats,
    )


def audit(*episodes: TeacherEpisode) -> PostureAudit:
    """Audit fixed episodes under the pure posture function."""
    return audit_corpus_posture(episodes)


def kinds(*episodes: TeacherEpisode) -> tuple[str, ...]:
    """List every finding kind the audit recorded, in order."""
    return tuple(finding.kind for finding in audit(*episodes).findings)


def quiet_seat(seat: TeacherSeatName) -> TeacherSeatOutcome:
    """Build one seat outcome that answered quietly."""
    return TeacherSeatOutcome(seat=seat, model="seat-model", outcome="brief", brief=brief_with(()))


def flagged_seat(seat: TeacherSeatName) -> TeacherSeatOutcome:
    """Build one seat outcome that raised a label."""
    return TeacherSeatOutcome(
        seat=seat, model="seat-model", outcome="brief", brief=brief_with(("halt-count rise",))
    )


class TestCapitalPostureArithmetic:
    """The day P&L must compose exactly from the equity pair."""

    def test_a_coherent_posture_yields_no_findings(self) -> None:
        """Equity minus day-start equaling the day P&L is quiet."""
        result = audit(episode(T0, facts=facts_picture(equity="101.0", day_pnl="1.0")))
        assert result.findings == ()
        assert result.contradictions == ()
        assert result.grounded_episode_count == 1

    def test_a_contradicted_day_pnl_is_flagged(self) -> None:
        """A day P&L that disagrees with the equity pair is a finding."""
        result = audit(episode(T0, facts=facts_picture(equity="100.0", day_pnl="-0.5")))
        assert [finding.kind for finding in result.findings] == [
            RiskFindingKind.DAY_PNL_CONTRADICTION
        ]
        assert "minus day-start" in result.findings[0].detail
        assert result.findings[0].equity_usdc == "100.0"

    def test_the_tolerance_absorbs_serialization_noise_only(self) -> None:
        """One micro-USDC of drift is quiet; more is a contradiction."""
        assert (
            audit(episode(T0, facts=facts_picture(equity="100.000001", day_pnl="0.0"))).findings
            == ()
        )
        assert (
            len(
                audit(episode(T0, facts=facts_picture(equity="100.000002", day_pnl="0.0"))).findings
            )
            == 1
        )

    def test_missing_or_partial_fields_are_absences_never_guesses(self) -> None:
        """Any absent posture field skips its check without a finding."""
        assert audit(episode(T0, facts=facts_picture(equity=None, day_pnl=None))).findings == ()
        assert audit(episode(T0, facts=facts_picture(day_start=None, day_pnl="5.0"))).findings == ()

    def test_malformed_and_nonfinite_readings_are_absences(self) -> None:
        """NaN, infinities, and garbage strings never fabricate findings."""
        for equity in ("NaN", "Infinity", "-Infinity", "not-a-number"):
            result = audit(episode(T0, facts=facts_picture(equity=equity, day_pnl=equity)))
            assert result.findings == (), equity


class TestHaltState:
    """The five-percent halt line and its entry discipline are pinned."""

    def test_a_five_percent_marked_drawdown_crosses_the_line(self) -> None:
        """Exactly five percent latches, matching the policy's fraction."""
        breached = audit(episode(T0, facts=facts_picture(equity="95.0")))
        assert [finding.kind for finding in breached.findings] == [
            RiskFindingKind.HALT_THRESHOLD_BREACHED
        ]
        assert "five-percent" in breached.findings[0].detail

    def test_just_inside_the_line_stays_quiet(self) -> None:
        """A 4.99-percent drawdown has not crossed the latch."""
        assert audit(episode(T0, facts=facts_picture(equity="95.01"))).findings == ()

    def test_a_positive_day_never_reads_as_a_drawdown(self) -> None:
        """Equity above the anchor cannot cross a halt line."""
        assert (
            audit(episode(T0, facts=facts_picture(equity="110.0", day_pnl="10.0"))).findings == ()
        )

    def test_a_nonpositive_anchor_cannot_fraction(self) -> None:
        """A zero or negative day-start anchor is an absence, not a crash."""
        assert audit(episode(T0, facts=facts_picture(day_start="0", day_pnl=None))).findings == ()

    def test_an_entry_after_the_breach_on_the_same_anchor_violates_discipline(self) -> None:
        """The halt blocks new entries for the rest of the anchor day."""
        result = audit(
            episode(T0, facts=facts_picture(equity="94.0", day_pnl="-6.0")),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(equity="94.5", day_pnl="-5.5", action="enter"),
            ),
        )
        discipline = [
            f for f in result.findings if f.kind == RiskFindingKind.HALT_DISCIPLINE_VIOLATION
        ]
        assert len(discipline) == 1
        assert "blocks new entries" in discipline[0].detail

    def test_a_recenter_is_maintenance_not_an_entry(self) -> None:
        """Recenters stay armed while the halt is latched."""
        result = audit(
            episode(T0, facts=facts_picture(equity="94.0", day_pnl="-6.0")),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(equity="94.5", day_pnl="-5.5", action="recenter"),
            ),
        )
        assert not [
            f for f in result.findings if f.kind == RiskFindingKind.HALT_DISCIPLINE_VIOLATION
        ]

    def test_an_entry_on_the_next_anchor_day_is_allowed(self) -> None:
        """A new day-start anchor is a new halt window."""
        next_day = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
        result = audit(
            episode(T0, facts=facts_picture(equity="94.0", day_pnl="-6.0")),
            episode(
                next_day,
                facts=facts_picture(equity="94.5", day_pnl="-5.5", action="enter"),
            ),
        )
        assert not [
            f for f in result.findings if f.kind == RiskFindingKind.HALT_DISCIPLINE_VIOLATION
        ]

    def test_a_pool_switch_is_an_entry_kind(self) -> None:
        """Switching pools commits new capital and honors the halt."""
        result = audit(
            episode(T0, facts=facts_picture(equity="94.0", day_pnl="-6.0")),
            episode(
                T0 + timedelta(hours=2),
                facts=facts_picture(equity="94.5", day_pnl="-5.5", action="pool_switch"),
            ),
        )
        assert [f for f in result.findings if f.kind == RiskFindingKind.HALT_DISCIPLINE_VIOLATION]


class TestExposureVersusCaps:
    """The hard ceiling and the sizing fraction read on their own terms."""

    def test_committed_exposure_above_one_hundred_breaches_the_hard_cap(self) -> None:
        """The 100 USDC ceiling refuses anything above it."""
        result = audit(episode(T0, facts=facts_picture(committed="100.000001")))
        assert [finding.kind for finding in result.findings] == [RiskFindingKind.HARD_CAP_BREACH]
        assert "hard exposure ceiling" in result.findings[0].detail

    def test_exactly_one_hundred_committed_is_within_the_ceiling(self) -> None:
        """The cap reads above, not at, one hundred."""
        assert audit(episode(T0, facts=facts_picture(committed="100.0"))).findings == ()

    def test_an_entry_above_eighty_percent_of_equity_breaches_sizing(self) -> None:
        """The locked sizing fraction binds at the entry moment."""
        result = audit(
            episode(
                T0,
                facts=facts_picture(equity="100.0", committed="80.01", action="enter"),
            )
        )
        assert [finding.kind for finding in result.findings] == [RiskFindingKind.SIZING_CAP_BREACH]

    def test_exactly_eighty_percent_at_entry_is_within_the_fraction(self) -> None:
        """The tolerance absorbs the grid's rounding only."""
        assert (
            audit(
                episode(
                    T0,
                    facts=facts_picture(equity="100.0", committed="80.0", action="enter"),
                )
            ).findings
            == ()
        )

    def test_exposure_without_an_entry_kind_is_not_a_sizing_breach(self) -> None:
        """A drifted open position is judged by the hard cap alone."""
        assert (
            audit(
                episode(T0, facts=facts_picture(equity="100.0", committed="45.0", action="hold"))
            ).findings
            == ()
        )


class TestContradictionChecks:
    """A quiet brief over a flagged posture is a recorded contradiction."""

    def test_a_quiet_student_over_a_flagged_posture_contradicts(self) -> None:
        """The counterparty desk catches what the student read as quiet."""
        result = audit(
            episode(
                T0,
                facts=facts_picture(equity="94.0", day_pnl="-6.0"),
                student=brief_with(()),
            )
        )
        assert result.findings
        assert [(entry.desk, entry.finding_kinds) for entry in result.contradictions] == [
            ("student", (RiskFindingKind.HALT_THRESHOLD_BREACHED,))
        ]

    def test_a_flagged_student_over_a_flagged_posture_is_not_a_contradiction(self) -> None:
        """A desk that raised any label gave a verdict, quiet or not."""
        result = audit(
            episode(
                T0,
                facts=facts_picture(equity="94.0", day_pnl="-6.0"),
                student=brief_with(("drawdown",)),
            )
        )
        assert result.contradictions == ()

    def test_an_absent_student_never_contradicts(self) -> None:
        """An absent desk gave no verdict to contradict."""
        result = audit(
            episode(
                T0,
                facts=facts_picture(equity="94.0", day_pnl="-6.0"),
                student_absent=True,
            )
        )
        assert result.findings
        assert result.contradictions == ()

    def test_every_quiet_teacher_seat_contradicts_independently(self) -> None:
        """The check names each desk that stayed quiet."""
        result = audit(
            episode(
                T0,
                facts=facts_picture(equity="94.0", day_pnl="-6.0"),
                student=brief_with(()),
                seats=(quiet_seat(TeacherSeatName.CLAUDE), flagged_seat(TeacherSeatName.CODEX)),
            )
        )
        assert [entry.desk for entry in result.contradictions] == ["student", "claude"]
        assert result.contradictions[1].model == "seat-model"

    def test_a_quiet_brief_over_a_clean_posture_is_not_a_contradiction(self) -> None:
        """Quiet days stay quiet; only flagged postures contradict."""
        result = audit(episode(T0, facts=facts_picture(), student=brief_with(())))
        assert result.contradictions == ()

    def test_the_injected_posture_contradiction_the_student_misses_is_caught(self) -> None:
        """The separation-of-duties pin: every desk quiet, the audit still flags."""
        flagged = episode(
            T0,
            facts=facts_picture(equity="90.0", day_pnl="-10.0"),
            student=brief_with(()),
            seats=(quiet_seat(TeacherSeatName.CLAUDE), quiet_seat(TeacherSeatName.CODEX)),
        )
        result = audit(flagged)
        # The finding exists, is named, and contradicts every quiet desk.
        assert [finding.kind for finding in result.findings] == [
            RiskFindingKind.HALT_THRESHOLD_BREACHED
        ]
        assert [entry.desk for entry in result.contradictions] == [
            "student",
            "claude",
            "codex",
        ]
        # The per-episode join the upgrade digest reads resolves by episode.
        assert result.kinds_for(flagged) == (RiskFindingKind.HALT_THRESHOLD_BREACHED,)
        assert result.kinds_for(episode(T0)) == ()


class TestAuditChronology:
    """The audit replays the timeline, not the file order."""

    def test_out_of_order_appends_still_audit_chronologically(self) -> None:
        """A later-appended earlier episode precedes its follower."""
        early = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
        later = datetime(2026, 9, 23, 22, 0, tzinfo=UTC)
        result = audit(
            episode(later, facts=facts_picture(equity="94.5", day_pnl="-5.5", action="enter")),
            episode(early, facts=facts_picture(equity="94.0", day_pnl="-6.0")),
        )
        assert [f for f in result.findings if f.kind == RiskFindingKind.HALT_DISCIPLINE_VIOLATION]

    def test_ungrounded_episodes_are_skipped_entirely(self) -> None:
        """A failed pull carries no facts to audit."""
        ungrounded = TeacherEpisode(
            stream=TeacherStream.TACTICAL,
            created_at=T0,
            window_outcome="window_unreachable",
        )
        assert audit(ungrounded).grounded_episode_count == 0

    def test_news_stream_episodes_are_audited_too(self) -> None:
        """The posture does not care which desk question was asked."""
        result = audit(
            episode(
                T0,
                stream=TeacherStream.NEWS,
                facts=facts_picture(equity="94.0", day_pnl="-6.0"),
            )
        )
        assert [finding.kind for finding in result.findings] == [
            RiskFindingKind.HALT_THRESHOLD_BREACHED
        ]


class TestReportShape:
    """The frozen report explains itself a month later."""

    def test_the_report_carries_the_schema_rules_and_counts(self) -> None:
        """Schema tag, posture rules, and per-kind counts ride inside."""
        episodes = (
            episode(T0, facts=facts_picture(equity="94.0", day_pnl="-6.0"), student=brief_with(())),
            episode(
                T0 + timedelta(hours=1),
                facts=facts_picture(equity="100.000002", day_pnl="0.0"),
            ),
        )
        result = audit(*episodes)
        report = compose_report(result, len(episodes), T0 + timedelta(hours=2))
        assert report.schema_version == RISK_MANAGER_REPORT_SCHEMA
        assert report.posture_rules == RISK_POSTURE_RULES
        assert report.episode_count == 2
        assert report.grounded_episode_count == 2
        assert [(counted.reason, counted.count) for counted in report.finding_counts] == [
            (RiskFindingKind.DAY_PNL_CONTRADICTION, 1),
            (RiskFindingKind.HALT_THRESHOLD_BREACHED, 1),
        ]
        assert [(counted.reason, counted.count) for counted in report.contradiction_counts] == [
            ("student", 1)
        ]

    def test_entries_are_bounded_to_the_most_recent_twenty(self) -> None:
        """Counts carry the whole truth; entries stay bounded."""
        episodes = tuple(
            episode(
                T0 + timedelta(minutes=30 * index),
                facts=facts_picture(equity="94.0", day_pnl="-6.0"),
                student=brief_with(()),
            )
            for index in range(RISK_FINDING_MAX + 5)
        )
        report = compose_report(audit(*episodes), len(episodes), T0 + timedelta(hours=12))
        assert len(report.findings) == RISK_FINDING_MAX
        assert len(report.contradictions) == RISK_FINDING_MAX
        assert report.finding_counts[0].count == RISK_FINDING_MAX + 5
        assert report.findings[-1].created_at == episodes[-1].created_at

    def test_brief_and_detail_snippets_are_collapsed_and_bounded(self) -> None:
        """Multi-line prose never escapes its bounds."""
        noisy = AdvisorBrief(
            brief="line one\nline two " + "x" * 900,
            anomalies=(),
        )
        report = compose_report(
            audit(episode(T0, facts=facts_picture(equity="94.0", day_pnl="-6.0"), student=noisy)),
            1,
            T0,
        )
        assert "\n" not in report.contradictions[0].brief
        assert len(report.contradictions[0].brief) <= 600

    def test_a_clean_corpus_reports_zero_everywhere(self) -> None:
        """The honest empty audit is zero, not absent."""
        report = compose_report(audit(episode(T0)), 1, T0)
        assert report.finding_counts == ()
        assert report.contradiction_counts == ()
        assert report.findings == ()


class TestCorpusWriting:
    """The report lands beside the corpus, atomically."""

    def test_write_report_rewrites_the_pinned_file_atomically(self, tmp_path: Path) -> None:
        """The report exists, parses, and leaves no temporary behind."""
        report = compose_report(audit(episode(T0)), 1, T0)
        path = write_report(report, tmp_path)
        assert path == tmp_path / "reports" / RISK_MANAGER_REPORT_NAME
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema_version"] == RISK_MANAGER_REPORT_SCHEMA
        assert not (tmp_path / "reports" / "risk_manager_last.json.tmp").exists()

    def test_the_cli_reads_the_corpus_and_writes_the_report(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A seeded corpus audits through the CLI end to end."""
        corpus = tmp_path / "teacher"
        corpus.mkdir()
        flagged = episode(
            T0,
            facts=facts_picture(equity="94.0", day_pnl="-6.0"),
            student=brief_with(()),
        )
        with (corpus / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(flagged.model_dump_json() + "\n")
        code = main(["--corpus-dir", str(corpus), "--json"])
        captured = capsys.readouterr()
        assert code == 0
        assert f'"schema_version": "{RISK_MANAGER_REPORT_SCHEMA}"' in captured.out
        assert (corpus / "reports" / RISK_MANAGER_REPORT_NAME).exists()

    def test_findings_do_not_fail_the_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A finding is an honest observation, not a failure exit."""
        corpus = tmp_path / "teacher"
        corpus.mkdir()
        flagged = episode(
            T0,
            facts=facts_picture(equity="94.0", day_pnl="-6.0"),
            student=brief_with(()),
            seats=(quiet_seat(TeacherSeatName.CLAUDE),),
        )
        with (corpus / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(flagged.model_dump_json() + "\n")
        assert main(["--corpus-dir", str(corpus)]) == 0
        captured = capsys.readouterr()
        assert "halt_threshold_breached" in captured.out
        assert "quiet-on-finding student: 1" in captured.out
        assert "student [qwen3.6:35b-a3b] quiet over halt_threshold_breached" in captured.out
        assert "rules:" in captured.out

    def test_a_clean_corpus_prints_the_clean_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The default summary says clean when nothing was found."""
        corpus = tmp_path / "teacher"
        corpus.mkdir()
        with (corpus / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(episode(T0).model_dump_json() + "\n")
        assert main(["--corpus-dir", str(corpus)]) == 0
        assert "posture clean" in capsys.readouterr().out

    def test_an_empty_or_missing_corpus_exits_zero(self, tmp_path: Path) -> None:
        """An honest empty audit is a clean pass."""
        assert main(["--corpus-dir", str(tmp_path / "absent")]) == 0
        assert (tmp_path / "absent" / "reports" / RISK_MANAGER_REPORT_NAME).exists()

    def test_an_unwritable_corpus_directory_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corpus directory that cannot hold reports fails honestly."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        code = main(["--corpus-dir", str(blocker)])
        captured = capsys.readouterr()
        assert code == 1
        assert "could not be written" in captured.err

    def test_an_undecodable_corpus_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Invalid UTF-8 in the corpus is a typed failure, not a traceback."""
        corpus = tmp_path / "teacher"
        corpus.mkdir()
        (corpus / "corpus.jsonl").write_bytes(b"\xff\xfe not utf-8")
        code = main(["--corpus-dir", str(corpus)])
        captured = capsys.readouterr()
        assert code == 1
        assert "could not be read" in captured.err

    def test_the_environment_override_feeds_the_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared corpus override reaches the audit without flags."""
        corpus = tmp_path / "env-corpus"
        corpus.mkdir()
        monkeypatch.setenv(TEACHER_CORPUS_DIR_ENV, str(corpus))
        with (corpus / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(episode(T0).model_dump_json() + "\n")
        assert main([]) == 0
        assert (corpus / "reports" / RISK_MANAGER_REPORT_NAME).exists()


class TestLockedConstants:
    """The audit's fractions mirror the locked policy parameters."""

    def test_the_halt_fraction_is_five_percent(self) -> None:
        """The latch fraction is the policy's locked value."""
        assert Decimal("0.05") == DAILY_LOSS_HALT_FRACTION
