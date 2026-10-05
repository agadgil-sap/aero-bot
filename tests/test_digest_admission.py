"""Pin the daily digest's durable rolling-24-hour send admission.

The captain's 2026-10-05 ruling allows one email per rolling 24 hours, and
the admission marker is the durable backstop behind the digest-mode
suppression: manual invocation, timer catch-up after downtime, concurrent
starts, a restart mid-send, and ambiguous provider failures must each land
on the same answer - at most one send per window, never a duplicate.
"""

import fcntl
import io
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from test_alerts import FakeTransport
from test_digest import RESEND_ENV, seeded_digest_store

from aero_bot.alerts import AlertTransportDefiniteError, AlertTransportError
from aero_bot.digest import deliver_daily_digest
from aero_bot.digest_admission import (
    DIGEST_SEND_INTERVAL,
    OUTCOME_ACCEPTED,
    OUTCOME_AMBIGUOUS,
    OUTCOME_REJECTED,
    STATE_FILE_MODE,
    STATE_FILE_NAME,
    DigestSendStateError,
    SendAdmission,
    claim_send_admission,
    record_send_outcome,
)

DAY_ONE = datetime(2026, 10, 6, 22, 0, 0, tzinfo=UTC)  # 09:00 Melbourne (AEDT)


def _read_marker(state_path: Path) -> dict[str, object]:
    """Read the marker as a checked JSON object."""
    document: dict[str, object] = json.loads(state_path.read_text(encoding="utf-8"))
    return document


def _write_marker(state_path: Path, attempt_at: datetime, outcome: str) -> None:
    state_path.write_text(
        json.dumps({"version": 1, "attempt_at": attempt_at.isoformat(), "outcome": outcome}),
        encoding="utf-8",
    )


class TestClaimSemantics:
    """The rolling window is a plain 24-hour span between UTC instants."""

    def test_a_missing_marker_admits_and_reserves(self, tmp_path: Path) -> None:
        """An absent marker admits and writes the conservative reservation."""
        state_path = tmp_path / STATE_FILE_NAME
        admission = claim_send_admission(state_path, DAY_ONE)
        assert admission.admitted is True
        assert admission.attempt_at == DAY_ONE
        marker = _read_marker(state_path)
        assert marker == {"version": 1, "attempt_at": DAY_ONE.isoformat(), "outcome": "ambiguous"}

    def test_the_second_claim_inside_the_window_refuses(self, tmp_path: Path) -> None:
        """A second claim inside the window refuses and names the closing instant."""
        state_path = tmp_path / STATE_FILE_NAME
        claim_send_admission(state_path, DAY_ONE)
        refusal = claim_send_admission(state_path, DAY_ONE + timedelta(hours=13))
        assert refusal.admitted is False
        assert refusal.attempt_at == DAY_ONE
        assert "24-hour" in refusal.reason
        assert DAY_ONE.isoformat() in refusal.reason

    def test_the_window_opens_again_at_exactly_24_hours(self, tmp_path: Path) -> None:
        """The window reopens at exactly the 24-hour boundary."""
        state_path = tmp_path / STATE_FILE_NAME
        claim_send_admission(state_path, DAY_ONE)
        edge = claim_send_admission(state_path, DAY_ONE + DIGEST_SEND_INTERVAL)
        assert edge.admitted is True
        assert edge.attempt_at == DAY_ONE + DIGEST_SEND_INTERVAL

    def test_one_second_short_of_24_hours_still_refuses(self, tmp_path: Path) -> None:
        """One second short of 24 hours still refuses."""
        state_path = tmp_path / STATE_FILE_NAME
        claim_send_admission(state_path, DAY_ONE)
        refusal = claim_send_admission(
            state_path, DAY_ONE + DIGEST_SEND_INTERVAL - timedelta(seconds=1)
        )
        assert refusal.admitted is False

    def test_a_rejected_outcome_releases_the_window(self, tmp_path: Path) -> None:
        """A provably undelivered attempt releases the window for a retry."""
        state_path = tmp_path / STATE_FILE_NAME
        admission = claim_send_admission(state_path, DAY_ONE)
        assert admission.attempt_at is not None
        record_send_outcome(state_path, admission.attempt_at, OUTCOME_REJECTED)
        retry = claim_send_admission(state_path, DAY_ONE + timedelta(minutes=5))
        assert retry.admitted is True

    def test_an_accepted_outcome_keeps_the_window_closed(self, tmp_path: Path) -> None:
        """An accepted send keeps the window closed for the full 24 hours."""
        state_path = tmp_path / STATE_FILE_NAME
        admission = claim_send_admission(state_path, DAY_ONE)
        assert admission.attempt_at is not None
        record_send_outcome(state_path, admission.attempt_at, OUTCOME_ACCEPTED)
        assert claim_send_admission(state_path, DAY_ONE + timedelta(hours=23)).admitted is False
        assert claim_send_admission(state_path, DAY_ONE + timedelta(hours=25)).admitted is True

    def test_an_ambiguous_outcome_keeps_the_window_closed(self, tmp_path: Path) -> None:
        """An ambiguous outcome keeps the window closed exactly like an accepted one."""
        state_path = tmp_path / STATE_FILE_NAME
        claim_send_admission(state_path, DAY_ONE)  # the reservation is ambiguous
        assert claim_send_admission(state_path, DAY_ONE + timedelta(hours=23)).admitted is False
        assert claim_send_admission(state_path, DAY_ONE + timedelta(hours=24)).admitted is True

    def test_the_marker_is_private_regardless_of_umask(self, tmp_path: Path) -> None:
        """The marker lands 0600 whatever the caller's umask."""
        state_path = tmp_path / STATE_FILE_NAME
        old_umask = os.umask(0o022)
        try:
            claim_send_admission(state_path, DAY_ONE)
        finally:
            os.umask(old_umask)
        assert (state_path.stat().st_mode & 0o777) == STATE_FILE_MODE


