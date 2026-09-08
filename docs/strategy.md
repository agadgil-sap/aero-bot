# Decision-only strategy E2E

The `aero-bot-decide` command runs the complete policy engine - the same locked parameters, event calendar, and decision precedence the rehearsal replays - against one live observation assembled from the same discovery, corrected emissions-APR convention, and depth estimates every executor surface uses.
It is decision-only by construction: nothing is built, signed, estimated for broadcast, or executed, and the run's single side effect is one `policy_decision` record on the audit chain.

```
uv run aero-bot-decide --symbol AAPLc [--equity-usdc 200] [--reference-price 318.5] [--json]
```

Exit codes: zero on any verdict (a hold is a decision, not a failure), one when live inputs cannot be assembled.

## What one run assembles

- The pool from live Sugar discovery, pinned to its snapshot block.
- The AMM price from the snapshot's sqrt ratio.
- The emissions APR in Aerodrome's displayed convention (the shared conversion in `aero_bot.emissions_apr`), priced at a live AERO read from the canonical USDC/AERO pair at the same snapshot block.
- The executable in-range depth from the planner's estimator.
- Equity: the supplied override, or the Safe's live USDC plus stock value at the snapshot price.
- The Base gas price in gwei, `None` (gas gate defers fail-closed) when the read is unavailable.
- The registry's pause state.
- The event-window view over the operator calendar and the derived weekday market open/close windows (sixty minutes before through thirty minutes after each session boundary in America/New_York).

## Flat verdicts during event windows are correct behavior

When any scheduled or session-derived window is active - a market open or close window on a weekday, or an operator-scheduled earnings or ex-dividend event - the engine returns `hold (event_window_flattened)` with the window's description, and the report marks the window active.
This is the strategy working exactly as designed, not a failure: the v1 doctrine is to be flat in USDC around US equity session boundaries and volatility events, so a flat verdict inside a window is the correct answer even when every other gate would pass.
The same doctrine holds for the condition-driven flats (a stale oracle observation or a paused registry), and for `hold (reference_stale)` - see below.

## The two honest input gaps

Both gaps are visible in every report's `input_notes` rather than hidden, because hiding them would let an incomplete observation masquerade as a complete verdict:

- **The real-market reference quote is not wired live yet.** Without `--reference-price`, every entry blocks fail-closed as `reference_stale`; the quote can be injected for complete verdicts because the policy consumes injected observations by design. Wiring the live reference feed is the oracle-health layer's scope, which is post-reassessment by the captain's order.
- **The fee APR stays at zero** because a live fee-evidence window needs the same price-path machinery the rehearsal reconstructs from history; zero understates expected gross yield, which makes the gas gate defer more, never less - the conservative direction.

## Live proofs (2026-09-08)

- `aero-bot-decide --symbol AAPLc` (no reference): `hold (reference_stale)` at block 51033468 - the honest fail-closed default, with the corrected emissions APR 1,581.80 percent at a live AERO price of 0.6322 USDC on the same observation.
- `aero-bot-decide --symbol AAPLc --equity-usdc 200 --reference-price 318.5`: `enter (entry_threshold_met)` at block 51033570 - the full gate chain ran: raw emissions APR 712.40 percent cleared the 150 percent gate, size 40.00 USDC took the equity cap against a 5,774 USDC depth cap, the range aligned to tick spacing 10 at the fallback ceiling width (no live ranging evidence yet - the width solver fails toward the locked ceiling with its label), and the gas gate passed at 13.5 gwei.

Nothing in either run was built, signed, or broadcast.
