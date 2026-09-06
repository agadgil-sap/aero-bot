"""Append-only local SQLite audit records with hash-chain verification."""

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from aero_bot.domain import (
    IMMUTABLE_MODEL_CONFIG,
    OpportunitySnapshot,
    RiskDecision,
    RiskPolicy,
)
from aero_bot.transactions import AllowancePlanResult, ExactAllowanceRequest, TransactionPolicy

# Schema version permits explicit future migrations rather than silent table changes.
AUDIT_SCHEMA_VERSION = 1
# Sixty-four zeroes represent the immutable predecessor of the first audit record.
GENESIS_HASH = "0" * 64
# Five seconds gives concurrent local writers time to complete a short append transaction.
SQLITE_TIMEOUT_SECONDS = 5.0
# Restrictive database permissions allow only the current local user to read or write records.
DATABASE_FILE_MODE = 0o600
# Restrictive directory permissions prevent other local users from traversing audit storage.
DATABASE_DIRECTORY_MODE = 0o700
# Read APIs are bounded to prevent accidental unbounded memory consumption.
MAX_RECORDS_PER_READ = 1_000
# Dangerous field names are rejected before any payload reaches durable storage.
SENSITIVE_FIELD_NAMES = frozenset(
    {
        "keystore",
        "mnemonic",
        "password",
        "privatekey",
        "rawtransaction",
        "secret",
        "seedphrase",
        "signature",
        "signedtransaction",
    }
)


class AuditIntegrityError(RuntimeError):
    """Indicate that a corrupt chain cannot accept another durable event."""


class AuditEventType(StrEnum):
    """Identify the explicitly supported immutable audit event categories."""

    # Risk decision captures a deterministic hold or eligible result and its inputs.
    RISK_DECISION = "risk_decision"
    # Transaction plan captures an unsigned simulation-only plan outcome.
    TRANSACTION_PLAN = "transaction_plan"
    # Transaction simulation captures read-only eth_call evidence.
    TRANSACTION_SIMULATION = "transaction_simulation"
    # System state captures startup, migration, and diagnostic events without secrets.
    SYSTEM_STATE = "system_state"


class AuditVerificationStatus(StrEnum):
    """Describe the result of complete local audit-chain verification."""

    # Empty means the initialized store contains no audit events yet.
    EMPTY = "empty"
    # Verified means sequence, canonical payload, predecessor, and hash checks all passed.
    VERIFIED = "verified"
    # Corrupt means at least one durable row failed an integrity invariant.
    CORRUPT = "corrupt"


class AuditRecord(BaseModel):
    """Represent one immutable event read from the local audit chain."""

    # Frozen strict fields prevent in-memory mutation after durable verification.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Sequence is contiguous and starts at one for the genesis event.
    sequence: Annotated[int, Field(gt=0)]
    # Created time is normalized to UTC before hashing and persistence.
    created_at: datetime
    # Event type restricts records to reviewed application behavior categories.
    event_type: AuditEventType
    # Payload JSON is canonical, sorted, compact, and contains no secret-bearing fields.
    payload_json: str
    # Previous hash links this record to the exact preceding event.
    previous_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    # Record hash covers sequence, time, event type, payload, and predecessor.
    record_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AuditVerification(BaseModel):
    """Expose complete chain status without hiding the first failed record."""

    # Frozen strict fields preserve one coherent verification outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status distinguishes empty, verified, and corrupt durable state.
    status: AuditVerificationStatus
    # Record count reports how many rows were inspected before returning.
    record_count: Annotated[int, Field(ge=0)]
    # First bad sequence is absent unless one row fails verification.
    first_bad_sequence: int | None
    # Diagnostic provides stable evidence suitable for local health reporting.
    diagnostic: str


class RiskDecisionAuditPayload(BaseModel):
    """Capture every validated dependency and output of one risk evaluation."""

    # Frozen strict fields preserve a coherent deterministic decision envelope.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Snapshot contains the complete market, oracle, pool, and portfolio input evidence.
    snapshot: OpportunitySnapshot
    # Policy contains every allowlist, threshold, cap, haircut, and emergency setting.
    policy: RiskPolicy
    # Decision contains the ordered hold or eligible result and exact calculations.
    decision: RiskDecision


