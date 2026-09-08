# Slipstream LP lifecycle execution

## Scope of this page

This page documents the LP position lifecycle work.
It records the verified contract interface the lifecycle builds on, the pure planning layer that turns a Sugar snapshot plus Safe inventory into a capped mint plan, the executor layer that composes, signs, validates, and audits the Safe transaction sequences, and the execute surface that broadcasts them one audited nonce at a time behind an explicit confirmation flag.

## Verified Slipstream contract interface

Everything below was verified live read-only against Base mainnet on 2026-09-08, cross-checked against Aerodrome's verified sources (github.com/aerodrome-finance/slipstream, `contracts/periphery/interfaces/INonfungiblePositionManager.sol` and `contracts/gauge/interfaces/ICLGauge.sol`) and the verified ABIs on Basescan.

### Three NFPM generations; the Sugar record is authoritative

Slipstream has three deployed generations, and each has its own NonfungiblePositionManager: Initial (`0x827922686190790b37229fd06084350E74485b72`), Gauge Caps (`0xa990C6a764b73BF43cee5Bb40339c3322FB9D55F`), and Gauges V3 (`0xe1f8cd9AC4e4A65F54f38a5CdAfCA44f6dD68b53`).
The LP Sugar record's `nfpm` field carries the NFPM that minted each pool's positions (the Sugar resolves it as the pool's gauge-factory `nft()`), so the LP executor must always take the NFPM address from the pool's own Sugar record and never from a hardcoded constant.
All three deployed NFPMs expose an identical ABI for every function below; the AAPLc/USDC pool `0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0` sits on the Gauges V3 generation with gauge `0x43021fbbd01b967704ab2379f6e90e2d367042f3`.

Note that the Sugar reports an empty `symbol` string for these B20 pools, so pool matching must key on token addresses, never on the Sugar symbol.

### NonfungiblePositionManager functions

| Function | Canonical signature | Selector |
| --- | --- | --- |
| mint | `mint((address,address,int24,int24,int24,uint256,uint256,uint256,uint256,address,uint256,uint160))` | `0xb5007d1f` |
| decreaseLiquidity | `decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))` | `0x0c49ccbe` |
| collect | `collect((uint256,address,uint128,uint128))` | `0xfc6f7865` |
| setApprovalForAll | `setApprovalForAll(address,bool)` | `0xa22cb465` |
| burn | `burn(uint256)` | `0x42966c68` |
| positions | `positions(uint256)` | `0x99fbab88` |

The Slipstream `mint` carries a twelfth struct field, `uint160 sqrtPriceX96`, that Uniswap v3 does not have; it is zero whenever the pool already exists.
The eleven-field v3-style selector `0x6d70c415` does not exist on any deployed NFPM: every probe against it reverts with empty data, while the twelve-field probe from the canary Safe reached the token pull and reverted `STF` for an unapproved sender, which is the existence proof.
`decreaseLiquidity` takes `uint128` liquidity, and `collect` takes `uint128` maxima (`type(uint128).max` collects everything owed).
Mint pulls both tokens from the sender through `transferFrom`, so the Safe needs bounded exact ERC20 approvals to the NFPM before minting, in the same style as the swap executor's bounded router allowance.

`positions(uint256)` returns twelve words in this order, verified against real positions: `uint96 nonce`, `address operator`, `address token0`, `address token1`, `int24 tickSpacing`, `int24 tickLower`, `int24 tickUpper`, `uint128 liquidity`, `uint256 feeGrowthInside0LastX128`, `uint256 feeGrowthInside1LastX128`, `uint128 tokensOwed0`, `uint128 tokensOwed1`.
A burned token id reverts with the message `ID`.

### CLGauge functions

| Function | Canonical signature | Selector |
| --- | --- | --- |
| deposit | `deposit(uint256)` | `0xb6b55f25` |
| withdraw | `withdraw(uint256)` | `0x2e1a7d4d` |
| getReward | `getReward(uint256)` | `0x1c4b774b` |
| earned | `earned(address,uint256)` | `0x3e491d47` |
| rewards | `rewards(uint256)` | `0xf301af42` |
| gaugeFactory | `gaugeFactory()` | `0x0d52333c` |
| depositTimestamp | `depositTimestamp(uint256)` | `0x4ede8c85` |

The classic-gauge shape `getReward(uint256,address[])` does not exist on any deployed CL gauge and must never be encoded; the per-token form is the only claim path.
Staking requires the NFPM to carry an operator approval for the gauge first, because the gauge's `deposit` pulls the NFT with `safeTransferFrom` from the caller.

The penalty-window views live on the factory the gauge's own `gaugeFactory()` names, not on the gauge or the v3 gauges factory:

| Function | Canonical signature | Selector |
| --- | --- | --- |
| penaltyRate | `penaltyRate()` | `0xd6b7494f` |
| minStakeTimes | `minStakeTimes(address)` | `0xe782453b` |

Live on the AAPLc gauge, `gaugeFactory()` returns `0x385293cae378c813f16f0c1334d774adddf56abb`, whose `penaltyRate()` is 10000 basis points and whose `minStakeTimes(pool)` for the AAPLc pool is 300 seconds.
The gauge-side `depositTimestamp(uint256)` maps a staked token id to its stake time, so one position's window closes at exactly `depositTimestamp + minStakeTimes(pool)`.

### Lifecycle mechanics that shape the executor

- `deposit` and `withdraw` both auto-sweep pending fees to the owner through an internal collect, and `withdraw` also auto-claims accrued emissions.
- While staked, the gauge holds the NFT, so `decreaseLiquidity` from the owner is impossible: the practical unstaked cycle is withdraw, then decrease/collect, then re-deposit.
- `earned(address,uint256)` reports only for the address that staked the token; any other account reverts `NA`.
- Gauges V3 charges a penalty on `getReward` or `withdraw` before the pool's minimum stake time has elapsed.
  Live on the AAPLc pool's gauge factory: `penaltyRate()` is 10000 basis points (100 percent), the factory default minimum is 10 seconds, and the AAPLc pool override `minStakeTimes(pool)` is 300 seconds.
  Any claim or withdrawal within five minutes of depositing therefore forfeits the entire stake's accrued emissions, so a recenter cycle must respect that bound.
- Emissions accrue pool-side per staked liquidity with range containment: only liquidity whose range contains the current tick accrues, checkpoints refresh on every deposit, withdraw, and getReward, and `earned` is exactest right after the pool's global growth refreshes (any swap does that).

### Live encoding proofs

- Every builder in `src/aero_bot/lp_calldata.py` is pinned by an offline canonical-vector test built from `cast calldata` output for fixed arguments (follow `tests/test_lp_calldata.py`).
- The module's own mint calldata, built for the real AAPLc pool and the canary Safe and sent read-only to the live Gauges V3 NFPM, was consumed by the contract and reverted `STF` exactly as an unapproved sender requires - the selector, struct layout, and argument order are contract-verified end to end.
- The twelve-word `positions` decoder reproduces the frozen live return of the gauge-staked AAPLc/USDC token 5660106 word for word.

## Pure mint planning layer

`src/aero_bot/lp_plan.py` turns one block-pinned Sugar observation plus the Safe's live inventory into a complete, capped mint plan before any transaction is built or signed.
Everything in the module is offline and pure: no network calls, no signing, no mutation.
`run/lp_mint_plan_probe.py` is the read-only driver that feeds it live evidence and prints the plan; the canary output below is its verbatim capture.

### Range derivation

The operator names a half width in tick spacings per side, and the planner derives the range around the anchor `floor(current_tick / spacing) * spacing` (lower address token on the low side).
The width is clamped, never widened, so the half width in whole ticks stays at or below the 0.3 percent ceiling: 29 whole ticks, since `floor(ln(1.003) / ln(1.0001)) = 29`, mirroring the ranging module's candidate-bound math.
On a spacing-10 grid the widest permitted range is therefore two spacings per side (20 ticks), and one spacing per side - the user's own canary practice - is always available.
Solver-derived widths carry the label `solver_derived_apr` through every plan and audit surface; the manual LP surface still requires the explicit override because the solver needs a reconstructed price path (the corrected emissions-APR convention itself is live).

### Amount mathematics

Position amounts come from the exact v3 identities over raw `sqrtPriceX96`, evaluated in 60-digit Decimal arithmetic: `amount0 = L * 2**96 * (Sb - Sp) / (Sp * Sb)` and `amount1 = L * (Sp - Sa) / 2**96`.
A unit of liquidity is valued over the range to split the budget into both sides; desired amounts floor to raw units and minima sit exactly one slippage tolerance (default 1 percent, raised from 0.1 percent by the captain's calibration ruling of 2026-09-08: 0.1-percent-of-amount minima on a width-1 range leave a ~0.005-tick price tolerance on the near-bound side, which the measured pool wobble of 0.265 ticks per 180 seconds always exceeds before the observation-to-execution latency completes) below.
A snapshot price at or beyond either bound refuses rather than producing a one-sided position.
The two-sided formulas are pinned by independent inversion (each side recovers the input liquidity), the geometric-mean identity `amount0 = amount1 * 2**192 / (Sa * Sb)`, and a float cross-reference.
The exit side instead reads amounts through `position_amounts_at_sqrt_ratio`, the general-case sibling that accepts any price: below the range the position is entirely token zero (`L * 2**96 * (Sb - Sa) / (Sa * Sb)`), above it entirely token one (`L * (Sb - Sa) / 2**96`), and zero liquidity is allowed because an emptied position still owes fees.
Both functions agree exactly on a strictly inside price, and the boundary continuity, one-sided collapses, and refusal paths are pinned by their own tests.
Range classification for status reporting is exact in tick space through `position_range_state`, under the range's inclusive-lower and exclusive-upper semantics, so a snapshot tick that has left the range reports honestly even when the raw square-root price rounds near a boundary.

### Pilot cap table

Caps are enforced in a fixed order by `plan_mint_entry`, and each evaluation is labeled in the returned plan's `caps_enforced` list.

| Cap | Default bound | Refusal code |
| --- | --- | --- |
| Per-pool position | 100 USDC | `budget_above_pool_cap` |
| Total pilot exposure | 100 USDC | `budget_above_total_exposure_cap` |
| Share of pool in-range depth | 1 percent | `position_above_pool_depth_fraction` |
| Snapshot price containment | strictly inside range | `price_outside_range` |
| Budget funds both raw sides | floors both sides above zero | `budget_too_small_for_both_sides` |
| Balancing swap impact | 0.1 percent ceiling | `swap_impact_above_ceiling` |
| Safe USDC covers entry | quote side plus swap | `insufficient_usdc_for_entry` |

The depth base values the pool's active liquidity over one tick spacing per side of the current tick, which is deliberately conservative: the true range-wide depth is always larger.
The policy model itself refuses construction above any documented ceiling, so no configuration can raise the pilot caps.
The per-pool bound was raised from 50 to 100 USDC by the captain's calibration ruling (2026-09-07 ~23:45, reconfirmed 2026-09-08), so one pool may now commit the whole pilot envelope; the fleet-wide total stays 100 USDC, and every refusal code and the enforcement order are unchanged.
The verbatim canary captures further down this page predate the raise and still read the 50 USDC per-pool bound.

### Balancing swap policy

When the Safe's stock balance cannot cover the stock side, the planner sizes a USDC-to-stock swap over the buffered shortfall (0.1 percent buffer above the raw value at the snapshot price).
Modeled impact is `units / (reserve + units)` against the pool's USDC reserve: at or above the 0.1 percent ceiling the entry refuses, and above the 0.05 percent threshold the swap splits into enough equal tranches to bring each under it.
The final cap re-checks that the quote side plus the whole swap fits the Safe's USDC balance.

### Live canary plan (verbatim, 2026-09-08)

`uv run python run/lp_mint_plan_probe.py` against Base mainnet, entirely read-only:

```text
[00:41:24] registry resolved AAPLc to 0xb200000000000000000000c2e324d24d7eecd1fb
[00:47:25] pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 (tick spacing 10, tick -11643) nfpm 0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53 gauge 0x43021fbbd01b967704ab2379f6e90e2d367042f3 at snapshot block 51001368
[00:47:25] Safe holds 7.000000 USDC and 0.00623693 stock

=== AAPLc canary mint plan (read-only; nothing signed or broadcast) ===
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 at snapshot block 51001368, price 320.334780461759049052219673258671943887355368783921781179368 USDC per AAPLc
range [-11660, -11640) ticks, half width 10 ticks (explicit override), fraction 0.0010004501200210025202100120004500100001
composition: 891602 raw USDC + 1906879 raw stock, liquidity 3912790290.46945373463574767381112018529598895715196687607872, sides valued 0.891602 + 6.10839665832138633697647598323823097687976318271315982173532 USDC of the 7 USDC budget
minima 890710/1904972 raw at tolerance 0.001
pool in-range depth estimate 404072.1180955263603196782295 USDC; budget is 0.0017% of it
balancing swap: 4114602 raw USDC -> ~1284469 raw stock (1 tranche(s), impact 0.000016) covering the 1283186-unit shortfall

caps enforced in order:
  - budget at or below the 50 USDC per-pool cap
  - budget at or below the remaining 100 USDC of the 100 USDC total pilot cap
  - half width 10 ticks at or below the 0.003 ceiling (explicit override)
  - snapshot price strictly inside the derived range
  - budget at or below 0.01 of the estimated 404072.1180955263603196782295 USDC in-range depth
  - balancing swap impact 0.000016 below the 0.001 ceiling in 1 tranche(s)
  - Safe USDC covers the quote side plus the balancing swap

mint through NFPM 0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53, then stake through gauge 0x43021fbbd01b967704ab2379f6e90e2d367042f3 (deposit pulls the NFT after NFPM approval)
```

The whole entry needs 5.006204 USDC (891602 quote side plus 4114602 swap input), inside the Safe's 7 USDC, and the held 0.00623693 AAPLc credits 623693 raw units against the 1906879-unit stock side.
The plan's amounts feed `build_lp_mint_calldata` directly, which the pinned offline tests demonstrate end to end.

## LP lifecycle executor

`src/aero_bot/lp_executor.py` is the lifecycle's signing and validation layer, shaped on the swap executor's containment posture.
Every action is a manual one-shot CLI request, every hard cap is enforced in code before anything is signed, and dry runs stop at building, read-only validation, and auditing; the execute subcommands add the broadcast path behind an explicit confirmation flag with the same audit-first discipline.
There is no loop, scheduler, watcher, or policy-driven trigger anywhere in the module.

### Sequence composition

An entry composes into a sequence of individual Safe transactions, each occupying its own consecutive nonce starting at the live Safe nonce:

1. `router_allowance` - the bounded 20-USDC standing USDC allowance for the router, composed only when the live allowance cannot cover the swap input (shared bound with the swap executor, never the infinite approval).
2. `balancing_swap` - the planner's exact-input USDC-to-stock swap through the whitelisted router, with `amount_out_min` floored one slippage tolerance below the expected output.
3. `nfpm_usdc_allowance` - an exact (not standing) USDC approval to the pool's own NFPM, composed only when the live allowance is short.
4. `nfpm_stock_allowance` - the exact stock approval to the NFPM, likewise skipped when already sufficient.
5. `mint` - the twelve-field Slipstream mint through the pool's own NFPM with `sqrtPriceX96` zero.

The NFPM itself executes the mint's `transferFrom` pull (the payer is the calling Safe), so the two exact approvals target the NFPM rather than the router.
A stake composes `nfpm_gauge_approval` (only when `isApprovedForAll` reports the gauge unapproved) then `gauge_deposit`, because the gauge's `deposit` pulls the NFT with `safeTransferFrom`.
A stake dry run may name a token id that does not exist yet: the report labels the missing ownership honestly instead of refusing, so the signing and encoding path can be proven before the mint confirms (an execute attempt on an unconfirmed id refuses as `position_unknown`).
A token the gauge already holds refuses upfront as `position_not_staked` and a foreign owner as `position_not_owned` - the same custody symmetry the exit side enforces - a gate added after the live 2026-09-08 idempotency battery showed a second stake composing a doomed deposit that only the estimate gate stopped.

The exit side completes the lifecycle with seven more actions, every one first resolving the token id through `ownerOf` into one of three custodies - the Safe itself (unstaked), this pool's gauge (staked), or anything else (refused):

1. `unstake` requires the gauge's custody, reads `earned` and `rewards` for the accrued emissions, resolves the penalty window, and composes exactly `gauge_withdraw`, whose source-verified behavior auto-sweeps the position's checkpointed fees, auto-claims the accrued emissions, and returns the NFT to the Safe.
2. `withdraw` requires the Safe's custody (the gauge holding the NFT blocks every NFPM operation) and composes `nfpm_decrease_liquidity` for the position's full liquidity at the snapshot price with slippage-floored minima, then `nfpm_collect` for both fee sides; a position with no liquidity and no fees collapses to the bare collect, and one with neither refuses as empty.
3. `collect` routes by custody: staked it composes `gauge_get_reward` (checkpointed position fees never flow through the gauge; they arrive on the unstaking withdraw), unstaked it composes the NFPM `collect`.
5. `burn` is the lifecycle's terminal step: it composes exactly `nfpm_burn`, clearing one position NFT the Safe itself holds after the position emptied through its withdraw. It refuses while the gauge holds the NFT (`position_staked`), while any liquidity remains (`position_not_empty`), and while any fees are still owed (`position_not_empty`), because burning non-empty contents would forfeit them outright; the live twelve-word position view's liquidity and both owed sides ride the `lp_burn_planned` audit record as the emptiness evidence.
6. `swap-back` returns the Safe's stock inventory to USDC through the same whitelisted universal router the entry's balancing swap uses, with the 43-byte concentrated path reversed (`stock || 0x08 || tickSpacing || USDC`). It composes an exact stock approval to the router (only when the live allowance is short) then one `V3_SWAP_EXACT_IN` swap whose `amountOutMinimum` floors one slippage tolerance (the captain's calibrated 1 percent) below the snapshot-price quote; the default input is the Safe's entire live stock balance, an explicit `--amount` must not exceed it, and the modeled impact is the conservative whole-reserve bound against the pool's stock-side reserve with the entry swap's identical ceiling and tranche discipline (the executor refuses above one tranche).
7. `recenter` recycles one position into a fresh mint inside a single sequenced batch: the gauge withdraw when staked, the decrease and collect when either liquidity or fees remain, the `nfpm_burn` clearing the emptied NFT, then the planner's full entry composition (bounded router allowance, balancing swap, exact approvals, mint) over a projected inventory that credits the decrease outputs and the collected fees, and finally the gauge operator approval when missing.
8. `status` composes nothing: it is a completely read-only observation of custody, both sides' amounts and values at the snapshot price, accrued AERO, the penalty window, a quoted emissions APR, and the unrealized P&L against a supplied entry cost.

Each composed step is signed over its EIP-712 SafeTx hash, proven read-only against the live Safe with `checkSignatures`, gas-estimated with `eth_estimateGas`, and appended to the local audit chain before the report returns.
A sequenced transaction's estimate legitimately reverts while its predecessors remain unexecuted, so an estimate revert behind index zero carries the diagnostic suffix "(expected while this transaction's predecessors in the sequence remain unexecuted)" instead of being treated as an anomaly.
Every swap and mint carries a deadline eight minutes past its build time, matching the swap executor's convention.

### Execution-layer cap table

These gates run before anything is signed, in order, and each appends its label to the report's `caps_enforced`.

| Gate | Default bound | Refusal code |
| --- | --- | --- |
| Registry verified | official B20 registry validates | `registry_unverified` |
| Symbol in registry | USDC plus the registry whitelist only | `symbol_not_in_registry` |
| Pool from live discovery | Sugar-verified pool, or the known-pool fast path below | `pool_not_discovered` |
| Snapshot evidence | observation time and pin block present | `snapshot_evidence_missing` |
| NFPM and gauge present | Sugar record carries both | `pool_missing_nfpm_or_gauge` |
| Snapshot staleness | 120 seconds | `snapshot_stale` |
| No untracked position NFTs | Safe's NFPM balanceOf is zero | `untracked_existing_positions` |
| Width source | explicit `--width-ticks` required | `derived_width_unavailable` |
| Single-tranche swap | planner tranche count is one | `multi_tranche_swap_unsupported` |
| Gas price cap | 1 gwei | `gas_price_above_cap` |
| Safe ETH floor | 0.00005 ETH | `safe_eth_below_floor` |
| Relayer ETH floor (execute) | 0.0002 ETH plus twice the bounded gas cost | `relayer_eth_insufficient` |
| Broadcast confirmation (execute) | explicit `--confirm-broadcast` flag | `broadcast_confirmation_missing` |
| Rebuilt hash pin (execute) | byte-exact against the report | `rebuild_hash_mismatch` |
| Execute-time signature | live `checkSignatures` accepts again | `signature_rejected` |
| Fresh estimate (execute) | succeeds with predecessors mined | `estimate_reverted` |

### Known-pool fast path and per-run discovery pinning

The 2026-09-08 canary measured each LP action at roughly 200 seconds, about 85 percent of it the full Sugar pool enumeration re-running before every action; the captain rejected that speed (manual is about 30 seconds), and this section records the fix.
Two layers remove the repeated enumeration without weakening a single gate.

**Per-run discovery pinning.** `LiveExecutionSources.discover_pools()` runs the Sugar sweep once per run and pins its block-stamped batch for the object's lifetime, mirroring the decimals cache: every later discovery call in the same process reuses the same sweep instead of re-enumerating.
Per-action freshness never depends on that cache - the executor's staleness gate still refuses any snapshot older than 120 seconds - it only stops a single run from paying for the same sweep twice.

**The known-pool fast path.** A pool's identity is immutable contract state, so after one full sweep verifies a pool, that identity is pinned in a local cache file (`lp_pool_pins.json` beside the audit store; override with `AERO_BOT_LP_POOL_PINS_PATH`).
When a pinned pool is requested, the executor skips enumeration entirely and instead:

1. re-verifies the pinned identity live against the pool contract's own views - `token0()`, `token1()`, `tickSpacing()`, `gauge()`, `factory()`, and the gauge factory's `nft()` (the Sugar's own NFPM resolution path) - refusing nothing silently: any mismatch or unreadable view falls back to the full enumeration, which re-verifies everything the slow way and rewrites the pin;
2. reads the pool's live state at one freshly pinned block - `slot0()` for price and tick, `liquidity()` for the depth-cap base, `stakedLiquidity()`, the pool's USDC and stock `balanceOf` for the two swap-impact bases, and the gauge's `rewardToken()` and per-second `rewardRate()`;
3. builds the observation with that block and the read time, so the 120-second staleness gate applies exactly as before.

The safety invariant is unchanged: planning still re-prices at execution with fresh on-chain estimates and slippage minima - speed comes from not re-enumerating, never from skipping verification.
Every registry gate, cap, and refusal fires identically on both paths, pinned by the tests: budget caps, depth share, swap impact, USDC coverage, staleness, untracked positions, and the registry refusals.
The pin file is a cache of verified facts, never a trust root: a corrupted, stale, or hand-edited file can only ever cost speed, because identity is re-derived from the chain before any pin is trusted and every failed fast path falls back to the sweep.
The full sweep refreshes the pin after every verified resolution, and `--full-discovery` bypasses pins for one run when an operator wants the slow path explicitly.

One documented information gap: the cheap read set cannot reproduce the Sugar's staked-reserve sides (`staked0`/`staked1`), so a fast-path status run reports the emissions APR as absent rather than quoting it; a `--full-discovery` status run quotes it as before.

#### Before and after (live, read-only)

The BEFORE wall times are the canary's measured per-action markers from `run/exec_markers.log` (2026-09-08, campaign endpoint); the docs' own captured builds corroborate them (mint dry-run `build took 200482.542 ms`, stake dry-run `build took 188123.104 ms`).
The AFTER numbers are live read-only probes of the same action shapes on Base mainnet over `base.publicnode.com`, with probe-local audit and pin state, run on 2026-09-08 after the speed pass:

| Action shape | BEFORE (full sweep each action) | AFTER (known-pool fast path) |
| --- | --- | --- |
| Mint plan (`plan mint --symbol AAPLc --amount 7 --width-ticks 1`) | 201 s (canary marker; 109 s fresh reproduction on the probe endpoint) | 3.8 s |
| Position action (`status --symbol AAPLc --token-id ...`) | 199 s stake marker (canary); 100 s status sweep on the probe endpoint | 3.8 s |

Both AFTER probes refused honestly at the same downstream gates as their full-sweep twins on the same live state - the mint at `untracked_existing_positions` (the campaign Safe held a position NFT) and the status at `penalty_state_unreadable` (the foreign-staked token's `NA`) - proving the speedup changes only how the pool resolves, never what the gates enforce.
The fast path's reads are the pool's own contract views, all verified live on the AAPLc pool during the speed pass: `slot0()` returned `sqrtPriceX96 44332337155365311163694903540` at tick `-11613`, `liquidity()` and `stakedLiquidity()` answered alongside, `tickSpacing() 10`, `factory() 0xf8f2eb4940cfe7d13603dddd87f123820fc061ef`, `gauge()`, the gauge's `rewardToken() 0x940181a94a35a4569e4529a3cdfb74e38fd98631` and `rewardRate() 103143109344970222` raw per second, and the gauge factory's `nft() 0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53`.

The untracked-positions gate is deliberately fail-closed: once the Safe holds any position NFT, the total-exposure cap cannot be evaluated honestly without a live position-value read, so entry refuses until that read exists.
The `derived_width_unavailable` gate stands because the width solver needs a reconstructed price path the manual surface does not carry; the corrected emissions-APR convention is live, so the gate is about plumbing, not calibration.
The router whitelist is exactly the swap executor's single router address, enforced by a validator that rejects any other configuration.

### Exit-side gates

The exit-side actions add their own gates, each enforcing the custody and emission-safety facts the contract interface established:

| Gate | Bound | Refusal code |
| --- | --- | --- |
| Token exists | `ownerOf` resolves or the positions view decodes | `position_unknown` |
| Known custody | owner is the Safe or this pool's gauge | `position_not_owned` |
| Unstake requires staking | gauge custody | `position_not_staked` |
| NFPM ops require custody | Safe custody | `position_staked` |
| Withdraw needs contents | liquidity or fees above zero | `position_empty` |
| Burn needs emptiness | liquidity and both owed sides at zero | `position_not_empty` |
| Swap-back needs stock | Safe's live stock balance above zero | `nothing_to_swap_back` |
| Swap-back input within balance | explicit amount at or below the live balance | `swap_back_input_above_balance` |
| Swap-back impact | conservative bound below the ceiling, one tranche | `swap_back_impact_above_ceiling` / `multi_tranche_swap_unsupported` |
| Penalty window clear | clears-at at or before now when emissions accrued | `within_penalty_window` |
| Penalty state readable | all four window views answer | `penalty_state_unreadable` |

The penalty gate refuses only when all three conditions hold together: the window is still open, the penalty rate is above zero, and emissions have actually accrued.
An open window with nothing accrued forfeits nothing, so the unstake proceeds and the report still prints the window state; a zero rate makes the window moot for any balance.
Every one of the four window reads (gauge factory, penalty rate, minimum stake time, deposit timestamp) fails closed: any revert refuses the whole action with `penalty_state_unreadable` rather than guessing.

### Recenter and the restake follow-up

The recenter's mint planning runs over a projected inventory: the Safe's live balances plus the decrease outputs plus both collected fee sides, all floored to raw units.
The default budget is therefore the recycled position value itself, and an explicit `--amount` may raise it into a balancing swap for the stock side or lower it, with every pilot cap still enforced by the same planner in the same order.
The fresh mint's gauge deposit cannot be bound into the same pre-execution batch: the NFPM exposes no next-id view (`nextTokenId()` reverts, verified live), so the new token id exists only after the mint confirms.
The report's `restake_followup` therefore documents the stake command with the confirmed token id as the explicit second step, and the batch's audit record carries that follow-up verbatim.

### Position status and Aerodrome's displayed emissions APR

Status values the position's two sides at the snapshot price, reports the accrued AERO from `earned` and `rewards`, resolves the penalty window, and quotes a pool-level emissions APR in Aerodrome's own displayed convention.
The conversion lives in `aero_bot.emissions_apr` and is shared verbatim with the rehearsal reconstruction, so the two paths cannot drift: the gauge's annualized reward value (per-second rate x seconds per year x AERO price) divided by the Sugar snapshot's current-cell staked value - the staked balances `LpSugar.vy`'s concentrated branch computes via `getAmountsForLiquidity(sqrtPriceX96, sqrtRatioAtTick(tick_low), sqrtRatioAtTick(tick_high), gauge_liquidity)` over the pool's current grid cell.
That base is what Aerodrome's frontend divides over, which makes the displayed number a per-cell concentration APR - the captain's screening indicator, deliberately inflated at thin widths, not a pool-wide average yield: halving the window carried per unit of staked liquidity doubles the number.
The historical 119.7-percent "naive" reconciliation (830.0 percent displayed for AAPLc on 2026-09-07) divided by the frontend's separately displayed staked-TVL column instead - a wider, different quantity - which is exactly the factor-6.94 gap; the identified cell base for that frozen snapshot is about 210,935 USDC, and the frozen unit test reproduces the displayed 830 percent from the reward rate 0.10227763 AERO/s, the AERO price 0.5428, and that base.
The report's diagnostic also carries the width family - the same reward stream at +/-1, +/-3, and +/-10 ticks around the grid anchor - because emissions accrue per unit of staked liquidity regardless of range, so the concentration APR scales with the window the same way.
The AERO price defaults to a live read from Aerodrome's own canonical USDC/AERO volatile pair (factory `getPool` lookup, reserve ratio at the snapshot block) and fails closed as `aero_price_unreadable`; `--aero-price` remains as an explicit override for reproducibility.
The rehearsal CLI keeps its `--aero-price` override for reproducibility but defaults to the same live read pinned to its anchor block.

Live verification (2026-09-08, formula from this repo's own Sugar reads vs the frontend minutes apart):

- MSFTc 3,128.8 percent computed vs 3,106.9 displayed; SPCXc 4,125.6 vs 4,078.87; TSLAc 3,692.2 vs 3,653.54; AAPLc 1,043.9 vs 991.78 (its displayed number had moved from 945.83 to 991.78 inside the hour); GOOGLc 1,282.9 vs 1,207.98.
- The captain's anchor read live - wtSGOV/USDC (spacing 1) at 357.21 percent - computes 346.5 percent forty minutes later, inside that pool's own display drift; the convention is exactly the one-cell window his "+/-1 tick" framing names.
- AMZNc, MSTRc, SNDKc, and wtSPYM display "+9,000%": that is the frontend's display clamp, not an anomaly - the convention computes values at or above the clamp for each (MSTRc 15,639 percent, SNDKc 8,373 percent at +/-1 spacing on the same snapshot).
- NVDAc moves too fast for minute-scale comparison (its displayed number moved five-fold inside an hour on live liquidity shifts); the convention's inputs come from the same Sugar record the frontend reads, so the divergence is display lag, not formula.

The unrealized P&L is reported only against an explicitly supplied `--entry-cost`; without one the diagnostic says the entry cost is unknown rather than implying zero.
Nothing in the status path builds, signs, estimates, or audits a transaction: the single audit event is `lp_status_reported`, and the read-only proof in tests is that the Safe script's preloaded nonce and signature queues are never consumed.

### Audit chain and CLI surface

Every plan, built transaction, and refusal appends to the same append-only hash-chained SQLite store as the swap executor, with these event types: `lp_mint_planned`, `lp_stake_planned`, `lp_unstake_planned`, `lp_exit_planned`, `lp_collect_planned`, `lp_burn_planned`, `lp_swap_back_planned`, `lp_recenter_planned`, `lp_status_reported`, `lp_transaction_built`, `lp_refused`, and - on the execute path - `lp_execute_sent`, `lp_execute_confirmed`, and `lp_execute_failed`.
A recenter appends its inner mint plan as `lp_mint_planned` followed by the `lp_recenter_planned` batch record, so the recycled entry stays inspectable as a first-class plan.
Refusal records carry the executor catalog code plus the planner's own code when the planner refused, and the action name (`mint`, `stake`, `unstake`, `withdraw`, `collect`, `burn`, `swap-back`, `recenter`, `status`), so both layers' decisions stay inspectable offline.
The CLI exits zero on success, one on failures, and two on any refusal, with the catalog code printed to stderr as `refused [<code>]`.

```text
aero-bot-lp [--full-discovery] plan mint --symbol AAPLc --amount 7 --width-ticks 1
aero-bot-lp dry-run mint --symbol AAPLc --amount 7 --width-ticks 1
aero-bot-lp dry-run stake --symbol AAPLc --token-id 0
aero-bot-lp dry-run unstake --symbol AAPLc --token-id 0
aero-bot-lp dry-run withdraw --symbol AAPLc --token-id 0
aero-bot-lp dry-run collect --symbol AAPLc --token-id 0
aero-bot-lp dry-run burn --symbol AAPLc --token-id 0
aero-bot-lp dry-run swap-back --symbol AAPLc [--amount 0.021]
aero-bot-lp dry-run recenter --symbol AAPLc --token-id 0 --width-ticks 1 [--amount 12]
aero-bot-lp execute mint --symbol AAPLc --amount 7 --width-ticks 1 --confirm-broadcast
aero-bot-lp execute stake --symbol AAPLc --token-id 0 --confirm-broadcast
aero-bot-lp execute unstake --symbol AAPLc --token-id 0 --confirm-broadcast
aero-bot-lp execute withdraw --symbol AAPLc --token-id 0 --confirm-broadcast
aero-bot-lp execute collect --symbol AAPLc --token-id 0 --confirm-broadcast
aero-bot-lp execute burn --symbol AAPLc --token-id 0 --confirm-broadcast
aero-bot-lp execute swap-back --symbol AAPLc [--amount 0.021] --confirm-broadcast
aero-bot-lp status --symbol AAPLc --token-id 0 --aero-price 0.30 [--entry-cost 7]
```

Every subcommand accepts `--json` for the complete typed report; the dry-run and execute subcommands accept `--ephemeral-key` to sign with a throwaway key whose signature check then honestly reports rejection.
Status takes no key at all, because nothing is signed, and requires the AERO price explicitly rather than reading one from an unregistered source.

## Execute path (broadcast surface)

The `execute` subcommands productize the canary driver's proven send loop as a first-class CLI surface with the same containment posture as the swap executor: read-only by default, broadcast only behind the explicit `--confirm-broadcast` flag, and a refusal - `broadcast_confirmation_missing` - audited and exited as code two without it before anything is built.
The recenter action has no execute form: its restake is a documented follow-up command by design, so it stays a dry-run-only batch until that composition changes.

Each execute runs the complete dry-run build first - every cap, refusal, live `checkSignatures` validation, and audit record, carrying `mode: execute` - and then broadcasts one Safe nonce at a time:

1. The built step is rebuilt from its exact transaction and the SafeTx hash is pinned byte-for-byte against the report's; any mismatch refuses as `rebuild_hash_mismatch` so substituted content can never broadcast.
2. The owner signature is proven against the live Safe again; a rejection refuses as `signature_rejected` before anything is sent.
3. A fresh `eth_estimateGas` runs with every predecessor mined, so a revert is a genuine refusal - `estimate_reverted` - that stops the sequence honestly at the completed prefix. The one observed transient - a `GS026` whose receipt simply had not reached the estimating endpoint's latest block yet - earns three bounded fresh re-reads four seconds apart, exactly the live behavior seen during the canary.
4. The delivery transaction is built exactly like the swap executor's: type 2, `to` the checksummed Safe, gas limit at the fresh estimate buffered by a fifth, both fee parameters at the observed gas price capped at the one-gwei policy, and the relaying EOA's pending nonce. The relayer preflight demands the policy floor (0.0002 ETH) and twice the bounded gas cost, refusing as `relayer_eth_insufficient` otherwise.
5. `eth_sendRawTransaction` lands, the transaction hash prints immediately to stderr, and the `lp_execute_sent` audit record is appended BEFORE any receipt wait - the audit chain, not a receipt poll, is the source of truth for what was broadcast.
6. The receipt is awaited under one bounded 600-second wait, polling every configured endpoint round-robin (the primary plus `base.publicnode.com`, `1rpc.io/base`, and `base.drpc.org`) every three seconds. A failing endpoint - a rate-limit 403, a 5xx, a transport error - is absorbed and the rotation moves on, because one endpoint's outage must never abandon a broadcast that already landed (the live 2026-09-08 burn of token 5703026 confirmed on-chain while its primary endpoint 403'd; the fix pins that incident as a test). Exhaustion reports the step as `unconfirmed` with a warning diagnostic and halts the sequence - never relabeled a failure, because a landed broadcast was mislabeled once before. An included-but-reverted delivery audits `lp_execute_failed` and exits one; a confirmed one audits `lp_execute_confirmed` and proceeds to the next nonce.

Every step report carries the full timing capture (rebuild, validate, estimate, delivery, send, inclusion), the gas and fee actually consumed, and the relayer nonce - the same evidence the canary's timing report needs.

### Live broadcast evidence (2026-09-08, canary driver)

This surface is the productized form of the `run/lp_canary.py` driver that executed the first real LP broadcasts through the canary Safe during the 2026-09-08 campaign.
Seven Safe transactions mined on Base mainnet (nonces 6-12), each hash-pinned, re-validated, freshly estimated, delivered at 6 mgas, receipted in 0.6-0.8 seconds, and audited:

| Safe nonce | Role | Transaction | Gas used | Fee (wei) |
| --- | --- | --- | --- | --- |
| 6 | balancing_swap | `0xc0c8bd1bf27d168acfb0c6c6490674dda75c59be0f61f50bd4ef641dd5a2e83f` | 286,348 | 1,718,088,000,000 |
| 7 | nfpm_usdc_allowance | `0x491037215ee4057486dc8eb56526e912888a7d93b3815cdd278c42a3a8a90ae3` | 94,959 | 569,754,000,000 |
| 8 | balancing_swap | `0x1fc5c6282c688a482ccd37e82b8842cb8196d3867074e2c3dc387ec247a9bdce` | 295,303 | 1,787,925,302,135 |
| 9 | nfpm_stock_allowance | `0xa6150472e748a6ecf7e9278f1f8feafe8a4da6056358f1339e64276f004f9a77` | 84,972 | 509,832,000,000 |
| 10 | nfpm_usdc_allowance | `0x65aca1a813a675b7baf99657bf309aa527ae3516331185cb88eb209a20145316` | 77,859 | 467,154,000,000 |
| 11 | nfpm_usdc_allowance | `0xe962b3baa2dbb19b465123f709154f864b89ca09e8dad116187d72779b013256` | 77,847 | 467,082,000,000 |
| 12 | nfpm_usdc_allowance | `0x87f437aa2c2e8060666f5d997a49991aa74431a4b72c5903c6e9016cf13da6d3` | 77,859 | 467,154,000,000 |

The mint step itself never broadcast: its fresh estimate refused honestly four times as `estimate_reverted` with the inner `PSC` revert, which the campaign's minima evidence (in the canary campaign data folder) traces to the 0.1-percent-of-amount minima convention on a 20-tick range - a measurement-backed calibration gap awaiting the captain's ruling, not a machinery fault.
The AERO token address recorded in the campaign brief also carried a one-character typo; the executor resolves the reward token from the gauge's own `rewardToken()` view instead.

### Live real-key dry-run evidence (verbatim, 2026-09-08)

The captures below ran against Base mainnet with the real Keychain key (`bot-signing-key` / `aero-bot`, the relayer EOA `0x0c49...c5c9`) and the real canary Safe.
Nothing was broadcast in these captures: dry runs never broadcast, and each capture's only chain writes are local audit appends.

The plan, read-only:

```text
$ uv run aero-bot-lp plan mint --symbol AAPLc --amount 7 --width-ticks 1
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 (snapshot block 51003042), budget 7 USDC
range [-11650, -11630) (10 ticks per side, explicit_override)
mint 908540 + 1903484 raw units, minimums 907631 + 1901580; balancing swap 4099634 raw USDC in for 1281070 raw units expected (impact 0.000007, 1 tranche(s))
```

The mint dry-run with the real key (exit code 0):

```text
$ AERO_BOT_KEYCHAIN_SERVICE=bot-signing-key AERO_BOT_KEYCHAIN_ACCOUNT=aero-bot \
    uv run aero-bot-lp dry-run mint --symbol AAPLc --amount 7 --width-ticks 1
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 (snapshot block 51003158), budget 7 USDC
range [-11650, -11630) (10 ticks per side, explicit_override)
mint 1547255 + 1703585 raw units, minimums 1545707 + 1701881; balancing swap 3459917 raw USDC in for 1080972 raw units expected (impact 0.000006, 1 tranche(s))
safe 0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28, relayer 0x0c49cc4d53423ccd6be2bcf115a25f418649c5c9 (Keychain key, nothing broadcast)
gas price 6000000 wei, Safe ETH 100000000000000 wei, router allowance 19000000 raw USDC, NFPM allowances 0 USDC / 0 stock
[balancing_swap] swap 3459917 raw USDC for at least 1079891 raw AAPLc covering the 1079892-unit shortfall
[balancing_swap] safeTxHash 0xb81761d1d267eba4e90fd6b3eb0163c36c20155fd71f40165d0e70dd1258d0f3 (nonce 6)
[balancing_swap] target 0xcaf22ce31298cf2bf1d152862f80216478ad7c67, calldata digest 0x8db40576d4ff80c4adbc2cde949081f97a57276bb95ac71ff772e2e509e2b304
[balancing_swap] signature accepted by live checkSignatures, 312932 gas estimated
[nfpm_usdc_allowance] approve exactly 1547255 raw USDC to the NFPM for the mint pull
[nfpm_usdc_allowance] safeTxHash 0x15b37e7df37c7183dfa74fb092b488d9a07e0283b91af23e341de1fca1142254 (nonce 7)
[nfpm_usdc_allowance] target 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913, calldata digest 0xb2a6901ab9e78c2bca9d0a9f887360bca95c5cd49d3dfc2dbaefc1e7be92de34
[nfpm_usdc_allowance] signature accepted by live checkSignatures, no estimate: the on-chain estimate reverted: RPC call reverted: execution reverted: GS026 (expected while this transaction's predecessors in the sequence remain unexecuted)
[nfpm_stock_allowance] approve exactly 1703585 raw AAPLc to the NFPM for the mint pull
[nfpm_stock_allowance] safeTxHash 0x0ef8c73e52c4deba10c348d80c8cbfbe8c49a436ac24fc74201fab9532806474 (nonce 8)
[nfpm_stock_allowance] target 0xb200000000000000000000c2e324d24d7eecd1fb, calldata digest 0xd8c87c83d2a9983325b16778adc435f6d052cf432cec05e34332880717536f08
[nfpm_stock_allowance] signature accepted by live checkSignatures, no estimate: the on-chain estimate reverted: RPC call reverted: execution reverted: GS026 (expected while this transaction's predecessors in the sequence remain unexecuted)
[mint] mint range [-11650, -11630) with 1547255 + 1703585 raw units
[mint] safeTxHash 0xf6afbf28756d9dcf6b15ee77d47bbf91fcd2c18754c793ad2bc1879bc99aca21 (nonce 9)
[mint] target 0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53, calldata digest 0x6e2cc90250cb23e54a79fea5c668524f92c6ee6a632ff4eae660b0528066a98a
[mint] signature accepted by live checkSignatures, no estimate: the on-chain estimate reverted: RPC call reverted: execution reverted: GS026 (expected while this transaction's predecessors in the sequence remain unexecuted)
build took 200482.542 ms
```

Three behaviors in that capture are the layer working as designed.
The `router_allowance` step is absent because the live 19-USDC standing allowance (set by the canary swap) already covers the 3.459917-USDC swap input, so the sufficiency check skipped it.
Every signature is accepted by the live Safe, which proves the Keychain key is the real owner key and the EIP-712 hashing chain is byte-exact.
Only the first transaction's estimate succeeds (312932 gas at nonce 6, the Safe's live nonce); the successors revert GS026 because the Safe validates a transaction against on-chain state that its unexecuted predecessors have not yet produced, which is exactly the labeled expectation rather than a fault.

The stake dry-run with the real key against a not-yet-minted token id (exit code 0), proving the machinery pre-mint with honest labels:

```text
$ AERO_BOT_KEYCHAIN_SERVICE=bot-signing-key AERO_BOT_KEYCHAIN_ACCOUNT=aero-bot \
    uv run aero-bot-lp dry-run stake --symbol AAPLc --token-id 0
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0, NFPM 0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53, gauge 0x43021fbbd01b967704ab2379f6e90e2d367042f3, token 0
ownerOf unavailable: ownerOf reverted, so the token is not minted yet or the id is unknown (RPC call reverted: execution reverted: ERC721: owner query for nonexistent token); a pre-mint dry run proves machinery only, and an execute attempt will refuse until the mint confirms
position view unavailable: positions view unavailable: RPC call reverted: execution reverted: ID
safe 0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28, relayer 0x0c49cc4d53423ccd6be2bcf115a25f418649c5c9 (Keychain key, nothing broadcast), gauge operator approval required
gas price 6000000 wei, Safe ETH 100000000000000 wei
[nfpm_gauge_approval] approve gauge 0x43021fbbd01b967704ab2379f6e90e2d367042f3 as NFPM operator so deposit can pull the NFT
[nfpm_gauge_approval] safeTxHash 0xb485526abc5d70516dbf27f3a309fc7c55b312024dbcf3a7a45a2024ff90fa50 (nonce 6)
[nfpm_gauge_approval] target 0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53, calldata digest 0x5d838d5175e7fa16c891d3f05d372e1b11d98183363fc92b0460c7c179ab61e8
[nfpm_gauge_approval] signature accepted by live checkSignatures, 86823 gas estimated
[gauge_deposit] stake token 0 into the gauge
[gauge_deposit] safeTxHash 0x3e85481325a2344a805ec4a4b9fd9129d312e895f2a2efc082d8c156a92f2a2e (nonce 7)
[gauge_deposit] target 0x43021fbbd01b967704ab2379f6e90e2d367042f3, calldata digest 0x43f76dddee1f0972ef564823c9fc8753a59149f8d7c8cb2a1142ee26af1f5489
[gauge_deposit] signature accepted by live checkSignatures, no estimate: the on-chain estimate reverted: RPC call reverted: execution reverted: GS026 (expected while this transaction's predecessors in the sequence remain unexecuted)
build took 188123.104 ms
```

Both dry runs refuse nothing because every gate passes on the live state: the registry validates, the pool and its NFPM and gauge resolve from live discovery, the snapshot is fresh, the Safe holds no position NFTs, the swap plans a single tranche, the 6-mwei gas price sits far below the 1-gwei cap, and the Safe's 0.0001 ETH clears the floor.
The build duration is dominated by the Sugar pool enumeration over the public RPC; the signing and validation themselves are local and instant.

### Live read-only refusal evidence (verbatim, 2026-09-08)

Two exit-side captures against Base mainnet, neither touching any key material (the first is the keyless status command, the second signs with a throwaway ephemeral key); both refuse honestly and exit two.

A status against a token id the NFPM has never minted:

```text
$ uv run aero-bot-lp status --symbol AAPLc --token-id 999999999 --aero-price 1
refused [position_unknown]: token 999999999 has no position on NFPM 0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53 (RPC call reverted: execution reverted: ID); verify the token id and pool symbol
exit=2
```

An unstake against the real gauge-staked token 5660106 - a position the canary Safe never staked, so it is foreign despite the gauge's custody:

```text
$ uv run aero-bot-lp dry-run unstake --symbol AAPLc --token-id 5660106 --ephemeral-key
refused [penalty_state_unreadable]: the gauge read earned(address,uint256) reverted (RPC call reverted: execution reverted: NA); the accrued emissions and penalty exposure cannot be established, so the attempt is refused rather than guessed at
exit=2
```

The `NA` revert is the gauge's own answer: `earned(owner, tokenId)` reports only for the address that staked the token, and the canary Safe is not 5660106's staker.
The executor treats that unreadable reward state exactly like an unreadable penalty window - fail-closed with the chain's reason quoted verbatim - rather than assuming zero emissions and proceeding into a withdraw that could forfeit someone else's accrued rewards.
Once the canary's own mint and stake confirm, its positions resolve through the same reads with the Safe as the staker, and the unstake path opens.

## Corrected replays under the displayed convention

The multi-pool rehearsal re-ran under the corrected convention (2026-09-08, live AERO price 0.626339 USDC read at anchor block 51029890, synthetic stress on, derived-width mode; ledgers preserved at `data/aero-bot-lp-canary-campaign/corrected-rehearsal-ledgers.json`):

| pool | window | obs | entries | sample entry APR | fees+AERO USDC | impact+gas USDC | net P&L USDC |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SPCXc | 2d | 6,664 | 33 | 1,623.8% | 0.153 | 0.155 | +0.067 |
| TSLAc | 2d | 2,923 | 6 | 3,744.6% | 0.125 | 0.030 | +0.098 |
| SNDKc | 2d | 2,540 | 23 | 11,695.2% | 0.141 | 0.113 | +0.108 |
| MSFTc | 2d | 7,940 | 39 | 6,300.5% | 0.132 | 2.234 | -2.072 |
| NVDAc | 2d | 13,630 | 11 | 1,573.0% | 0.982 | 0.149 | +0.728 |
| MSTRc | 2d | 3,672 | 42 | 37,499.6% | 0.426 | 0.171 | +0.392 |
| GOOGLc | 2d | 6,898 | 9 | 1,360.1% | 0.711 | 0.126 | +0.224 |
| AMZNc | 2d | 5,017 | 17 | 10,628.4% | 0.394 | 0.077 | +0.323 |
| METAc | 2d | 5,398 | 13 | 623.4% | 0.719 | 0.160 | +0.413 |
| AAPLc | 12h | 2,443 | 6 | 899.4% | 0.359 | 0.082 | -0.005 |

AAPLc ran on a twelve-hour window because the public RPC hard-fails its two-day swap-log volume (`eth_getLogs` 500 after bounded retries, reproduced four times); every other pool covered the full two-day window.

Versus the old numbers, nothing carries forward: the understated 4.35-35.4 percent series meant the 150-percent raw-emissions entry gate never opened, so the old replays produced no entries at all; under the corrected convention every pool clears the gate and the decisions now turn on the depth, dislocation, and defensive gates instead.
The economics that matter are small in absolute terms (a 200-USDC rehearsal stake over two days): eight of ten pools net positive on emissions plus fees against impact and gas, MSFTc loses to swap impact in a thin executable depth (2.07 USDC of impact across 39 entries), and AAPLc is flat on a quiet twelve-hour window.
The corrected lesson for the policy layer: emissions APRs in the hundreds-to-thousands of percent are real but concentrated - the width solver's inputs, not the entry gate, are what should discipline deployment, which is exactly the strategy/policy E2E's next scope.
