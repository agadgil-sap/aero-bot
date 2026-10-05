"""Durable rolling-window send admission for the daily digest.

The captain's 2026-10-05 ruling allows exactly one email per rolling 24
hours, and the digest is the one email that remains. Suppression of every
other path is configuration (``AERO_BOT_ALERT_MODE=digest``, sealed in the
cycle environment); this module is the independent, durable backstop that
makes the one remaining send itself safe against every duplicate vector:
manual invocation, timer catch-up after downtime, concurrent starts, a
restart mid-send, and a provider exchange that fails ambiguously after the
request left the box.

The admission is one small JSON marker beside the audit store
(``digest_send_state.json``, overridable through the cycle's
``AERO_BOT_DIGEST_STATE_PATH``) guarded by an exclusive ``flock`` on a
sibling lock file whose inode is never replaced. A claim either reserves
the rolling window by writing the attempt instant with the conservative
``ambiguous`` outcome BEFORE the transport fires, or refuses with the
honest closing instant. Outcomes then settle three ways:

- ``accepted``: the provider answered success; the window stays closed for
  24 hours from the attempt.
- ``ambiguous``: the exchange failed in a way that cannot prove the email
  was not delivered (a timeout, a dropped connection mid-exchange, a crash
  between the reservation and the send). The window stays closed - a
  possibly-delivered digest is never retried into a duplicate.
- ``rejected``: the transport proved the request never delivered (the
  provider answered a nonzero status, or the connection never established).
  The reservation is released so the next daily tick retries cleanly.

DST needs no special case because the window is a fixed 24-hour span
between UTC instants: the one visible effect is the spring-forward morning
whose 09:00 Melbourne tick lands 23 real hours after the previous send and
defers to the following day's tick (the journal carries the closing
instant; local logging, the audit chain, and every protection are
unaffected). An unparseable marker refuses every send until an operator
inspects or removes it - a reset is deleting the marker and lock files,
never editing them.
"""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# The rolling window between digest sends: exactly one email per 24 hours.
DIGEST_SEND_INTERVAL = timedelta(hours=24)
# The durable marker's file name beside the audit store.
STATE_FILE_NAME = "digest_send_state.json"
# The lock file's suffix; its inode is never replaced, so the lock stays
# honest across the marker's atomic rewrites.
LOCK_FILE_SUFFIX = ".lock"
# The marker schema version; a mismatched version is unparseable, not wrong.
STATE_VERSION = 1
# The marker is private: the service state directory is 0700, and the file
# itself is created 0600 regardless of the caller's umask.
STATE_FILE_MODE = 0o600

# The outcome recorded while the send's result is still unknown: the
# conservative reservation that stands through crashes and ambiguous
# failures so a possibly-delivered digest is never resent.
OUTCOME_AMBIGUOUS = "ambiguous"
# The provider accepted the send; the window stays closed.
OUTCOME_ACCEPTED = "accepted"
# The transport proved the request never delivered; the window reopens.
OUTCOME_REJECTED = "rejected"

_ADMITTED_REASON = "no prior digest send is recorded; sending and reserving the window"


class DigestSendStateError(RuntimeError):
    """Signal that the durable digest send marker is present but unreadable."""


@dataclass(frozen=True)
class SendAdmission:
    """Carry one admission decision for the digest send.

    Attributes:
        admitted: Whether the caller may send under the rolling window.
        reason: The honest one-line justification for the journal.
        attempt_at: The instant the window is measured from; the claim's
            reservation instant when admitted, the standing reservation
            otherwise. None only when no marker exists and the claim
            nonetheless admitted (never in practice once written).
    """

    admitted: bool
    reason: str
    attempt_at: datetime | None