class TransactionPlanAuditPayload(BaseModel):
    """Capture every validated dependency and output of exact-allowance planning."""

    # Frozen strict fields preserve one coherent deterministic planning envelope.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Request contains only public addresses, allowance quantities, and Base block evidence.
    request: ExactAllowanceRequest
    # Policy contains the emergency state and complete contract allowlists used for planning.
    policy: TransactionPolicy
    # Result contains the exact blocked, no-action, or unsigned ready outcome returned by the API.
    result: AllowancePlanResult


class AuditStore:
    """Persist canonical application events in an append-only SQLite hash chain."""

    def __init__(self, database_path: Path) -> None:
        """Create or open a local audit database with restrictive permissions.

        Args:
            database_path: Dedicated SQLite file path controlled by the local operator.

        Raises:
            ValueError: If the target is a symbolic link or existing directory.
        """
        # Expanded path honors a local operator's home-relative configuration.
        expanded_path = database_path.expanduser()
        if expanded_path.is_symlink():
            raise ValueError("audit database path must not be a symbolic link")
        if expanded_path.exists() and expanded_path.is_dir():
            raise ValueError("audit database path must be a file")
        # Existing parent state is retained so shared directories are never silently chmodded.
        parent_already_existed = expanded_path.parent.exists()
        if expanded_path.parent.is_symlink():
            raise ValueError("audit database parent must not be a symbolic link")
        # Parent is created before resolution so the final path has a stable absolute location.
        expanded_path.parent.mkdir(parents=True, exist_ok=True, mode=DATABASE_DIRECTORY_MODE)
        if not parent_already_existed:
            # A newly dedicated audit directory is explicitly restricted despite process umask.
            os.chmod(expanded_path.parent, DATABASE_DIRECTORY_MODE)
        # Existing broad parents fail closed rather than changing unrelated operator permissions.
        parent_mode = expanded_path.parent.stat().st_mode & 0o777
        if parent_mode & 0o077:
            raise ValueError("audit database parent must grant access only to the current user")
        # Absolute path prevents connection behavior changing with the process working directory.
        self._database_path = expanded_path.resolve()
        if self._database_path.exists():
            # Existing dedicated files are restricted before SQLite reads any durable content.
            os.chmod(self._database_path, DATABASE_FILE_MODE)
        else:
            # Exclusive creation applies 0600 before SQLite can write schema or journal content.
            creation_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            # Descriptor is closed immediately because SQLite owns all later database access.
            file_descriptor = os.open(
                self._database_path,
                creation_flags,
                DATABASE_FILE_MODE,
            )
            os.close(file_descriptor)
        # Schema initialization establishes append-only triggers before the store is exposed.
        self._initialize()

    @property
    def database_path(self) -> Path:
        """Return the absolute local SQLite path used by this store."""
        return self._database_path

    def append(
        self,
        event_type: AuditEventType,
        payload: BaseModel,
        created_at: datetime,
    ) -> AuditRecord:
        """Append one canonical model payload inside an immediate SQLite transaction.

        Args:
            event_type: Reviewed category identifying the recorded application behavior.
            payload: Validated Pydantic model containing no credential-bearing fields.
            created_at: Time of the source event, including a timezone offset.

        Returns:
            The immutable durable record linked to its predecessor.

        Raises:
            ValueError: If time is naive or payload fields could contain signing credentials.
        """
        # Canonical timestamp is computed before acquiring the short database write lock.
        created_at_text = self._normalize_datetime(created_at)
        # Canonical payload and secret rejection happen before any durable mutation.
        payload_json = self._canonical_payload(payload)
        with closing(self._connect()) as connection, connection:
            # Immediate mode serializes sequence and predecessor selection across local writers.
            connection.execute("BEGIN IMMEDIATE")
            # Full verification under the write lock blocks appending to corrupt history.
            existing_rows = connection.execute(
                "SELECT * FROM audit_records ORDER BY sequence ASC"
            ).fetchall()
            # Corruption fails closed before a new sequence or hash can be allocated.
            verification = self._verification_from_rows(existing_rows)
            if verification.status is AuditVerificationStatus.CORRUPT:
                raise AuditIntegrityError(verification.diagnostic)
            # Verified latest row supplies both the next sequence and exact predecessor hash.
            predecessor = existing_rows[-1] if existing_rows else None
            # Genesis begins at one, while every later record increments the durable sequence.
            sequence = 1 if predecessor is None else int(predecessor["sequence"]) + 1
            # Genesis uses the explicit zero hash rather than an ambiguous null predecessor.
            previous_hash = GENESIS_HASH if predecessor is None else str(predecessor["record_hash"])
            # Record hash binds exact content to the selected predecessor and sequence.
            record_hash = self._calculate_hash(
                sequence,
                created_at_text,
                event_type,
                payload_json,
                previous_hash,
            )
            # Parameterized insertion prevents payload text from changing SQL structure.
            connection.execute(
                """
                INSERT INTO audit_records (
                    sequence, created_at, event_type, payload_json, previous_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    created_at_text,
                    event_type.value,
                    payload_json,
                    previous_hash,
                    record_hash,
                ),
            )
        return AuditRecord(
            sequence=sequence,
            created_at=datetime.fromisoformat(created_at_text.replace("Z", "+00:00")),
            event_type=event_type,
            payload_json=payload_json,
            previous_hash=previous_hash,
            record_hash=record_hash,
        )

    def read_records(self, limit: int = 100) -> tuple[AuditRecord, ...]:
        """Read a bounded ascending prefix of immutable audit records.

        Args:
            limit: Positive maximum number of oldest records to return.

        Returns:
            Immutable records ordered by contiguous sequence.

        Raises:
            ValueError: If limit falls outside the safe read boundary.
        """
        if limit < 1 or limit > MAX_RECORDS_PER_READ:
            raise ValueError(f"limit must be between 1 and {MAX_RECORDS_PER_READ}")
        with closing(self._connect()) as connection:
            # Parameterized limit retains a bounded query without interpolated SQL text.
            rows = connection.execute(
                "SELECT * FROM audit_records ORDER BY sequence ASC LIMIT ?", (limit,)
            ).fetchall()
        # Each row is validated into an immutable public model before leaving the store.
        return tuple(self._record_from_row(row) for row in rows)

    def verify_chain(self) -> AuditVerification:
        """Verify every durable sequence, payload, predecessor, and record hash."""
        with closing(self._connect()) as connection:
            # Complete ordered reads are required because every record depends on its predecessor.
            rows = connection.execute(
                "SELECT * FROM audit_records ORDER BY sequence ASC"
            ).fetchall()
        return self._verification_from_rows(rows)

    def _verification_from_rows(
        self,
        rows: Sequence[sqlite3.Row],
    ) -> AuditVerification:
        """Verify one ordered SQLite snapshot of the complete audit chain.

        Args:
            rows: Ascending durable rows read within one coherent database transaction.

        Returns:
            Empty, verified, or first-failure evidence for the supplied complete snapshot.
        """
        if not rows:
            return AuditVerification(
                status=AuditVerificationStatus.EMPTY,
                record_count=0,
                first_bad_sequence=None,
                diagnostic="Audit store is initialized and contains no records.",
            )

        # Expected predecessor begins at the explicit genesis hash.
        expected_previous_hash = GENESIS_HASH
        for row_index, row in enumerate(rows, start=1):
            # Raw sequence remains inspectable even when another field is corrupt.
            sequence = int(row["sequence"])
            # Durable strings are read exactly as hashed during insertion.
            created_at_text = str(row["created_at"])
            # Unknown event values are corruption rather than extensible implicit behavior.
            event_type_text = str(row["event_type"])
            # Payload text must remain valid canonical JSON.
            payload_json = str(row["payload_json"])
            # Predecessor and record hashes must retain exact lowercase digest representations.
            previous_hash = str(row["previous_hash"])
            # Stored digest is compared after all other content checks.
            record_hash = str(row["record_hash"])
            if sequence != row_index:
                return self._corrupt_verification(
                    len(rows), sequence, "Audit sequence is missing, duplicated, or reordered."
                )
            try:
                # Enum construction rejects unreviewed event categories introduced by tampering.
                event_type = AuditEventType(event_type_text)
                # Decoded object is re-encoded to verify canonical durable representation.
                payload_object: object = json.loads(payload_json)
                # Secret policy remains enforced when inspecting externally modified records.
                self._reject_sensitive_fields(payload_object)
                # Timestamp normalization rejects invalid or non-canonical durable spellings.
                parsed_created_at = datetime.fromisoformat(created_at_text.replace("Z", "+00:00"))
                canonical_created_at = self._normalize_datetime(parsed_created_at)
            except ValueError:
                return self._corrupt_verification(
                    len(rows),
                    sequence,
                    "Audit event type, time, payload JSON, or secret policy is invalid.",
                )
            if canonical_created_at != created_at_text:
                return self._corrupt_verification(
                    len(rows), sequence, "Audit event time is not canonical UTC."
                )
            # Canonical JSON must use sorted compact output with no alternate spellings.
            canonical_payload = json.dumps(
                payload_object,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            if canonical_payload != payload_json:
                return self._corrupt_verification(
                    len(rows), sequence, "Audit payload JSON is not canonical."
                )
            if previous_hash != expected_previous_hash:
                return self._corrupt_verification(
                    len(rows), sequence, "Audit predecessor hash does not match the prior record."
                )
            # Recalculation detects changes to any content covered by the record hash.
            expected_record_hash = self._calculate_hash(
                sequence,
                created_at_text,
                event_type,
                payload_json,
                previous_hash,
            )
            if record_hash != expected_record_hash:
                return self._corrupt_verification(
                    len(rows), sequence, "Audit record hash does not match its durable content."
                )
            # Verified digest becomes the exact predecessor expected by the next row.
            expected_previous_hash = record_hash
        return AuditVerification(
            status=AuditVerificationStatus.VERIFIED,
            record_count=len(rows),
            first_bad_sequence=None,
            diagnostic=f"Verified the complete {len(rows)}-record local audit hash chain.",
        )

    def _initialize(self) -> None:
        """Create the versioned table and append-only triggers idempotently."""
        with closing(self._connect()) as connection, connection:
            # Version zero is a new database, while any other unsupported version fails closed.
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version not in (0, AUDIT_SCHEMA_VERSION):
                raise RuntimeError(f"unsupported audit schema version {current_version}")
            # Strict table checks primitive types and digest shapes at the storage boundary.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_records (
                    sequence INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL CHECK (
                        length(previous_hash) = 64 AND lower(previous_hash) = previous_hash
                    ),
                    record_hash TEXT NOT NULL UNIQUE CHECK (
                        length(record_hash) = 64 AND lower(record_hash) = record_hash
                    )
                ) STRICT
                """
            )
            # Trigger makes accidental or ordinary SQL updates fail instead of mutating history.
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_records_no_update
                BEFORE UPDATE ON audit_records
                BEGIN
                    SELECT RAISE(ABORT, 'audit records are append-only');
                END
                """
            )
            # Trigger makes accidental or ordinary SQL deletion fail instead of erasing history.
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS audit_records_no_delete
                BEFORE DELETE ON audit_records
                BEGIN
                    SELECT RAISE(ABORT, 'audit records are append-only');
                END
                """
            )
            # Explicit schema version records successful initialization for future migrations.
            connection.execute("PRAGMA user_version = 1")
        # SQLite creates the main file during initialization, after which mode can be tightened.
        os.chmod(self._database_path, DATABASE_FILE_MODE)

    def _connect(self) -> sqlite3.Connection:
        """Open one hardened short-lived SQLite connection."""
        # Timeout bounds lock waiting while permitting normal concurrent local appends.
        connection = sqlite3.connect(self._database_path, timeout=SQLITE_TIMEOUT_SECONDS)
        # Row objects make schema reads explicit instead of relying on column positions.
        connection.row_factory = sqlite3.Row
        # Foreign-key enforcement is enabled now so future versioned relations fail safely.
        connection.execute("PRAGMA foreign_keys = ON")
        # WAL permits readers while a short append transaction holds the write lock.
        connection.execute("PRAGMA journal_mode = WAL")
        # Full synchronization prioritizes durable audit evidence over write throughput.
        connection.execute("PRAGMA synchronous = FULL")
        # Defensive query mode prevents writable-schema behavior on this connection.
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    def _canonical_payload(self, payload: BaseModel) -> str:
        """Serialize one model deterministically after rejecting sensitive field names.

        Args:
            payload: Validated Pydantic event payload.

        Returns:
            Sorted compact ASCII JSON used for persistence and hashing.
        """
        # JSON-mode conversion normalizes Decimal, enum, date, and tuple values deterministically.
        payload_object: object = payload.model_dump(mode="json")
        # Recursive field inspection blocks credential-shaped payload schemas.
        self._reject_sensitive_fields(payload_object)
        return json.dumps(
            payload_object,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def _reject_sensitive_fields(self, value: object) -> None:
        """Reject nested payload keys that could contain credentials or signed transactions.

        Args:
            value: JSON-compatible model value requiring recursive field inspection.

        Raises:
            ValueError: If any nested mapping key has a prohibited normalized name.
        """
        if isinstance(value, dict):
            for field_name, field_value in value.items():
                # Alphanumeric normalization catches snake, kebab, and camel spellings alike.
                normalized_field_name = "".join(
                    character for character in str(field_name).casefold() if character.isalnum()
                )
                if normalized_field_name in SENSITIVE_FIELD_NAMES:
                    raise ValueError(
                        f"audit payload field {field_name!r} is prohibited by secret policy"
                    )
                self._reject_sensitive_fields(field_value)
        elif isinstance(value, list):
            for item in value:
                self._reject_sensitive_fields(item)

    def _normalize_datetime(self, value: datetime) -> str:
        """Normalize one aware datetime to a stable microsecond UTC representation.

        Args:
            value: Source event instant with an explicit timezone offset.

        Returns:
            ISO 8601 UTC timestamp ending in Z.

        Raises:
            ValueError: If the source time is timezone-naive.
        """
        if value.utcoffset() is None:
            raise ValueError("audit created_at must be timezone-aware")
        # UTC conversion ensures equivalent instants produce identical timestamp text.
        utc_value = value.astimezone(UTC)
        # Fixed microseconds avoid alternate canonical encodings for the same precision.
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _calculate_hash(
        self,
        sequence: int,
        created_at_text: str,
        event_type: AuditEventType,
        payload_json: str,
        previous_hash: str,
    ) -> str:
        """Calculate the stable digest for one complete audit record.

        Args:
            sequence: Contiguous durable record number.
            created_at_text: Canonical UTC event timestamp.
            event_type: Reviewed event category.
            payload_json: Canonical secret-free model payload.
            previous_hash: Exact digest of the preceding record or genesis hash.

        Returns:
            Lowercase SHA-256 digest used for local tamper evidence, not signing.
        """
        # Structured material avoids ambiguous delimiter parsing between record fields.
        record_material = json.dumps(
            {
                "created_at": created_at_text,
                "event_type": event_type.value,
                "payload": json.loads(payload_json),
                "previous_hash": previous_hash,
                "sequence": sequence,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        # Digest supplies tamper evidence only and is never used as a signature or credential.
        return hashlib.sha256(record_material.encode(), usedforsecurity=False).hexdigest()

    def _record_from_row(self, row: sqlite3.Row) -> AuditRecord:
        """Validate one SQLite row into an immutable public record.

        Args:
            row: Named columns read from the strict audit table.

        Returns:
            Validated immutable audit record.
        """
        return AuditRecord(
            sequence=int(row["sequence"]),
            created_at=datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")),
            event_type=AuditEventType(str(row["event_type"])),
            payload_json=str(row["payload_json"]),
            previous_hash=str(row["previous_hash"]),
            record_hash=str(row["record_hash"]),
        )

    def _corrupt_verification(
        self,
        record_count: int,
        sequence: int,
        diagnostic: str,
    ) -> AuditVerification:
        """Build one consistent fail-closed chain verification result.

        Args:
            record_count: Total rows inspected by the verification pass.
            sequence: First durable sequence that failed an invariant.
            diagnostic: Stable human-readable failure evidence.

        Returns:
            Corrupt verification result naming the first bad sequence.
        """
        return AuditVerification(
            status=AuditVerificationStatus.CORRUPT,
            record_count=record_count,
            first_bad_sequence=sequence,
            diagnostic=diagnostic,
        )
