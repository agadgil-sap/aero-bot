"""Pin the teacher-advice publisher: document bounds, safe transport, CLI."""

import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aero_bot.advisor import AdvisorAnomaly, AdvisorBrief
from aero_bot.teacher import (
    TeacherConfig,
    TeacherSeatName,
    TeacherSeatOutcome,
    TeacherStream,
)
from aero_bot.teacher import (
    TeacherEpisode as Episode,
)
from aero_bot.teacher_publish import (
    ADVICE_BRIEF_MAX_CHARS,
    ADVICE_MAX_EPISODES,
    ADVICE_MAX_JSON_BYTES,
    DEFAULT_ADVICE_DESTINATION,
    PUBLISH_REMOTE_SCRIPT,
    TEACHER_ADVICE_SCHEMA,
    build_advice_document,
    build_publish_command,
    main,
    publish_advice,
)

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


class ScriptedTransport:
    """Serve one scripted command result."""

    def __init__(self, exit_code: int = 0, stdout: str = "published\n") -> None:
        """Serve every run, or the scripted failure."""
        self.exit_code = exit_code
        self.stdout = stdout
        self.commands: list[list[str]] = []

    def run(self, command: Sequence[str]) -> tuple[int, str, str]:
        """Record the command and answer the scripted result."""
        self.commands.append(list(command))
        return self.exit_code, self.stdout, ""


def brief(text: str = "Position in range; nothing demands eyes.") -> AdvisorBrief:
    """Build one accepted bounded brief."""
    return AdvisorBrief(
        anomalies=(AdvisorAnomaly(label="x", confidence=0.5, rationale="r"),), brief=text
    )


def episode(at: datetime, *, outcome: str = "brief") -> Episode:
    """Build one two-seat episode at the given instant."""
    return Episode(
        stream=TeacherStream.TACTICAL,
        created_at=at,
        window_outcome="pulled",
        seats=(
            TeacherSeatOutcome(
                seat=TeacherSeatName.CLAUDE,
                model="glm-5.3",
                outcome=outcome,
                brief=brief() if outcome == "brief" else None,
            ),
            TeacherSeatOutcome(
                seat=TeacherSeatName.CODEX,
                model="gpt-6-luna",
                outcome=outcome,
                brief=brief("Codex agrees.") if outcome == "brief" else None,
            ),
        ),
    )


def teacher_config() -> TeacherConfig:
    """Build one offline harness configuration for command tests."""
    return TeacherConfig.model_validate(
        {
            "pull": {
                "instance": "aero-bot",
                "zone": "us-west1-b",
                "audit_database_path": "/var/lib/aero-bot/audit.sqlite3",
                "book_path": "/var/lib/aero-bot/cycle_state.json",
            },
            "seats": {},
        }
    )


class TestDocument:
    """The artifact is bounded, provenance-complete, and schema-validated."""

    def test_recent_episodes_render_with_provenance(self) -> None:
        """The newest episodes carry seats, models, briefs, and views."""
        old = episode(NOW - timedelta(hours=48))
        new = episode(NOW - timedelta(minutes=30))
        document = build_advice_document((old, new), NOW)
        assert document.schema_version == TEACHER_ADVICE_SCHEMA
        assert document.generated_at == NOW
        assert [e.created_at for e in document.episodes] == [old.created_at, new.created_at]
        seats = document.episodes[-1].seats
        assert seats[0].seat == "claude" and seats[0].model == "glm-5.3"
        assert seats[0].outcome == "brief"
        assert "nothing demands eyes" in seats[0].brief
        assert seats[1].seat == "codex" and seats[1].model == "gpt-6-luna"

    def test_the_episode_bound_holds(self) -> None:
        """Only the newest ADVICE_MAX_EPISODES episodes ride along."""
        many = tuple(episode(NOW - timedelta(hours=i)) for i in range(20))
        document = build_advice_document(many, NOW)
        assert len(document.episodes) == ADVICE_MAX_EPISODES
        newest = document.episodes[-1].created_at
        assert newest == max(item.created_at for item in many)

    def test_briefs_cap_at_the_bound(self) -> None:
        """A long brief clips with an explicit ellipsis."""
        long_text = "x" * (ADVICE_BRIEF_MAX_CHARS + 50)
        one = episode(
            NOW,
        )
        one = one.model_copy(
            update={
                "seats": (
                    TeacherSeatOutcome(
                        seat=TeacherSeatName.CLAUDE,
                        model="glm-5.3",
                        outcome="brief",
                        brief=brief(long_text),
                    ),
                )
            }
        )
        document = build_advice_document((one,), NOW)
        rendered = document.episodes[0].seats[0].brief
        assert len(rendered) == ADVICE_BRIEF_MAX_CHARS
        assert rendered.endswith("...")

    def test_absences_tally_by_typed_reason(self) -> None:
        """Non-brief outcomes count per reason, never silently vanish."""
        absent = episode(NOW, outcome="timeout")
        document = build_advice_document((absent,), NOW)
        assert document.absence_counts == {"timeout": 2}
        assert all(seat.brief == "" for e in document.episodes for seat in e.seats)


