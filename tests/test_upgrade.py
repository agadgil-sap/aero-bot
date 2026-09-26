"""Pin the upgrade loop's digest, proposal contract, and CLI shape."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from aero_bot.advisor import (
    ADVISOR_SYSTEM_PROMPT,
    ADVISOR_TEACHING_MAX_CHARS,
    AdvisorAnomaly,
    AdvisorAnomalyAuditPayload,
    AdvisorBrief,
    AdvisorReportedAuditPayload,
    AdvisorWindowFacts,
    PositionVerdict,
    PositionView,
    ViewConfidence,
)
from aero_bot.hindsight import episode_verdicts, grade_corpus_views
from aero_bot.risk_manager import RiskFindingKind, audit_corpus_posture
from aero_bot.teacher import (
    StudentWindowBrief,
    TeacherConfig,
    TeacherEpisode,
    TeacherProcessResult,
    TeacherSeatConfig,
    TeacherSeatName,
    TeacherSeatOutcome,
    TeacherSeatTimeoutError,
    TeacherSeatTransport,
    TeacherStream,
)
from aero_bot.upgrade import (
    UPGRADE_DIGEST_BRIEF_MAX_CHARS,
    UPGRADE_DIGEST_ENTRY_MAX,
    UPGRADE_PROPOSAL_SCHEMA,
    UPGRADE_RATIONALE_MAX_CHARS,
    SeatUpgradeOutcome,
    UpgradeDivergenceDigest,
    UpgradeProposal,
    UpgradeReport,
    ask_upgrade_seat,
    build_divergence_digest,
    build_upgrade_user_prompt,
    main,
    print_upgrade_report,
    run_upgrade_pass,
    write_upgrade_artifacts,
)

# One fixed time base; NOW sits one minute past T0's 24-hour horizon so a
# quiet verdict on T0 is final and a bad follower inside it scores.
T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 24, 20, 1, tzinfo=UTC)
HORIZON = 24.0

PROPOSAL_ANSWER: dict[str, object] = {
    "teaching_block": "Flag when the halted-cycle count rises across adjacent snapshots.",
    "rationale": "The digest shows one unflagged miss ahead of a halt increment.",
    "exemplars": ["Flag halt-count deltas early, citing the baseline count."],
}


def facts_picture(
    *,
    day_pnl: str | None = "1.0",
    halted: int = 0,
) -> AdvisorWindowFacts:
    """Build one grounded window snapshot with deterministic economics."""
    return AdvisorWindowFacts(
        record_count=5,
        tracked_symbol="SPCXc",
        equity_usdc="100.0",
        day_start_equity_usdc="99.0",
        day_pnl_usdc=day_pnl,
        halted_count=halted,
    )


def brief_with(labels: tuple[str, ...]) -> AdvisorBrief:
    """Build one accepted answer carrying the given anomaly labels."""
    return AdvisorBrief(
        brief="a window reading worth teaching from",
        anomalies=tuple(
            AdvisorAnomaly(label=label, confidence=0.8, rationale="deterministic")
            for label in labels
        ),
    )


def seat_outcome(
    seat: TeacherSeatName,
    *,
    brief: AdvisorBrief | None,
    outcome: str = "brief",
    model: str = "seat-model",
) -> TeacherSeatOutcome:
    """Build one seat outcome, defaulting the label to its brief state."""
    return TeacherSeatOutcome(seat=seat, model=model, outcome=outcome, brief=brief)


def student_payload(
    *,
    outcome: str = "brief",
    brief: AdvisorBrief | None = None,
) -> AdvisorReportedAuditPayload:
    """Build one audited student answer."""
    answered = brief if outcome == "brief" and brief is not None else None
    return AdvisorReportedAuditPayload(
        outcome=outcome,
        model="qwen3.6:35b-a3b",
        brief=answered.brief
        if answered is not None
        else ("the student read" if outcome == "brief" else ""),
        anomalies=tuple(
            AdvisorAnomalyAuditPayload(
                label=anomaly.label, confidence=anomaly.confidence, rationale=anomaly.rationale
            )
            for anomaly in (answered.anomalies if answered is not None else ())
        ),
        view=answered.view if answered is not None else None,
        view_declined=answered.view_declined if answered is not None else None,
        latency_ms=1000,
        window_records=5,
    )


def episode(
    at: datetime,
    *,
    stream: TeacherStream = TeacherStream.TACTICAL,
    facts: AdvisorWindowFacts | None = None,
    seats: tuple[TeacherSeatOutcome, ...] = (),
    student: AdvisorReportedAuditPayload | None = None,
) -> TeacherEpisode:
    """Build one corpus episode with deterministic shape."""
    resolved = facts if facts is not None else facts_picture()
    return TeacherEpisode(
        stream=stream,
        created_at=at,
        window_outcome="pulled",
        facts=resolved,
        student=(
            StudentWindowBrief(created_at=at - timedelta(minutes=30), payload=student)
            if student is not None
            else None
        ),
        seats=seats,
    )


def digest_for(
    *episodes: TeacherEpisode,
    horizon: float = HORIZON,
    now: datetime = NOW,
) -> UpgradeDivergenceDigest:
    """Compute the digest over fixed episodes under the standard clock."""
    verdicts = episode_verdicts(episodes, horizon, now=now)
    return build_divergence_digest(episodes, verdicts, horizon)


def miss_episode(
    at: datetime = T0,
    *,
    teacher_labels: tuple[str, ...] = ("halt-count rise",),
) -> TeacherEpisode:
    """Build one episode where the teacher flagged and the student did not."""
    return episode(
        at,
        seats=(
            seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with(teacher_labels)),
            seat_outcome(TeacherSeatName.CODEX, brief=brief_with(teacher_labels)),
        ),
        student=student_payload(brief=brief_with(())),
    )


def bad_follower(at: datetime = T0 + timedelta(hours=2)) -> TeacherEpisode:
    """Build the later episode whose facts realize the bad truth."""
    return episode(at, facts=facts_picture(day_pnl="-0.5"))


def posture_picture(
    *,
    equity: str | None = "100.0",
    day_start: str | None = "100.0",
    day_pnl: str | None = "0.0",
    committed: str | None = None,
    action: str | None = None,
) -> AdvisorWindowFacts:
    """Build one window snapshot with the given posture fields."""
    return AdvisorWindowFacts(
        record_count=5,
        tracked_symbol="SPCXc" if committed is not None else None,
        committed_usdc=committed,
        equity_usdc=equity,
        day_start_equity_usdc=day_start,
        day_pnl_usdc=day_pnl,
        latest_action=action,
    )


def posture_flagged_episode(
    *,
    student_brief: AdvisorBrief | None = None,
    student_quiet: bool = False,
) -> TeacherEpisode:
    """Build one episode whose posture the deterministic desk flags."""
    return episode(
        T0,
        facts=posture_picture(equity="90.0", day_pnl="-10.0"),
        student=student_payload(brief=brief_with(()) if student_quiet else student_brief)
        if (student_quiet or student_brief is not None)
        else None,
    )


class ScriptedSeatTransport:
    """Serve scripted seat invocations, writing codex last-message files."""

    def __init__(
        self,
        results: list[TeacherProcessResult | Exception],
        *,
        last_message: str = "",
    ) -> None:
        """Queue one answer per expected invocation."""
        self._results = results
        self._last_message = last_message
        self.invocations: list[tuple[list[str], str]] = []

    def invoke(
        self,
        argv: Sequence[str],
        *,
        prompt: str,
        timeout_seconds: float,
        cwd: Path,
    ) -> TeacherProcessResult:
        """Record the invocation and serve the next scripted answer."""
        argv_list = list(argv)
        self.invocations.append((argv_list, prompt))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        if "-o" in argv_list:
            message_path = Path(argv_list[argv_list.index("-o") + 1])
            message_path.parent.mkdir(parents=True, exist_ok=True)
            message_path.write_text(self._last_message, encoding="utf-8")
        return result


def claude_body(answer: object) -> str:
    """Render one successful claude result envelope."""
    return json.dumps(
        {
            "is_error": False,
            "subtype": "success",
            "result": answer if isinstance(answer, str) else json.dumps(answer),
            "modelUsage": {"glm-5.3": {"inputTokens": 10}},
        }
    )


def proposal_result() -> TeacherProcessResult:
    """One successful claude invocation answering a valid proposal."""
    return TeacherProcessResult(exit_code=0, stdout=claude_body(PROPOSAL_ANSWER), stderr="")


def seeded_config() -> TeacherConfig:
    """One configuration with both seats' binaries pinned off PATH."""
    return TeacherConfig(
        seats={
            TeacherSeatName.CLAUDE: TeacherSeatConfig(binary="/bin/claude"),
            TeacherSeatName.CODEX: TeacherSeatConfig(binary="/bin/codex"),
        }
    )


