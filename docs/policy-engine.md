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
| Reference staleness bound | 300 seconds |

The entry threshold reads the pool's raw AERO emissions APR per staked liquidity in the same APR convention Aerodrome displays (150 percent APR equals about 0.41 percent per day simple).
Fees are credited on top and the conservative haircut is applied inside the P&L forecast, never to this gate.

## Range construction

The entry range centers on the observed pool AMM price.
The raw bounds at plus and minus 0.3 percent are aligned outward onto the pool tick grid (spacing 10): the lower boundary floors to the greatest grid tick at or below the raw lower bound and the upper boundary ceils to the least grid tick at or above the raw upper bound, so the aligned range always contains the raw width.

## Decision precedence

Safety exits are evaluated first and are never deferred.

1. Downside stop: pool price at or below 0.5 percent under the lower range edge burns the position and swaps all inventory back to USDC.
2. Emissions dilution: while open, the raw emissions APR is re-evaluated at every observation, because other LPs can add sticky staked liquidity that persistently lowers APR per unit of staked liquidity; a fall below the 150 percent threshold exits through the same burn-and-swap path.
3. Event window: a flat window that opens while a position is open burns and swaps all inventory back to USDC.
4. Upside recenter: price above the upper edge starts a 15-minute time-based wait; the recenter burns and re-mints the range around the current pool price only after the wait elapses.
5. In-range or below-edge holds keep the position otherwise.

Below the range edge but above the stop level, the position holds for recovery.

Entry gates are evaluated in fixed order while flat: daily loss halt, re-entry cooldown, event window, reference staleness, emissions threshold, and finally the size caps.
The size is the smaller of 20 percent of current equity and 1 percent of observed pool depth.

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

## Deliberate v1 scope boundaries

The engine currently decides the lifecycle above only.
Three locked-policy behaviors land in follow-up commits and are documented here so the boundary is explicit: the gas sense-check deferral gate, the underlying dislocation monitor with its asymmetric stale-high and stale-low actions, and per-swap execution-quality modeling with tranche splitting.
The reference-staleness entry gate is already active because a missing or older-than-bound real-market quote must never support a new entry, but while a position is open a missing reference does not by itself force an exit until the dislocation monitor lands.