def _parse_instant(raw: object) -> datetime:
    """Parse one ISO-8601 instant from the marker; naive stamps are corrupt."""
    if not isinstance(raw, str):
        raise DigestSendStateError(f"the timestamp entry is not a string: {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise DigestSendStateError(f"the timestamp entry is unparseable: {raw!r}") from error
    if parsed.tzinfo is None:
        raise DigestSendStateError(f"the timestamp entry carries no timezone: {raw!r}")
    return parsed


def _parse_state(raw: str) -> dict[str, object]:
    """Validate the marker's exact schema; any deviation is unreadable."""
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise DigestSendStateError(f"the marker is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise DigestSendStateError("the marker is not a JSON object")
    if document.get("version") != STATE_VERSION:
        raise DigestSendStateError(f"the marker's schema version is not {STATE_VERSION}")
    outcome = document.get("outcome")
    if outcome not in {OUTCOME_AMBIGUOUS, OUTCOME_ACCEPTED, OUTCOME_REJECTED}:
        raise DigestSendStateError(f"the marker's outcome is unknown: {outcome!r}")
    _parse_instant(document.get("attempt_at"))
    return document


def _read_state(state_path: Path) -> dict[str, object] | None:
    """Read the marker; None when absent, corrupt raises for the caller to refuse."""
    try:
        raw = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return _parse_state(raw)


def _write_state(state_path: Path, attempt_at: datetime, outcome: str) -> None:
    """Write the marker atomically at private permissions."""
    document = {"version": STATE_VERSION, "attempt_at": attempt_at.isoformat(), "outcome": outcome}
    lock_path = _lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(f"{state_path.name}.tmp-{os.getpid()}")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, STATE_FILE_MODE)
    os.replace(temporary, state_path)
    # The directory entry must outlive a crash for the reservation to stand.
    directory = os.open(state_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _lock_path(state_path: Path) -> Path:
    """Resolve the sibling lock file whose inode is never replaced."""
    return state_path.with_name(f"{state_path.name}{LOCK_FILE_SUFFIX}")


@contextmanager
def _locked(state_path: Path) -> Iterator[None]:
    """Hold the exclusive admission lock across one marker decision."""
    lock_path = _lock_path(state_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        os.chmod(lock_path, STATE_FILE_MODE)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def claim_send_admission(state_path: Path, now: datetime) -> SendAdmission:
    """Reserve the rolling 24-hour window or refuse with the honest reason.

    The decision and the reservation are one atomic step under the
    exclusive lock: two concurrent claims serialize, the loser reads the
    winner's fresh reservation, and no window can admit twice. The
    reservation is written with the conservative ambiguous outcome before
    the caller's transport fires, so a crash between this claim and the
    send still suppresses the next 24 hours.

    Args:
        state_path: The durable marker's path beside the audit store.
        now: The admission instant; a wall-clock UTC datetime in practice.

    Returns:
        The admission decision with its journal line.

    Raises:
        DigestSendStateError: If the marker exists but cannot be parsed;
            sends must refuse until an operator resets it.
        OSError: If the marker cannot be read or written.
    """
    with _locked(state_path):
        state = _read_state(state_path)
        if state is not None and state.get("outcome") != OUTCOME_REJECTED:
            attempt_at = _parse_instant(state.get("attempt_at"))
            closes_at = attempt_at + DIGEST_SEND_INTERVAL
            if now < closes_at:
                return SendAdmission(
                    admitted=False,
                    reason=(
                        f"the rolling 24-hour digest window stands open until "
                        f"{closes_at.astimezone(UTC).isoformat()} "
                        f"(last attempt {attempt_at.astimezone(UTC).isoformat()}, "
                        f"outcome {state.get('outcome')})"
                    ),
                    attempt_at=attempt_at,
                )
        _write_state(state_path, now, OUTCOME_AMBIGUOUS)
        return SendAdmission(
            admitted=True,
            reason=_ADMITTED_REASON,
            attempt_at=now,
        )


def record_send_outcome(state_path: Path, attempt_at: datetime, outcome: str) -> None:
    """Settle the standing reservation's outcome after the transport fired.

    Keeps the claim's reservation instant (a marker removed mid-send is
    rewritten with the original instant, never a fresh window) and only
    ever rewrites the outcome field.

    Args:
        state_path: The durable marker's path beside the audit store.
        attempt_at: The claim's reservation instant.
        outcome: One of the three outcome constants.

    Raises:
        DigestSendStateError: If the marker exists but cannot be parsed.
        OSError: If the marker cannot be read or written.
        ValueError: If the outcome is not one of the three constants.
    """
    if outcome not in {OUTCOME_AMBIGUOUS, OUTCOME_ACCEPTED, OUTCOME_REJECTED}:
        raise ValueError(f"unknown digest send outcome: {outcome!r}")
    with _locked(state_path):
        _write_state(state_path, attempt_at, outcome)


__all__ = [
    "DIGEST_SEND_INTERVAL",
    "DigestSendStateError",
    "OUTCOME_ACCEPTED",
    "OUTCOME_AMBIGUOUS",
    "OUTCOME_REJECTED",
    "SendAdmission",
    "STATE_FILE_NAME",
    "STATE_FILE_MODE",
    "STATE_VERSION",
    "claim_send_admission",
    "record_send_outcome",
]