def write_corpus(corpus_dir: Path, *episodes: TeacherEpisode) -> Path:
    """Write fixed episodes as the corpus JSONL file."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    path = corpus_dir / "corpus.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for item in episodes:
            handle.write(item.model_dump_json() + "\n")
    return path


class TestDigest:
    """The deterministic divergence classes, pinned end to end."""

    def test_a_teacher_flag_the_student_missed_on_a_bad_episode_is_a_miss(self) -> None:
        """Teacher flagged, student answered unflagged, bad truth followed."""
        digest = digest_for(miss_episode(), bad_follower())
        assert digest.total_divergences == 2
        miss = digest.misses[0]
        assert miss.seat == "claude"
        assert miss.teacher_labels == ("halt-count rise",)
        assert miss.student_brief == "a window reading worth teaching from"
        assert miss.day_pnl_usdc == "1.0"
        assert digest.grounded_episode_count == 2
        assert digest.bad_episode_count == 1
        assert digest.quiet_episode_count == 0

    def test_a_teacher_answer_the_student_never_gave_is_an_availability_gap(self) -> None:
        """An absent student on a bad episode yields gaps, not misses."""
        absent = episode(
            T0,
            seats=(seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with(("halt-count rise",))),),
            student=student_payload(outcome="timeout"),
        )
        unobserved = episode(
            T0,
            seats=(seat_outcome(TeacherSeatName.CODEX, brief=brief_with(("halt-count rise",))),),
        )
        digest = digest_for(absent, unobserved, bad_follower())
        assert digest.misses == ()
        assert len(digest.availability_gaps) == 2
        by_seat = {gap.seat: gap for gap in digest.availability_gaps}
        assert by_seat["claude"].student_outcome == "timeout"
        assert by_seat["codex"].student_outcome == ""
        assert by_seat["claude"].teacher_brief == "a window reading worth teaching from"

    def test_labels_only_the_teacher_raised_are_a_label_divergence(self) -> None:
        """Both flagged but the teacher alone named the realized label."""
        both_flagged = episode(
            T0,
            seats=(
                seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with(("halt rise", "fee dip"))),
            ),
            student=student_payload(brief=brief_with(("fee dip",))),
        )
        digest = digest_for(both_flagged, bad_follower())
        assert digest.misses == ()
        assert len(digest.label_divergences) == 1
        assert digest.label_divergences[0].teacher_only_labels == ("halt rise",)
        assert digest.label_divergences[0].student_labels == ("fee dip",)

    def test_matching_labels_are_not_divergence(self) -> None:
        """The student naming every teacher label is agreement."""
        matched = episode(
            T0,
            seats=(seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with(("halt rise",))),),
            student=student_payload(brief=brief_with(("halt rise",))),
        )
        digest = digest_for(matched, bad_follower())
        assert digest.total_divergences == 0

    def test_a_teacher_who_also_stayed_quiet_is_not_divergence(self) -> None:
        """A bad episode both desks missed teaches nothing about the teacher."""
        both_quiet = episode(
            T0,
            seats=(seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with(())),),
            student=student_payload(brief=brief_with(())),
        )
        digest = digest_for(both_quiet, bad_follower())
        assert digest.total_divergences == 0
        assert digest.bad_episode_count == 1

    def test_pending_and_quiet_episodes_never_contribute_entries(self) -> None:
        """Undecided truth gates the whole digest, honestly."""
        pending = digest_for(miss_episode(), episode(T0 + timedelta(hours=2)))
        assert pending.total_divergences == 0
        assert pending.grounded_episode_count == 2
        quiet = digest_for(
            miss_episode(),
            episode(T0 + timedelta(hours=2), facts=facts_picture(day_pnl="0.5")),
        )
        assert quiet.total_divergences == 0
        assert quiet.quiet_episode_count == 1

    def test_news_episodes_are_out_of_scope(self) -> None:
        """News has no deterministic follow-up truth to diverge over."""
        news = episode(
            T0,
            stream=TeacherStream.NEWS,
            seats=(seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with(("doctrine",))),),
            student=student_payload(brief=brief_with(())),
        )
        digest = digest_for(news, bad_follower())
        assert digest.total_divergences == 0
        assert digest.grounded_episode_count == 1

    def test_entries_are_bounded_and_most_recent(self) -> None:
        """Each class keeps only its most recent bounded entries."""
        episodes: list[TeacherEpisode] = []
        for index in range(UPGRADE_DIGEST_ENTRY_MAX + 1):
            episodes.append(miss_episode(at=T0 + timedelta(minutes=index)))
        episodes.append(bad_follower(at=T0 + timedelta(hours=2)))
        digest = digest_for(*episodes)
        assert len(digest.misses) == UPGRADE_DIGEST_ENTRY_MAX
        assert digest.misses[-1].created_at == T0 + timedelta(minutes=UPGRADE_DIGEST_ENTRY_MAX)

    def test_recency_is_timestamped_not_positional(self) -> None:
        """Out-of-order appends cannot displace newer evidence from the tail."""
        # The newest episode lands first in the file (a slower older pass
        # appended after it), so positional tailing would drop it.
        episodes = [miss_episode(at=T0 + timedelta(minutes=UPGRADE_DIGEST_ENTRY_MAX))]
        for index in range(UPGRADE_DIGEST_ENTRY_MAX):
            episodes.append(miss_episode(at=T0 + timedelta(minutes=index)))
        episodes.append(bad_follower(at=T0 + timedelta(hours=2)))
        digest = digest_for(*episodes)
        kept = [miss.created_at for miss in digest.misses]
        assert len(kept) == UPGRADE_DIGEST_ENTRY_MAX
        # The oldest timestamp is the one dropped; the newest survives.
        assert T0 not in kept
        assert kept[-1] == T0 + timedelta(minutes=UPGRADE_DIGEST_ENTRY_MAX)
        assert kept == sorted(kept)

    def test_brief_text_is_bounded_and_collapsed(self) -> None:
        """Digest brief text collapses whitespace and heads at its bound."""
        noisy = episode(
            T0,
            seats=(seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with(("halt rise",))),),
            student=student_payload(
                brief=AdvisorBrief(brief="  student \n\n words  " * 100, anomalies=())
            ),
        )
        digest = digest_for(noisy, bad_follower())
        assert len(digest.misses[0].teacher_brief) <= UPGRADE_DIGEST_BRIEF_MAX_CHARS
        assert len(digest.misses[0].student_brief) <= UPGRADE_DIGEST_BRIEF_MAX_CHARS
        assert "\n" not in digest.misses[0].student_brief


def conviction_digest(
    *episodes: TeacherEpisode,
    horizon: float = HORIZON,
    now: datetime = NOW,
) -> UpgradeDivergenceDigest:
    """Compute the digest with the conviction layer threaded in."""
    verdicts = episode_verdicts(episodes, horizon, now=now)
    view_grades = grade_corpus_views(episodes, horizon, now=now)
    return build_divergence_digest(episodes, verdicts, horizon, view_grades=view_grades)


def stated_view(
    verdict: PositionVerdict,
    confidence: ViewConfidence = ViewConfidence.HIGH,
) -> PositionView:
    """Build one stated view citing the locked policy."""
    return PositionView(
        verdict=verdict,
        confidence=confidence,
        reason="In range with emissions above the locked floor.",
    )


def brief_with_view(
    verdict: PositionVerdict | None,
    *,
    labels: tuple[str, ...] = (),
    declined: bool = False,
) -> AdvisorBrief:
    """Build one accepted answer carrying a view, a decline, or neither."""
    return AdvisorBrief(
        brief="a window reading worth teaching from",
        anomalies=tuple(
            AdvisorAnomaly(label=label, confidence=0.8, rationale="deterministic")
            for label in labels
        ),
        view=None if verdict is None or declined else stated_view(verdict),
        view_declined="the window is too stale to defend a verdict" if declined else None,
    )


def conviction_episode(
    at: datetime = T0,
    *,
    teacher_verdict: PositionVerdict = PositionVerdict.HOLD,
    student_verdict: PositionVerdict | None = PositionVerdict.EXIT,
    student_declined: bool = False,
) -> TeacherEpisode:
    """Build one episode where the teacher holds a view over the student's."""
    return episode(
        at,
        seats=(
            seat_outcome(TeacherSeatName.CLAUDE, brief=brief_with_view(teacher_verdict)),
            seat_outcome(TeacherSeatName.CODEX, brief=None, outcome="dark"),
        ),
        student=student_payload(
            brief=brief_with_view(
                student_verdict,
                declined=student_declined,
            )
            if student_verdict is not None or student_declined
            else brief_with(())
        ),
    )