class TestPersistenceAndRestart:
    """The window survives process restarts because it lives on disk."""

    def test_a_fresh_process_reads_the_standing_window(self, tmp_path: Path) -> None:
        """A fresh process reads the same standing window from disk."""
        state_path = tmp_path / STATE_FILE_NAME
        claim_send_admission(state_path, DAY_ONE)
        # A new process (a new claim call, the same marker) one hour later.
        assert claim_send_admission(state_path, DAY_ONE + timedelta(hours=1)).admitted is False

    def test_a_crash_between_reservation_and_send_still_blocks(self, tmp_path: Path) -> None:
        """A crash between the reservation and the send leaves the window closed."""
        # The reservation is written before the transport fires, so the
        # send never happening (a crash) leaves the ambiguous outcome
        # standing and the window closed.
        state_path = tmp_path / STATE_FILE_NAME
        claim_send_admission(state_path, DAY_ONE)
        assert claim_send_admission(state_path, DAY_ONE + timedelta(minutes=30)).admitted is False


class TestConcurrentStarts:
    """Concurrent claims serialize through the exclusive lock."""

    def test_two_immediate_claims_admit_exactly_one(self, tmp_path: Path) -> None:
        """Two immediate claims admit exactly one send."""
        state_path = tmp_path / STATE_FILE_NAME
        first = claim_send_admission(state_path, DAY_ONE)
        second = claim_send_admission(state_path, DAY_ONE + timedelta(seconds=1))
        assert first.admitted is True
        assert second.admitted is False

    def test_a_claim_blocked_on_the_lock_sees_the_winners_reservation(self, tmp_path: Path) -> None:
        """A claim that waited on the lock reads the winner's reservation."""
        state_path = tmp_path / STATE_FILE_NAME
        lock_path = state_path.parent / f"{state_path.name}.lock"
        # Hold the admission lock exactly as a concurrent claimant would.
        handle = os.open(lock_path, os.O_CREAT | os.O_RDWR, STATE_FILE_MODE)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            outcome: dict[str, SendAdmission] = {}

            def claim() -> None:
                outcome["admission"] = claim_send_admission(
                    state_path, DAY_ONE + timedelta(hours=1)
                )

            thread = threading.Thread(target=claim)
            thread.start()
            # While the lock is held the claim cannot finish: seed the
            # winner's reservation, then release so the claim proceeds and
            # must read it rather than admitting a second send.
            _write_marker(state_path, DAY_ONE, OUTCOME_AMBIGUOUS)
            fcntl.flock(handle, fcntl.LOCK_UN)
            thread.join(timeout=10)
        finally:
            os.close(handle)
        assert not thread.is_alive(), "the claim must not deadlock, only wait"
        admission = outcome["admission"]
        assert admission.admitted is False
        assert admission.attempt_at == DAY_ONE


