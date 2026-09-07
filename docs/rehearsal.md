# Rehearsal command

The `aero-bot-rehearse` command is the only component that performs live reads for the rehearsal objective.
It discovers the accepted B20/USDC pools through LP Sugar, reconstructs each pool's price path and emissions-APR series from read-only logs, replays the locked policy engine over them, and writes every per-pool profit-and-loss ledger into one JSON report.
The command performs read-only JSON-RPC calls only: `eth_call`, `eth_getLogs`, `eth_getBlockByNumber`, and `eth_blockNumber`.
It never signs, broadcasts, or touches a wallet.

## Usage

```bash
uv run aero-bot-rehearse [--lookback-days N] [--aero-price USD] [--gas-price-gwei GWEI]
                         [--pool ADDRESS]... [--no-synthetic-stress]
                         [--output PATH] [--header-batch-size N]
```

The RPC endpoint and Sugar address come from the application settings, so `AERO_BOT_BASE_RPC_URL` and `AERO_BOT_LP_SUGAR_ADDRESS` override them unchanged.
`--pool` is repeatable and selects a subset; every named address must appear in the verified discovery or the run refuses to start.
`--lookback-days` defaults to the locked 21-day rehearsal window and multi-week windows take hours against the public RPC, so shorter proof runs are normal.
`--header-batch-size` groups block-header reads into JSON-RPC batches; the public Base endpoint serves at most ten calls per batch, which is the default and the maximum.
The exit code is zero only when every selected pool produced a ledger, and one otherwise, so a partial run is never mistaken for a complete one.

## Run pipeline

One run executes the same per-pool pipeline for every selected pool, in pool-address order.

1. Discovery: LP Sugar enumerates every pool in one block-pinned snapshot and the venue adapter accepts only live-gauge Slipstream pools pairing one official B20 token with native USDC.
   The snapshot's pin block becomes the anchor block for every downstream reconstruction.
2. Token decimals: each stock token's `decimals()` is read once through a read-only `eth_call` because neither the registry nor Sugar records decimal counts, and native USDC's six decimals are read the same way.
3. Price path: the pool's `Swap` logs over the lookback window reconstruct its exact price path, each swap keeping its own block's exact header timestamp.
4. Emissions history: the gauge's `Deposit` and `Withdraw` logs fold backward from the anchor into the stepwise emissions-APR series, with the documented constant-anchor fallback when the fold contradicts the anchor.
   The anchor staked value prices both staked balances at the snapshot's square-root price.
5. Replay: the pure replay module folds the locked policy engine over the two histories and emits the per-pool ledger.
6. Report: every ledger and every fail-closed failure lands in one immutable JSON report written to the output path, and a one-line summary per pool is printed.

A pool whose reads or replay fail is recorded as a failure with its diagnostic while the remaining pools continue.
One pool never appears as both a ledger and a failure, and the report model rejects any such drift.

## Report contents

Each ledger carries the replay window, final equity, cash, open position and held inventory, P&L and return fraction, exposure and in-range seconds, accrued fees and AERO, total swap-impact and gas drag, per-action-kind counts cross-checked against the recorded action list, and every assumption label in force.
Each failure carries the pool, token, symbol, and the fail-closed diagnostic.
The report header carries the discovery source and pin block, the lookback, the AERO and gas price assumptions, and whether the synthetic stress overlay was applied.

## Documented approximations

Every approximation below is a first-class label on the ledger itself, so no downstream reader can mistake modeled numbers for observed ones.

- The reference path equals the AMM price at every observation unless a labeled synthetic episode overlays it, because the underlying reference market cannot be reconstructed keylessly minute-by-minute.
- The synthetic schedule, applied by default, overlays four bounded episodes positioned as fractions of the window span: a two-minute stale-high spell at 30 percent, a two-minute stale-low spell at 50 percent, an eight-minute stale-low spell at 70 percent, and a twenty-minute stale-feed outage at 85 percent, each displacing the reference by half a percent.
  Episodes must not overlap inside the window, so a pool whose swaps span less than about 53 minutes cannot carry the default schedule and is recorded as a failure rather than silently dropping the overlay.
- Fees accrue pro rata to active pool liquidity for each swap while the position is open and in range at the post-swap price.
- AERO emissions accrue to the in-range staked liquidity share, held piecewise constant between observations.
- The AERO price is a constant assumption carried on the series, the ledger, and the report, defaulting to 0.50 USDC.
- The gauge's reward rate is held constant across the window because Aerodrome resets it only at weekly epochs.
- Staked value scales linearly with staked liquidity at the frozen per-liquidity-unit anchor value because other LPs' range shapes are private.
- When the stake-event fold contradicts the anchor, the emissions APR is held at the anchor level for the whole window and the ledger is labeled with the constant-anchor fallback.
- Pool depth is the active-liquidity value across a plus-or-minus one-percent price band.
- Swap tranches execute immediately with impact charged at half the modeled end impact.
- Gas is held constant at the configured gwei with ETH at the engine's documented 3,000-USDC assumption.
- The historical fee APR is unknown, so the gas gate sees a conservative zero fee APR.
- The APR before the first reconstructed step is clamped to that step.
- Oracle and registry health are assumed healthy for the whole window.
- Swaps that execute against zero observed depth mark the ledger with an unmodeled-impact label.

## Runtime bounds and politeness

Every request retries rate-limit failures with exponential backoff, bounded at five attempts.
Log reads page through 5,000-block windows with a 1,000-log bound per window and 50,000 decoded events per reconstruction; a window whose answer reaches the bound is presumed truncated and splits in half until every half answers below it, and only a single-block window still at the bound fails closed because no split can rule truncation out.
Block-header reads are bounded at one per unique event block plus the binary search's probes and a small window allowance, and the batched header prefetch groups them into at most ten-request batches so multi-week windows stay feasible without interpolating any timestamp.
One pool's failure never aborts the run: it is recorded and the next pool proceeds.
