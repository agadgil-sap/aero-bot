# The scheduled decision cycle

The `aero-bot-cycle` command runs exactly one decision cycle for one registry symbol and exits: reconcile, decide, act. The systemd timer - never an in-process scheduler - decides when cycles run, so a wedged cycle can never overlap a second one and a missed tick catches up on the next start.

```
uv run aero-bot-cycle --symbol AAPLc [--dry-run] [--reference-price 318.5] [--json]
```

Exit codes: zero on any completed cycle (a hold is a decision, not a failure), one on failures, two when any refusal or out-of-band condition halted the cycle.

## The fixed cycle order

1. **Reconcile first.** The Safe's live USDC, stock, and relayer-ETH balances; the complete held-NFT inventory on the pool's NFPM (`safe_position_inventory`); and, when a position is tracked, its live custody, value, and unrealized P&L against the recorded entry cost. The chain - never memory - is the source of truth.
2. **Decide.** The complete locked policy engine (`PolicyEngine(LOCKED_POLICY_PARAMETERS, load_event_calendar())`) runs over one live observation assembled exactly like `aero-bot-decide`, threading the reconciled policy state: the open position (range, committed cost, entry time), held inventory, the re-entry cooldown, and the America/New_York day-start equity anchor for the five-percent daily loss halt.
3. **Act.** Only a policy-authorized action executes, and only through the proven audited surfaces inside the existing caps and refusal catalog. Any refusal or failed delivery halts the cycle; an out-of-band condition refuses, records, and stops the cycle without acting.

The cycle appends exactly one `cycle_reported` audit record per run beside every record the executors already write (`lp_mint_planned`, `lp_execute_sent`, `lp_execute_confirmed`, and the rest). The audit chain stays the authoritative record of everything broadcast; the cycle's own memory is one self-healing JSON book beside the audit store (`AERO_BOT_CYCLE_STATE_PATH` overrides the default `cycle_state.json` next to the audit database).

## Action mapping

| Policy verdict | Cycle execution |
| --- | --- |
| `hold` | Nothing; the verdict is the product. |
| `enter` | `execute_mint` at the decision's size (the policy's spacing width, rounded up to whole spacings) -> decode the minted token id from the delivery receipt's `IncreaseLiquidity` event -> `execute_stake` -> track the position. |
| `recenter` | The exit sequence below, then a fresh `execute_mint` -> stake at the decision's new range. The burned-out empty NFT remains (the manual recenter batch's burn is not part of the live mapping; a later recenter or manual burn clears residuals). |
| `stop_out`, `dilution_exit`, `event_exit`, `dislocation_exit`, `defensive_exit` | `execute_unstake` (when staked) -> `execute_withdraw` -> `execute_exit_swap`; the book clears. |
| `stale_low_burn` | `execute_unstake` (when staked) -> `execute_withdraw`; the returned stock is recorded as held inventory, never swapped below the market. |
| `sell_inventory` | `execute_exit_swap` alone; the held-inventory record clears. |

The exit swap (`aero-bot-lp execute exit-swap`) is the canary-proven reverse-direction router swap productized into the LP lifecycle: exact-input of the Safe's entire stock balance, minimum output at the locked one-percent tolerance, and a hard refusal when the quoted output exceeds the 100 USDC per-pool pilot cap - an out-of-band inventory never moves.

## Crash discipline

A crashed cycle reconciles, never double-acts. Every broadcast flows through the executors' per-nonce Safe sequences - each step re-validated, freshly estimated, hash-pinned, and audit-recorded before its delivery - so a crash can only leave a completed prefix mined. On restart:

- A **crashed entry** (mint confirmed, book not saved) is adopted back only through the audit chain's own evidence: the newest confirmed mint delivery whose `IncreaseLiquidity` receipt names exactly the one live untracked position. The adoption takes its cost basis from the same period's audited mint budget. No evidence, no adoption - the cycle refuses out-of-band instead of guessing.
- A **crashed exit** leaves the position (or stock balance) partially unwound; reconciliation sees the true custody and the policy re-decides over it, holding any leftover stock as inventory the convergence machinery will unwind.
- **Unproven live positions** - an NFT with liquidity or owed fees that the cycle does not track and cannot prove - refuse the cycle out-of-band. Empty residual NFTs (zero liquidity, zero owed fees, like the canary's leftover 5703026) carry no exposure and no longer block entry.

## The reference-quote doctrine

The real-market reference feed is not wired live yet (the oracle-health layer is post-reassessment scope), and an open position requires a reference quote every cycle: without one the engine holds entries fail-closed as `reference_stale` and orders a `defensive_exit` for any open position past the staleness bound. The cycle therefore accepts an injected quote - `--reference-price` or `AERO_BOT_CYCLE_REFERENCE_PRICE_USDC` - whose age the operator owns honestly: an injected constant is only as fresh as its last update, and the deployment docs spell out that duty. When the live feed lands, the injection disappears.

## Flat verdicts are the doctrine

A `hold (event_window_flat)` inside a market open/close window, the condition-driven flats, and the reference-stale hold are the v1 strategy working exactly as designed - the same doctrine `docs/strategy.md` pins for the decision-only surface.

## Dry runs

`--dry-run` reconciles and decides without loading the signing key and without building anything. The report still carries the complete verdict, the reconciliation, and the P&L; relayer ETH reads need `AERO_BOT_RELAYER_ADDRESS` (a public address, never a secret) to be set, otherwise that one line reports unknown.

## systemd wiring

`deploy/systemd/aero-bot-cycle@.service` and `aero-bot-cycle@.timer` carry the deployment contract, pinned by tests:

- One template unit per symbol: `systemctl enable --now aero-bot-cycle@AAPLc.timer`.
- Default cadence `OnCalendar=hourly` with `Persistent=true` (missed ticks catch up) and `RandomizedDelaySec=180`; a `systemctl edit` drop-in changes the cadence.
- `Type=oneshot`, `Restart=no`: cycles never overlap and never auto-retry - the next tick reconciles.
- Sealed environment through `/etc/aero-bot/cycle.env` (mode 0600): the symbol's Safe, the relayer address, the key-source selection (chapter 1's sealed variable or 0600 key file under `/etc/aero-bot/`), and the optional reference quote.
- Hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, state under `StateDirectory=aero-bot`.
- The JSON report lands in the journal: `journalctl -u aero-bot-cycle@AAPLc.service`.

See `docs/deployment.md` (the deployment kit) for the full Ubuntu install and smoke checklist.
