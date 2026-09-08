# LP canary cycle - full E2E timing report

Cycle: AAPLc/USDC, budget 7 USDC, width 1 spacing (20-tick range), 1 percent mint tolerance (captain's calibration, PR #2).
Window: 2026-09-08T03:45:12Z (read-only plan) through 2026-09-08T04:31:05Z (exit swap included).
Executor: the merged productized `aero-bot-lp execute` surface (PR #1) plus the captain-ordered exit swap through the proven audited swap path (reverse direction, `run/swap_exit.py`, discipline identical).
Safe `0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28`, relayer EOA `0x0c49cc4d53423ccd6be2bcf115a25f418649c5c9` (Keychain key; public address only).
Every broadcast is on the hash-chained audit store (`~/Library/Application Support/Aero Bot/audit.sqlite3`, sequences 114-140 this cycle).

## Outcome

- The full lifecycle executed on-chain: mint -> stake -> (hold 6m00s >= 300s min stake) -> collect -> unstake -> withdraw -> captain-ordered exit swap.
- Final Safe state: **8.972790 USDC, 0 AAPLc** (all-USDC as ordered).
- Residuals: empty position NFT 5703026 (zero liquidity, back at the Safe; burnable by a future recenter) and AERO 1.839255877811365502 (1.837715 pre-existed the campaign; ~0.0015410 earned as emissions).
- Cycle P&L on the Safe: entered tonight's execution holding 2.119135 USDC + 0.02146954 AAPLc (about 8.976 USDC at the exit price); exited holding 8.972790 USDC plus the AERO dust - approximately flat (a 20-minute position with one crossing trade; fees earned and spread/drift net to about -0.003 USDC).
- Relayer gas for all 9 broadcasts tonight: 14,436,774 gwei = 0.000014436774 ETH (about 5 cents), every delivery at the 6-mgas policy price.

## The three mint attempts (captain's requested account)

All three ran the identical command: `aero-bot-lp execute mint --symbol AAPLc --amount 7 --width-ticks 1 --confirm-broadcast`.
Each runs the full dry-run pipeline first (caps, live checkSignatures, audit) and only then broadcasts, one Safe nonce at a time.

**Attempt 1 - 03:47:17Z, refused at the plan gate after 183.8s (`insufficient_usdc_for_entry`, audit seq 115).**
The Sugar snapshot (taken at the END of discovery, ~170s in) caught the pool mid down-swing: the previous night's balancing swaps left the Safe stock-heavy (2.119135 USDC held), and the fresh composition needed 2.616034 USDC on the quote side (snapshot continuous tick about -11597.5 by the composition sawtooth).
The quote-side cap refused honestly before anything was built or signed.
The price fell 19 ticks in the following five minutes (measured -11596 at 03:45, -11615 at 03:52) - the snapshot simply drew an unlucky grid-cell position during that swing.

**Attempt 2 - 03:57:33Z, refused at the fresh-estimate gate after 199.2s (`estimate_reverted`, GS013; audits 116-118).**
Snapshot block 51025253 at 04:00:35 (continuous tick -11614.075, solved exactly from the plan's own amount ratio), range [-11630,-11610), desired 1,426,633 USDC + 1,744,817 AAPLc, 1 percent minima 1,412,366 / 1,727,368.
Plan caps passed, the SafeTx was built and its owner signature accepted by live checkSignatures (built 04:00:42), and then the fresh eth_estimateGas reverted at ~04:00:50 with `execution reverted: GS013` - the Safe's named wrapper for "required transaction failed", i.e. the inner NFPM mint reverted.
Inner reason (the exact pool revert string is not recoverable - every reachable endpoint collapses revert data to "execution reverted"):
the pool's position-slippage check, the Slipstream PSC() custom error, exactly as diagnosed in the prior campaign.
Numeric proof by reconstruction (`run/gs013_recovery.py`, liquidity pinned at the snapshot): the breach threshold is +0.041 ticks of up-drift (required USDC crosses desired/0.99 at 1,441,043), and the price was measured -11614.2 at 04:01:34 - 0.125 ticks above the snapshot and still rising (~0.13-0.2 ticks/min).
At that price the same liquidity needs 1,470,351 USDC (more than desired), so the pool binds on the USDC side and the stock actually pullable falls to about 1.68M raw, 2.8 percent below the floored minimum - a deterministic PSC revert.
The observation-to-estimate latency was only ~17 seconds (the Sugar snapshot pins at the end of discovery); one ordinary trade in that window moved the price 3x past the tolerance.
Nothing was broadcast.

**Attempt 3 - 04:04:44Z, FILLED after 201.0s (audit seq 119+).**
Snapshot block 51025469 at 04:07:46 (tick -11614), composition 1,105,446 USDC + 1,845,538 AAPLc - deeper in the pass window, quote side 1.105 USDC.
Caps passed, signature validated, the fresh estimate passed 17s after the snapshot (no adverse trade in the window), broadcast, and included in block 51025570 as tx `0x85ac42f2994ca554905cd46acc30b5a4d4e39815cc114e5e03a379e9b4399188` (487,773 gas, 0.000002926638 ETH).
The IncreaseLiquidity event (NFPM log, topic0 0x3067048b...) carries tokenId **5,703,026**; `ownerOf` confirmed the Safe.

Why attempts 1-2 refused and 3 filled, in one sentence each: attempt 1 drew a snapshot position whose composition exceeded the Safe's USDC holding (a cap refusal, zero signatures); attempt 2 passed every gate but one trade's worth of up-drift (~0.05+ ticks against a 0.041-tick tolerance at 1 percent) breached the mint minima inside the 17-second build-to-estimate window (GS013 wrapping PSC); attempt 3 drew a pass-window snapshot and no adverse trade landed before the estimate.

The structural lesson (feeds the parallel speed-and-caps worker): per-grid-cell the quote-side need sweeps 3.5 -> 0.35 USDC (sawtooth), so pass windows are transient, and the 1 percent minima tolerance at width 1 is worth only ~0.04-0.14 ticks of drift - the fill is a lottery on trade timing unless latency drops dramatically or the width widens.

## Per-action timings

Wall times include process start, Sugar discovery sweep (the dominant cost), full build, live validation, estimates, broadcast, and inclusion.
Per-step stage timings come from the executor's own reports (rebuild/validate/estimate/delivery/send/inclusion).

### Mint - attempt 3 (filled), wall 201.0s

| stage | time |
| --- | --- |
| discovery sweep + plan (incl. caps + audit) | ~197s of the 198.3s build (snapshot pinned at its end: block 51025469, 04:07:46Z) |
| SafeTx build + sign | included in build above |
| step: mint (Safe nonce 16) | rebuild 0.0ms, validate 306ms, estimate 320ms, delivery 939ms, send 314ms, inclusion 626ms |

tx `0x85ac42f2...39188`, block 51025570, 487,773 gas at 6 mgas, fee 0.000002926638 ETH.
Minted position: tokenId 5,703,026, range [-11630,-11610), 1,105,446 raw USDC + 1,845,538 raw AAPLc.

### Stake, wall 199.0s (build 189.5s: discovery + ownership reads)

| step | nonce | gas | fee (ETH) | stage timings (validate/estimate/delivery/send/inclusion) |
| --- | --- | --- | --- | --- |
| nfpm_gauge_approval `0xaa2739...855424` | 17 | 85,899 | 0.000000515394 | 327/313/1015/315/641 ms |
| gauge_deposit `0x506a77...31a8726` | 18 | 498,937 | 0.000002993622 | 318/4740*/1030/321/712 ms |

*One GS026 endpoint-lag re-read cycle (predecessor receipt lag), resolved on the bounded retry.

Post-verify: `ownerOf(5703026)` = gauge `0x43021fbb...67042f3`.
Deposit block timestamp 1788840485 (04:08:05Z); penalty window (300s, 10000 bps) cleared 04:13:05Z.

### Hold

Deposit 04:08:05Z -> collect started 04:14:57Z: 6m52s staked (>= 300s minimum; emissions accrued ~0.0015 AERO).

### Collect, wall 206.9s (build 195.7s)

| step | nonce | gas | fee (ETH) | stages |
| --- | --- | --- | --- | --- |
| gauge_get_reward `0x2a0991...e858098` | 19 | 185,480 | 0.000001112880 | validate 8894ms** / estimate 311 / delivery 939 / send 314 / inclusion 634 |

**The longer validate window includes the gauge's earned/rewards views read before the claim is signed.

### Unstake, wall 208.0s (build 196.7s)

| step | nonce | gas | fee (ETH) | stages |
| --- | --- | --- | --- | --- |
| gauge_withdraw `0x27c0f1...c8e519a` | 20 | 406,110 | 0.000002436660 | validate 8892ms / estimate 370 / delivery 953 / send 332 / inclusion 647 |

Penalty-window gate read on-chain and passed (window closed, rate 10000 bps, emissions claimed on withdraw per the gauge's verified behavior).

### Withdraw, wall 198.0s (build 187.9s)

| step | nonce | gas | fee (ETH) | stages |
| --- | --- | --- | --- | --- |
| nfpm_decrease_liquidity `0x71e968...c541a9` | 21 | 212,623 | 0.000001275738 | 357/347/996/323/640 ms |
| nfpm_collect `0x796a01...810cecf` | 22 | 158,532 | 0.000000951192 | 304/4644*/1251/537/1055 ms |

*One GS026 re-read cycle again, resolved on retry.
Post-state: NFT 5703026 empty and back at the Safe; holdings 2.037745 USDC + 2,172,446 raw AAPLc.

### Captain-ordered exit swap (entire remaining AAPLc -> USDC), wall 35.5s

`run/swap_exit.py` - the proven audited swap path (universal-router `execute`, V3_SWAP_EXACT_IN) in the reverse direction through the same Safe discipline: registry-verified symbol, known verified pool (pinned; slot0 read fresh for the quote), byte-pinned SafeTx hash, live checkSignatures, fresh estimate, buffered type-2 delivery at the 6-mgas cap with relayer preflight, audit records before receipt waits, rotating-endpoint receipt confirmation.

Quote (1.1s): 2,172,446 raw AAPLc at 319.3874 USDC/AAPLc -> expected 6,938,517 raw USDC, 1 percent floor 6,869,131.

| step | nonce | gas | fee (ETH) | stages (build/validate/estimate/delivery/inclusion) |
| --- | --- | --- | --- | --- |
| router_stock_allowance `0xff0f6f...0df9fa` | 23 | 84,972 | 0.000000509832 | 347/336/316/993/616 ms |
| swap_aaplc_to_usdc `0x059d2a...2d0ec73` | 24 | 285,803 | 0.000001714818 | 8935**/310/305/907/20100ms*** |

**Approval-signature build includes the allowance read; swap build includes the fresh slot0 re-read.
***Inclusion poll spanned two rate-limited receipt endpoints (1rpc.io plan limit, one 403) before confirming on rotation - the bounded-wait discipline worked as designed.

Realized swap output: 6,935,045 raw USDC (99.95 percent of quote, within the 1 percent floor).
Final balances: **AAPLc 0, USDC 8,972,790 raw** - the Safe is all-USDC.

## Speed observation (for the parallel speed-and-caps worker)

- Every LP action spent 188-198s in its build, overwhelmingly the per-action Sugar discovery sweep; per-step execution mechanics are sub-2s each.
- The exit swap on the known-pool fast path (slot0 + balance reads only) quoted in 1.1s and completed its whole two-tx sequence in 35.5s wall - live proof of the captain's under-a-minute target.
- Mint fill odds are latency-bound: at width 1 with 1 percent minima, the tolerance is ~0.04 ticks near the bound; discovery-pinned planning (snapshot at decision time, not 3 minutes stale) is what makes fills deterministic rather than lotteries.

## Audit trail

Sequences 114-140 on the hash-chained store: read-only plan, the two refusals with codes, the filled mint's planned/built/sent/confirmed records, and the same four-record lifecycle for every subsequent step, plus the exit swap's quote/built/sent/confirmed records.
No key material anywhere in the chain; public addresses only.
