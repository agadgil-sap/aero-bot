# Immutable local audit log

## Storage boundary

The audit store uses a dedicated local SQLite file and the Python standard library.
Initialization creates its parent directory with user-only permissions and tightens the database file to user read and write access.
SQLite uses write-ahead logging, full synchronization, foreign-key enforcement, and an explicit schema version.

The first schema supports reviewed risk-decision, unsigned-plan, read-only-simulation, and system-state event categories.
Only validated Pydantic models can be appended.
Nested credential-shaped field names such as private keys, seed phrases, passwords, signed transactions, and raw transactions are rejected before persistence.
This is a defense in depth control and does not make the audit database an acceptable place for secrets.

## Immutability and tamper evidence

Payloads are serialized as sorted compact JSON after enum, Decimal, date, and tuple values are normalized through Pydantic's JSON mode.
Every record contains a contiguous sequence, canonical UTC event time, explicit event type, canonical payload, previous record hash, and its own SHA-256 integrity hash.
The genesis record links to an explicit 64-character zero hash.

Database triggers reject ordinary update and delete statements.
Appends use immediate transactions so concurrent local writers allocate one sequence and predecessor at a time.
Readers can continue while a short append transaction is active.

The hash chain provides tamper evidence, not cryptographic authorship or protection against an attacker who can replace the complete database.
Complete verification checks sequence continuity, reviewed event type, canonical JSON, predecessor links, and every record hash.
It reports the first bad sequence and never treats a verified prefix as a verified complete log.

## Integration status

This iteration establishes and verifies the persistence boundary without enabling it in application routes.
A later slice must configure the macOS application-data path, expose health diagnostics, and append complete risk, plan, and simulation evidence atomically at their service boundaries.
No private key, signature, or broadcast payload may be introduced during that integration.
