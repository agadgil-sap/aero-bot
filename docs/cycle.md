# The scheduled decision cycle

The `aero-bot-cycle` command runs exactly one decision cycle for one registry symbol - or for the whole verified B20 board in selector mode - and exits: reconcile, decide, act. The systemd timer - never an in-process scheduler - decides when cycles run, so a wedged cycle can never overlap a second one and a missed tick catches up on the next start.

```
uv run aero-bot-cycle [--symbol AAPLc | auto] [--dry-run] [--reference-price 318.5 | SYM=PRICE,...] [--json]
```

Exit codes: zero on any completed cycle (a hold is a decision, not a failure), one on failures, two when any refusal or out-of-band condition halted the cycle.

## Pinned symbol versus selector mode (the captain's 2026-09-09 cross-board ruling)

The symbol is optional in the sealed cycle environment: `AERO_BOT_CYCLE_SYMBOL` unset, empty, or `auto` runs the cross-board selector (the default), and an explicit symbol pins one pool for operator runs - the `--symbol` flag overrides the environment, and `--symbol auto` means the selector too, so `aero-bot-cycle@auto.timer` drives selector mode with the existing template unit.

In selector mode the decide step enumerates **every verified B20 stock-token pool** - the same factory, pair, kind, uniqueness, gauge-liveness, and AERO-emission validation the screener applies over one Sugar sweep - evaluates the COMPLETE entry gate chain per pool (condition flats, reference freshness, the 150 percent emissions floor, the equity and depth caps, and the gas gate), and hands the ranked qualifying board to the portfolio allocator (the captain's gnhf 33 ruling).
The pure selection mathematics live in `src/aero_bot/selector.py`; the portfolio mathematics in `src/aero_bot/allocator.py`.

## The portfolio allocator (captain's ruling, gnhf 33)

The book runs a tiered portfolio of up to ten concurrent positions on the proven ~100-per-position scale, with the deployed count an OUTPUT of the qualifying yield distribution under the risk bounds - sometimes five positions using the full 1000 USDC total cap, sometimes cash held as dry powder for APR spikes, and never an entry bar tightened to chase deployment.

- **Tiered allocation.** The top-ranked pool by weighted qualifying APR receives the largest tranche; pools inside the configurable band of the top (default: qualifying APR at or above fifty percent of the top's, `AERO_BOT_CYCLE_TIER_BAND_FRACTION` or `--tier-band`) share the remaining tiers weight-proportionally; the residual is cash.
Measured in-range discipline can weight the ranking (`weighted_apr = APR x measured in-range fraction`); names without a measurement rank on their raw APR exactly like the locked selector ranking, and wiring the live measurement is the documented follow-up.
- **Count-as-output bounds.** At most ten concurrent positions (`AERO_BOT_CYCLE_MAX_POSITIONS`, hard ceiling), no tranche below the effective minimum position size (the gnhf 36 coherence rule below), and no name above the thirty-five percent concentration cap of book equity (`AERO_BOT_CYCLE_CONCENTRATION_CAP_FRACTION`); the deployed count emerges from the qualifying distribution under these bounds.
A rich board deploys up to ten; a thin book runs three-to-five plus cash.
- **Portfolio mechanics.** The 1000 USDC total cap spans every position (the LP executor counts the cycle book's tracked live positions toward it and refuses strangers); the per-pool cap and the measured one-percent depth gate are unchanged per position; the continuous drawdown latch and the day-start halt operate at portfolio equity; every position keeps its own out-of-range grace discipline.
- **Rebalancing.** A held pool whose weighted APR decayed below the switch margin versus the next qualifying in-band candidate - the selector's thirty percent margin (`AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION`) generalized from switch-to-switch to portfolio reallocation - is exited and the candidate entered, exit-before-entry inside the one step, with the exit-plus-entry gas economics passing and the minimum hold window and the gauge's early-exit penalty window respected.
- **Sequencing.** The rebalance plan runs one fixed order - safety exits, the held-inventory resolution, recenters, reallocations, then entries - so every exit lands before the entry it funds, exactly one position is funded per step, and the total cap is never breached even transiently.
A refusal or failure halts the cycle with its completed prefix as chain truth the next cycle reconciles.

### Parameter coherence: the effective minimum (captain's gnhf 36 ruling)

The configured eighty-USDC minimum and the thirty-five percent concentration cap were mutually unsatisfiable below 80 / 0.35 = 228.57 USDC of book equity.
The clamp forced every tranche under the minimum, so the captain's trial-scale book (about 105 USDC) refused its top-qualified pool every cycle with `below_min_position_size` while every protective gate passed.
The coherence rule ships the unblock: the EFFECTIVE per-name minimum is `max(floor, min(configured minimum, concentration clamp at the live equity))` - the clamp governs when it binds, the configured eighty governs naturally at 300-plus equity, and a hard gas-efficiency floor (default 30 USDC, `AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC`, never above the configured minimum) bounds how far the rule may lower the bound.
The floor never licenses breaching the concentration cap: when the clamp itself sits below the floor, the book stays cash - the cap is the locked safety bound.
Every `below_min_position_size` exclusion names the effective minimum and its full derivation (both bounds, the floor, the arithmetic, and the governing term) beside the percent-annotated income forgone.

At the defaults (minimum 80, floor 30, concentration fraction 0.35), the boundary table reads:

| Book equity (USDC) | Concentration clamp (0.35 x equity) | Effective minimum | Governing term | Behavior |
| --- | --- | --- | --- | --- |
| below 85.71 | below 30 | 30 | the hard floor | stays cash - the cap is never breached to reach the floor |
| 85.71 to 228.57 | 30 to 80 | the clamp | the concentration clamp | funds at the clamp (the trial-scale unblock: at 105 equity the clamp is 36.84, so the top pool funds at 36.84) |
| 228.57 and above | 80 and above | 80 | the configured minimum | the classic bound: at 300-plus equity the eighty governs naturally |

The `below_min_position_size` boundary crosses at exactly 228.57 (minimum over fraction) and 85.71 (floor over fraction) USDC equity; both crossings scale with any sealed override of the three parameters.

The anti-churn discipline is part of the same heritage:

- While a position is held, another pool displaces it only when its qualifying emissions APR exceeds the held pool's by the configurable relative switch margin (default thirty percent, `AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION` or `--switch-margin`) AND the exit-plus-entry combined gas cost passes the locked five-percent cost-share bound of the new position's expected daily gross yield - the same bound the entry gate applies to the entry alone, here over both batches' Safe overheads.
- A reallocation executes as exit-then-enter inside one cycle: the held position runs the full unstake/withdraw/exit-swap sequence first, and any refusal or failure there halts the cycle before anything is minted, so capital is freed before it re-commits.
- The existing per-pool re-entry cooldowns apply per pool: a stop-out or dilution exit arms the fifteen-minute cooldown for its own pool only, never blocking another pool's entry, and a voluntary reallocation arms no cooldown at all.
- The board never enters while unsold inventory from a stale-low burn is held; the convergence machinery resolves it first, exactly as the per-pool engine orders.

Selector cycles pay one verified Sugar sweep per run - the whole board comes from one block-pinned snapshot, which is also what makes the cross-pool APR comparison coherent - and every enumerated pool is pinned so later pinned-symbol runs keep the known-pool fast path.
Expect the sweep's honest per-page progress on the first run and on rate-limited endpoints, and tens of seconds once warm-adjacent; pinned cycles keep their seconds-scale fast path untouched.

### Entry-gate transparency and the idle book (captain's rulings, gnhf 34)

The first allocator night exposed three defects the captain rated a horrible mistake: the engine sat flat all night behind an opaque `entry_gate_refused` label while the top pool qualified, the flat book's top-level reason read the position-scoped `open_in_range`, and the dashboard's audit-health endpoint verified an empty store of its own.
The gnhf 34 rulings, all live:

- **The tranche re-derivation keeps the book's equity basis.** Each tranche re-runs the complete entry gate chain at its own scaled sizing basis, and the scaled observation now carries the full portfolio equity (`PolicyObservation.portfolio_equity_usd`) so the day-start anchor, the running peak, and both drawdown latches keep judging the whole book.
Before the fix, a small tranche's scaled equity read as a portfolio drawdown against the full-book day-start anchor and the daily loss halt refused every fresh entry - the exact overnight mechanism, with the production numbers: a 36.92 USDC tranche scaled to a 46.15 basis against a 102.57 anchor latched a fifty-five percent "drawdown".
The halt protection itself is unchanged: a true book drawdown still latches at the scaled basis, because the latch reads the portfolio equity.
- **Every exclusion names its gate, its bound, and the income forgone.** The allocator's excluded-list line carries the typed reason with its compact cause - `MSTRc (entry_gate_refused: gas_gate_deferred)`, `SNDKc (below_tier_band: weighted 15.29 below band floor 34.79)` - and each exclusion's detail line states the operational consequence: the refusing gate, its measured value, its bound, and the income forgone per day at the pool's qualifying APR, the same lost-yield framing the out-of-range diagnostics carry.
- **The gate chain is audit evidence.** Every cycle's decision diagnostics - and therefore every `cycle_reported` audit record - carry the complete per-gate evaluation for the top-ranked pool at the exact basis the allocator judged it: eight ordered lines (`daily_loss_halt`, `reentry_cooldown`, `condition_flat`, `reference_freshness`, `emissions_floor`, `entry_size`, `gas_ceiling`, `gas_cost_vs_yield`), each with its verdict, measurement, and bound.
Any why-is-it-flat question is answerable from the audit store alone.
- **A flat book carries a flat label.** While no position exists the top-level reason is `flat_awaiting_entry` (qualified pools stayed unfunded) or `no_qualifying_pool` (nothing qualified), never a position-scoped reason; the tracked-symbol fields stay explicitly null, matching the pnl layer.
- **A silent idle book alerts.** When more than the configurable fraction of equity sits as cash (default eighty percent, `AERO_BOT_ALERT_IDLE_CASH_FRACTION`) while at least one pool ranks above the tier band but stays excluded by a gate or bound, the alerting email fires once per episode - the cycle book carries the episode signature, and only the first cycle of a changed cause alerts - naming the pool, the gate, the bound, and the income forgone.

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
| `range_grace_exit` | `execute_unstake` (when staked) -> `execute_withdraw`; the stock inventory is recorded with origin `out_of_range_exit` and the convergence machinery unwinds it, exactly like a stale-low burn. When the position sat above its range the composition is already all USDC and the exit completes flat. That pool's re-entry cooldown arms. |
| `stale_low_burn` | `execute_unstake` (when staked) -> `execute_withdraw`; the returned stock is recorded as held inventory, never swapped below the market. |
| `sell_inventory` | `execute_exit_swap` alone; the held-inventory record clears. |

The exit swap (`aero-bot-lp execute exit-swap`) is the canary-proven reverse-direction router swap productized into the LP lifecycle: exact-input of the Safe's entire stock balance, minimum output at the locked one-percent tolerance, and a hard refusal when the quoted output exceeds the 1000 USDC per-pool cap - an out-of-band inventory never moves.

## Day economics: the equity anchor and the day P&L

The five-percent daily loss halt anchors on the day-start equity at the America/New_York rollover, and that anchor prices the whole book: Safe USDC, held stock, and the tracked position's marked value - the same composition the engine's observation and the selector's equity gate carry.
The engine's own day-rollover reset is authoritative: on rollover it re-anchors from the fully composed observation, so a deployed position can never read as a drawdown against a cash-only anchor.
The production book exposed exactly that trap on 2026-09-23 - an 80.73 anchor beside a roughly 99 book - which is why the cycle's pre-decision seed matches the composition too: whenever the book carries no anchor yet, the seed is cash plus the tracked LP mark, never cash alone.
The report and the `cycle_reported` audit record both carry `equity_usdc`, `day_start_equity_usdc`, and `day_pnl_usdc`, so the daily number the captain reads is computed where the decision happened, not reconstructed afterward.
An out-of-band cycle carries no day economics and says so in `day_diagnostic` rather than publishing a number it cannot stand behind.
Beside the anchor, the report carries the running equity high-water mark (`peak_equity_usd`) the continuous drawdown latch measures from - the captain's 2026-09-27 ruling: the peak survives the New York rollover, and a drawdown of at least five percent from it re-arms the same halt machinery on every day it holds, so a swing that crosses midnight can no longer reset the latch.

## Yield attribution (the emissions engine, visible daily)

Every cycle report - and therefore the daily report email and the `cycle_reported` audit record - decomposes the day P&L into the strategy's income streams: AERO rewards accrued since the day's baseline (unclaimed units plus anything converted today, valued at the last observed price), fees earned (computed, never the stale checkpoint - see the LP execution fee measurement below), and stock-token mark-to-market on the day's opening quantity, with an explicit unattributed residual absorbing actions, gas, collections, and every marking the components do not price.
Since the allocator ruling the decomposition runs **per position** - one row per funded name at its own pool's price, covering its AERO earned delta, its computed fees, and its own mark-to-market - with the portfolio rollup summing the rows, so the tier decisions are judged by measured income daily, name by name.
The day's baseline is snapshotted from the first cycle of each New York day - before that cycle's actions, so a reward conversion never reads as lost income - and the position-scoped fee words re-baseline when a recenter changes the tracked token.
The method lines ride the attribution so every surface names computed-versus-checkpointed explicitly.

## The reward conversion (AERO to USDC)

Unclaimed AERO is income sitting idle, so live cycles convert it inside the act step: once unclaimed AERO - the Safe's balance plus the staked position's `earned` - exceeds the sealed threshold in USDC at the decision's last observed price (default 5 USDC, `AERO_BOT_CYCLE_AERO_CONVERSION_MIN_USDC` or `--aero-conversion-min-usdc`), the cycle first claims through the audited collect surface (never inside the gauge's early-exit penalty window) and then swaps the Safe's entire AERO balance to USDC through the capped executor surface (`aero-bot-lp execute aero-swap` under the hood; [docs/lp_execution.md](docs/lp_execution.md) carries its refusal catalog).
The conversion is a treasury action of the act step, never a policy verdict and never executed on dry runs; a refusal or failed delivery halts the cycle like any other action.
Idle AERO counts in the book's reconcile and in the equity the halt measures, so the converted value flows into the next cycle's day economics honestly.

## Fee evidence (measurement only)

Every cycle measures the tracked position's fee economics without letting them touch a decision: the policy keeps its conservative zero fee APR, exactly as locked.
The claimable-now reading is checkpointed truth from the audited position-status read - the NFPM's `tokensOwed0/1` valued at the snapshot price - and the cycle threads it into a per-token accrual window persisted in the book (`fee_samples`, bounded to the eight most recent token ids).
The first sample reports the claimable balance and opens the window; a second sample closes a measured accrual rate and its annualized fraction of the position's marked value, all riding the cycle report (`fee_evidence`) and the audit record (`claimable_pool_fees_usdc`, `measured_fee_apr`).
The staked AERO emissions ride beside it as `claimable_aero_units`, the live `earned` reading.
The caveat is in every diagnostic: the checkpointed claimable is a lower bound the pool refreshes only on position modifications, so a flat window means the checkpoint was not refreshed, not that no fees accrued, and a falling window means a collect or checkpoint refresh landed inside it.
That limitation is now superseded for reporting by the computed fee measurement ([the LP execution fee measurement](docs/lp_execution.md)): the position status derives earned fees from the pool's own fee-growth accumulators, and the cycle's yield attribution and fee evidence carry that computed number beside the checkpointed one, each labeled with its method.
The pool keeps its conservative zero fee APR for decisions exactly as locked; both measurements are evidence, never inputs.
See [the LP execution fee-evidence section](docs/lp_execution.md) for the contract-view derivation and the staged path to a full fee APR.

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
- Sealed environment through `/etc/aero-bot/cycle.env` (mode 0600): the symbol's Safe, the relayer address, the key-source selection (chapter 1's sealed variable or 0600 key file under `/etc/aero-bot/`), the optional reference quote (single or per-symbol pairs), the optional symbol pin, the optional switch margin, the optional out-of-range grace window (`AERO_BOT_CYCLE_OUT_OF_RANGE_GRACE_MINUTES`, default 10), the optional reward-conversion threshold (`AERO_BOT_CYCLE_AERO_CONVERSION_MIN_USDC`, default 5), and the allocator's optional portfolio bounds (`AERO_BOT_CYCLE_TIER_BAND_FRACTION` default 0.50, `AERO_BOT_CYCLE_MAX_POSITIONS` default 10, `AERO_BOT_CYCLE_MIN_POSITION_USDC` default 80, `AERO_BOT_CYCLE_CONCENTRATION_CAP_FRACTION` default 0.35, `AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC` default 30 under the configured minimum - every override under the hard ceilings).
- Hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, state under `StateDirectory=aero-bot`.
- The JSON report lands in the journal: `journalctl -u aero-bot-cycle@AAPLc.service`.

See `docs/deployment.md` (the deployment kit) for the full Ubuntu install and smoke checklist.

## The range watchtower complement

Between cycles, the always-on range watchtower (`aero-bot-watchtower`, [docs/watchtower.md](docs/watchtower.md)) polls the tracked pool's tick every few seconds and fires this same defensive exit the moment a verified trip leaves the earning range - never gated by market windows or the reference quote, and dark until the sealed enable flag arms it.

## Email alerts

Every cycle can email its summary and its alerts - position state, P&L vs entry, gas and balance floors (relayer ETH, Safe USDC), and any refusal, failure, or out-of-band condition - through a provider-agnostic SMTP transport or a Resend-style HTTP adapter, with credentials sealed in the environment and a delivery failure that never crashes the cycle.
See [the alerts documentation](docs/alerts.md) for the configuration table, the alert semantics, and the wiring.