def quiet_follower(at: datetime = T0 + timedelta(hours=2)) -> TeacherEpisode:
    """Build the later episode whose quiet facts drain the horizon."""
    return episode(at, facts=facts_picture(day_pnl="0.5"))


class TestConvictionDigest:
    """The fifth evidence class: measured conviction, pinned end to end."""

    def test_a_teacher_right_where_the_student_was_wrong_is_a_miss(self) -> None:
        """The quiet-drained truth backs the entry without any bad outcome."""
        digest = conviction_digest(conviction_episode(), quiet_follower())
        assert len(digest.conviction_misses) == 1
        miss = digest.conviction_misses[0]
        assert miss.seat == "claude"
        assert miss.teacher_verdict == "hold"
        assert miss.teacher_confidence == "high"
        assert "locked floor" in miss.teacher_reason
        assert miss.student_state == "stated"
        assert miss.student_verdict == "exit"
        assert miss.day_pnl_usdc == "1.0"
        assert digest.total_divergences == 1

    def test_a_student_decline_against_a_right_teacher_is_a_miss(self) -> None:
        """An explicit no-view declaration diverges from measured conviction."""
        digest = conviction_digest(
            conviction_episode(student_verdict=None, student_declined=True),
            quiet_follower(),
        )
        assert len(digest.conviction_misses) == 1
        assert digest.conviction_misses[0].student_state == "declined"
        assert digest.conviction_misses[0].student_verdict is None

    def test_a_student_view_gap_against_a_right_teacher_is_a_miss(self) -> None:
        """A positioned episode the student answered without a view counts."""
        digest = conviction_digest(
            conviction_episode(student_verdict=None),
            quiet_follower(),
        )
        assert len(digest.conviction_misses) == 1
        assert digest.conviction_misses[0].student_state == "missing"

    def test_a_right_student_view_is_never_a_miss(self) -> None:
        """Matching conviction is not divergence."""
        digest = conviction_digest(
            conviction_episode(student_verdict=PositionVerdict.HOLD),
            quiet_follower(),
        )
        assert digest.conviction_misses == ()
        assert digest.total_divergences == 0

    def test_only_decided_teacher_truth_opens_the_class(self) -> None:
        """Pending and ungradeable teacher views never contribute."""
        open_now = T0 + timedelta(hours=3)
        digest = conviction_digest(conviction_episode(), quiet_follower(), now=open_now)
        assert digest.conviction_misses == ()

    def test_news_episodes_are_out_of_scope(self) -> None:
        """The outside-world stream carries no deterministic view truth."""
        digest = conviction_digest(
            episode(
                T0,
                stream=TeacherStream.NEWS,
                seats=(
                    seat_outcome(
                        TeacherSeatName.CLAUDE, brief=brief_with_view(PositionVerdict.HOLD)
                    ),
                ),
                student=student_payload(brief=brief_with_view(PositionVerdict.EXIT)),
            ),
            quiet_follower(),
        )
        assert digest.conviction_misses == ()

    def test_the_legacy_composition_without_view_grades_stays_valid(self) -> None:
        """Direct digest calls without the conviction layer stay available."""
        episodes = (conviction_episode(), quiet_follower())
        verdicts = episode_verdicts(episodes, HORIZON, now=NOW)
        digest = build_divergence_digest(episodes, verdicts, HORIZON)
        assert digest.conviction_misses == ()
        assert digest.total_divergences == 0

    def test_entries_are_bounded_and_most_recent(self) -> None:
        """The conviction class keeps only its most recent bounded entries."""
        episodes: list[TeacherEpisode] = []
        for index in range(UPGRADE_DIGEST_ENTRY_MAX + 5):
            at = T0 + timedelta(minutes=12 * index)
            episodes.append(conviction_episode(at))
            episodes.append(quiet_follower(at + timedelta(hours=2)))
        digest = conviction_digest(*episodes, now=T0 + timedelta(hours=48))
        assert len(digest.conviction_misses) == UPGRADE_DIGEST_ENTRY_MAX
        newest = max(entry.created_at for entry in digest.conviction_misses)
        assert digest.conviction_misses[-1].created_at == newest

    def test_the_human_summary_names_conviction_misses(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The printed divergence line carries the fifth class."""
        episodes = (conviction_episode(), quiet_follower())
        verdicts = episode_verdicts(episodes, HORIZON, now=NOW)
        view_grades = grade_corpus_views(episodes, HORIZON, now=NOW)
        digest = build_divergence_digest(
            episodes,
            verdicts,
            HORIZON,
            view_grades=view_grades,
        )
        report = UpgradeReport(
            created_at=NOW,
            horizon_hours=HORIZON,
            gated=False,
            digest=digest,
        )
        print_upgrade_report(report)
        summary = capsys.readouterr().out
        assert "1 conviction misses" in summary


class TestProposalSchema:
    """The bounded proposal object a seat must answer with."""

    def test_a_valid_proposal_is_accepted(self) -> None:
        """The canonical shape validates and pins its schema tag."""
        proposal = UpgradeProposal.model_validate(PROPOSAL_ANSWER)
        assert proposal.schema_version == UPGRADE_PROPOSAL_SCHEMA
        assert proposal.exemplars == tuple(cast("list[str]", PROPOSAL_ANSWER["exemplars"]))

    def test_bounds_are_enforced(self) -> None:
        """Every field bound rejects its violation."""
        with pytest.raises(ValueError):
            UpgradeProposal.model_validate(
                {**PROPOSAL_ANSWER, "teaching_block": "x" * (ADVISOR_TEACHING_MAX_CHARS + 1)}
            )
        with pytest.raises(ValueError):
            UpgradeProposal.model_validate(
                {**PROPOSAL_ANSWER, "rationale": "y" * (UPGRADE_RATIONALE_MAX_CHARS + 1)}
            )
        with pytest.raises(ValueError):
            UpgradeProposal.model_validate({**PROPOSAL_ANSWER, "exemplars": ["e"] * 6})
        with pytest.raises(ValueError):
            UpgradeProposal.model_validate({**PROPOSAL_ANSWER, "exemplars": ["z" * 601]})
        with pytest.raises(ValueError):
            UpgradeProposal.model_validate({"teaching_block": "standalone", "rationale": ""})
        with pytest.raises(ValueError):
            UpgradeProposal.model_validate({"teaching_block": "", "rationale": "because"})


class TestAskUpgradeSeat:
    """Every absence is typed; only a valid proposal is accepted."""

    def test_a_valid_answer_becomes_a_proposal(self, tmp_path: Path) -> None:
        """A schema-valid answer is accepted and tagged with the model."""
        transport = ScriptedSeatTransport([proposal_result()])
        outcome = ask_upgrade_seat(
            TeacherSeatName.CLAUDE,
            TeacherSeatConfig(binary="/bin/claude"),
            "prompt",
            cast("TeacherSeatTransport", transport),
            work_dir=tmp_path,
        )
        assert outcome.outcome == "proposal"
        assert outcome.model == "glm-5.3"
        assert outcome.proposal is not None
        assert outcome.proposal.teaching_block == cast("str", PROPOSAL_ANSWER["teaching_block"])

    def test_fenced_answers_are_tolerated(self, tmp_path: Path) -> None:
        """A fenced JSON proposal still validates."""
        fenced = "```json\n" + json.dumps(PROPOSAL_ANSWER) + "\n```"
        transport = ScriptedSeatTransport(
            [TeacherProcessResult(exit_code=0, stdout=claude_body(fenced), stderr="")]
        )
        outcome = ask_upgrade_seat(
            TeacherSeatName.CLAUDE,
            TeacherSeatConfig(binary="/bin/claude"),
            "prompt",
            cast("TeacherSeatTransport", transport),
            work_dir=tmp_path,
        )
        assert outcome.outcome == "proposal"

    def test_empty_malformed_and_schema_invalid_answers_are_typed(self, tmp_path: Path) -> None:
        """Answer-level failures keep the stable absence catalog."""
        transport = ScriptedSeatTransport(
            [TeacherProcessResult(exit_code=0, stdout=claude_body("   "), stderr="")]
        )
        assert (
            ask_upgrade_seat(
                TeacherSeatName.CLAUDE,
                TeacherSeatConfig(binary="/bin/claude"),
                "prompt",
                cast("TeacherSeatTransport", transport),
                work_dir=tmp_path,
            ).outcome
            == "empty_content"
        )
        transport = ScriptedSeatTransport(
            [TeacherProcessResult(exit_code=0, stdout=claude_body("not json"), stderr="")]
        )
        assert (
            ask_upgrade_seat(
                TeacherSeatName.CLAUDE,
                TeacherSeatConfig(binary="/bin/claude"),
                "prompt",
                cast("TeacherSeatTransport", transport),
                work_dir=tmp_path,
            ).outcome
            == "malformed_json"
        )
        transport = ScriptedSeatTransport(
            [
                TeacherProcessResult(
                    exit_code=0,
                    stdout=claude_body({"brief": "a brief is not a proposal"}),
                    stderr="",
                )
            ]
        )
        assert (
            ask_upgrade_seat(
                TeacherSeatName.CLAUDE,
                TeacherSeatConfig(binary="/bin/claude"),
                "prompt",
                cast("TeacherSeatTransport", transport),
                work_dir=tmp_path,
            ).outcome
            == "schema_invalid"
        )

    def test_invocation_failures_are_typed(self, tmp_path: Path) -> None:
        """Timeout, missing CLI, and dark keep the shared catalog."""
        transport = ScriptedSeatTransport([TeacherSeatTimeoutError("slow")])
        assert (
            ask_upgrade_seat(
                TeacherSeatName.CODEX,
                TeacherSeatConfig(binary="/bin/codex"),
                "prompt",
                cast("TeacherSeatTransport", transport),
                work_dir=tmp_path,
            ).outcome
            == "timeout"
        )
        assert (
            ask_upgrade_seat(
                TeacherSeatName.CODEX,
                TeacherSeatConfig(enabled=False),
                "prompt",
                cast("TeacherSeatTransport", ScriptedSeatTransport([])),
                work_dir=tmp_path,
            ).outcome
            == "dark"
        )


class TestUpgradePass:
    """The composed pass: digest, honest gate, and the seats' questions."""

    def test_a_pending_corpus_gates_every_seat_out(self, tmp_path: Path) -> None:
        """All-pending truth writes a gated report and asks nobody."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, miss_episode(), episode(T0 + timedelta(hours=2)))
        transport = ScriptedSeatTransport([])
        report = run_upgrade_pass(
            seeded_config(),
            cast("TeacherSeatTransport", transport),
            corpus_dir,
            HORIZON,
            now=NOW,
        )
        assert isinstance(report, UpgradeReport)
        assert report.gated
        assert report.seats == ()
        assert transport.invocations == []
        assert report.digest.total_divergences == 0

    def test_skipped_malformed_lines_surface_never_gate(self, tmp_path: Path) -> None:
        """A corrupted corpus is distinguishable from an honest gate."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, miss_episode(), episode(T0 + timedelta(hours=2)))
        with (corpus_dir / "corpus.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"truncated": ')
        transport = ScriptedSeatTransport([])
        report = run_upgrade_pass(
            seeded_config(),
            cast("TeacherSeatTransport", transport),
            corpus_dir,
            HORIZON,
            now=NOW,
        )
        assert report.gated
        assert report.digest.malformed_episode_count == 1
        # Corruption surfaces; it never fabricates a question.
        assert transport.invocations == []

    def test_a_seeded_miss_asks_both_seats_over_the_digest(self, tmp_path: Path) -> None:
        """Realized divergence asks each seat with the digest embedded."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, miss_episode(), bad_follower())
        transport = ScriptedSeatTransport(
            [proposal_result(), proposal_result()], last_message=json.dumps(PROPOSAL_ANSWER)
        )
        report = run_upgrade_pass(
            seeded_config(),
            cast("TeacherSeatTransport", transport),
            corpus_dir,
            HORIZON,
            now=NOW,
        )
        assert not report.gated
        assert len(report.seats) == 2
        assert all(outcome.proposal is not None for outcome in report.seats)
        assert len(transport.invocations) == 2
        prompt = transport.invocations[0][1]
        assert ADVISOR_SYSTEM_PROMPT.split(".")[0] in prompt
        assert "halt-count rise" in prompt
        assert "hindsight_desk_scores" in prompt
        assert any(desk.desk == "student" for desk in report.desk_scores)

    def test_a_conviction_miss_alone_opens_the_gate(self, tmp_path: Path) -> None:
        """Measured conviction asks the seats with no anomaly divergence.

        The teacher's hold view graded right against the quiet-drained
        horizon while the student's exit view graded wrong: decided view
        truth, no bad outcome anywhere, a clean posture.
        """
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, conviction_episode(), quiet_follower())
        transport = ScriptedSeatTransport(
            [proposal_result(), proposal_result()], last_message=json.dumps(PROPOSAL_ANSWER)
        )
        report = run_upgrade_pass(
            seeded_config(),
            cast("TeacherSeatTransport", transport),
            corpus_dir,
            HORIZON,
            now=NOW,
        )
        assert not report.gated
        assert len(report.seats) == 2
        assert len(report.digest.conviction_misses) == 1
        assert report.digest.misses == ()
        assert report.digest.posture_misses == ()
        # The seats' prompt carries the measured conviction: the digest
        # entry and the per-desk view scores riding the hindsight block.
        prompt = transport.invocations[0][1]
        assert "conviction_misses" in prompt
        assert '"views"' in prompt
        assert '"teacher_verdict": "hold"' in prompt

    def test_the_seat_filter_restricts_the_pass(self, tmp_path: Path) -> None:
        """A filtered pass asks one seat and records only it."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, miss_episode(), bad_follower())
        transport = ScriptedSeatTransport([proposal_result()])
        report = run_upgrade_pass(
            seeded_config(),
            cast("TeacherSeatTransport", transport),
            corpus_dir,
            HORIZON,
            now=NOW,
            seat_filter=frozenset({TeacherSeatName.CLAUDE}),
        )
        assert tuple(outcome.seat for outcome in report.seats) == (TeacherSeatName.CLAUDE,)

    def test_the_prompt_declares_the_block_bounds_and_untrusted_data(self) -> None:
        """The user prompt carries the bounds; the contract the honesty."""
        digest = digest_for(miss_episode(), bad_follower())
        prompt = build_upgrade_user_prompt(digest, ())
        assert '"max_chars"' in prompt
        assert "student_current_instructions" in prompt


class TestArtifacts:
    """The dated proposals trail and the atomic last report."""

    def test_artifacts_append_and_rewrite_atomically(self, tmp_path: Path) -> None:
        """Same-day runs append; the last report is one valid object."""
        corpus_dir = tmp_path / "corpus"
        first = UpgradeReport(
            created_at=NOW,
            horizon_hours=HORIZON,
            gated=True,
            digest=UpgradeDivergenceDigest(horizon_hours=HORIZON),
        )
        proposal_path, last_path = write_upgrade_artifacts(first, corpus_dir)
        assert proposal_path.name == "upgrade-20260924.jsonl"
        assert proposal_path.parent.name == "proposals"
        assert last_path.name == "upgrade_last.json"
        write_upgrade_artifacts(first, corpus_dir)
        lines = proposal_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        persisted = json.loads(last_path.read_text(encoding="utf-8"))
        assert persisted["schema_version"] == "upgrade_report/1"
        later = UpgradeReport(
            created_at=NOW + timedelta(days=1),
            horizon_hours=HORIZON,
            gated=True,
            digest=UpgradeDivergenceDigest(horizon_hours=HORIZON),
        )
        dated_path, _ = write_upgrade_artifacts(later, corpus_dir)
        assert dated_path.name == "upgrade-20260925.jsonl"

    def test_a_failed_trail_write_names_itself(self, tmp_path: Path) -> None:
        """A blocked proposals directory fails naming the trail artifact."""
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "proposals").write_text("not a directory", encoding="utf-8")
        report = UpgradeReport(
            created_at=NOW,
            horizon_hours=HORIZON,
            gated=True,
            digest=UpgradeDivergenceDigest(horizon_hours=HORIZON),
        )
        with pytest.raises(OSError, match="proposals trail"):
            write_upgrade_artifacts(report, corpus_dir)

    def test_a_failed_report_write_names_the_surviving_trail(self, tmp_path: Path) -> None:
        """A blocked report still records the pass in the dated trail."""
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "reports").write_text("not a directory", encoding="utf-8")
        report = UpgradeReport(
            created_at=NOW,
            horizon_hours=HORIZON,
            gated=True,
            digest=UpgradeDivergenceDigest(horizon_hours=HORIZON),
        )
        with pytest.raises(OSError, match="already carries this pass"):
            write_upgrade_artifacts(report, corpus_dir)
        trail = corpus_dir / "proposals" / "upgrade-20260924.jsonl"
        assert len(trail.read_text(encoding="utf-8").strip().splitlines()) == 1


