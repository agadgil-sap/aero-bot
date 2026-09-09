# The scheduled decision cycle

The `aero-bot-cycle` command runs exactly one decision cycle for one registry symbol - or for the whole verified B20 board in selector mode - and exits: reconcile, decide, act. The systemd timer - never an in-process scheduler - decides when cycles run, so a wedged cycle can never overlap a second one and a missed tick catches up on the next start.

```
uv run aero-bot-cycle [--symbol AAPLc | auto] [--dry-run] [--reference-price 318.5 | SYM=PRICE,...] [--json]
```

Exit codes: zero on any completed cycle (a hold is a decision, not a failure), one on failures, two when any refusal or out-of-band condition halted the cycle.

## Pinned symbol versus selector mode (the captain's 2026-09-09 cross-board ruling)

The symbol is optional in the sealed cycle environment: `AERO_BOT_CYCLE_SYMBOL` unset, empty, or `auto` runs the cross-board selector (the default), and an explicit symbol pins one pool for operator runs - the `--symbol` flag overrides the environment, and `--symbol auto` means the selector too, so `aero-bot-cycle@auto.timer` drives selector mode with the existing template unit.

In selector mode the decide step enumerates **every verified B20 stock-token pool** - the same factory, pair, kind, uniqueness, gauge-liveness, and AERO-emission validation the screener applies over one Sugar sweep - evaluates the COMPLETE entry gate chain per pool (condition flats, reference freshness, the 150 percent emissions floor, the equity and depth caps, and the gas gate), and selects the best-qualifying pool by qualifying emissions APR, with ties broken on the lexicographically smallest symbol so runs are reproducible.
The pure selection mathematics live in `src/aero_bot/selector.py`.

The anti-churn discipline is part of the same ruling:

- While a position is held, another pool displaces it only when its qualifying emissions APR exceeds the held pool's by the configurable relative switch margin (default thirty percent, `AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION` or `--switch-margin`) AND the exit-plus-entry combined gas cost passes the locked five-percent cost-share bound of the new position's expected daily gross yield - the same bound the entry gate applies to the entry alone, here over both batches' Safe overheads.
- A switch executes as exit-then-enter inside one cycle: the held position runs the full unstake/withdraw/exit-swap sequence first, and any refusal or failure there halts the cycle before anything is minted, so exactly one position is funded at every instant.
- The existing per-pool re-entry cooldowns apply per pool: a stop-out or dilution exit arms the fifteen-minute cooldown for its own pool only, never blocking another pool's entry, and a voluntary switch arms no cooldown at all.
- The board never enters while unsold inventory from a stale-low burn is held; the convergence machinery resolves it first, exactly as the per-pool engine orders.
- Never more than one position: the single-position invariant holds under every path, inside the unchanged hard caps (100 USDC total exposure, 100 USDC per pool, enforced by the LP executor's refusal catalog).

Selector cycles pay one verified Sugar sweep per run - the whole board comes from one block-pinned snapshot, which is also what makes the cross-pool APR comparison coherent - and every enumerated pool is pinned so later pinned-symbol runs keep the known-pool fast path.
Expect the sweep's honest per-page progress on the first run and on rate-limited endpoints, and tens of seconds once warm-adjacent; pinned cycles keep their seconds-scale fast path untouched.

## The fixed cycle order

1. **Reconcile first.** The Safe's live USDC, stock, and relayer-ETH balances; the complete held-NFT inventory on the pool's NFPM (`safe_position_inventory`); and, when a position is tracked, its live custody, value, and unrealized P&L against the recorded entry cost. The chain - never memory - is the source of truth. Selector cycles anchor these reads on the tracked (or held-inventory) symbol, and on the deterministic first board listing while flat; while flat they also sweep every board token's balance so unsold stock in any pool is adopted with its own symbol.
2. **Decide.** The complete locked policy engine (`PolicyEngine(LOCKED_POLICY_PARAMETERS, load_event_calendar())`) runs over one live observation assembled exactly like `aero-bot-decide`, threading the reconciled policy state: the open position (range, committed cost, entry time), held inventory, the per-pool re-entry cooldowns, and the America/New_York day-start equity anchor for the five-percent daily loss halt. In selector mode the whole board is evaluated and the selection's verdict - including a composed `pool_switch` - rides the same report and audit shapes, with every pool's qualification outcome in the decision diagnostics.
3. **Act.** Only a policy-authorized action executes, and only through the proven audited surfaces inside the existing caps and refusal catalog. Any refusal or failed delivery halts the cycle; an out-of-band condition refuses, records, and stops the cycle without acting.

The cycle appends exactly one `cycle_reported` audit record per run beside every record the executors already write (`lp_mint_planned`, `lp_execute_sent`, `lp_execute_confirmed`, and the rest). The audit chain stays the authoritative record of everything broadcast; the cycle's own memory is one self-healing JSON book beside the audit store (`AERO_BOT_CYCLE_STATE_PATH` overrides the default `cycle_state.json` next to the audit database).

## Action mapping

| Policy verdict | Cycle execution |
| --- | --- |
| `hold` | Nothing; the verdict is the product. |
| `enter` | `execute_mint` at the decision's size (the policy's spacing width, rounded up to whole spacings) -> decode the minted token id from the delivery receipt's `IncreaseLiquidity` event -> `execute_stake` -> track the position. |
| `pool_switch` | The held pool's full exit sequence below, then a fresh `execute_mint` -> stake in the winning pool; a failed exit halts before any mint. |
| `recenter` | The exit sequence below, then a fresh `execute_mint` -> stake at the decision's new range. The burned-out empty NFT remains (the manual recenter batch's burn is not part of the live mapping; a later recenter or manual burn clears residuals). |
| `stop_out`, `dilution_exit`, `event_exit`, `dislocation_exit`, `defensive_exit` | `execute_unstake` (when staked) -> `execute_withdraw` -> `execute_exit_swap`; the book clears and that pool's re-entry cooldown arms (none for `event_exit`). |
| `stale_low_burn` | `execute_unstake` (when staked) -> `execute_withdraw`; the returned stock is recorded as held inventory, never swapped below the market. |
| `sell_inventory` | `execute_exit_swap` alone; the held-inventory record clears. |

