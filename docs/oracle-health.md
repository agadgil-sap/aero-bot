# Chainlink B20 oracle health

## Evidence boundary

Chainlink documents Coinbase B20 total-return feeds on Base in its [tokenized equity feed guide](https://docs.chain.link/data-feeds/tokenized-equity-feeds/coinbase).
Each total return value combines the underlying equity market price with a Coinbase onchain oracle-registry multiplier.
The multiplier is a redemption ratio represented with 18-decimal WAD precision, so one B20 token must not be assumed to equal one underlying share.
Coinbase can pause the multiplier, and Chainlink documents that a paused feed holds its last good value instead of advancing.

The application also follows Chainlink's [data feed API reference](https://docs.chain.link/data-feeds/api-reference) by modeling the complete `latestRoundData()` timestamp evidence rather than relying on `latestAnswer()`.
The deprecated `answeredInRound` field is retained for audit evidence but is not compared with the current round as a health gate.

This release does not bundle B20 feed proxy addresses because no independently reviewed address snapshot has yet been added to the repository.
It also has no read-only Base RPC observation backend yet.
The API and dashboard therefore report oracle coverage as unavailable with zero configured feeds and make no live price or health claims.

## Deterministic health gates

When a verified proxy snapshot and read-only backend provide observations, the evaluator applies these gates in a fixed order:

1. The Base sequencer uptime answer must report up.
2. The B20 feed and sequencer reads must be observed within 30 seconds of each other.
3. A recovered sequencer must remain up for the configured one-hour grace period.
4. The Coinbase oracle registry must not report the B20 multiplier paused.
5. The Chainlink round ID, total return value, Coinbase WAD multiplier, and timestamp ordering must be valid and positive where required.
6. `updatedAt` must not exceed the observation time by more than 30 seconds.
7. A closed reference market produces an explicit `market_closed` result and is never trade-eligible.
8. A market-open observation must be no more than one hour old.

Only an observation that passes every market-open gate is `healthy`.
Every other outcome is fail-closed and supplies an ordered diagnostic suitable for later risk-engine input.

Chainlink documents that B20 feeds hold the last close outside supported sessions and do not publish heartbeats during off-hours.
The separate `market_closed` outcome prevents expected weekend behavior from being mislabeled as an ordinary stale-feed fault while still blocking eligibility under the conservative first-release policy.

## Live underlying-equity reference feed

The keyless real-market quote the policy's reference gates consume is a separate, bounded read of each underlying equity (`src/aero_bot/stock_reference.py`), armed per cycle with `--reference-feed` or the sealed `AERO_BOT_CYCLE_REFERENCE_FEED` (`off` by default - production is unchanged until the operator arms it; `yahoo` or `finnhub` select the provider).

### Provider evidence

- **yahoo-chart (keyless, primary).** The public chart endpoint `query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?interval=1m&range=1d` needs no credential and served live reads for every verified B20 underlying on 2026-10-03: all ten quote USD on NasdaqGS as EQUITY instruments with the provider's own `regularMarketTime` observation stamp (that evening, every as-of was the 16:00:01 New York session close). The endpoint publishes no API contract, so the adapter pins the fields it depends on - currency, `regularMarketPrice`, `regularMarketTime`, `currentTradingPeriod` windows - with fixtures captured from live responses and fails closed on every structural surprise; the diagnostic session windows are the one exception, because corrupt window evidence must not cost the quote: their epochs arrive as JSON numbers or strict decimal-integer strings, and any invalid, out-of-range, or absent window labels the session unknown instead of asserting a closed market the evidence cannot prove. The delay is unlabeled by the provider and is never claimed as real-time; freshness rides the as-of age alone.
- **finnhub-quote (keyed, documented fallback).** Finnhub's primary API reference documents `GET /quote` for US stocks under API-key security as real-time quote data; its own sample response carries the `t` price timestamp even though the formal schema omits it. The adapter requires `t` - a quote without a provider timestamp is refused rather than stamped with the fetch instant - and is verified against the documented sample by fixtures. Arming it needs a free API key sealed as `AERO_BOT_STOCK_REFERENCE_TOKEN` (create one at finnhub.io/register; the cycle names this requirement at the CLI boundary when the token is missing); no live proof exists for this backend yet because signing up is the operator's act.

Each verified B20 symbol maps to exactly one underlying ticker in `UNDERLYING_BY_B20_SYMBOL` (NVDAc to NVDA and so on for all ten, live-verified 2026-10-03; the provider's longName for MSTRc reads "Strategy Inc" because MicroStrategy renamed while keeping the MSTR ticker). A registry symbol without a reviewed mapping - a corporate action, a rename, or a new listing - fails closed with its own diagnostic and is never guessed.

### The provenance contract

Every quote carries the provider identity, both symbols, the USD price, the provider's observation time (`as_of`) separately from the fetch time, the session derived from the provider's own trading-period windows (`regular`, `pre_market`, `post_market`, `closed`, or `unknown`), the delay label, and the venue. The age the policy gates read is measured from `as_of` at the decision instant, never from the fetch: a delayed quote, a held close, or a closed-market last price therefore grows honestly stale no matter when the poll ran, and the 300-second entry bound and the 900-second open-position bound keep their exact shipped semantics. Quotes are read once per cycle at decide time (after the board sweep, so the sweep's minutes never masquerade as quote age), bounded by a ten-second timeout, one retry with backoff (rate limits are respected, then named in the fail-closed evidence), and a one-megabyte body bound enforced incrementally while the body streams - a hostile or misbehaving endpoint is cut off mid-download, never after its payload has landed.

### Degradation and recovery

Every failure is per-symbol and explicit: a provider outage, rate limit, invalid payload, wrong currency, missing price, or a missing, non-finite, or out-of-range timestamp leaves that symbol unquoted with a diagnostic line in the report's input notes, and the remaining symbols still read. A fully degraded feed reads exactly like the unarmed behavior - no fabricated quotes, no crash - and the next cycle's read recovers without operator action. An injected constant (`--reference-price`) still wins over the feed for its own symbols with its operator-owned age, and the report names the skipped symbols explicitly.

### Closed-market availability versus the 24/7 ruling

The underlying equity market closes nights and weekends while the B20 pools trade continuously under the captain's 2026-09-09 24/7 ruling. The feed does not reconcile this conflict silently: outside the regular session every quote is labeled with its session, its as-of age crosses the staleness bounds honestly, and the cycle report carries an explicit line naming the closed session, the unchanged 24/7 pool-authority posture, and this section - keeping the tension exposed for captain reassessment rather than resolved by either side quietly changing.
