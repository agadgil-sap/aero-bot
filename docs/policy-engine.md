# Emissions-farming policy engine

The policy engine is pure decision code with no I/O inside decisions.
The caller injects every observation, the event calendar, and the immutable locked parameters, and the engine returns one typed immutable decision plus the successor state to thread into the next observation.
This makes a whole trading session a deterministic fold over observations, which is exactly the shape the rehearsal harness replays.

All prices are USDC per one stock token.
The engine never signs, broadcasts, or touches a wallet.

## Locked parameters

| Parameter | Locked value |
| --- | --- |
| Range half width | 0.3 percent each side of the reference price |
| Tick grid | spacing 10 on the 500-ppm Slipstream tier |
| Upside recenter wait | 15 minutes out of range |
| Downside stop | 0.5 percent below the lower range edge |
| Re-entry cooldown | 15 minutes after stop or dilution exits |
| Entry threshold | raw AERO emissions APR of at least 150 percent |
| Position cap | 20 percent of current equity per pool |
| Depth hard gate | 1 percent of observed pool depth |
| Daily loss halt | 5 percent of day-start equity |
| Starting equity | 200 USDC |
| Reference staleness bound (entries) | 300 seconds |
| Reference staleness bound (open position) | 900 seconds, then a defensive exit |
| Dislocation threshold | 0.15 percent between AMM and reference prices |
| Convergence timeout | 5 minutes after a stale-low burn |
| Swap impact ceiling | 0.1 percent per swap |
| Swap tranche split bound | 0.05 percent impact per tranche |
| Gas price ceiling | 0.5 gwei |
| Gas cost vs yield | defer above 5 percent of expected daily gross yield |
| Safe proxy overhead | 100,000 gas per batch |
| ETH price assumption | 3000 USDC per ETH (documented, configurable) |

The entry threshold reads the pool's raw AERO emissions APR per staked liquidity in the same APR convention Aerodrome displays (150 percent APR equals about 0.41 percent per day simple).
Fees are credited on top and the conservative haircut is applied inside the P&L forecast, never to this gate.

## Range construction

The entry range centers on the observed pool AMM price.
The raw bounds at plus and minus 0.3 percent are aligned outward onto the pool tick grid (spacing 10): the lower boundary floors to the greatest grid tick at or below the raw lower bound and the upper boundary ceils to the least grid tick at or above the raw upper bound, so the aligned range always contains the raw width.

## Decision precedence

Held stock inventory from a stale-low burn resolves first, then safety exits are evaluated in fixed order, and none of them is ever deferred.

1. Reference-stale defensive exit: a reference quote missing or older than the 900-second open-position bound leaves the monitor blind, so the position burns and swaps all inventory back to USDC.
2. Dislocation monitor: with a reference at least as fresh as the 300-second entry bound, an AMM at least 0.15 percent above or below the reference triggers the asymmetric actions below, which take precedence over every AMM-anchored rule because the reference market is treated as the true price.
3. Downside stop: pool price at or below 0.5 percent under the lower range edge burns the position and swaps all inventory back to USDC.
4. Emissions dilution: while open, the raw emissions APR is re-evaluated at every observation, because other LPs can add sticky staked liquidity that persistently lowers APR per unit of staked liquidity; a fall below the 150 percent threshold exits through the same burn-and-swap path.
5. Event window: a flat window that opens while a position is open burns and swaps all inventory back to USDC.
6. Upside recenter: price above the upper edge starts a 15-minute time-based wait; the recenter burns and re-mints the range around the current pool price only after the wait elapses and the gas gate allows it.
7. In-range or below-edge holds keep the position otherwise.

Between the 300-second entry bound and the 900-second open-position bound, the reference is too old for dislocation comparisons but fresh enough to keep the position, so the ordinary lifecycle rides on.

Below the range edge but above the stop level, the position holds for recovery.

Entry gates are evaluated in fixed order while flat: daily loss halt, re-entry cooldown, event window, reference staleness, emissions threshold, the size caps, and finally the gas sense-check gate.
The size is the smaller of 20 percent of current equity and 1 percent of observed pool depth.

## Underlying dislocation monitor

AMM pools reprice more slowly than order books, so while any position is open the engine compares the keyless real-market reference quote against the pool AMM price at every observation.
The reference source, its staleness bounds, and its fail-closed treatment are documented in the rehearsal harness notes; a stale or unavailable reference blocks new entries at the 300-second bound and forces a defensive exit at the 900-second bound.

Actions are asymmetric and direction-dependent at the 0.15 percent threshold.

1. Stale-high (AMM above reference): immediately burn the position and swap all stock inventory back to USDC on the AMM while it still prices above the real market.
   This includes the crash-anticipation case where the real market shows a large imminent permanent loss Aerodrome has not yet reflected, so it is never deferred by the gas gate and outranks the downside stop.
2. Stale-low (reference above AMM): burn the position and hold the stock tokens unsold, because selling into a stale-low pool realizes the wrong price.
   The tokens sell back to USDC once the AMM converges to within the threshold of a fresh reference, with a 5-minute convergence timeout after which they are sold at market as a safety bound.
   A flat window (scheduled event, oracle-stale, registry-pause) forces an immediate market sell of held tokens, and a reference stale past the entry bound suspends convergence judgments until the timeout releases them.
   While tokens are held, no new entries are possible and no re-entry cooldown applies.