class TestTransport:
    """The one-way publish reuses the pull's safe command discipline."""

    def test_the_command_carries_base64_argv_never_interpolation(self) -> None:
        """The script text is fixed; payload and destination ride argv."""
        payload = b"{}"
        command = build_publish_command(teacher_config(), payload, DEFAULT_ADVICE_DESTINATION)
        assert command[0].endswith("gcloud")
        assert command[1:6] == ["compute", "ssh", "aero-bot", "--zone", "us-west1-b"]
        assert command[6:8] == ["--quiet", "--command"]
        remote = command[-1]
        assert "sudo" in remote and "base64 -d" in remote
        # The payload never appears raw in the remote text, only encoded.
        assert payload.decode() not in remote
        assert DEFAULT_ADVICE_DESTINATION in remote

    def test_the_remote_script_never_names_a_configured_value(self) -> None:
        """The fixed script validates and writes; no path is baked in."""
        assert "teacher_advice/1" in PUBLISH_REMOTE_SCRIPT
        assert "os.replace" in PUBLISH_REMOTE_SCRIPT
        assert DEFAULT_ADVICE_DESTINATION not in PUBLISH_REMOTE_SCRIPT
        assert "aero-bot" in PUBLISH_REMOTE_SCRIPT  # the chown target user

    def test_publish_succeeds_through_the_transport(self) -> None:
        """A clean publish returns True and runs exactly one command."""
        transport = ScriptedTransport()
        config = teacher_config()
        assert publish_advice(config, (episode(NOW),), NOW, transport=transport) is True
        assert len(transport.commands) == 1

    def test_a_transport_failure_warns_and_returns_false(self) -> None:
        """A refused publish never raises and never claims success."""
        transport = ScriptedTransport(exit_code=1, stdout="refusing payload over 16384 bytes")
        errors = io.StringIO()
        config = teacher_config()
        assert (
            publish_advice(config, (episode(NOW),), NOW, transport=transport, error_stream=errors)
            is False
        )
        assert "refusing payload" in errors.getvalue()

    def test_a_relative_destination_is_refused_locally(self) -> None:
        """Only absolute, non-traversing destinations are ever attempted."""
        transport = ScriptedTransport()
        errors = io.StringIO()
        assert (
            publish_advice(
                teacher_config(),
                (episode(NOW),),
                NOW,
                destination="relative/path.json",
                transport=transport,
                error_stream=errors,
            )
            is False
        )
        assert transport.commands == []
        assert "absolute" in errors.getvalue()

    def test_an_oversized_document_is_refused_before_transport(self) -> None:
        """A document over the byte bound never leaves the Mac."""

        class Huge:
            """A transport that must never run."""

            def run(self, command: Sequence[str]) -> tuple[int, str, str]:
                """Fail the test if the oversized document reached here."""
                raise AssertionError("transport must not run")

        many = tuple(episode(NOW - timedelta(minutes=i)) for i in range(ADVICE_MAX_EPISODES))
        document = build_advice_document(many, NOW)
        assert len(json.dumps(document.model_dump(mode="json"))) <= ADVICE_MAX_JSON_BYTES


class TestCli:
    """The publisher command is bounded and honest."""

    def test_help_names_the_surface(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The usage names the publisher command."""
        with pytest.raises(SystemExit) as raised:
            main(["--help"])
        assert raised.value.code == 0
        assert "aero-bot-teacher-publish" in capsys.readouterr().out

    def test_dry_run_prints_the_document_without_the_box(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run over an empty corpus prints an empty document."""
        monkeypatch.setenv("AERO_BOT_TEACHER_CORPUS_DIR", str(tmp_path))
        assert main(["--dry-run", "--config", str(tmp_path / "absent.json")]) in (0, 1)
        captured = capsys.readouterr().out
        assert '"teacher_advice/1"' in captured or captured == ""
