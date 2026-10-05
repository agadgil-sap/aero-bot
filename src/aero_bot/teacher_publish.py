"""Publish one bounded teacher-advice evidence file to the production box.

The captain's 2026-10-05 digest ruling needs the 09:00 email to carry
summarized teacher advice, and the teacher corpus lives on the operator's
Mac (firstmate 027's inventory found no existing route). This module is the
minimal task-specific transfer firstmate 028 authorized: one one-way,
secret-free, bounded JSON artifact - the most recent advice with its full
provenance - atomically published beside the VM's audit store through the
teacher harness's existing authenticated gcloud channel, for the daily
digest to read.

It is deliberately NOT a control plane: no API, no generic sync, no
credential flow, no execution authority of any kind. The remote side of the
transfer is one fixed validation-and-write script whose text never sees a
configured value (the payload rides base64-encoded on its argv, exactly
like the harness's read-only window pull); the destination is a
non-executable data file owned by the service user, mode 0644, and nothing
on the box ever executes or obeys it - the digest renders it as text. A
failed or absent publish never suppresses the daily report: the digest
states the honest missing/malformed/stale marker instead.
"""

import argparse
import base64
import json
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO

from pydantic import BaseModel, ConfigDict, Field

from aero_bot.teacher import (
    DEFAULT_GCLOUD_PATH,
    TeacherConfig,
    TeacherEpisode,
    load_episodes_with_skips,
    load_teacher_config,
    resolve_config_path,
    resolve_corpus_dir,
)

# The artifact's schema tag; the consumer validates it exactly.
TEACHER_ADVICE_SCHEMA = "teacher_advice/1"
# How many recent episodes ride along (both seats per episode).
ADVICE_MAX_EPISODES = 8
# Each seat's brief excerpt bound, well under the corpus's own cap.
ADVICE_BRIEF_MAX_CHARS = 400
# The serialized document bound: generous over 8 episodes of capped prose.
ADVICE_MAX_JSON_BYTES = 16_384
# The default destination beside the audit store: a non-executable data
# file the aero-bot service user owns and the digest reads.
DEFAULT_ADVICE_DESTINATION = "/var/lib/aero-bot/teacher_advice.json"
# The digest treats advice whose newest episode is older than this as
# stale and says so rather than presenting it as current.
ADVICE_STALE_HOURS = 24


class AdviceSeat(BaseModel):
    """Carry one seat's summarized advice with its provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The seat that produced the outcome (claude, codex).
    seat: str
    # The model tag the seat ran under.
    model: str
    # The outcome: brief, or the stable typed absence reason.
    outcome: str
    # The accepted brief's prose, capped; empty on absence.
    brief: str = ""
    # The stated position view's verdict, else None.
    view_verdict: str | None = None
    # The explicit no-view declaration, else None.
    view_declined: str | None = None
    # The invocation's wall-clock duration in milliseconds.
    latency_ms: int = 0


class AdviceEpisode(BaseModel):
    """Carry one episode's summarized advice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # When the harness pass ran (the episode's own timestamp).
    created_at: datetime
    # The stream the pass served (tactical, daily, news).
    stream: str
    # Every seat's outcome in seat order.
    seats: tuple[AdviceSeat, ...] = ()


class TeacherAdviceDocument(BaseModel):
    """Carry the complete bounded artifact the digest consumes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The artifact schema tag.
    schema_version: str = TEACHER_ADVICE_SCHEMA
    # When this document was generated (Mac wall clock, UTC).
    generated_at: datetime
    # The most recent episodes, oldest first.
    episodes: tuple[AdviceEpisode, ...] = ()
    # Typed-absence counts across the selected episodes, for honesty.
    absence_counts: dict[str, int] = Field(default_factory=dict)


# The fixed remote script: decode the base64 payload on argv, validate its
# shape and size, write the destination atomically under the service user,
# and refuse everything else. No configured value is ever interpolated into
# this text - the payload and the destination path ride on its argv.
PUBLISH_REMOTE_SCRIPT = """
import base64
import json
import os
import pwd
import sys

payload = base64.b64decode(sys.argv[1], validate=True)
destination = sys.argv[2]
if len(payload) > 16384:
    sys.exit("refusing payload over 16384 bytes")
document = json.loads(payload)
if not isinstance(document, dict):
    sys.exit("payload is not an object")