class TestDstBoundaries:
    """The window needs no local-time special case; the ticks prove it."""

    def test_the_spring_forward_morning_defers_to_the_next_tick(self, tmp_path: Path) -> None:
        """The spring-forward tick 23 real hours later defers; the next tick sends."""
        # Melbourne springs forward on the first Sunday of October: 09:00
        # AEST October 2 is 23:00 UTC October 1; 09:00 AEDT October 3 is
        # 22:00 UTC October 2 - twenty-three real hours later, one short of
        # the window, so that morning defers to the following tick.
        state_path = tmp_path / STATE_FILE_NAME
        last_send = datetime(2027, 10, 1, 23, 0, 0, tzinfo=UTC)
        _write_marker(state_path, last_send, OUTCOME_ACCEPTED)
        spring_forward_tick = datetime(2027, 10, 2, 22, 0, 0, tzinfo=UTC)
        refusal = claim_send_admission(state_path, spring_forward_tick)
        assert refusal.admitted is False
        assert "until" in refusal.reason
        next_tick = datetime(2027, 10, 3, 22, 0, 0, tzinfo=UTC)
        assert claim_send_admission(state_path, next_tick).admitted is True

    def test_the_fall_back_morning_still_sends(self, tmp_path: Path) -> None:
        """The fall-back tick 25 real hours later still sends."""
        # Melbourne falls back on the first Sunday of April: 09:00 AEDT
        # April 3 is 22:00 UTC April 2; 09:00 AEST April 4 is 23:00 UTC
        # April 3 - twenty-five real hours later, comfortably inside a new
        # window, so that morning still sends.
        state_path = tmp_path / STATE_FILE_NAME
        last_send = datetime(2027, 4, 2, 22, 0, 0, tzinfo=UTC)
        _write_marker(state_path, last_send, OUTCOME_ACCEPTED)
        fall_back_tick = datetime(2027, 4, 3, 23, 0, 0, tzinfo=UTC)
        assert claim_send_admission(state_path, fall_back_tick).admitted is True


class TestCorruptMarker:
    """An unreadable marker refuses sends instead of guessing."""

    def test_corrupt_json_refuses(self, tmp_path: Path) -> None:
        """Corrupt JSON refuses rather than guessing."""
        state_path = tmp_path / STATE_FILE_NAME
        state_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(DigestSendStateError):
            claim_send_admission(state_path, DAY_ONE)

    def test_a_naive_timestamp_refuses(self, tmp_path: Path) -> None:
        """A timezone-less timestamp refuses rather than guessing the zone."""
        state_path = tmp_path / STATE_FILE_NAME
        state_path.write_text(
            json.dumps({"version": 1, "attempt_at": "2026-10-06T22:00:00", "outcome": "accepted"}),
            encoding="utf-8",
        )
        with pytest.raises(DigestSendStateError):
            claim_send_admission(state_path, DAY_ONE)

    def test_an_unknown_outcome_refuses(self, tmp_path: Path) -> None:
        """An unknown outcome value refuses."""
        state_path = tmp_path / STATE_FILE_NAME
        _write_marker(state_path, DAY_ONE, "maybe")
        with pytest.raises(DigestSendStateError):
            claim_send_admission(state_path, DAY_ONE)

    def test_a_wrong_schema_version_refuses(self, tmp_path: Path) -> None:
        """A wrong schema version refuses."""
        state_path = tmp_path / STATE_FILE_NAME
        state_path.write_text(
            json.dumps({"version": 2, "attempt_at": DAY_ONE.isoformat(), "outcome": "accepted"}),
            encoding="utf-8",
        )
        with pytest.raises(DigestSendStateError):
            claim_send_admission(state_path, DAY_ONE)

    def test_deleting_the_marker_re_admits(self, tmp_path: Path) -> None:
        """Deleting the marker is the documented reset."""
        state_path = tmp_path / STATE_FILE_NAME
        claim_send_admission(state_path, DAY_ONE)
        state_path.unlink()
        assert claim_send_admission(state_path, DAY_ONE + timedelta(minutes=1)).admitted is True


