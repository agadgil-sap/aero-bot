# Deterministic risk policy

The risk engine is a pure, deterministic boundary with no LLM calls and no transaction side effects.
Identical immutable inputs and policy values produce an identical ordered result.

`hold` is the default and first-class outcome.
`eligible` means every gate passed, not that capital must be deployed.
The engine never signs or broadcasts transactions.

## Gate order

The engine evaluates emergency state, contract allowlists, B20 pause state, market regime, Chainlink health and freshness, oracle deviation, liquidity, exit depth, capital exposure, realized loss, adverse selection, and conservative net yield.
It returns all failed gates in stable order so the dashboard and future audit records can show complete evidence rather than one opaque rejection.

The default policy is intentionally unusable for entry.
Emergency halt starts enabled and both contract allowlists start empty.
Official discovery must provide verified contracts before an opportunity can become eligible.

## Fee and emissions treatment

Aerodrome pays swap fees and AERO emissions to the same staked in-range position, so fee APR and haircut emissions APR are additive return streams rather than mutually exclusive alternatives.
A staked Slipstream position earns its observed retained share of swap fees and its AERO emissions at the same time.
The engine adds the two streams and never selects only the larger one.

The calculation reports retained fee APR, haircut emissions APR, and their total compensation APR.
Retained fee APR applies the observed fee-retention fraction to gross fee APR.
Haircut emissions APR applies the conservative reward haircut to raw AERO emissions APR so volatile rewards cannot dominate the total.

Impermanent-loss and adverse-selection estimates are subtracted from the total compensation APR.
The resulting annualized estimate is divided by 365 before comparison with the daily opportunity threshold.

The fee-retention fraction is observation evidence rather than a hidden constant because Aerodrome can apply a protocol share to concentrated-liquidity fees.
The default documented protocol share is currently 10 percent for emissions-eligible Slipstream pools, but a live adapter must read the applicable pool state instead of assuming that value.

The default one-percent daily value is only an opportunity threshold.
It is not a return promise, performance objective, or instruction to trade.

## Market regimes

Only the `market_open` regime can pass the first-release policy.
`overnight` and `weekend` observations force a hold because the tokenized equity reference market is closed and price discovery may be impaired.
This conservative behavior can be revisited only with explicit policy, evidence, and tests.