The exit swap (`aero-bot-lp execute exit-swap`) is the canary-proven reverse-direction router swap productized into the LP lifecycle: exact-input of the Safe's entire stock balance, minimum output at the locked one-percent tolerance, and a hard refusal when the quoted output exceeds the 100 USDC per-pool pilot cap - an out-of-band inventory never moves.

## Crash discipline

A crashed cycle reconciles, never double-acts. Every broadcast flows through the executors' per-nonce Safe sequences - each step re-validated, freshly estimated, hash-pinned, and audit-recorded before its delivery - so a crash can only leave a completed prefix mined. On restart:

- A **crashed entry** (mint confirmed, book not saved) is adopted back only through the audit chain's own evidence: the newest confirmed mint delivery whose `IncreaseLiquidity` receipt names exactly the one live untracked position. The adoption takes its cost basis from the same period's audited mint budget. No evidence, no adoption - the cycle refuses out-of-band instead of guessing.
- A **crashed exit** leaves the position (or stock balance) partially unwound; reconciliation sees the true custody and the policy re-decides over it, holding any leftover stock as inventory the convergence machinery will unwind.
- **Unproven live positions** - an NFT with liquidity or owed fees that the cycle does not track and cannot prove - refuse the cycle out-of-band. Empty residual NFTs (zero liquidity, zero owed fees, like the canary's leftover 5703026) carry no exposure and no longer block entry.

## The reference-quote doctrine

The real-market reference feed is not wired live yet (the oracle-health layer is post-reassessment scope), and an open position requires a reference quote every cycle: without one the engine holds entries fail-closed as `reference_stale` and orders a `defensive_exit` for any open position past the staleness bound. The cycle therefore accepts injected quotes - `--reference-price` or `AERO_BOT_CYCLE_REFERENCE_PRICE_USDC` - whose age the operator owns honestly: an injected constant is only as fresh as its last update, and the deployment docs spell out that duty. Pinned cycles take one quote (`318.5`); selector mode takes per-symbol pairs (`AAPLc=318.5,FIXc=100`), and a pool without a fresh quote simply fails its entry gate fail-closed. When the live feed lands, the injection disappears.

## Twenty-four-seven operation (captain's ruling 2026-09-09)

The B20 pools are continuous DeFi markets - nights and weekends are in scope - so the market-session/event-window gate no longer blocks entries and no longer forces exits.
Flat verdicts around market open/close and scheduled events are gone; the condition-driven flats (a stale oracle observation or a paused registry) and the reference-stale hold remain exactly as shipped, and every other gate - the 150 percent emissions floor, the depth caps, the gas gate, reference freshness, out-of-band refusals, and the defensive exits - is unchanged.
The event-calendar machinery stays loaded and the cycle report still names the active window, informationally.

## Dry runs

`--dry-run` reconciles and decides without loading the signing key and without building anything. The report still carries the complete verdict, the reconciliation, and the P&L; relayer ETH reads need `AERO_BOT_RELAYER_ADDRESS` (a public address, never a secret) to be set, otherwise that one line reports unknown.

The decide phase resolves its pool through the known-pool fast path (`src/aero_bot/known_pool.py`): once one verified Sugar sweep has pinned the pool's identity, every later cycle resolves it from sixteen block-pinned contract views in seconds instead of re-enumerating every pool Aerodrome hosts, which the public RPC rate-limits into a multi-minute stall.
The first run on a clean checkout - or any run whose pin has drifted - pays that one sweep, pins the verified identity beside the audit store, and reports its progress honestly: one stderr line per enumerated page ("lp sugar enumeration: N pools enumerated at block B") and one per rate-limit retry ("rpc eth_call attempt A of 5 failed (HTTP status 429); backing off Xs"), so a slow first sweep is visible progress rather than silence; stdout stays machine-clean JSON for the journal.
Steady-state pinned cycles complete in tens of seconds end to end (22 s measured live over a healthy public endpoint on 2026-09-09, versus the multi-minute sweep every run before the fast path).
See [the LP execution fast-path section](docs/lp_execution.md) for the verified read set and the safety invariant.

## systemd wiring

`deploy/systemd/aero-bot-cycle@.service` and `aero-bot-cycle@.timer` carry the deployment contract, pinned by tests:

- One template unit per symbol: `systemctl enable --now aero-bot-cycle@AAPLc.timer` for a pinned pool, or `aero-bot-cycle@auto.timer` for the cross-board selector (the sealed `AERO_BOT_CYCLE_SYMBOL` variable in `/etc/aero-bot/cycle.env` reaches the same selector mode; unset, empty, or `auto` selects, an explicit symbol pins).
- Default cadence `OnCalendar=hourly` with `Persistent=true` (missed ticks catch up) and `RandomizedDelaySec=180`; a `systemctl edit` drop-in changes the cadence.
- `Type=oneshot`, `Restart=no`: cycles never overlap and never auto-retry - the next tick reconciles.
- Sealed environment through `/etc/aero-bot/cycle.env` (mode 0600): the symbol's Safe, the relayer address, the key-source selection (chapter 1's sealed variable or 0600 key file under `/etc/aero-bot/`), the optional reference quote (single or per-symbol pairs), the optional symbol pin, and the optional switch margin.
- Hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, state under `StateDirectory=aero-bot`.
- The JSON report lands in the journal: `journalctl -u aero-bot-cycle@AAPLc.service`.

See `docs/deployment.md` (the deployment kit) for the full Ubuntu install and smoke checklist.

## The range watchtower complement

Between cycles, the always-on range watchtower (`aero-bot-watchtower`, [docs/watchtower.md](docs/watchtower.md)) polls the tracked pool's tick every few seconds and fires this same defensive exit the moment a verified trip leaves the earning range - never gated by market windows or the reference quote, and dark until the sealed enable flag arms it.

## Email alerts

Every cycle can email its summary and its alerts - position state, P&L vs entry, gas and balance floors (relayer ETH, Safe USDC), and any refusal, failure, or out-of-band condition - through a provider-agnostic SMTP transport or a Resend-style HTTP adapter, with credentials sealed in the environment and a delivery failure that never crashes the cycle.
See [the alerts documentation](docs/alerts.md) for the configuration table, the alert semantics, and the wiring.
