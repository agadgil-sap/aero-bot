# Slipstream LP lifecycle execution

## Scope of this page

This page documents the LP position lifecycle work.
It records the verified contract interface the lifecycle builds on, the pure planning layer that turns a Sugar snapshot plus Safe inventory into a capped mint plan, and the executor layer that composes, signs, validates, and audits the Safe transaction sequences without ever broadcasting them.
The canary broadcast procedure is added when that surface lands.

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

The classic-gauge shape `getReward(uint256,address[])` does not exist on any deployed CL gauge and must never be encoded; the per-token form is the only claim path.
Staking requires the NFPM to carry an operator approval for the gauge first, because the gauge's `deposit` pulls the NFT with `safeTransferFrom` from the caller.

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
Solver-derived widths carry the label `solver_pre_fix_apr` through every plan and audit surface until the APR convention fix lands, so nothing derived from the understated APR can masquerade as an operator override.

### Amount mathematics

Position amounts come from the exact v3 identities over raw `sqrtPriceX96`, evaluated in 60-digit Decimal arithmetic: `amount0 = L * 2**96 * (Sb - Sp) / (Sp * Sb)` and `amount1 = L * (Sp - Sa) / 2**96`.
A unit of liquidity is valued over the range to split the budget into both sides; desired amounts floor to raw units and minima sit exactly one slippage tolerance (default 0.1 percent) below.
A snapshot price at or beyond either bound refuses rather than producing a one-sided position.
The two-sided formulas are pinned by independent inversion (each side recovers the input liquidity), the geometric-mean identity `amount0 = amount1 * 2**192 / (Sa * Sb)`, and a float cross-reference.

### Pilot cap table

Caps are enforced in a fixed order by `plan_mint_entry`, and each evaluation is labeled in the returned plan's `caps_enforced` list.

| Cap | Default bound | Refusal code |
| --- | --- | --- |
| Per-pool position | 50 USDC | `budget_above_pool_cap` |
| Total pilot exposure | 100 USDC | `budget_above_total_exposure_cap` |
| Share of pool in-range depth | 1 percent | `position_above_pool_depth_fraction` |
| Snapshot price containment | strictly inside range | `price_outside_range` |
| Budget funds both raw sides | floors both sides above zero | `budget_too_small_for_both_sides` |
| Balancing swap impact | 0.1 percent ceiling | `swap_impact_above_ceiling` |
| Safe USDC covers entry | quote side plus swap | `insufficient_usdc_for_entry` |

The depth base values the pool's active liquidity over one tick spacing per side of the current tick, which is deliberately conservative: the true range-wide depth is always larger.
The policy model itself refuses construction above any documented ceiling, so no configuration can raise the pilot caps.

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
Every action is a manual one-shot CLI request, every hard cap is enforced in code before anything is signed, and this release has no broadcast path at all: the `aero-bot-lp` command stops at building, read-only validation, and auditing.
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
A stake dry run may name a token id that does not exist yet: the report labels the missing ownership honestly instead of refusing, so the signing and encoding path can be proven before the mint confirms.

Each composed step is signed over its EIP-712 SafeTx hash, proven read-only against the live Safe with `checkSignatures`, gas-estimated with `eth_estimateGas`, and appended to the local audit chain before the report returns.
A sequenced transaction's estimate legitimately reverts while its predecessors remain unexecuted, so an estimate revert behind index zero carries the diagnostic suffix "(expected while this transaction's predecessors in the sequence remain unexecuted)" instead of being treated as an anomaly.
Every swap and mint carries a deadline eight minutes past its build time, matching the swap executor's convention.

### Execution-layer cap table

These gates run before anything is signed, in order, and each appends its label to the report's `caps_enforced`.

| Gate | Default bound | Refusal code |
| --- | --- | --- |
| Registry verified | official B20 registry validates | `registry_unverified` |
| Symbol in registry | USDC plus the registry whitelist only | `symbol_not_in_registry` |
| Pool from live discovery | Sugar-verified pool required | `pool_not_discovered` |
| Snapshot evidence | observation time and pin block present | `snapshot_evidence_missing` |
| NFPM and gauge present | Sugar record carries both | `pool_missing_nfpm_or_gauge` |
| Snapshot staleness | 120 seconds | `snapshot_stale` |
| No untracked position NFTs | Safe's NFPM balanceOf is zero | `untracked_existing_positions` |
| Width source | explicit `--width-ticks` required | `derived_width_unavailable` |
| Single-tranche swap | planner tranche count is one | `multi_tranche_swap_unsupported` |
| Gas price cap | 1 gwei | `gas_price_above_cap` |
| Safe ETH floor | 0.00005 ETH | `safe_eth_below_floor` |

The untracked-positions gate is deliberately fail-closed: once the Safe holds any position NFT, the total-exposure cap cannot be evaluated honestly without a live position-value read, so entry refuses until that read exists.
The `derived_width_unavailable` gate stands until the emissions-APR convention fix lands, because the solver's APR input is known understated; no solver-derived width can masquerade as an operator override in the meantime.
The router whitelist is exactly the swap executor's single router address, enforced by a validator that rejects any other configuration.

### Audit chain and CLI surface

Every plan, built transaction, and refusal appends to the same append-only hash-chained SQLite store as the swap executor, with four new event types: `lp_mint_planned`, `lp_stake_planned`, `lp_transaction_built`, and `lp_refused`.
Refusal records carry the executor catalog code plus the planner's own code when the planner refused, so both layers' decisions stay inspectable offline.
The CLI exits zero on success, one on failures, and two on any refusal, with the catalog code printed to stderr as `refused [<code>]`.

```text
aero-bot-lp plan mint --symbol AAPLc --amount 7 --width-ticks 1
aero-bot-lp dry-run mint --symbol AAPLc --amount 7 --width-ticks 1
aero-bot-lp dry-run stake --symbol AAPLc --token-id 0
```

Every subcommand accepts `--json` for the complete typed report; the dry-run subcommands accept `--ephemeral-key` to sign with a throwaway key whose signature check then honestly reports rejection.

### Live real-key dry-run evidence (verbatim, 2026-09-08)

The captures below ran against Base mainnet with the real Keychain key (`bot-signing-key` / `aero-bot`, the relayer EOA `0x0c49...c5c9`) and the real canary Safe.
Nothing was broadcast: this release has no broadcast path, and each capture's only chain writes are local audit appends.

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
