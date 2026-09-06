"""Behavior tests for immutable local SQLite audit persistence."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from aero_bot.audit import (
    DATABASE_DIRECTORY_MODE,
    DATABASE_FILE_MODE,
    GENESIS_HASH,
    AuditEventType,
    AuditStore,
    AuditVerificationStatus,
)

# Fixed aware time makes hash and ordering behavior deterministic.
CREATED_AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class FixturePayload(BaseModel):
    """Provide a small validated payload for audit behavior tests."""

    # Decision is a non-sensitive fixture outcome.
    decision: str
    # Score proves numeric payload values retain canonical representation.
    score: int


class SensitivePayload(BaseModel):
    """Model a prohibited credential-shaped payload field."""

    # Private key field must be rejected regardless of its fixture value.
    private_key: str


def fixture_payload(score: int = 1) -> FixturePayload:
    """Build one deterministic non-sensitive audit payload.

    Args:
        score: Numeric fixture value distinguishing appended records.

    Returns:
        Validated payload safe for durable audit tests.
    """
    return FixturePayload(decision="hold", score=score)


def test_new_store_is_empty_and_uses_restrictive_permissions(tmp_path: Path) -> None:
    """Initialization creates a versioned private database with an empty valid chain."""
    # Dedicated subdirectory proves the store creates and tightens its parent path.
    database_path = tmp_path / "private-audit" / "audit.sqlite3"
    # Initialization is the only operation needed to create schema and triggers.
    store = AuditStore(database_path)

    assert store.database_path == database_path.resolve()
    assert store.verify_chain().status is AuditVerificationStatus.EMPTY
    assert store.verify_chain().record_count == 0
    assert database_path.stat().st_mode & 0o777 == DATABASE_FILE_MODE
    assert database_path.parent.stat().st_mode & 0o777 == DATABASE_DIRECTORY_MODE


def test_existing_broad_parent_fails_without_changing_its_mode(tmp_path: Path) -> None:
    """Initialization rejects a shared parent rather than changing unrelated permissions."""
    # Broad fixture directory models an operator accidentally selecting shared storage.
    broad_parent = tmp_path / "shared"
    # Explicit mode change remains safe because the fixture is isolated under tmp_path.
    broad_parent.mkdir(mode=0o755)
    broad_parent.chmod(0o755)

    with pytest.raises(ValueError, match="only to the current user"):
        AuditStore(broad_parent / "audit.sqlite3")

    assert broad_parent.stat().st_mode & 0o777 == 0o755


def test_append_builds_canonical_persistent_hash_chain(tmp_path: Path) -> None:
    """Sequential model events persist canonically and link to exact predecessors."""
    # One local file is reopened after writes to verify durable rather than in-memory behavior.
    database_path = tmp_path / "audit.sqlite3"
    # Initial store performs two independent append transactions.
    store = AuditStore(database_path)
    # First record becomes the genesis-linked event.
    first = store.append(AuditEventType.RISK_DECISION, fixture_payload(1), CREATED_AT)
    # Second record links to the first at a distinct event time.
    second = store.append(
        AuditEventType.TRANSACTION_PLAN,
        fixture_payload(2),
        CREATED_AT + timedelta(seconds=1),
    )
    # Reopened store proves schema initialization is idempotent and rows are durable.
    reopened_store = AuditStore(database_path)
    # Bounded read returns validated records in ascending sequence.
    records = reopened_store.read_records()

    assert first.sequence == 1
    assert first.previous_hash == GENESIS_HASH
    assert first.payload_json == '{"decision":"hold","score":1}'
    assert second.sequence == 2
    assert second.previous_hash == first.record_hash
    assert records == (first, second)
    assert reopened_store.verify_chain().status is AuditVerificationStatus.VERIFIED
    assert reopened_store.verify_chain().record_count == 2


def test_update_and_delete_are_blocked_by_database_triggers(tmp_path: Path) -> None:
    """Ordinary direct SQL cannot alter or erase an appended record."""
    # Store creates strict append-only triggers and one target record.
    store = AuditStore(tmp_path / "audit.sqlite3")
    store.append(AuditEventType.SYSTEM_STATE, fixture_payload(), CREATED_AT)
    # External connection models accidental maintenance SQL outside the store class.
    connection = sqlite3.connect(store.database_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit_records SET event_type = 'system_state'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_records")
    finally:
        connection.close()

    assert store.verify_chain().status is AuditVerificationStatus.VERIFIED


def test_hash_verification_detects_privileged_tampering(tmp_path: Path) -> None:
    """Hash verification reports the first changed row even if guards are removed."""
    # Two records make both direct content and successor-link integrity observable.
    store = AuditStore(tmp_path / "audit.sqlite3")
    store.append(AuditEventType.RISK_DECISION, fixture_payload(1), CREATED_AT)
    store.append(AuditEventType.RISK_DECISION, fixture_payload(2), CREATED_AT)
    # Privileged external writer removes one trigger to model file-level compromise.
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute("DROP TRIGGER audit_records_no_update")
        connection.execute(
            "UPDATE audit_records SET payload_json = ? WHERE sequence = 1",
            ('{"decision":"eligible","score":1}',),
        )
        connection.commit()
    finally:
        connection.close()

    # Verification fails at the directly modified first record before trusting its successor.
    verification = store.verify_chain()
    assert verification.status is AuditVerificationStatus.CORRUPT
    assert verification.first_bad_sequence == 1
    assert "record hash" in verification.diagnostic


def test_concurrent_appends_remain_contiguous_and_verifiable(tmp_path: Path) -> None:
    """Immediate transactions serialize concurrent local writers without lost events."""
    # One shared file receives appends from independent short-lived SQLite connections.
    store = AuditStore(tmp_path / "audit.sqlite3")

    def append_score(score: int) -> int:
        """Append one thread-specific payload and return its allocated sequence.

        Args:
            score: Unique fixture number written by this worker.

        Returns:
            Contiguous sequence allocated under the immediate transaction lock.
        """
        # Each writer supplies a unique timestamp and payload without shared mutable state.
        record = store.append(
            AuditEventType.SYSTEM_STATE,
            fixture_payload(score),
            CREATED_AT + timedelta(seconds=score),
        )
        return record.sequence

    # Eight workers overlap connection and transaction acquisition against one store.
    with ThreadPoolExecutor(max_workers=8) as executor:
        # Executor result collection waits for every append to finish or raise.
        sequences = tuple(executor.map(append_score, range(1, 17)))

    assert sorted(sequences) == list(range(1, 17))
    assert store.verify_chain().status is AuditVerificationStatus.VERIFIED
    assert store.verify_chain().record_count == 16


def test_secret_shaped_fields_and_naive_timestamps_are_rejected(tmp_path: Path) -> None:
    """Credential-bearing schemas and ambiguous event times never reach SQLite."""
    # Empty initialized store must remain unchanged after both rejected append attempts.
    store = AuditStore(tmp_path / "audit.sqlite3")
    # Credential key is rejected even though the test value is not a real private key.
    with pytest.raises(ValueError, match="prohibited by secret policy"):
        store.append(
            AuditEventType.SYSTEM_STATE,
            SensitivePayload(private_key="fixture-only"),  # noqa: S106
            CREATED_AT,
        )
    # Naive time cannot support an unambiguous immutable audit record.
    with pytest.raises(ValueError, match="timezone-aware"):
        store.append(
            AuditEventType.SYSTEM_STATE,
            fixture_payload(),
            datetime(2026, 9, 6, 12, 0),
        )

    assert store.verify_chain().status is AuditVerificationStatus.EMPTY


@pytest.mark.parametrize("limit", [0, 1_001])
def test_record_reads_enforce_bounded_limits(tmp_path: Path, limit: int) -> None:
    """Invalid read sizes are rejected before a database query.

    Args:
        tmp_path: Isolated directory supplied by pytest.
        limit: Too-small or too-large requested record count.
    """
    # Store need not contain records for limit validation behavior.
    store = AuditStore(tmp_path / "audit.sqlite3")

    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.read_records(limit)
