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
Below the activation equity (default 1000 USDC) there is no per-name minimum and no minimum-driven reserve at all - the captain's 2026-09-28 sub-1000 correction - so the unfunded book deploys its available funds into the qualifying board; the dry-powder framing belongs to the engaged-cap scale.

- **Tiered allocation.** The top-ranked pool by weighted qualifying APR receives the largest tranche; pools inside the configurable band of the top (default: qualifying APR at or above fifty percent of the top's, `AERO_BOT_CYCLE_TIER_BAND_FRACTION` or `--tier-band`) share the remaining tiers weight-proportionally; the residual is cash.
Measured in-range discipline can weight the ranking (`weighted_apr = APR x measured in-range fraction`); names without a measurement rank on their raw APR exactly like the locked selector ranking, and wiring the live measurement is the documented follow-up.
- **Count-as-output bounds.** At most ten concurrent positions (`AERO_BOT_CYCLE_MAX_POSITIONS`, hard ceiling), no tranche below the effective minimum position size at or above the activation equity (the coherence rule below - below that equity there is no minimum at all, the captain's sub-1000 correction), and no name above the thirty-five percent concentration cap of book equity once the cap is engaged (`AERO_BOT_CYCLE_CONCENTRATION_CAP_FRACTION` - the activation ruling below); the deployed count emerges from the qualifying distribution under these bounds.
A rich board deploys up to ten; a thin book runs three-to-five plus cash.
- **Portfolio mechanics.** The 1000 USDC total cap spans every position (the LP executor counts the cycle book's tracked live positions toward it and refuses strangers); the per-pool cap and the measured one-percent depth gate are unchanged per position; the continuous drawdown latch and the day-start halt operate at portfolio equity; every position keeps its own out-of-range grace discipline.
- **Rebalancing.** A held pool whose weighted APR decayed below the switch margin versus the next qualifying in-band candidate - the selector's thirty percent margin (`AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION`) generalized from switch-to-switch to portfolio reallocation - is exited and the candidate entered, exit-before-entry inside the one step, with the exit-plus-entry gas economics passing and the minimum hold window and the gauge's early-exit penalty window respected.
- **Sequencing.** The rebalance plan runs one fixed order - safety exits, the held-inventory resolution, recenters, reallocations, then entries - so every exit lands before the entry it funds, exactly one position is funded per step, and the total cap is never breached even transiently.
A refusal or failure halts the cycle with its completed prefix as chain truth the next cycle reconciles.

### Concentration cap activation and parameter coherence (captain's rulings, gnhf 36 superseded 2026-09-28)

**CAPTAIN RULING 2026-09-28: the per-name concentration cap is IGNORED until the book reaches the activation equity (default 1000 USDC, `AERO_BOT_CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC` or `--concentration-cap-activation-usdc`; the hard ceiling is the 1000-USDC total cap itself, so a sealed override can only engage the cap sooner, never later).**
**CAPTAIN CORRECTION, same day (the sub-1000 ruling): below the activation equity there is NO per-name minimum and NO minimum-driven reserve either - the unfunded book deploys its available funds into the qualifying board.**
The correction's live evidence: the 105.73-USDC book held 78.03 USDC of free cash (about 24.59 USDC of loose METAc stock pending convergence and 3.34 USDC of unclaimed AERO beside it), the top pool qualified, and every tier target floored to the configured eighty-USDC minimum the free cash could not fund - an idle book the captain rated exactly what it was, a dry-powder reserve the unfunded book never needed.
At 1000-plus the thirty-five-percent bound governs every name beside the configured minimum, exactly as before - the captain will revisit that policy once the book is actually funded, and nothing pre-empts it.
The rulings supersede the gnhf 36 interplay directly: the gnhf 36 coherence rule (the effective minimum `max(floor, min(configured minimum, concentration clamp))` with the hard 30-USDC gas-efficiency floor, `AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC`) stays only for the sealed early-activation override, where an engaged clamp below the configured minimum can still bind - and the floor never licenses breaching an engaged cap: when the clamp itself sits below the floor, the book stays cash.

At the defaults (activation 1000, minimum 80, fraction 0.35, floor 30), the boundary table reads:

| Book equity (USDC) | Concentration cap | Effective minimum | Behavior |
| --- | --- | --- | --- |
| below 1000 | not engaged - no per-name clamp | none - no minimum at all (the sub-1000 correction) | the book deploys its available funds: at 105.73 equity with 78.03 free cash a single qualifying pool funds the whole 78.03 deployable budget, and even a ten-USDC dust book funds when its yield carries the gas cost-share bound |
| 1000 and above | engaged at 0.35 x equity (350 at 1000) | 80 (the clamp sits above the minimum) | the classic bound: no name above thirty-five percent of the book, no tranche under eighty USDC |
| early-activation override (engaged below 228.57) | engaged at 0.35 x equity | the gnhf 36 rule | the clamp governs between 85.71 and 228.57; below the 30-USDC floor the book stays cash, never breaching the engaged cap |

Every `below_min_position_size` exclusion names the effective minimum and its full derivation - the engaged bounds and the governing term, or the not-engaged line naming the activation equity - beside the percent-annotated income forgone.

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
- **The gate chain is audit evidence.** Every cycle's decision diagnostics - and therefore every `cycle_reported` audit record - carry the complete per-gate evaluation for the top-ranked pool at the exact basis the allocator judged it: nine ordered lines (`daily_loss_halt`, `reentry_cooldown`, `condition_flat`, `reference_freshness`, `emissions_floor`, `entry_size`, `gas_ceiling`, `gas_cost_vs_yield`, `range_width_solve`), each with its verdict, measurement, and bound.
  The ninth gate is the adaptive width solve itself - the executable-geometry verdict behind every entry: solved picks an in-band range, deferred holds the entry on missing or stale evidence, and cash-hold refuses on a nonpositive best candidate.
  The emissions-floor gate's bound rises to the floor plus the 30 percent dilution re-entry margin when the book carries that pool's dilution marker.
Any why-is-it-flat question is answerable from the audit store alone.
- **A flat book carries a flat label.** While no position exists the top-level reason is `flat_awaiting_entry` (qualified pools stayed unfunded) or `no_qualifying_pool` (nothing qualified), never a position-scoped reason; the tracked-symbol fields stay explicitly null, matching the pnl layer.
- **A silent idle book alerts.** When more than the configurable fraction of equity sits as cash (default eighty percent, `AERO_BOT_ALERT_IDLE_CASH_FRACTION`) while at least one pool ranks above the tier band but stays excluded by a gate or bound, the alerting email fires once per episode - the cycle book carries the episode signature, and only the first cycle of a changed cause alerts - naming the pool, the gate, the bound, and the income forgone (quoted on the conservative income basis, never the spike).

### Conservative income expectations (captain's correction, 2026-09-28)

The reference for APR is the VENUE ITSELF - aerodrome.finance/liquidity/stocks - and the venue UI genuinely displays the very high emissions APRs the engine reads (the captain saw MSTRc at 19k percent there; the engine read 25,400 percent an hour later; both real under the venue convention: the current gauge reward rate annualized against the currently staked cell value, which is thin on the stock pools).
The earlier 4-155 percent band came from DefiLlama's external screen with its haircut convention - a secondary surface, never the reference.
Deploying into high-emissions moments is the core strategy (the boosted yield thesis), so nothing excludes a high reading.
The correction, with the convention verification in [lp_execution.md](lp_execution.md)'s displayed-APR section:

- **Information, never exclusion.** The engine's qualifying emissions APR is verified to match the venue's own UI convention exactly - the gauge's reward rate x seconds per year x AERO price over the current cell's staked value (reproduced to the digit in the live decomposition; the only divergences are the frontend's own display lag and its +9,000 percent clamp).
High readings rank, qualify, and deploy exactly as before.
- **Conservative income expectations in sizing.** Wherever entry sizing or the income-forgone accounting assumes an expected daily yield, the assumption reads a conservative floor: `min(current reading, median of the trailing N-cycle readings)` per pool (default N=6 cycles - half an hour at the five-minute cadence, `AERO_BOT_CYCLE_INCOME_HISTORY_CYCLES`; hard ceiling 48).
The affected surfaces: the range-width solve, the gas cost-versus-yield sense-checks (entry, recenter, and switch economics), the out-of-range lost-yield evidence, and every income-forgone line - a transient spike never narrows a range, justifies a batch cost, or inflates a forgone-income number.
The readings thread through the cycle book (`apr_history`), so the window survives restarts; a cold book reads unchanged (the median of one reading is itself).
- **Evidence on every cycle.** Each cycle's decision diagnostics - and therefore each `cycle_reported` audit record - carry one line per floored pool naming the basis, the window, and the instantaneous reading (`conservative income basis: METAc expected-yield surfaces read 3 (about 300 percent), the trailing 6-cycle median flooring the instantaneous 281.72 (about 28,172 percent)`).
- **Display alignment.** The dashboard shows the engine's qualifying emissions APR (the venue convention) as the primary yield number - the `/api/cycle/latest-apr` endpoint and its panel - with the DefiLlama screen clearly labeled a secondary external reference under a different haircut convention.

## The fixed cycle order

1. **Reconcile first.** The Safe's live USDC, stock, and relayer-ETH balances; the complete held-NFT inventory on the pool's NFPM (`safe_position_inventory`); and, when a position is tracked, its live custody, value, and unrealized P&L against the recorded entry cost. The chain - never memory - is the source of truth. Selector cycles anchor these reads on the tracked (or held-inventory) symbol, and on the deterministic first board listing while flat; while flat they also sweep every board token's balance so unsold stock in any pool is adopted with its own symbol.
2. **Decide.** The complete locked policy engine (`PolicyEngine(LOCKED_POLICY_PARAMETERS, load_event_calendar())`) runs over one live observation assembled exactly like `aero-bot-decide`, threading the reconciled policy state: the open position (range, committed cost, entry time), held inventory, the per-pool re-entry cooldowns, and the America/New_York day-start equity anchor for the five-percent daily loss halt. In selector mode the whole board is evaluated and the selection's verdict - including a composed `pool_switch` - rides the same report and audit shapes, with every pool's qualification outcome in the decision diagnostics.
3. **Act.** Only a policy-authorized action executes, and only through the proven audited surfaces inside the existing caps and refusal catalog. Any refusal or failed delivery halts the cycle; an out-of-band condition refuses, records, and stops the cycle without acting.

The cycle appends exactly one `cycle_reported` audit record per run beside every record the executors already write (`lp_mint_planned`, `lp_execute_sent`, `lp_execute_confirmed`, and the rest). The audit chain stays the authoritative record of everything broadcast; the cycle's own memory is one self-healing JSON book beside the audit store (`AERO_BOT_CYCLE_STATE_PATH` overrides the default `cycle_state.json` next to the audit database).

## Action mapping

| Policy verdict | Cycle execution |
| --- | --- |
| `hold` | Nothing; the verdict is the product. |
| `enter` | `execute_mint` at the decision's size and exact scored aligned bounds (the adaptive solve's executable geometry, no rounding step) -> decode the minted token id from the delivery receipt's `IncreaseLiquidity` event -> `execute_stake` -> track the position. |
| `pool_switch` | The held pool's full exit sequence below, then a fresh `execute_mint` -> stake in the winning pool; a failed exit halts before any mint. |
| `recenter` | The exit sequence below, then a fresh `execute_mint` -> stake at the decision's new range. The burned-out empty NFT remains (the manual recenter batch's burn is not part of the live mapping; a later recenter or manual burn clears residuals). |
| `stop_out`, `dilution_exit`, `event_exit`, `dislocation_exit`, `defensive_exit` | `execute_unstake` (when staked) -> `execute_withdraw` -> `execute_exit_swap`; the book clears and that pool's re-entry cooldown arms (none for `event_exit`). |
| `range_grace_exit` | `execute_unstake` (when staked) -> `execute_withdraw`; the stock inventory is recorded with origin `out_of_range_exit` and the convergence machinery unwinds it, exactly like a stale-low burn. When the position sat above its range the composition is already all USDC and the exit completes flat. That pool's re-entry cooldown arms. |
| `stale_low_burn` | `execute_unstake` (when staked) -> `execute_withdraw`; the returned stock is recorded as held inventory, never swapped below the market. |
| `sell_inventory` | `execute_exit_swap` alone; the held-inventory record clears. |

The exit swap (`aero-bot-lp execute exit-swap`) is the canary-proven reverse-direction router swap productized into the LP lifecycle: exact-input of the Safe's entire stock balance, minimum output at the locked one-percent tolerance, and a hard refusal when the quoted output exceeds the 1000 USDC per-pool cap - an out-of-band inventory never moves.

## Day economics: the equity anchor and the day P&L

The five-percent daily loss halt anchors on the day-start equity at the America/New_York rollover, and that anchor prices the whole book: Safe USDC, held stock, every tracked position's marked value, and unclaimed AERO at its observed price - the same composition the engine's observation and the selector's equity gate carry.
The engine's own day-rollover reset is authoritative: on rollover it re-anchors from the fully composed observation, so a deployed position can never read as a drawdown against a cash-only anchor.
In portfolio mode the selected pool's full-equity observation advances the shared day, day-start anchor, running peak, and halt latch before the book saves; persisting the pre-observation session state instead resets the day every cycle and can admit an entry after a real five-percent loss.
The production book exposed exactly that trap on 2026-09-23 - an 80.73 anchor beside a roughly 99 book - which is why the cycle's pre-decision seed matches the composition too: whenever the book carries no anchor yet, the seed is cash plus the tracked LP mark, never cash alone.
The report and the `cycle_reported` audit record both carry `equity_usdc`, `day_start_equity_usdc`, and `day_pnl_usdc`, so the daily number the captain reads is computed where the decision happened, not reconstructed afterward.
An out-of-band cycle carries no day economics and says so in `day_diagnostic` rather than publishing a number it cannot stand behind.
Beside the anchor, the report carries the running equity high-water mark (`peak_equity_usd`) the continuous drawdown latch measures from - the captain's 2026-09-27 ruling: the peak survives the New York rollover, and a drawdown of at least five percent from it re-arms the same halt machinery on every day it holds, so a swing that crosses midnight can no longer reset the latch.

## Yield attribution (the emissions engine, visible daily)

Every cycle report - and therefore the daily report email and the `cycle_reported` audit record - decomposes the day P&L into the strategy's income streams: AERO rewards accrued since the day's baseline (unclaimed units plus anything converted today, valued at the last observed price), fees earned (computed, never the stale checkpoint - see the LP execution fee measurement below), and stock-token mark-to-market on the day's opening quantity, with an explicit unattributed residual absorbing actions, gas, collections, and every marking the components do not price.
Since the allocator ruling the decomposition runs **per position** - one row per funded name at its own pool's price, covering its AERO earned delta, its computed fees, and its own mark-to-market - with the portfolio rollup summing the rows, so the tier decisions are judged by measured income daily, name by name.
The day's baseline is snapshotted from the first cycle of each New York day - before that cycle's actions, so a reward conversion never reads as lost income - and the position-scoped fee words re-baseline when a recenter changes the tracked token.
A position's fee row is priced only while its liquidity is unchanged since the baseline: the day's fee-growth delta spans a liquidity change (a partial burn, an increase, or a full exit to zero), so the accrual before the change belonged to the baseline liquidity and the accrual after to the new one - one liquidity number cannot price it, and the row's fee component reads explicitly unmeasured ("liquidity changed since the day baseline") instead of attributing growth to liquidity that no longer backs it.
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

## The stray-stock dust floor (captain's ruling, 2026-10-03)

The reconcile's stray-stock sweep classifies every nonzero unrecorded balance at its own pool's freshly pinned snapshot price - the same block-pinned discovery snapshot the decision runs over, converted through the token's validated decimals.
A balance strictly below the per-token dust floor (default 0.01 USDC, `AERO_BOT_CYCLE_STOCK_DUST_FLOOR_USDC` or `--stock-dust-floor-usdc`) is dust: retained in the Safe, never adopted as held inventory, never swapped, because an exit swap of sub-cent stock costs more in gas than it returns.
The floor exists because the 2026-10-02 post-closeout book refused every cycle forever on exactly that shape - three sub-cent balancing-leg remainders (TSLAc, METAc, SNDKc) tripped the multi-pool unrecorded-stock halt with no economic floor, stranding a healthy book behind rounding dust.

The bound is aggregate as well as per token: the sum of every ignored dust value must stay at or below the aggregate bound (default 0.10 USDC, `AERO_BOT_CYCLE_STOCK_DUST_AGGREGATE_USDC` or `--stock-dust-aggregate-usdc`, always at least the floor), so splitting meaningful exposure across many dust-sized tokens cannot hide behind the floor - a set whose total exceeds the bound refuses the cycle exactly like any other unrecorded stock.
Both sealed overrides sit under hard one-USDC ceilings: ignored exposure stays economically meaningless, and an override may only narrow the bound, never widen it past meaninglessness.

Unpriceable is never ignorable: a nonzero balance whose value cannot be computed at the pinned snapshot - degenerate pool ratio, or decimals outside the documented ERC20 range - is unknown truth, and the cycle refuses fail-closed rather than guessing it small.
A recorded held-inventory row is never subject to the floor; its own convergence machinery still governs whatever quantity it tracks.

Ignored never means invisible: every ignored balance rides the reconciliation's diagnostics (one line per symbol with quantity and pinned value, plus the aggregate summary), the report's `ignored_stock_dust` rows, the default equity composition (every enumerated stock balance is priced into the selector's equity input at the same snapshot prices), and the audited `cycle_reported` payload (`ignored_dust_symbols` and `ignored_dust_usdc`).
Meaningful unrecorded stock is untouched by the floor: a single above-floor balance in a flat book still adopts as held inventory, multiple above-floor balances still refuse the cycle out-of-band, and the pinned cycle's anchor sweep floors its one token by the same rule.

## Crash discipline

A crashed cycle reconciles, never double-acts. Every broadcast flows through the executors' per-nonce Safe sequences - each step re-validated, freshly estimated, hash-pinned, and audit-recorded before its delivery - so a crash can only leave a completed prefix mined. On restart:

- A **crashed entry** (mint confirmed, book not saved) is adopted back only through the audit chain's own evidence: the newest confirmed mint delivery whose `IncreaseLiquidity` receipt names exactly the one live untracked position. The adopted row's symbol and cost basis both come from the execute-mode mint plan receipt-linked to that same confirmation - the plan the executor records during the build, immediately before the delivery it confirms - never from the reconcile anchor (Slipstream shares one NFPM per generation across every pool, so the adopting inventory spans all pools and proves nothing about the NFT's own) or the chain's newest plan (which a refused entry for another symbol can crown). When no plan is so linked the adoption skips with a visible diagnostic line. No evidence, no adoption - the cycle refuses out-of-band instead of guessing.
- A **crashed exit** leaves the position (or stock balance) partially unwound; reconciliation sees the true custody and the policy re-decides over it, holding any leftover stock as inventory the convergence machinery will unwind.
- **Unproven live positions** - an NFT with liquidity or owed fees that the cycle does not track and cannot prove - refuse the cycle out-of-band. Empty residual NFTs (zero liquidity, zero owed fees, like the canary's leftover 5703026) carry no exposure and no longer block entry.
- A **crashed recenter** whose withdraw leg completed on-chain but whose re-entry mint died (the 2026-09-28 11:32 UTC audit-store `database is locked` crash on METAc token 7149956) leaves exactly the trap the first rule could not name: the tracked NFT reconciles empty, the stale book keeps commanding it, and every cycle halts on the withdraw's `position_empty` refusal. The reconcile therefore drops a tracked position that is verifiably empty on-chain - zero liquidity, zero checkpointed fees, unstaked in the Safe - with a loud diagnostic line; the empty NFT carries no exposure to manage, its value already returned to the Safe, and any stock the dead re-entry's balancing swap bought is adopted as held inventory by the existing stray-stock sweep. The drop is idempotent and never touches a position holding liquidity, owed fees, or a staked NFT. The audit append that crashed that cycle now retries lock contention with bounded backoff (0.5 to 8 seconds, six attempts total) before the error may surface, so a transient local writer lock can never kill an acting cycle again.
- A **timed-out multi-entry cycle** (the 2026-09-30 12:16 UTC systemd thirty-minute timeout after TSLAc 7311805, METAc 7312392, and SNDKc 7312807 were already minted and gauge-staked, but before the aggregate book save) is repaired on two axes. First, durability: the act layer checkpoints the book to the state store the moment each mint-and-stake pair completes (`_book_with_position` saves before returning), so an interruption between siblings can never leave completed positions outside the book - the aggregate rebuild still ends the cycle as before. Second, discovery: the reconcile recovers gauge-custodied positions the Safe-owned inventory cannot see, from audit-proven evidence - every execute-mode `LP_STAKE_PLANNED` record whose recorded owner is this runner's Safe and that names an untracked NFT (the audit store is shared by every Safe that executes through it and a gauge is a shared custodian, so the plan itself must prove the candidate was minted for this Safe), paired with the newest preceding execute-mode `LP_MINT_PLANNED` budget for its symbol as the committed basis, then verified against the NFT's live custody (the pool's gauge or the Safe; the scan is bounded to the newest twelve candidates and skips burned history quietly, while a proven NFT held by a stranger refuses the cycle out-of-band - whether the custody read reports the stranger directly or refuses with `position_not_owned`). The recovery runs only in portfolio (selector) cycles: a pinned cycle skips it with a visible diagnostic naming the skipped candidate NFTs and their committed value, because its single-position book cannot represent cross-symbol rows. Recovered positions fold into the book beside surviving tracked siblings before the equity observation, so the next cycle prices them into the day anchors and the loss latch instead of misreading their value as a drawdown; an unstaked recovery rides the existing stake-recovery pass, which stakes only positions the reconcile recorded unstaked. Because the recovery proves the prior equity reading omitted value, a daily-loss latch carried for the current New York day is dropped for re-derivation exactly when positions recover - the engine's own day observation re-latches immediately on any real drawdown (a genuine loss still halts entries; the phantom 36-USDC bookkeeping omission never did), and nothing else ever clears a latched day.

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
- Sealed environment through `/etc/aero-bot/cycle.env` (mode 0600): the symbol's Safe, the relayer address, the key-source selection (chapter 1's sealed variable or 0600 key file under `/etc/aero-bot/`), the optional reference quote (single or per-symbol pairs), the optional symbol pin, the optional switch margin, the optional out-of-range grace window (`AERO_BOT_CYCLE_OUT_OF_RANGE_GRACE_MINUTES`, default 10), the optional reward-conversion threshold (`AERO_BOT_CYCLE_AERO_CONVERSION_MIN_USDC`, default 5), the optional stray-stock dust bounds (`AERO_BOT_CYCLE_STOCK_DUST_FLOOR_USDC` default 0.01 per token and `AERO_BOT_CYCLE_STOCK_DUST_AGGREGATE_USDC` default 0.10 across all ignored balances, both under the one-USDC hard ceilings), and the allocator's optional portfolio bounds (`AERO_BOT_CYCLE_TIER_BAND_FRACTION` default 0.50, `AERO_BOT_CYCLE_MAX_POSITIONS` default 10, `AERO_BOT_CYCLE_MIN_POSITION_USDC` default 80 governing only at or above the activation equity - below it there is no minimum (the sub-1000 correction), `AERO_BOT_CYCLE_CONCENTRATION_CAP_FRACTION` default 0.35, `AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC` default 30 under the configured minimum - every override under the hard ceilings).
- Hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, state under `StateDirectory=aero-bot`.
- The JSON report lands in the journal: `journalctl -u aero-bot-cycle@AAPLc.service`.

See `docs/deployment.md` (the deployment kit) for the full Ubuntu install and smoke checklist.

## The range watchtower complement

Between cycles, the always-on range watchtower (`aero-bot-watchtower`, [docs/watchtower.md](docs/watchtower.md)) polls the tracked pool's tick every few seconds and fires this same defensive exit the moment a verified trip leaves the earning range - never gated by market windows or the reference quote, and dark until the sealed enable flag arms it.

## Email alerts

Every cycle can email its summary and its alerts - position state, P&L vs entry, gas and balance floors (relayer ETH, Safe USDC), and any refusal, failure, or out-of-band condition - through a provider-agnostic SMTP transport or a Resend-style HTTP adapter, with credentials sealed in the environment and a delivery failure that never crashes the cycle.
See [the alerts documentation](docs/alerts.md) for the configuration table, the alert semantics, and the wiring.
