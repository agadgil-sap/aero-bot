# Aero Bot

Aero Bot is a local Aerodrome LP analysis application for Base with one narrow, manually triggered execution path.
The current foundation exposes a loopback-only FastAPI dashboard and health endpoint.
The API and dashboard never request a private key and cannot sign or broadcast anything; the only signing and broadcast path is the `aero-bot-swap` command's explicitly confirmed, hard-capped Safe swaps documented in [the execution guide](docs/execution.md).

## Install and run on macOS

Install [`uv`](https://docs.astral.sh/uv/) and use Python 3.12 or newer.

```bash
uv sync --extra dev
uv run aero-bot
```

Open <http://127.0.0.1:8765> in a browser.
The server rejects non-loopback bind addresses by design.
Immutable records default to `~/Library/Application Support/Aero Bot/audit.sqlite3`.
Set `AERO_BOT_AUDIT_DATABASE_PATH` to another absolute path only when its dedicated parent directory is private to the current user.

## Verify

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --cov
```

## Security boundary

This release is analysis and simulation software plus one manually triggered execution path.
Do not place wallet seed phrases, private keys, signing material, or other secrets in this repository or its environment files.
The only venue enabled by the product is Aerodrome on Base.
Transaction signing and broadcasting exist only inside the `aero-bot-swap` command behind its explicit `execute --confirm-broadcast` invocation, with every hard cap enforced in code before anything is signed; the API and dashboard remain structurally unable to sign or broadcast, and no loop, scheduler, or policy trigger can reach the execution path.

## Official B20 identities

The packaged B20 registry is sourced from the [official Base stocks list](https://www.base.org/stocks).
The source states that its list is the complete set of Coinbase-issued tokenized stocks and advises matching contract addresses before interaction.
Each bundled identity retains the official BaseScan link and the date the source was observed.
The application validates the entire registry and rejects duplicate or malformed identities before exposing any address.
If the evidence resource is missing or invalid, the API and dashboard show an explicit blocked diagnostic and expose no token identities.

## Chainlink health

The application models Chainlink Coinbase B20 total-return rounds, Coinbase multiplier pause state, and the Base sequencer recovery boundary as deterministic health gates.
Only a market-open observation that passes every gate is healthy.
The current release has no reviewed B20 proxy-address snapshot or read-only observation backend, so `/api/oracles/chainlink` and the dashboard report an explicit unavailable diagnostic instead of making live feed claims.
See [the oracle health policy](docs/oracle-health.md) for evidence sources and exact gate ordering.

## Pool discovery

Aerodrome's own LP Sugar contract is the authoritative pool inventory for the application.
When `AERO_BOT_POOL_DISCOVERY_ENABLED` is set, the application enumerates every pool through read-only paginated `eth_call` requests against the Base RPC endpoint, pins one block for a coherent snapshot, and accepts only official Slipstream pools pairing one verified B20 with native USDC under an alive gauge actively emitting official AERO.
The default public Base RPC endpoint and pinned Sugar deployment can be overridden with `AERO_BOT_BASE_RPC_URL` and `AERO_BOT_LP_SUGAR_ADDRESS`; no third-party API key is used or embedded.
See [the venue trust boundary](docs/venue-boundary.md) for the complete acceptance and fail-closed rules.

## Live yield screen

The dashboard and `/api/market-data/aerodrome-yields` perform a bounded read-only query against DefiLlama's public yield dataset.
The scanner accepts only Base records from `aerodrome-slipstream` whose underlying contracts are exactly native USDC and one address in the verified official B20 registry.
Positive reward yield must identify the official AERO token contract.
Fee APY and AERO emissions APY remain separate inputs, and the displayed daily screen adds fee APY to fifty-percent-haircut emissions APY because a staked in-range position earns both streams.
This secondary-source screen is not eligible for execution until onchain pool identity, oracle, exit-depth, concentrated-liquidity, IL, and adverse-selection checks also pass.

## Rehearsal history reconstruction

The rehearsal harness reconstructs each accepted pool's exact historical price path and emissions-APR series from onchain evidence alone.
Every Slipstream pool emits one `Swap` event per swap carrying the post-swap square-root price, so read-only `eth_getLogs` filtering by the event topic recovers the path without any keyed service.
Block windows are located from timestamps through a bounded binary search over block headers, and every swap keeps its own block's exact header timestamp rather than an interpolated estimate.
The reconstruction is read-only, bounded, and fail-closed: rate-limited reads retry with exponential backoff, oversized or truncated log windows abort, and malformed evidence never yields a price point.

Each pool's emissions APR is reconstructed the same way from its CLGauge `Deposit` and `Withdraw` events.
Staked liquidity is anchored at the block-pinned Sugar snapshot and walked backward through the window's net stake events, so historical dilution and concentration appear as mined whenever the stake events explain the anchor, and the reconstruction never reads the chain head.
Staked positions can also change liquidity through the position manager while the gauge holds them, which stake events do not carry, so a fold that contradicts the anchor is never fabricated into a series: the fetch falls back to a window-long constant APR at the anchor's exact level, labeled `constant_anchor_apr` on the series itself.
Three approximations are documented per series: the gauge's AERO reward rate is held constant across the window because Aerodrome resets it only at weekly epochs, staked value scales linearly with staked liquidity at the frozen per-unit anchor value because other LPs' range shapes are private, and the constant fallback holds the anchor level across the window when the event fold cannot close.
The AERO price behind every APR is an explicit assumption carried on the series itself.

## Rehearsal replay and per-pool P&L ledger

The replay module folds the locked policy engine over one pool's reconstructed histories and emits a deterministic per-pool profit-and-loss ledger.
Every reconstructed swap becomes one observation, the books mark at each price, and every decision applies to USDC cash, the open position, and any held inventory exactly as the engine emitted it.
Between observations, AERO emissions accrue to the open in-range position pro rata to gauge staked liquidity, and each observed swap credits fees pro rata to active pool liquidity while the position is open and in range.
Because the underlying reference market cannot be reconstructed keylessly minute-by-minute, the reference path defaults to the assumption that the AMM equaled the real market at every swap, and a clearly labeled synthetic schedule may overlay bounded stale-high, stale-low, and stale-feed episodes so the dislocation monitor's exits, holds, and convergence-timeout sells appear in the ledger.
The ledger carries final equity, P&L, exposure time, accrued fees and emissions, total swap-impact and gas drag, per-action-kind counts cross-checked against the recorded action list, and every documented approximation as explicit assumption labels.
The replay is pure: all economic inputs are injected, so the module performs no I/O and unit tests drive it entirely from synthetic paths.

## Rehearsal command

The `aero-bot-rehearse` command wires the discovery, reconstruction, and replay modules into one read-only run against Base and writes every per-pool P&L ledger into a single JSON report.
It accepts a lookback window, the AERO and gas price assumptions, an optional pool filter, and the synthetic stress switch, and exits zero only when every selected pool produced a ledger.
A pool whose reads fail is recorded as a fail-closed failure with its diagnostic while the remaining pools continue.
See [the rehearsal command documentation](docs/rehearsal.md) for the full pipeline, runtime bounds, and every documented approximation.

## Decision command

The `aero-bot-decide` command runs one complete policy-engine verdict from live Base reads - discovery, the corrected emissions-APR convention, depth, gas, and the event calendar - decision-only: nothing is built, signed, or broadcast, and the run audits one `policy_decision` record.
Flat verdicts during closed-market event windows are the v1 doctrine working correctly, and the two honest input gaps (the unwired live reference quote and the zero fee APR) surface in every report's notes rather than hiding.
See [the decision command documentation](docs/strategy.md) for the observation assembly, the flat-window doctrine, and the live proofs.

## Cycle command

The `aero-bot-cycle` command runs one scheduled decision cycle - reconcile on-chain state, run the locked policy engine, and execute the authorized action through the audited capped surfaces - then exits; a hardened systemd timer decides when cycles run.
A crashed cycle reconciles toward chain truth and never double-acts, adopting a crashed entry only through the audit chain's own confirmed-mint evidence.
See [the cycle command documentation](docs/cycle.md) for the fixed cycle order, the action mapping, the crash discipline, and the systemd wiring.
Each cycle can email its summary and alerts through a sealed-credential SMTP or Resend-style transport; see [the alerts documentation](docs/alerts.md) for the configuration and semantics.

## Watchtower command

The `aero-bot-watchtower` command is the always-on range watcher: one cheap tick poll every few seconds against the tracked position's range bounds, and the audited defensive close the moment a verified trip leaves the earning range - never gated by market windows or the reference quote, fail-safe on unreadable state, dark until the sealed enable flag arms it.
See [the watchtower documentation](docs/watchtower.md) for the trip semantics, the latch and cooldown, the configuration, and the arming path.

## Shadow advisor command

The `aero-bot-advisor` command is the intelligence layer's advisory surface: it composes the audited factual picture - the tracked position, day economics, fee evidence, decision cadence - and asks one language model on the operator's private inference plane for a bounded brief and anomaly flags.
It is fail-closed (every failure is a typed absence, never a guess), advisory-only (it sends nothing, signs nothing, and never writes the cycle book), and dark until the sealed plane values arm it.
See [the advisor documentation](docs/advisor.md) for the sealed environment, the absence catalog, the systemd wiring, and the bake-off harness.

## Teacher harness command

The `aero-bot-teacher` command is the intelligence layer's Mac-side dual-seat advisory surface: it pulls one read-only window from the production box over `gcloud` and asks two premium seats - the Claude Code CLI's configured model and Codex pinned to GPT 6 Luna at high reasoning, both through the operator's coding-plan logins - one bounded question per stream (tactical, daily, news).
Every answer validates against the same strict brief schema the student advisor uses, every absence is typed, and the only effect is one episode appended to the local corpus - the teaching material for hindsight scoring and student upgrades.
See [the teacher documentation](docs/teacher.md) for the three streams, the read-only pull, the seat invocations, the launchd kit, and the manual runs.

## Wallet-free transaction planning

The application can plan deterministic unsigned exact allowances and can pass a revalidated plan only to a read-only simulation interface.
The default policy is emergency-halted with empty transaction allowlists and no simulation backend.
Wallet onboarding, private-key input, signing, and broadcasting remain structurally unavailable through both the API and dashboard; the only signing path is the explicitly confirmed swap command below.
See [the transaction simulation boundary](docs/transaction-simulation.md) for exact allowance and backend evidence rules.

## Safe transaction layer

The Safe transaction layer builds `execTransaction` payloads for the canary Safe on Base: it computes the EIP-712 SafeTx hash, signs it with an owner key through the audited `eth-account` library, ABI-encodes the calldata with the zero gas-parameter shape of every reference transaction, and proves the signature read-only against the live contract through `checkNSignatures`.
Two contract facts are verified live rather than assumed: this Safe hashes with the minimal EIP-712 domain carrying only the chain ID and verifying contract, and its nonce getter is `nonce()` while `getNonce()` reverts.
The layer never sources key material itself - raw key bytes arrive as an argument, sign exactly one hash, and are never stored, logged, or persisted - and every produced signature must pass a local recovery round trip before it leaves the module.
The computed hashes reproduce every Safe Transaction Service record for this Safe, and the read-only RPC backend carries the same bounded retries and fail-closed decoding as the discovery layer.

## Keychain signing-key source

The macOS Keychain is the only key source in the application: the keychain module reads the bot owner's key at runtime through `/usr/bin/security find-generic-password` with the service and account names taken from `AERO_BOT_KEYCHAIN_SERVICE` (default `aero-bot`) and `AERO_BOT_KEYCHAIN_ACCOUNT` (default `bot-key`).
The absolute tool path prevents PATH substitution, every failure is a clean actionable error that quotes at most a bounded stderr tail and never the secret, and nothing about the key is ever logged, cached, or persisted - the module derives and reports only the public address.
A missing item, an empty secret, a non-hex secret, and the all-zero placeholder each fail closed with distinct diagnostics.

## Capped manual swap execution

The `aero-bot-swap` command is the application's only signing and broadcast path: a manual one-shot CLI for hard-capped USDC-to-B20 stock swaps through the canary Safe on Base.
The `quote` subcommand prices through live Sugar discovery, the default `dry-run` builds, signs, and validates the Safe transactions without broadcasting anything, and broadcasting exists only behind `execute --confirm-broadcast`.
Every cap is enforced in code before anything is signed: swaps default to at most 1 USDC (validator ceiling 5), the standing USDC allowance is the bounded 20-USDC number and never infinite, the router whitelist is exactly the reference swap's universal router, tokens are USDC plus the official registry only, pools come from live discovery, gas above 1 gwei or a Safe below its ETH floor refuses, and quotes older than two minutes are stale.
The swap calldata reproduces the Safe's executed reference transaction byte for byte, delivery transactions are bounded type-2 EOA transactions paid by the relaying key, and the live `checkNSignatures` verdict gates every broadcast.
Every attempt appends its quote, build, broadcast, and receipt evidence to the immutable audit chain with no key material anywhere.
There is no loop, scheduler, watcher, or policy wiring anywhere in the execution path.
See [the execution guide](docs/execution.md) for the cap table, Keychain setup, the canary procedure, the refusal catalog, and the live read-only verification transcript.

## Concentrated-liquidity analysis

The position analyzer implements Slipstream's inherited square-root-price inventory formulas with exact Decimal arithmetic.
It distinguishes below-range, active, and above-range positions, reports current asset concentration, and compares current LP inventory with holding the entry assets.
Fees and AERO emissions remain separate from the conservative impermanent-loss estimate.
See [the concentrated-liquidity policy](docs/concentrated-liquidity.md) for formulas, assumptions, and adapter requirements.

## Fee plus AERO compensation

The deterministic risk engine models Aerodrome compensation additively.
A staked in-range position earns swap fees and AERO emissions at the same time, so the engine adds retained fee APR to haircut emission APR before subtracting impermanent-loss and adverse-selection costs.
The `/api/risk/evaluate` endpoint exposes complete hold or eligible evidence without enabling a transaction.

## Emissions-farming policy engine

The v1 policy engine is pure decision code: observations, the event calendar, and the locked parameters are injected, and every decision returns a typed immutable action plus the successor state.
Entry requires the pool's raw AERO emissions APR of at least 150 percent per staked liquidity, the position's range width is derived per entry and recenter from a target net daily yield of 1 percent on deployed capital (tightest tick-aligned width meeting it, one tick spacing minimum, plus-or-minus 0.3 percent as the safety ceiling and fallback), upside recenters wait 15 minutes out of range, the downside stop and emissions dilution exits burn and swap all inventory back to USDC with a 15-minute re-entry cooldown, event windows hold flat in USDC, and a 5 percent same-day equity loss halts new entries until the next day.
A dislocation monitor compares the keyless reference quote against the pool at every observation with asymmetric 0.15 percent actions (sell on a stale-high AMM, burn and hold through a stale-low AMM until convergence or a 5-minute timeout), non-urgent actions defer behind a gas sense-check gate (0.5 gwei or 5 percent of expected daily gross yield), and every swap carries a tranche-split execution plan that keeps modeled impact at or below 0.05 percent per tranche against the hard 0.1 percent ceiling.
The `/api/policy/decide` endpoint exposes the fold over HTTP: a request carries one observation plus the threaded state (defaulting to a fresh 200-USDC session) and returns the decision with its successor state.
See [the policy engine design](docs/policy-engine.md) for the complete locked parameters, decision precedence, and event-window sources.

## Immutable local audit storage

The local persistence boundary uses a versioned SQLite database with append-only triggers, canonical model payloads, contiguous sequences, and a verifiable SHA-256 hash chain.
It rejects credential-shaped fields before persistence and uses restrictive user-only filesystem permissions.
The dashboard, process health route, and `/api/audit/health` expose complete chain verification.
Every risk response is persisted with its validated input, active policy, and exact decision before it is returned.
Every policy decision is persisted with its injected observation, threaded state, locked parameters, event calendar, and exact decision before it is returned.
Every exact-allowance planning response is persisted with its public request, active policy, and exact result before it is returned.
Every read-only simulation response is persisted with its submitted unsigned plan, active revalidation policy, and complete result before it is returned.
Every capped swap attempt persists its quote, each fully built Safe transaction, each broadcast submission, and each inclusion receipt before the command reports, with no key material in any event.
See [the audit log design](docs/audit-log.md) for guarantees, limitations, and integration status.
The chain leaves the host daily as an encrypted bundle on a private git branch through a deploy key; see [the audit backup documentation](docs/audit-backup.md) for the verify-export-encrypt-push order and the sealed configuration.

## Ubuntu deployment

The complete kit for the $0-tier always-on Linux VM ships in `deploy/`: an idempotent installer (uv, the dedicated service user, the locked venv, sealed environment templates at 0600/0640, every systemd unit, ufw with OpenSSH-only before enable, unattended-upgrades) plus the docs - and it never arms a timer: Phase 2 seals the secrets, funds the Safe, walks the ten-line smoke checklist, and verifies one live micro-cycle before the captain leaves.
The repo stays loopback-only; the operator reaches the dashboard through an SSH tunnel.
See [the deployment guide](docs/deployment.md) for the install, the secrets discipline, the arming steps, the smoke checklist, and the day-two runbook.
