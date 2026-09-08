# Live cycle 2 - full matrix executed, evidence-complete

Ordered 2026-09-08 ~21:40 AEST; expanded mandate and evidence standard received mid-run.
Executor: the productized `aero-bot-lp` / `aero-bot-swap` surfaces on branch `fm/aero-bot-live-cycle-2` (PR #9), including four chapters shipped during this session (gnhf 12-15, below).
Safe `0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28`, relayer EOA `0x0c49cc4d53423ccd6be2bcf115a25f418649c5c9` (Keychain key; public address only, no key material anywhere in this package).
Evidence standard: every broadcast below carries its transaction hash (BaseScan link), block, timestamp, gas, and status from a direct receipt fetch; every refusal carries its audit-chain record; every balance claim is a block-pinned read.
The machine-readable evidence package sits beside this report in `evidence/` (receipts.json, balances.json, audit-export.json - 317 audit records, sequences 153-469).

## Executive summary

- The original cycle was blocked at mint by the residual canary NFT 5703026; the captain ruled the burn into the product (gnhf 12), it was built, and the NFT was burned live at 10:41:29Z.
- One captain-ordered timed cycle plus five scenario cycles then executed: **5 positions minted, staked, held past the 300-second window, unstaked, withdrawn, burned, and swapped back to USDC** (token ids 5722835, 5723970, 5726464, 5727479, 5729078).
- 72 broadcasts total (71 LP + 1 swap-executor), every one receipt-verified: 71 confirmed, 1 honestly failed on-chain (a decreaseLiquidity drift revert, recovered by re-run).
- Emissions earned and claimed across the five stakes: **4,385,453,358,359,003 raw AERO = 0.004385453358359003 AERO** (balance-delta, block-pinned; per-cycle live `earned` reads in the table below), roughly 0.0027 USDC at the ~0.626 USDC/AERO level - the pool's emissions are real but sub-cent at this position size and hold length.
- Principal friction over the whole matrix: USDC 8,972,790 → 8,921,957 raw (**-0.050833 USDC** across 5 cycles, 13 swap legs, and 7 refused-then-retried attempts), relayer gas 0.0000992 ETH (~$0.03) paid from the EOA, not the Safe.
- Every scenario in the captain's matrix completed or was proven structurally unreachable, with the two genuinely missing paths built as chapters: the burn (gnhf 12) and the stock-to-USDC swap-back (gnhf 14).
- Two defects found and fixed mid-run: a receipt-poll 403 crash after a landed broadcast (gnhf 13) and the missing stake custody gate (gnhf 15).

## The unblock: burning the residual canary NFT (gnhf 12 live)

Pre-state (block 51037330): Safe holds 8.972790 USDC, 0 AAPLc, 1.839255877811365502 AERO, nonce 25, and NFT 5703026 (`ownerOf` = the Safe).
Real-key dry run accepted the owner signature (144,650 gas estimated), then the execute broadcast confirmed:

| action | tx | block | timestamp (UTC) | gas | outcome |
| --- | --- | --- | --- | --- | --- |
| burn 5703026 | [0x01ea92a4...b9a209](https://basescan.org/tx/0x01ea92a477514d3acef6ec655dd00acb1b53f2596541b688d4fba27713b9a209) | 51037371 | 2026-09-08T10:41:29 | 113,698 | confirmed |

Post-state (block 51037386): `ownerOf(5703026)` reverts (burned), NFPM `balanceOf` 0, nonce 26.
The CLI exited 1 despite the on-chain success - the primary RPC's receipt poll 403'd after the send; that defect became gnhf 13 (fixed, test-pinned) and the audit chain honestly carries `lp_execute_sent` at sequence 170 with the receipt above proving inclusion.

## The captain's timed cycle (cycle 1)

The exact ordered sequence, every action timed (`run/exec_markers.log` verbatim in evidence):

| step | action | wall | tx(es) | block(s) | gas | outcome |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | balancing swap 1.00 USDC -> AAPLc (`aero-bot-swap execute`) | **208s - over budget; cause: the standalone swap executor still runs the full Sugar sweep, the speed pass never covered it** | [0xcb97047f...61aa6ac](https://basescan.org/tx/0xcb97047f359997dde23f4d3564064b76e14fe23d8f9cb467b94ef7d3e61aa6ac) | 51037983 | 315,388 | confirmed; realized 313,520 raw AAPLc, slippage 0.0488% |
| 2 | mint 8.5 USDC, width 1 (4 nonces: router allowance top-up, balancing swap 4,130,322 raw USDC, both exact NFPM approvals, mint) | **72s - cause: rate-limit backoff across the four-broadcast build** | [0x2a349bb4...0257dc1f](https://basescan.org/tx/0x2a349bb4ab9487cd69df22b9fd414931e758702f8f4f71ca042dddae0257dc1f), [0x1c6b264f...85cd344](https://basescan.org/tx/0x1c6b264f2f9ae09da29c0a4a5d0bd72cbaaf8a8c29ba8df4eea5d76da85cd344), [0x62b3658d...ded944](https://basescan.org/tx/0x62b3658d42e2ef1fde7021fad9a1f9f2c6e34e81d14f3c112eb94a0af0ded944), [0x8518312a...c2e6652c](https://basescan.org/tx/0x8518312afa3ccdcdd74f887def9bf568bc7e43ce51bc02fcdd6fa9bec2e6652c) | 51038034-51038042 | 77859+291898+67872+482917 | confirmed; **token 5,722,835**, range [-11610,-11590), composition 1,777,431 USDC + 2,108,781 AAPLc raw |
| 3 | stake | 42s | [0xbe3f0489...c54544af](https://basescan.org/tx/0xbe3f0489b40dfc6b86c127dff176da9cd984681d874a56c87ea3e583c54544af) | 51038111 | 504,537 | confirmed; single tx - the canary's gauge operator approval was **reused**, no re-approval |
| 4 | hold | 6m36s (deposit 11:06:09 -> unstake 11:12:45, past the 300s window) | - | - | - | window cleared at 11:11:09 |
| 5 | unstake (gauge withdraw: auto-claims emissions + sweeps fees) | 53s | [0xee8821b3...89e772a](https://basescan.org/tx/0xee8821b373890b585a403f5b4877dce33085ae772e66e5248db640b8299e772a) | 51038309 | 415,898 | confirmed; **claimed 597,736,248,643,411 raw AERO** by the live `earned` view |
| 6 | withdraw (decrease + collect) | 45s | [0x311c7842...fa8f16](https://basescan.org/tx/0x311c7842133921304be8767d378ac92a5b7384aea2e9efe6caf6cdefedfa8f16), [0xa8a679cb...05c1bdc0](https://basescan.org/tx/0xa8a679cb9886b59d9216908a6b3725baaea361391cf975587975fc1c05c1bdc0) | 51038351-51038353 | 212,607 + 158,520 | confirmed; returned 1,768,146 raw USDC + 2,111,553 raw AAPLc (collect's on-chain transfers: 1,768,210 + 2,112,065 raw) |
| 7 | burn | 41s | [0x537926ac...00721215](https://basescan.org/tx/0x537926acd8daa46c792bc5ac4978931bb0ff5751f32493c0685862eb00721215) | 51038406 | 113,698 | confirmed; NFT cleared |
| 8 | swap-back (first-ever live, gnhf 14; approval + swap) | 44s (+31s real-key dry run) | [0x1431389b...bd5a4c1](https://basescan.org/tx/0x1431389b4097aa6dced9ec05b1b72b8312a7134fde81acfa60852024ebd5a4c1), [0x8655ac5f...c5e62d8e](https://basescan.org/tx/0x8655ac5fe68584efbf836502374ff728d362573852fbbf93a3508283c5e62d8e) | 51038456-51038458 | 84,972 + 284,507 | confirmed; 2,112,590 raw AAPLc swapped, quote 6,734,695 / floor 6,667,348 raw USDC |

Cycle 1 P&L: USDC 8,972,790 -> 8,966,008 (-0.007782), AAPLc 0 -> 0, AERO +0.000633954 (balance-delta), NFT count 0.
All other LP actions this session ran 38-53s wall on the fast path; the one-time cold sweep (first resolution after the pin store was empty) cost 198s and wrote the AAPLc pin at block 51036544.

## Scenario 1 - three consecutive complete cycles (plus the timed cycle above)

Cycles 2 and 3 complete the three-peat; all hashes receipt-verified.

**Cycle 2** (11:18:58 - 11:30:24Z): mint (66s, 4 txs, token 5,722,835's successor **5,723,970**, blocks 51038523-51038529) -> stake (41s, [0x3f85c600...d03452b](https://basescan.org/tx/0x3f85c600e3d857af5b4780dbcc46381acc382f371abe58700bdb081d2d03452b), approval reused again) -> 300s+ hold -> unstake (52s, [0xc72986f8...47856](https://basescan.org/tx/0xc72986f81a14743510dbcddf0ee72e27d5dcffa4dc97233cba6a7ef946b47856), **claimed 744,104,922,881,394 raw AERO**) -> withdraw (48s, decrease+collect blocks 51038782-51038785) -> burn (44s, [0x39a8ed70...7260ea3](https://basescan.org/tx/0x39a8ed70c82ae672cf35cef9b4a83fab02029ee2472ee6c0c874148507260ea3)) -> swap-back (50s, blocks 51038836-51038839).

**Cycle 3** (11:33 - 12:02Z) - the richest one, including two live recovery events detailed under scenario 2: after two refused mint attempts and two inventory-rebalancing partial swap-backs, the mint filled on attempt 12 (token **5,726,464**, mint tx [0x49240f14...c16d60](https://basescan.org/tx/0x49240f14a1c35911beb90352e3abf4f47aed63d95953e23d02408c5760c16d60) with its two approvals at blocks 51039346-51039352) with the **held stock covering the entire stock side - no balancing swap composed**; stake ([0x3a7ae7fa...d032f93](https://basescan.org/tx/0x3a7ae7fad01b3fec569fc16b4e82cbd2a9c8445d5e43dcd5f12830d05d032f93)); 305s hold; unstake ([0x9c11ee74...7bcc25](https://basescan.org/tx/0x9c11ee749c04ab03d9b889d36b35433b1e563588a824d78da02229b2327bcc25), **claimed 622,079,869,360,607 raw AERO**); withdraw hit the matrix's only on-chain failure (below), recovered; burn ([0x96bc2d5a...258c6a8](https://basescan.org/tx/0x96bc2d5a7a79b4555eb86ba25cd4b69c28edcedb1a7f831fe8fa3cd6a258c6a8)); residual stock swept by three swap-backs (two partial `--amount` runs of 0.005 and 0.003 whole stock, then the final full sweep).
Cycle-3 close (block 51039789): USDC 8,945,814, AAPLc 0.

Rapid-repeat findings, evidenced: the Safe nonce advanced 25 -> 96 across the session with zero nonce collisions; the router's standing USDC allowance and the NFPM gauge operator approval were **reused** after their first setup (every stake after the canary is a single tx); per-cycle dust (a few hundred raw AAPLc) was swept by each cycle's swap-back; the composition's exact NFPM approvals are re-issued per mint by design and cost 67,872-103,071 gas each.

## Scenario 2 - interrupted-cycle recovery (the critical one)

Four interruption states, each hit live and each reconciled by the next commands without double-acting:

**Post-swap (cycle 3, 11:33Z).** The mint attempt's first four nonces broadcast (router allowance [0x4215073a...af59e](https://basescan.org/tx/0x4215073aed6c265e15a2756eb35539a1aebc6c2e7a876a3503ae38caa66af59e), balancing swap [0x8810f62...2e06f0](https://basescan.org/tx/0x8810f6278edd067fb663b43029cf5ccebb392d3d713ac1c4bfcd02e5752e06f0), approvals [0x82cf2432...bb5fd](https://basescan.org/tx/0x82cf24321df3f21005726d9a6ffad8b754c25174c6f959db998e5f56792bb5fd), [0x1e8c645...57d0c4b](https://basescan.org/tx/0x1e8c645599f2b933a4091bcddabed13e1cd41917bf06c9b228f609f3e57d0c4b)) and the mint itself refused at the fresh-estimate gate (GS013, audit `lp_refused estimate_reverted`).
The Safe sat stock-heavy (2,427,666 raw AAPLc, block-pinned).
Recovery: the next mint attempts **credited the held stock** - the planner's inventory input - and once the quote side was funded (two partial swap-backs, themselves evidence-wrapped), attempt 12 composed **no balancing swap at all** and minted from held inventory.
No double-buy, no stranded stock: the final swap-back of the cycle returned the Safe to 0 AAPLc.

**Post-mint (cycle 4, 12:03Z).** Token 5,727,479 minted; the process deliberately paused 60 seconds with the NFT sitting at the Safe.
Recovery proof: a second mint refused `untracked_existing_positions` (audit sequence 402) exactly as the gate demands, and the lifecycle then continued - stake [0x42462f9d...272b158b](https://basescan.org/tx/0x42462f9dd9f754fac52e997d900d8781c9927f228e1b52510ef635bf272b158b), hold, unstake [0x7660ddda...c7f6fac](https://basescan.org/tx/0x7660dddaa77eae6e2a48f364604666eb77923d142c9ab6bb872154b6c7f6fac), withdraw, burn [0x750814a1...481d062](https://basescan.org/tx/0x750814a1e98289cfd880e14543c0f7c9cd94d23ed43ff3d8adeca2487481d062), swap-back.
The half-state was adopted, not duplicated.

**Post-stake (cycle 5, 12:19Z).** Token 5,729,078 staked ([0xd7b7924f...1890ef](https://basescan.org/tx/0xd7b7924f3cacb4ce46b6c236b03722dc512adab4d3eef3a23cb08054921890ef)); the process went dark 320 seconds, deliberately past the window.
Recovery: unstake ([0x6efea16b...3a28543](https://basescan.org/tx/0x6efea16b99c50ce26f56f2bb330591edc04f372b9aa9faaaa563495773a28543), **claimed 1,129,854,062,310,902 raw AERO**), withdraw, burn [0x881833fc...644e3cd3](https://basescan.org/tx/0x881833fca2e11c8f8b4e2dd204a428b43fe7fb5d4c0173f9d3e3016a644e3cd3), swap-back - all confirmed.

**Post-unstake (cycle 3, 11:55Z) - including the matrix's only on-chain failure.** After the unstake, the withdraw's decreaseLiquidity broadcast [0xd8ecdf4f...a8930bc](https://basescan.org/tx/0xd8ecdf4fa510e95469a5338bed6f332e8dcfc2b87ca25afb0bc130d79a8930bc) **reverted on-chain** (status 0, block 51039591, 181,324 gas burned, audited `lp_execute_failed`) - the price drifted past the 1% minima between the passing estimate and inclusion.
The executor halted at the completed prefix, exited 1, and the immediately-following burn refused `position_not_empty` (audit seq 355) - the interlock preventing a burn that would forfeit the stranded liquidity.
Recovery: one re-run of withdraw (decrease [0x16930a79...90a2b3](https://basescan.org/tx/0x16930a79848c054a103162c17de41e9a2146cba77d0582c79daadd81c690a2b3) + collect both confirmed, blocks 51039696-51039699), then the burn succeeded.
The VM-loop lesson, evidenced twice (mint GS013 refusals and this revert): at width 1 with 1% minima, every amount-bearing action is drift-exposed for the ~2-40 seconds between snapshot and inclusion; the gates make every failure honest and recoverable, and one retry has always sufficed.

## Scenario 3 - range geometry

- Every productized mint anchors its range on the live tick (`floor(tick/spacing)*spacing` +- width), so **an out-of-range mint is structurally impossible through the planner**: the `price_outside_range` gate exists for the general case, but the derived range always contains the snapshot price by construction.
The single-sided shape therefore only arises from drift after minting; all five positions happened to stay in-range through their holds (every withdraw report carries `range_state: in_range`), so a live one-sided exit did not materialize this session.
What did materialize: the drift guard refusing amounts past their minima in both directions (the cycle-3 mint GS013s above and the reverted decrease), which is the same protection a one-sided exit depends on.
- Composition spread across grid cells, all evidenced in the mint reports: cycle 1/2/4/5 sampled cells near 46/54 USDC/stock value (e.g. 1,777,431 + 2,108,781 raw), while cycle 3's quiet cell sat near 40/60 with the quote side pinned at ~3.4 USDC of the 8.5 budget - the sawtooth the canary documented, now traversed in both directions by the partial swap-backs (0.005 and 0.003 whole-stock inputs, [0x4d640d44...c6df98](https://basescan.org/tx/0x4d640d4412d11fe6c1102654bf1980f8cbed992b463ee9aa5d33d52e2cc6df98) among them) rebalancing toward the cell's needs.
- Swap sizing proved in both shapes: the entry balancing swap sized from the planner's shortfall (4,130,322 raw USDC in cycle 1, none in cycle 3's held-stock mint), and the swap-back sized from the whole or partial stock balance with the reversed 43-byte path and 1% floors - every realized output landed inside its floor.

## Scenario 4 - refusal gates, live

| gate | live proof | evidence |
| --- | --- | --- |
| `untracked_existing_positions` | second mint with the NFT at the Safe refused (cycle 4); also the original blocker, twice, pre-burn | audit seqs 153-155, 402; balances showing NFT held |
| `insufficient_usdc_for_entry` | refused whenever the quote side exceeded the live USDC balance (cycle-3 recovery, seven consecutive refusals as the cell pinned the need at ~3.4 USDC vs 2.18 held) | audit `lp_refused` records; the refusal text quotes both numbers |
| `estimate_reverted` (GS013/PSC) | three mint attempts and one withdraw attempt refused pre-broadcast on drift; one withdraw reverted on-chain post-broadcast (the only `lp_execute_failed` of the session) | audit seqs ~272-287, ~351-353; receipt 0xd8ecdf4f (status 0) |
| `position_not_empty` | the burn of the stranded post-failed-decrease position refused | audit seq 355 |
| `within_penalty_window` | **not live-triggerable, honestly reported**: inside the 300-second window the gauge's `earned` read **0 raw AERO live** in both tested cycles (statuses at ~2 min and ~3.6 min, audit `lp_status_reported` seqs 345, 291) - the gate refuses only when the window is open AND the rate is nonzero AND emissions have accrued, so with nothing accrued there is nothing to forfeit and the unstake is permitted by design; every real unstake this session ran past the window, zero penalty ever paid (all five unstake receipts status 1, full emissions claimed). The gate's firing path is pinned by the scripted suite (`test_dry_run_collect_refuses_inside_the_penalty_window` and the unstake twin) | status logs + unstake receipts |
| gas price / ETH floors | not live-forceable without draining the relayer or spiking Base gas: the floors are code-fixed (`DEFAULT_GAS_PRICE_CAP_WEI` 1 gwei, Safe floor 5e13 wei, relayer floor 2e14 wei + 2x bounded gas) and every observed gas price this session was 6-6.01 mgas, far under the cap; the scripted suite pins the refusals | caps table in `docs/lp_execution.md`; session receipts all at 6,000,000-6,010,803 wei |
| `signature_rejected` | every ephemeral-key control run refused exactly there, proving signature validation runs against the live Safe | the refusal logs in `run/logs/refusal-*` |

## Scenario 5 - idempotency (re-run each action immediately)

| double-run | result | evidence |
| --- | --- | --- |
| mint twice | refused `untracked_existing_positions`, zero broadcast | audit seq 402 |
| stake twice | **gap found**: composed a doomed gauge deposit for the already-staked token, stopped only at the ephemeral signature / would stop at the estimate gate; **fixed as gnhf 15** (upfront `position_not_staked` custody refusal, test-pinned) | the pre-fix live run + gnhf 15's scripted pin |
| unstake twice | refused `position_not_staked` ("held by the Safe, not the gauge") | cycle-4 refusal log |
| withdraw twice | refused `position_empty` on the emptied position | cycle-5 refusal log |
| burn twice | refused `position_unknown` ("ERC721: owner query for nonexistent token") | cycle-4 refusal log |
| swap-back twice | refused `nothing_to_swap_back` (zero stock balance) | cycle-4 refusal log |

No double-run broadcast anything; every second invocation is a pre-sign refusal with its audit record.

## Emissions and fees across the matrix

Per-cycle claimed emissions at unstake (live `earned` reads; balance-delta total 4,385,453,358,359,003 raw = 0.004385453358359003 AERO including claim-time drift):

| cycle | raw AERO claimed | AERO units |
| --- | --- | --- |
| 1 (5722835) | 597,736,248,643,411 | 0.000597736248643411 |
| 2 (5723970) | 744,104,922,881,394 | 0.000744104922881394 |
| 3 (5726464) | 622,079,869,360,607 | 0.000622079869360607 |
| 4 (5727479) | 1,142,845,889,099,036 | 0.001142845889099036 |
| 5 (5729078) | 1,129,854,062,310,902 | 0.001129854062310902 |

All emissions landed in the Safe's AERO balance (1,839,255,877,811,365,502 -> 1,843,641,331,169,724,505 raw, block-pinned at 51037330 and 51040711); none was swapped - **AERO sits outside the swap surfaces' token whitelist by design** (USDC plus the B20 registry), so it is not swapable "within the audited path"; the balance is reported as held.
Trading fees: each cycle's collect swept the checkpointed fees together with the returned principal (the cycle-1 collect's on-chain transfers: 1,768,210 raw USDC + 2,112,065 raw AAPLc against a mint composition of 1,777,431 + 2,108,781) - at these sizes and hold lengths the fee component is sub-cent and inseparable from price drift in the balance deltas; the net per-cycle USDC friction (-0.005 to -0.026) bounds fees-plus-spread-plus-drift combined, honestly reported as such.

## Final balances (block-pinned)

| asset | open (block 51037330) | close (block 51040711) | delta |
| --- | --- | --- | --- |
| USDC | 8,972,790 raw | 8,921,957 raw | -50,833 (-0.050833) |
| AAPLc | 0 | 0 | 0 |
| AERO | 1,839,255,877,811,365,502 raw | 1,843,641,331,169,724,505 raw | +4,385,453,358,359,003 (+0.004385 AERO) |
| ETH (Safe) | 100,000,000,000,000 wei | unchanged floor | 0 |
| ETH (relayer) | 974,614,963,677,242 wei | 875,409,699,562,319 wei | -99,205,264,114,923 (gas for 72 deliveries) |
| Safe nonce | 25 | 96 | 71 Safe transactions |

## Gaps found and chapters shipped this session

1. **gnhf 12 - `execute burn`** (the ruling): the lifecycle's terminal step, custody and emptiness gated, `lp_burn_planned` audited; live-proven on 5703026 and five cycle burns.
2. **gnhf 13 - receipt-poll outage absorption**: the live 403-after-broadcast crash fixed (rotation absorbs per-endpoint failures; bounded unconfirmed warning remains the worst case), pinned by the incident.
3. **gnhf 14 - `execute swap-back`**: the productized terminal stock-to-USDC swap (reversed concentrated path, exact router approval, planner-grade impact and tranche caps, `lp_swap_back_planned` audited); live-proven seven times including partial amounts.
4. **gnhf 15 - stake custody gates**: the idempotency gap closed (`position_not_staked` / `position_not_owned` / execute-mode `position_unknown`).

Documented, deliberately not built: AERO-to-USDC swapping (outside the token whitelist by design - a policy change, not a missing path); the standalone `aero-bot-swap` executor's full-sweep latency (recommendation: extend the pin fast path to it in a future chapter); `within_penalty_window` live firing (blocked by the gauge's zero-accrual inside 300s, not by the gate).

## Evidence manifest

- `evidence/receipts.json` - 72 receipts, one per broadcast, each with block, timestamp, gas, fee, status, BaseScan link.
- `evidence/balances.json` - 40+ block-pinned Safe-state snapshots (every action's before/after).
- `evidence/audit-export.json` - the session's 317 hash-chained audit records (sequences 153-469): 9 mints planned, 7 stakes, 7 unstakes, 8 exits, 8 burns, 9 swap-backs, 92 built transactions, 71 sent, 69 confirmed, 1 failed, 28 refusals, 3 status reports.
- `run/exec_markers.log` - every action's wall-clock window (verbatim timings); `run/logs/` - full stdout/stderr of every command.
- Reproduce any refusal offline: the audit export carries the catalog code and the action for each.
