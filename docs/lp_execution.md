# Slipstream LP lifecycle execution

## Scope of this page

This page documents the LP position lifecycle work.
It currently records the verified contract interface the lifecycle builds on; the executor, cap table, refusal catalog, and canary procedure sections are added as those layers land.

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
