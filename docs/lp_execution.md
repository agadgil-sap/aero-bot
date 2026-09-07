# Slipstream LP lifecycle execution

## Scope of this page

This page documents the LP position lifecycle work.
It records the verified contract interface the lifecycle builds on and the pure planning layer that turns a Sugar snapshot plus Safe inventory into a capped mint plan; the executor and canary broadcast procedure sections are added as those layers land.

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
