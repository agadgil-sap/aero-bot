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
