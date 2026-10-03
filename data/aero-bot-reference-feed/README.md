# Live underlying-stock reference feed - live proof evidence

Collected 2026-10-03 (a Friday evening UTC, the underlying US equity market
closed since 20:00 UTC) from this worktree, all unsigned and read-only:
`aero-bot-cycle --dry-run` never loads the signing key and builds nothing.

## What is proven live versus by fixtures

- **Live (this directory):** quote ingestion through the actual CLI cycle
  path. `live-proof-pinned-aaplc.json` is the complete report of
  `uv run aero-bot-cycle --symbol AAPLc --dry-run --reference-feed yahoo
  --json` against live Base reads and a live Yahoo chart read, with
  worktree-local audit/state paths. Its `input_notes` carry the full
  provenance chain:
  - `reference feed yahoo-chart quoted 1 of 1 requested symbol(s)`
  - `reference AAPLc=333.69 USD via yahoo-chart as-of
    2026-10-02T20:00:01+00:00 (age 36653s, session closed, delay unlabeled
    by provider, exchange NasdaqGS, fetched 2026-10-03T06:10:54...)`
  - the closed-market conflict line naming the unchanged 24/7 pool-authority
    posture and pointing at `docs/oracle-health.md` for reassessment.

  The age is the honest headline: the poll ran seconds before the decision,
  but the age reads 36,653 seconds because it is measured from the
  provider's own as-of (the 16:00:01 New York session close) - a
  closed-market last price is never relabeled fresh because a poll ran.
  The decision itself stayed on pool authority (`enter`,
  `entry_threshold_met`), exactly the shipped production semantics.
- **Live (earlier the same session):** all ten verified B20 underlyings
  (NVDA, META, AAPL, GOOGL, AMZN, MSFT, MSTR, SNDK, SPCX, TSLA) resolved on
  the same endpoint with currency USD, NasdaqGS, instrument EQUITY, and
  per-symbol as-of stamps - the verification behind
  `UNDERLYING_BY_B20_SYMBOL`.
- **Fixtures (tests/test_stock_reference.py):** the adapter contracts of
  both backends - the Yahoo parsing pinned against a live-captured chart
  meta, the Finnhub contract pinned against the provider's own documented
  sample response - plus every fail-closed mode (non-USD, missing price,
  missing provider timestamp, future as-of, persistent 429, transport
  faults, oversized bodies, ticker injection), the feed's per-symbol
  isolation, cache TTL, and the honest-age math. The keyed Finnhub backend
  is fixture-proof only: live proof requires the operator to seal a free
  API key (`AERO_BOT_STOCK_REFERENCE_TOKEN`), which no agent may sign up
  for.

## The full-board selector live proof - rate-limit limitation, not a success claim

The complete cross-board selector cycle (`--symbol auto`) was attempted
twice against public Base RPCs during this window and both attempts were
RPC-bound, not feed-bound:

1. `live-proof-selector.stderr` (attempt 1, `mainnet.base.org`): the board
   enumerated through the known-pool fast path, then the reconcile's reads
   exhausted their bounded retries and the cycle failed with the RPC's own
   `HTTP 410` - a concrete public-RPC degradation, minutes before the same
   endpoint stopped answering at all.
2. `live-proof-selector-attempt2.stderr` (attempt 2, `base.publicnode.com`):
   the full 25,026-pool Sugar enumeration completed, but per-pool
   verification then ground through persistent `HTTP 403` throttling for
   well over an hour and was bounded and stopped per the supervisor's
   instruction.

Neither failure touches the reference feed: the pinned run above proves
quote ingestion through the same runner, and the selector wiring is the
same code path with more symbols (covered deterministically by
`tests/test_cycle.py::TestLiveReferenceFeedCycles`). Capturing the
full-board selector report against a healthy RPC (or the deployment's own
endpoint) remains open follow-up evidence; discovery throughput itself
belongs to the existing amplification work, not to this feed.

## Run environment

- Worktree-local state: `AERO_BOT_AUDIT_DATABASE_PATH`,
  `AERO_BOT_LP_POOL_PINS_PATH`, and `AERO_BOT_CYCLE_STATE_PATH` all pointed
  inside this directory; nothing outside the worktree was mutated, and the
  audit parent was mode 0700 as the audit store requires.
- RPCs: `base.drpc.org` (pinned success), `base.publicnode.com` and
  `mainnet.base.org` (documented degradations above).
- No signer, no broadcast, no sealed configuration, no timer.