class TestCliContract:
    """Exit codes and the honest human summary."""

    def test_an_empty_corpus_exits_zero_with_a_gated_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nothing to teach from is honest, not failed."""
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(tmp_path / "corpus"))
        assert main([]) == 0
        captured = capsys.readouterr()
        assert "gated" in captured.out
        assert (tmp_path / "corpus" / "reports" / "upgrade_last.json").exists()

    def test_a_seeded_miss_runs_end_to_end(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A real divergence drives both seats and writes the artifacts."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, miss_episode(), bad_follower())
        config_path = tmp_path / "teacher.json"
        config_path.write_text(
            json.dumps(
                {
                    "seats": {
                        "claude": {"binary": "/bin/claude"},
                        "codex": {"binary": "/bin/codex"},
                    }
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("AERO_BOT_TEACHER_CONFIG", str(config_path))
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(corpus_dir))
        transport = ScriptedSeatTransport(
            [proposal_result(), proposal_result()], last_message=json.dumps(PROPOSAL_ANSWER)
        )
        monkeypatch.setattr(
            "aero_bot.upgrade.SubprocessTeacherTransport",
            lambda: cast("TeacherSeatTransport", transport),
        )
        assert main([]) == 0
        captured = capsys.readouterr()
        assert "proposed a teaching block" in captured.out
        assert "seal" in captured.out
        proposals = list((corpus_dir / "proposals").glob("upgrade-*.jsonl"))
        assert len(proposals) == 1
        line = json.loads(proposals[0].read_text(encoding="utf-8").strip())
        assert line["seats"][0]["proposal"]["schema_version"] == UPGRADE_PROPOSAL_SCHEMA

    def test_the_seat_filter_and_json_flag(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The filter asks one seat; --json prints the whole object."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, miss_episode(), bad_follower())
        config_path = tmp_path / "teacher.json"
        config_path.write_text(
            json.dumps({"seats": {"claude": {"binary": "/bin/claude"}}}), encoding="utf-8"
        )
        monkeypatch.setenv("AERO_BOT_TEACHER_CONFIG", str(config_path))
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(corpus_dir))
        transport = ScriptedSeatTransport([proposal_result()])
        monkeypatch.setattr(
            "aero_bot.upgrade.SubprocessTeacherTransport",
            lambda: cast("TeacherSeatTransport", transport),
        )
        assert main(["--seat", "claude", "--json"]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert len(printed["seats"]) == 1
        assert printed["seats"][0]["seat"] == "claude"

    def test_an_invalid_configuration_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A malformed configuration file fails closed, named."""
        config_path = tmp_path / "teacher.json"
        config_path.write_text('{"seats": {"claude": {"enabled": "maybe"}}}', encoding="utf-8")
        monkeypatch.setenv("AERO_BOT_TEACHER_CONFIG", str(config_path))
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(tmp_path / "corpus"))
        assert main([]) == 1
        assert str(config_path) in capsys.readouterr().err

    def test_an_undecodable_configuration_exits_one_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Invalid UTF-8 in the configuration is a typed failure, not a traceback."""
        config_path = tmp_path / "teacher.json"
        config_path.write_bytes(b'{"seats": "\xff\xfe"}')
        monkeypatch.setenv("AERO_BOT_TEACHER_CONFIG", str(config_path))
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(tmp_path / "corpus"))
        assert main([]) == 1
        assert str(config_path) in capsys.readouterr().err

    def test_an_undecodable_corpus_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Invalid UTF-8 in the corpus is a typed failure, not a traceback."""
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "corpus.jsonl").write_bytes(b"\xff\xfe not utf-8")
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(corpus_dir))
        assert main([]) == 1
        assert "could not be read" in capsys.readouterr().err

    def test_an_unwritable_corpus_directory_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corpus path that cannot hold artifacts fails named."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(blocker))
        assert main([]) == 1
        assert "failed" in capsys.readouterr().err

    def test_the_human_summary_prints_absent_seats_with_detail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An absent seat prints its reason and bounded detail tail."""
        outcome = SeatUpgradeOutcome(
            seat=TeacherSeatName.CODEX,
            model="gpt-6-luna",
            outcome="cli_error",
            detail="spawn failed",
        )
        assert outcome.status == "cli_error"
        report = UpgradeReport(
            created_at=T0,
            horizon_hours=HORIZON,
            gated=False,
            digest=UpgradeDivergenceDigest(horizon_hours=HORIZON),
            seats=(outcome,),
        )
        print_upgrade_report(report)
        captured = capsys.readouterr()
        assert "codex: absent cli_error - spawn failed" in captured.out

    def test_a_corrupted_corpus_gates_with_the_honesty_line(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A gated pass over a corrupted corpus says so in its summary."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, miss_episode(), episode(T0 + timedelta(hours=2)))
        with (corpus_dir / "corpus.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"truncated": ')
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(corpus_dir))
        assert main([]) == 0
        captured = capsys.readouterr()
        assert "corpus honesty" in captured.out
        assert "1 malformed" in captured.out

    def test_an_unwritable_corpus_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corpus path under a file cannot be created; exit one."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(blocker / "corpus"))
        assert main([]) == 1
        assert "failed" in capsys.readouterr().err

    def test_horizon_bounds_are_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Out-of-bounds horizons are a usage error."""
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(tmp_path / "corpus"))
        with pytest.raises(SystemExit) as raised:
            main(["--horizon-hours", "0.5"])
        assert raised.value.code == 2