for key in ("schema_version", "generated_at", "episodes"):
    if key not in document:
        sys.exit(f"payload missing {key}")
if document.get("schema_version") != "teacher_advice/1":
    sys.exit("payload schema_version mismatch")
if not isinstance(document.get("episodes"), list):
    sys.exit("payload episodes is not a list")
parent = os.path.dirname(destination)
if not os.path.isdir(parent):
    sys.exit(f"destination directory missing: {parent}")
entry = pwd.getpwnam("aero-bot")
temporary = destination + ".publish.tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    handle.write(payload.decode("utf-8"))
    handle.flush()
    os.fsync(handle.fileno())
os.chmod(temporary, 0o644)
os.chown(temporary, entry.pw_uid, entry.pw_gid)
os.replace(temporary, destination)
print(f"published {len(payload)} bytes to {destination}")
"""


class PublishTransport(Protocol):
    """Define the invocation boundary the publisher drives."""

    def run(self, command: Sequence[str]) -> tuple[int, str, str]:
        """Run one command; return (exit code, stdout, stderr)."""
        ...


class SubprocessPublishTransport:
    """Run the publish command through the ambient process table."""

    def run(self, command: Sequence[str]) -> tuple[int, str, str]:
        """Run one command through subprocess; never raise on failure."""
        import subprocess

        completed = subprocess.run(  # noqa: S603 - fixed gcloud argv, never seat text
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr


def _clip(text: str, limit: int) -> str:
    """Cap one excerpt at a word boundary with an explicit ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def build_advice_document(
    episodes: Sequence[TeacherEpisode],
    now: datetime,
    max_episodes: int = ADVICE_MAX_EPISODES,
) -> TeacherAdviceDocument:
    """Summarize the most recent episodes into the bounded artifact.

    Args:
        episodes: The corpus episodes, any order.
        now: The generation instant (Mac wall clock, timezone-aware).
        max_episodes: The episode bound.

    Returns:
        The validated document carrying the newest episodes oldest-first,
        every seat's outcome with provenance, and the typed-absence tally.
    """
    chosen = sorted(episodes, key=lambda episode: episode.created_at)[-max_episodes:]
    rendered: list[AdviceEpisode] = []
    absence_counts: dict[str, int] = {}
    for episode in chosen:
        seats: list[AdviceSeat] = []
        for seat in episode.seats:
            if seat.outcome != "brief":
                absence_counts[seat.outcome] = absence_counts.get(seat.outcome, 0) + 1
            brief = seat.brief
            seats.append(
                AdviceSeat(
                    seat=seat.seat.value,
                    model=seat.model,
                    outcome=seat.outcome,
                    brief=_clip(brief.brief, ADVICE_BRIEF_MAX_CHARS) if brief else "",
                    view_verdict=(
                        brief.view.verdict.value
                        if brief is not None and brief.view is not None
                        else None
                    ),
                    view_declined=(
                        brief.view_declined if brief is not None and brief.view_declined else None
                    ),
                    latency_ms=seat.latency_ms,
                )
            )
        rendered.append(
            AdviceEpisode(
                created_at=episode.created_at,
                stream=episode.stream.value,
                seats=tuple(seats),
            )
        )
    return TeacherAdviceDocument(
        generated_at=now,
        episodes=tuple(rendered),
        absence_counts=absence_counts,
    )


def build_publish_command(
    config: TeacherConfig,
    payload: bytes,
    destination: str,
) -> list[str]:
    """Build the one-way publish command line over the existing channel.

    Args:
        config: The harness configuration carrying the pull target (the
            same instance, zone, and remote python the read-only window
            pull uses).
        payload: The canonical serialized document.
        destination: The validated absolute destination path.

    Returns:
        The argv: the fixed validation-and-write script base64-piped into
        the remote python under sudo, with the payload and destination on
        its argv - never interpolated into the script text.
    """
    pull = config.pull
    gcloud = pull.gcloud_binary.strip() or shutil.which("gcloud") or DEFAULT_GCLOUD_PATH
    encoded_script = base64.b64encode(PUBLISH_REMOTE_SCRIPT.encode("utf-8")).decode("ascii")
    encoded_payload = base64.b64encode(payload).decode("ascii")
    remote = (
        f"echo {encoded_script} | base64 -d | sudo {shlex.quote(pull.remote_python)} - "
        f"{shlex.quote(encoded_payload)} {shlex.quote(destination)}"
    )
    return [
        gcloud,
        "compute",
        "ssh",
        pull.instance,
        "--zone",
        pull.zone,
        "--quiet",
        "--command",
        remote,
    ]


