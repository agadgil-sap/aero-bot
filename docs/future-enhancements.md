# Future-enhancement register

Prospective work the captain has rated worth doing, registered here so it
is never lost and never silently implemented: every entry names the current
behavior, the expected benefit, the scope a change would carry, and the
evidence needed before any of it is built. Nothing in this file is
implemented today, and no entry justifies changing the locked policy engine
on its own.

## 1. Arm the external stock-reference feed (absent/stale quotes)

Registered 2026-10-05 (firstmate 026), from the live restart evidence: the
cycle reports state verbatim that "no real-market reference quote reached
this board" and that selector mode treats references as diagnostic-only.

Current behavior: the underlying-equity reference feed
(`src/aero_bot/stock_reference.py`, wired per deployment through
`AERO_BOT_CYCLE_REFERENCE_FEED`, `off` by default) can read one as-of-stamped
quote per board symbol at decide time - yahoo keyless or finnhub keyed - but
today no quote reaches the board, and in scheduled selector mode references
are diagnostic-only regardless: every selector action prices off the
resolved Aerodrome pool's own on-chain state.

Benefit: with the feed armed and fresh, the dislocation monitor (the
0.15-percent pool-versus-reference bound) and the reference-stale defensive
exit gain a live input in enforced contexts, so a pool that drifts from its
underlying market while the position is open is caught by evidence rather
than by luck; the closed-session tension (24/7 pools against market hours)
is already surfaced honestly rather than resolved silently.

Scope: seal the feed arm and (for finnhub) the token in the cycle
environment; no code change is required to arm it. Any change that promotes
references from diagnostic-only to execution-authoritative in selector mode
is a policy change the captain must rule on separately, because today the
pool is the trading authority by design.

Evidence needed before further work: one sealed deployment reading quotes
for every verified B20 symbol across open, closed, and delayed sessions;
the per-symbol absence rates; and a measured count of dislocation
monitor activations that the current pool-authority posture would have
missed.

## 2. Wire the oracle-health layer into live freshness/dislocation protection

Registered 2026-10-05 (firstmate 026), from the live cycle note: "oracle
staleness is not yet wired live; the oracle-health layer is post-reassessment
scope."

Current behavior: `src/aero_bot/oracle_health.py` (Chainlink-style total
return feeds composed with the Coinbase B20 oracle-registry multiplier,
`docs/oracle-health.md`) exists as a fail-closed health surface with zero
configured feeds; it makes no live price or health claims and no execution
path reads it. Execution has no TWAP or oracle input anywhere.

Benefit: a genuinely independent on-chain price source would let the
reference-stale defensive exit and the dislocation monitor run around the
clock on the same venues the pools trade on, closing the gap where the
stock-reference feed is stale (nights, weekends, provider outages) and
today nothing watches dislocation.

Scope: a validated design that composes oracle observations into the
existing freshness bounds (300 s entry, 900 s open position) and the
0.15-percent dislocation bound without changing any locked threshold;
feeds configured and sealed per symbol; fail-closed degradation identical
to the reference feed's per-symbol honesty.

Evidence needed before further work: live oracle coverage for every
verified B20 symbol (feed addresses, heartbeat and deviation tolerances,
observed update cadence on Base); a measured comparison of oracle versus
pool-spot divergence on the live pools; and the captain's ruling that an
oracle may influence execution at all - today the pool is the only
authority, and that posture was chosen deliberately.
