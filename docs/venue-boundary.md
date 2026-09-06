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

The venue adapter accepts candidates only when the factory is officially allowlisted, the pool kind matches that factory, the pair contains exactly one issuer-verified B20 and native USDC, and the pool address appears once.
One invalid candidate rejects the entire batch so callers never receive a misleading partial result.

The application currently has no live Base RPC backend.
Its API and dashboard therefore report discovery as blocked, identify the four reviewed factories and official B20 identity count, and make no onchain pool claims.
A later web3.py backend must remain read-only and supply source and observation-time evidence through this adapter.