class TestDeliverIntegration:
    """deliver_daily_digest rides the admission on the real send path."""

    def test_the_daily_tick_sends_once_per_rolling_window(self, tmp_path: Path) -> None:
        """The daily tick sends once; a later manual run never duplicates."""
        store = seeded_digest_store(tmp_path)
        state_path = tmp_path / STATE_FILE_NAME
        with patch("aero_bot.digest.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert (
                deliver_daily_digest(store, RESEND_ENV, now=DAY_ONE, state_path=state_path) is True
            )
            # A manual invocation thirteen hours later never duplicates.
            assert (
                deliver_daily_digest(
                    store,
                    RESEND_ENV,
                    now=DAY_ONE + timedelta(hours=13),
                    state_path=state_path,
                )
                is False
            )
        assert len(transport.sent) == 1
        assert _read_marker(state_path)["outcome"] == OUTCOME_ACCEPTED

    def test_the_next_daily_tick_sends_again(self, tmp_path: Path) -> None:
        """The next daily tick 24 hours later sends again."""
        store = seeded_digest_store(tmp_path)
        state_path = tmp_path / STATE_FILE_NAME
        with patch("aero_bot.digest.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            deliver_daily_digest(store, RESEND_ENV, now=DAY_ONE, state_path=state_path)
            assert (
                deliver_daily_digest(
                    store,
                    RESEND_ENV,
                    now=DAY_ONE + DIGEST_SEND_INTERVAL,
                    state_path=state_path,
                )
                is True
            )
        assert len(transport.sent) == 2

    def test_an_ambiguous_failure_never_retries_into_a_duplicate(self, tmp_path: Path) -> None:
        """An ambiguous failure keeps the window closed; no resend inside 24 hours."""
        store = seeded_digest_store(tmp_path)
        state_path = tmp_path / STATE_FILE_NAME
        errors = io.StringIO()
        with patch("aero_bot.digest.build_alert_transport") as builder:
            builder.return_value = FakeTransport(
                failure=AlertTransportError("the read operation timed out")
            )
            assert (
                deliver_daily_digest(
                    store,
                    RESEND_ENV,
                    error_stream=errors,
                    now=DAY_ONE,
                    state_path=state_path,
                )
                is False
            )
        assert "never resent" in errors.getvalue()
        assert _read_marker(state_path)["outcome"] == OUTCOME_AMBIGUOUS
        # The window stays closed through the ambiguous attempt...
        assert claim_send_admission(state_path, DAY_ONE + timedelta(hours=23)).admitted is False
        # ...and reopens only at 24 hours.
        assert claim_send_admission(state_path, DAY_ONE + DIGEST_SEND_INTERVAL).admitted is True

    def test_a_definite_failure_releases_the_window_for_the_next_tick(self, tmp_path: Path) -> None:
        """A definite rejection releases the window for the next tick."""
        store = seeded_digest_store(tmp_path)
        state_path = tmp_path / STATE_FILE_NAME
        errors = io.StringIO()
        with patch("aero_bot.digest.build_alert_transport") as builder:
            builder.return_value = FakeTransport(
                failure=AlertTransportDefiniteError("answered 429")
            )
            assert (
                deliver_daily_digest(
                    store,
                    RESEND_ENV,
                    error_stream=errors,
                    now=DAY_ONE,
                    state_path=state_path,
                )
                is False
            )
        assert "answered 429" in errors.getvalue()
        assert _read_marker(state_path)["outcome"] == OUTCOME_REJECTED
        assert claim_send_admission(state_path, DAY_ONE + timedelta(minutes=1)).admitted is True

    def test_a_corrupt_marker_refuses_without_firing_the_transport(self, tmp_path: Path) -> None:
        """A corrupt marker refuses without firing the transport."""
        store = seeded_digest_store(tmp_path)
        state_path = tmp_path / STATE_FILE_NAME
        state_path.write_text("{broken", encoding="utf-8")
        errors = io.StringIO()
        with patch("aero_bot.digest.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert (
                deliver_daily_digest(
                    store,
                    RESEND_ENV,
                    error_stream=errors,
                    now=DAY_ONE,
                    state_path=state_path,
                )
                is False
            )
        assert transport.sent == []
        assert "unreadable" in errors.getvalue()

    def test_provider_none_never_consumes_the_window(self, tmp_path: Path) -> None:
        """Provider none never consumes the window."""
        store = seeded_digest_store(tmp_path)
        state_path = tmp_path / STATE_FILE_NAME
        assert deliver_daily_digest(store, {}, now=DAY_ONE, state_path=state_path) is False
        assert not state_path.exists()

    def test_an_unreadable_store_never_consumes_the_window(self, tmp_path: Path) -> None:
        """An unreadable audit window never consumes the admission."""

        class BrokenStore:
            def read_records(self, limit: int = 100, *, offset: int = 0) -> tuple[()]:
                raise OSError("disk unavailable")

        state_path = tmp_path / STATE_FILE_NAME
        errors = io.StringIO()
        assert (
            deliver_daily_digest(
                BrokenStore(),
                RESEND_ENV,
                error_stream=errors,
                now=DAY_ONE,
                state_path=state_path,
            )
            is False
        )
        assert "disk unavailable" in errors.getvalue()
        assert not state_path.exists()

    def test_without_a_state_path_the_send_stays_unguarded(self, tmp_path: Path) -> None:
        """Without a state path the send keeps its pre-marker behavior."""
        # The parameter stays optional: composition-only callers (and the
        # pre-marker behavior) send exactly as before.
        store = seeded_digest_store(tmp_path)
        with patch("aero_bot.digest.build_alert_transport") as builder:
            transport = FakeTransport()
            builder.return_value = transport
            assert deliver_daily_digest(store, RESEND_ENV, now=DAY_ONE) is True
            assert deliver_daily_digest(store, RESEND_ENV, now=DAY_ONE) is True
        assert len(transport.sent) == 2