def publish_advice(
    config: TeacherConfig,
    episodes: Sequence[TeacherEpisode],
    now: datetime | None = None,
    destination: str = DEFAULT_ADVICE_DESTINATION,
    transport: PublishTransport | None = None,
    error_stream: TextIO | None = None,
) -> bool:
    """Build, bound-check, and publish the advice artifact.

    Args:
        config: The harness configuration.
        episodes: The corpus episodes.
        now: The generation instant; None reads the wall clock.
        destination: The absolute destination path on the box.
        transport: The invocation boundary; None uses subprocess.
        error_stream: Where failures warn.

    Returns:
        Whether the artifact was published. Oversized documents, transport
        failures, and remote refusals all warn and return False; nothing
        on the box changes on a refusal (the remote script never touches
        the destination before every validation passes).
    """
    moment = now if now is not None else datetime.now(UTC)
    stream = error_stream if error_stream is not None else sys.stderr
    if not destination.startswith("/") or ".." in destination.split("/"):
        print(
            f"advice publish refused: destination must be absolute without .., not {destination!r}",
            file=stream,
        )
        return False
    document = build_advice_document(episodes, moment)
    payload = json.dumps(document.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    if len(payload) > ADVICE_MAX_JSON_BYTES:
        print(
            f"advice publish refused: document is {len(payload)} bytes over the "
            f"{ADVICE_MAX_JSON_BYTES} bound",
            file=stream,
        )
        return False
    command = build_publish_command(config, payload, destination)
    runner = transport if transport is not None else SubprocessPublishTransport()
    exit_code, stdout, stderr = runner.run(command)
    if exit_code != 0:
        detail = (stderr or stdout or "").strip().splitlines()[-1:] or ["unknown"]
        print(
            f"advice publish failed (exit {exit_code}): {detail[0]}",
            file=stream,
        )
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """Publish one bounded teacher-advice artifact to the box.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on a clean publish, one on
        configuration, corpus, or transport failures.
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-teacher-publish",
        description=(
            "Publish one bounded, secret-free teacher-advice artifact to the "
            "production box for the daily digest: the most recent episodes' "
            "outcomes and capped briefs with full provenance, atomically "
            "written beside the audit store over the existing authenticated "
            "channel. One-way advisory evidence only - nothing on the box "
            "executes it, and no secrets ride along."
        ),
    )
    parser.add_argument(
        "--destination",
        default=DEFAULT_ADVICE_DESTINATION,
        help=(
            "The absolute destination path on the box (default "
            f"{DEFAULT_ADVICE_DESTINATION}); validated, never interpolated "
            "into the remote script."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the document and command shape without touching the box.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="The JSON configuration file; default resolves the standard path.",
    )
    arguments = parser.parse_args(argv)
    try:
        config = load_teacher_config(arguments.config or resolve_config_path(os.environ))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    corpus_dir = resolve_corpus_dir(config, os.environ)
    episodes, malformed = load_episodes_with_skips(corpus_dir / "corpus.jsonl")
    if malformed:
        print(f"note: {malformed} malformed corpus line(s) skipped", file=sys.stderr)
    if arguments.dry_run:
        document = build_advice_document(episodes, datetime.now(UTC))
        print(json.dumps(document.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    if not publish_advice(config, episodes, destination=arguments.destination):
        return 1
    print("teacher advice published")
    return 0


__all__ = [
    "ADVICE_BRIEF_MAX_CHARS",
    "ADVICE_MAX_EPISODES",
    "ADVICE_MAX_JSON_BYTES",
    "ADVICE_STALE_HOURS",
    "DEFAULT_ADVICE_DESTINATION",
    "PUBLISH_REMOTE_SCRIPT",
    "SubprocessPublishTransport",
    "TeacherAdviceDocument",
    "AdviceEpisode",
    "AdviceSeat",
    "TEACHER_ADVICE_SCHEMA",
    "build_advice_document",
    "build_publish_command",
    "main",
    "publish_advice",
]
