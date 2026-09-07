# Venue trust boundary

Aerodrome is the only venue enabled for the first release.
Adding another venue requires a new typed identifier, explicit membership in the reputable-venue allowlist, and a dedicated adapter implementation.
No arbitrary DEX, factory, router, or pool scanning is permitted.

## Primary contract evidence

The classic deployment is sourced from the [official Aerodrome contracts repository](https://github.com/aerodrome-finance/contracts).
The concentrated-liquidity deployments are sourced from the [official Aerodrome Slipstream repository](https://github.com/aerodrome-finance/slipstream).
Native Base USDC is sourced from [Circle's Base announcement](https://www.circle.com/blog/usdc-now-available-natively-on-base), which distinguishes it from bridged USDbC.

The allowlist contains the classic PoolFactory and all three published Slipstream PoolFactory generations.
The newest Slipstream deployment is marked current for newly created gauges, while earlier deployments remain relevant to existing pools and gauges.
Removing historical factories would make legitimate existing positions invisible.

## Discovery behavior

Pool inventory is enumerated from Aerodrome's own [LP Sugar](https://github.com/velodrome-finance/sugar) read layer rather than factory events or secondary yield feeds.
Factory-event scanning and DefiLlama both silently miss pools; the Sugar contract is the venue's complete inventory.
One paginated sequence of read-only `all(limit, offset)` `eth_call` requests returns every classic and Slipstream pool with its factory, gauge, emissions, and fee configuration.

The backend pins one Base block with `eth_blockNumber` before the first page so every page describes a single coherent state.
Pages use a small page size under the contract cap, a short politeness delay between requests, and bounded retries with exponential backoff for transient failures such as the public endpoint's rate-limit error.
Any structural inconsistency in a response fails closed with a diagnostic instead of yielding partial records.

The vendored `Lp` struct ABI fragment (`src/aero_bot/lp_sugar_abi.json`) is inspected from the pinned sugar-sdk 0.3.1 Base configuration, which publishes the Sugar address `0x27fc745390d1f4BaF8D184FBd97748340f786634` and the matching contract sources.
The pinned deployment and the Base public RPC endpoint can be overridden through settings, and no third-party API key is ever required or embedded.

The venue adapter accepts candidates only when the factory is officially allowlisted, the pool kind matches that factory, the pair contains exactly one issuer-verified B20 and native USDC, and the pool address appears once.
One invalid candidate rejects the entire batch so callers never receive a misleading partial result.
Accepted pools must additionally be Slipstream pools with an official gauge that is alive and actively emitting the official AERO token; classic-factory pools, gaugeless pools, dead gauges, and live gauges emitting nothing are excluded with documented diagnostics.

Live discovery is opt-in through `AERO_BOT_POOL_DISCOVERY_ENABLED`.
Until it is enabled, the API and dashboard report discovery as blocked, identify the reviewed factories and official B20 identity count, and make no onchain pool claims.