3. Bounded arbitrage on a stale-low AMM is deliberately out of scope for v1: the spec marks it optional, and the engine only farms emissions rather than trading dislocations.

## Gas sense-check gate

Before committing any transaction batch, the engine reads the injected Base gas price and estimates the batch's gas cost in USD: protocol-side gas for the action plus a 100,000-gas Safe proxy execution overhead per batch, times the gas price, times the documented 3,000-USDC-per-ETH price assumption (configurable, never a live quote).

Non-urgent actions (entries, re-entries, recenters) are deferred whenever the L2 gas price exceeds 0.5 gwei or the estimated batch cost exceeds 5 percent of the position's expected daily gross yield, whichever trips first.
The expected daily gross yield is the position value times the sum of the raw emissions APR and the fee APR over 365.
An unavailable gas price reading defers non-urgent actions fail-closed.
Safety exits (downside stop, dilution exit, event exit, both dislocation actions, the defensive exit, and inventory sells) are never deferred by the gas gate, because spike gas cost is trivial versus gap risk; their decisions still carry the gas estimate for the ledger, or None cost when the reading is unavailable.

Gas estimates per batch: entries 650,000 units (swap, mint, stake, approvals), recenters 550,000 (burn, swap, re-mint, re-stake), exits 350,000 (burn, unstake, swap), and inventory sells 180,000 (single swap), each plus the Safe overhead.

## Swap execution quality

Every swap the policy performs (entry rebalances, recenter rebalances, stop-out exits, dilution exits, dislocation sells, and inventory sells) is modeled against the pool's observed executable depth as a typed immutable swap plan attached to the decision.

The impact model is deliberately simple and conservative: a swap of one percent of the executable depth counts as one percent of price impact, so tranches split no later than a curve model would require.
Every splittable swap's tranches stay at or below the 0.05 percent bound, which keeps each executed swap well under the hard 0.1 percent price-impact ceiling, and whenever the whole swap would exceed 0.05 percent it is split into equal tranches executed one per observation interval.
Tranche sizes floor to USDC's six decimals with the final tranche absorbing the remainder, so tranches always sum exactly to the plan total.
v1 has exactly one venue per stock, so deepest-path routing degenerates to the pool itself; the depth used for modeling is the observed pool depth.
A zero or vanishing depth cannot bound impact, so such swaps model as a single tranche labeled unmodeled, and a position that outgrew its pool past a thousand tranches models as one whole-size tranche whose impact number itself flags the violation.

## Event windows

Event windows mean flat in USDC from 60 minutes before to 30 minutes after each event.

Events come from three sources.

1. US equity market open 09:30 and close 16:00 America/New_York, derived for every weekday trading day (the v1 calendar has no holiday source, so weekdays are treated as trading days).
2. Earnings and ex-dividend dates from the bundled `policy_events.toml`, which starts empty.
   Naive timestamps in that file are interpreted as America/New_York, and an optional `token_address` scopes a window to one B20 pool while omitting it applies the window to every pool.
3. Oracle-stale and registry-pause signals from the existing oracle health gates, treated as condition-driven flat events that are active while the condition holds.

Event-window exits carry no re-entry cooldown; re-entry simply requires the threshold to clear again once the window has ended.

## Dilution exits and re-entry

A dilution exit uses the same burn-and-swap-to-USDC path as the downside stop and sets the same 15-minute re-entry cooldown.
Re-entry after the cooldown requires the 150 percent threshold to clear again at the current diluted share, so a pool that lost its emissions yield stays out of the portfolio.

## Daily loss halt

The halt anchors to the America/New_York day-start equity, which resets on the first observation of each new day.
A marked drawdown of at least 5 percent from that anchor latches the halt for the rest of the day even if equity recovers, and blocks only new entries; every safety exit remains armed while it is active.
Maintenance of an already-open position (the upside recenter) is not treated as a new entry.

## Position composition

Exit swap sizes and held-inventory quantities use the exact v3-style composition of a range entered at its geometric center, which is how the engine builds every range: liquidity follows from the committed value at the center, below the range the position is entirely stock tokens, in range the stock side spans the current-to-upper square-root-price band, and above the range the position is entirely USDC.
Entry rebalances buy half the committed value in stock and recenters buy half the committed value back, matching the even value split at the range center.
These sizing rules are decision-level approximations of the swap the executor performs; the rehearsal ledger refines them with reconstructed history where it can and labels the rest as assumptions.

## App exposure and audit

The `/api/policy/decide` endpoint exposes the pure engine through the local HTTP boundary.
A request carries one injected observation and the threaded engine state; an omitted state starts a fresh session at the documented 200-USDC starting equity.
The response returns the typed decision together with its successor state, so a caller threads a whole session through repeated requests exactly like the rehearsal harness folds observations.
Every decision is appended to the immutable local audit chain before it is returned, and a corrupt chain blocks the endpoint rather than returning an unaudited decision.
The durable event retains the complete observation, the threaded state, the locked parameters, the event calendar, and the exact decision, which is everything required to reproduce the outcome deterministically.

## Deliberate v1 scope boundaries

The engine decides the full lifecycle above: every gate, exit, dislocation action, gas deferral, and swap plan in this document.
Exposure through the app and the audit chain is described above; the optional bounded arbitrage on stale-low pools remains outside v1 in favor of pure emissions farming.
No swap in v1 executes against anything but the position's own pool, and no decision in this module performs I/O.
