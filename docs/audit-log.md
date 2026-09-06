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

The application defaults to `~/Library/Application Support/Aero Bot/audit.sqlite3` and permits an absolute-path override through `AERO_BOT_AUDIT_DATABASE_PATH`.
Startup initializes the private audit store before exposing any route.
The process health response, `/api/audit/health`, and dashboard verify the complete chain and expose its status and record count.
A corrupt chain degrades process health and is shown as an integrity failure rather than a healthy prefix.

Every `/api/risk/evaluate` response is appended before it is returned.
The event contains the complete validated opportunity snapshot, immutable active policy, and exact deterministic hold or eligible decision.
If durable append fails, the endpoint does not return an unaudited decision.
Each append verifies the complete existing chain under its immediate write transaction and refuses to extend corrupt history.

Every `/api/transactions/plan/exact-allowance` response is also appended before it is returned.
The event contains the complete public request, immutable transaction policy, and exact blocked, no-action, or unsigned ready result.
No audit payload includes a private key, signature, signed transaction, or broadcast capability.

Every `/api/transactions/simulate` response is appended before it is returned.
The event contains the exact caller-submitted unsigned plan, immutable revalidation policy, and complete blocked, unavailable, rejected, reverted, or passed result.
Passed results preserve the read-only backend source, pinned Base block, aware observation time, and one ordered `eth_call` observation per unsigned transaction.
An audit failure can suppress a simulation response after a backend call, but that external operation remains read-only and cannot sign or broadcast.
