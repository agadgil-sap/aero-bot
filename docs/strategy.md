# Decision-only strategy E2E

The `aero-bot-decide` command runs the complete policy engine - the same locked parameters, event calendar, and decision precedence the rehearsal replays - against one live observation assembled from the same discovery, corrected emissions-APR convention, and depth estimates every executor surface uses.
It is decision-only by construction: nothing is built, signed, estimated for broadcast, or executed, and the run's single side effect is one `policy_decision` record on the audit chain.

```
uv run aero-bot-decide [--symbol AAPLc | auto] [--equity-usdc 200] [--reference-price 318.5 | SYM=PRICE,...] [--json]
```

Exit codes: zero on any verdict (a hold is a decision, not a failure), one when live inputs cannot be assembled.

## What one run assembles

- The pool from the known-pool fast path when a verified Sugar sweep has pinned it (sixteen block-pinned contract views; see [the LP execution fast-path section](docs/lp_execution.md)), otherwise from live Sugar discovery pinned to its snapshot block - and that first sweep persists the pin so the next run is fast.
- The AMM price from the snapshot's sqrt ratio.
- The emissions APR in Aerodrome's displayed convention (the shared conversion in `aero_bot.emissions_apr`), priced at a live AERO read from the canonical USDC/AERO pair at the same snapshot block.
- The executable in-range depth from the planner's estimator.
- Equity: the supplied override, or the Safe's live USDC plus stock value at the snapshot price.
- The Base gas price in gwei, `None` (gas gate defers fail-closed) when the read is unavailable.
- The registry's pause state.
- The event-window view over the operator calendar and the derived weekday market open/close windows (sixty minutes before through thirty minutes after each session boundary in America/New_York).
  Since the captain's 2026-09-09 twenty-four-seven ruling this view is informational only - see below.

## Pinned symbol versus the cross-board selector

An explicit `--symbol AAPLc` pins one pool and decides exactly that pool, as this surface always has.
`auto` - which is also the default when `--symbol` is omitted - runs the cross-board selector over every verified B20 pool: the same complete locked entry gate chain evaluates each pool, the best-qualifying pool by qualifying emissions APR wins (ties break on the lexicographically smallest symbol so runs are reproducible), and while a position is held another pool only displaces it past the relative switch margin with the exit-plus-entry gas economics passing.
The selector's pure mathematics live in `src/aero_bot/selector.py`; [the cycle documentation](docs/cycle.md) carries the full selector doctrine (margin, per-pool cooldowns, the single-position invariant).

Selector mode needs per-symbol reference quotes: `--reference-price AAPLc=318.5,FIXc=100` (or the same grammar in `AERO_BOT_CYCLE_REFERENCE_PRICE_USDC`).
The single-number form still quotes one pinned symbol.

## Twenty-four-seven operation (captain's ruling 2026-09-09)

The B20 pools are continuous DeFi markets - nights and weekends are in scope - so the market-session/event-window gate no longer blocks entries and no longer forces exits.
The event-calendar machinery stays loaded and the report still prints the active window, marked informational: an active window is operator awareness, never a verdict.
The condition-driven flats - a stale oracle observation or a paused registry - still require the policy to be flat in USDC exactly as shipped, as does the `hold (reference_stale)` fail-closed posture below.

## The two honest input gaps

Both gaps are visible in every report's `input_notes` rather than hidden, because hiding them would let an incomplete observation masquerade as a complete verdict:

- **The real-market reference quote is not wired live yet.** Without `--reference-price`, every entry blocks fail-closed as `reference_stale`; the quote can be injected for complete verdicts because the policy consumes injected observations by design. Wiring the live reference feed is the oracle-health layer's scope, which is post-reassessment by the captain's order.
- **The fee APR stays at zero** because a live fee-evidence window needs the same price-path machinery the rehearsal reconstructs from history; zero understates expected gross yield, which makes the gas gate defer more, never less - the conservative direction.

## Live proofs (2026-09-08)

- `aero-bot-decide --symbol AAPLc` (no reference): `hold (reference_stale)` at block 51033468 - the honest fail-closed default, with the corrected emissions APR 1,581.80 percent at a live AERO price of 0.6322 USDC on the same observation.
- `aero-bot-decide --symbol AAPLc --equity-usdc 200 --reference-price 318.5`: `enter (entry_threshold_met)` at block 51033570 - the full gate chain ran: raw emissions APR 712.40 percent cleared the 150 percent gate, size 40.00 USDC took the then-twenty-percent equity cap against a 5,774 USDC depth cap, the range aligned to tick spacing 10 at the fallback ceiling width (no live ranging evidence yet - the width solver fails toward the locked ceiling with its label), and the gas gate passed at 13.5 gwei. The captain's 2026-09-09 sizing ruling later raised the equity-fraction cap to eighty percent, so the same pool's depth cap (57.74 USDC at one percent) now binds below the equity cap there.

Nothing in either run was built, signed, or broadcast.