class TestPostureWiring:
    """The deterministic risk desk feeds the digest's fourth evidence class."""

    def test_a_quiet_student_over_a_flagged_posture_is_a_posture_miss(self) -> None:
        """The counterparty finding backs a scorable miss on its own truth."""
        flagged = posture_flagged_episode(student_quiet=True)
        audit = audit_corpus_posture((flagged,))
        digest = build_divergence_digest((flagged,), (), HORIZON, posture_audit=audit)
        assert digest.posture_finding_count == 1
        assert digest.total_divergences == 1
        miss = digest.posture_misses[0]
        assert miss.finding_kinds == (RiskFindingKind.HALT_THRESHOLD_BREACHED,)
        assert miss.student_brief
        assert miss.day_pnl_usdc == "-10.0"
        assert miss.halted_count == 0

    def test_multiple_findings_carry_their_kinds_bounded(self) -> None:
        """One episode can miss several findings at once."""
        flagged = episode(
            T0,
            facts=posture_picture(
                equity="120.0",
                day_start="100.0",
                day_pnl="-10.0",
                committed="120.0",
                action="enter",
            ),
            student=student_payload(brief=brief_with(())),
        )
        audit = audit_corpus_posture((flagged,))
        digest = build_divergence_digest((flagged,), (), HORIZON, posture_audit=audit)
        assert digest.posture_finding_count == 3
        assert len(digest.posture_misses) == 1
        assert set(digest.posture_misses[0].finding_kinds) == {
            RiskFindingKind.DAY_PNL_CONTRADICTION,
            RiskFindingKind.HARD_CAP_BREACH,
            RiskFindingKind.SIZING_CAP_BREACH,
        }

    def test_a_flagged_student_never_contributes_a_posture_miss(self) -> None:
        """A desk that raised any label gave a verdict, not a miss."""
        flagged = posture_flagged_episode(student_brief=brief_with(("drawdown",)))
        audit = audit_corpus_posture((flagged,))
        digest = build_divergence_digest((flagged,), (), HORIZON, posture_audit=audit)
        assert digest.posture_misses == ()
        assert digest.posture_finding_count == 1
        assert digest.total_divergences == 0

    def test_an_absent_student_is_not_a_posture_miss(self) -> None:
        """The finding still counts as context; the miss needs an answer."""
        flagged = posture_flagged_episode()
        audit = audit_corpus_posture((flagged,))
        digest = build_divergence_digest((flagged,), (), HORIZON, posture_audit=audit)
        assert digest.posture_misses == ()
        assert digest.posture_finding_count == 1

    def test_the_gate_opens_on_a_posture_miss_alone(self, tmp_path: Path) -> None:
        """No teacher ever flagged; the deterministic desk alone asks the seats."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, posture_flagged_episode(student_quiet=True))
        transport = ScriptedSeatTransport(
            [proposal_result(), proposal_result()], last_message=json.dumps(PROPOSAL_ANSWER)
        )
        report = run_upgrade_pass(
            seeded_config(),
            cast("TeacherSeatTransport", transport),
            corpus_dir,
            HORIZON,
            now=NOW,
        )
        assert not report.gated
        assert len(transport.invocations) == 2
        prompt = transport.invocations[0][1]
        assert "risk_manager" in prompt
        assert "posture_finding_counts" in prompt
        assert len(report.digest.posture_misses) == 1
        assert all(outcome.proposal is not None for outcome in report.seats)

    def test_the_prompt_carries_the_risk_manager_block(self) -> None:
        """The counterparty seat's rules and counts ride the prompt."""
        flagged = posture_flagged_episode(student_quiet=True)
        audit = audit_corpus_posture((flagged,))
        digest = build_divergence_digest((flagged,), (), HORIZON, posture_audit=audit)
        prompt = build_upgrade_user_prompt(digest, (), audit)
        assert '"risk_manager"' in prompt
        assert "daily-loss-halt line" in prompt
        assert '"kind": "halt_threshold_breached"' in prompt
        assert '"count": 1' in prompt

    def test_without_an_audit_the_digest_stays_three_classed(self) -> None:
        """The audit is optional; the legacy composition is unchanged."""
        digest = digest_for(miss_episode(), bad_follower())
        assert digest.posture_misses == ()
        assert digest.posture_finding_count == 0
        prompt = build_upgrade_user_prompt(digest, ())
        assert "risk_manager" not in prompt

    def test_the_live_pass_always_audits_the_corpus(self, tmp_path: Path) -> None:
        """run_upgrade_pass carries the counterparty count even when gated."""
        corpus_dir = tmp_path / "corpus"
        write_corpus(corpus_dir, bad_follower())
        transport = ScriptedSeatTransport([])
        report = run_upgrade_pass(
            seeded_config(),
            cast("TeacherSeatTransport", transport),
            corpus_dir,
            HORIZON,
            now=NOW,
        )
        assert report.gated
        assert report.digest.posture_finding_count == 1
        assert report.digest.posture_misses == ()
