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

Aerodrome LP compensation is explicitly modeled as two mutually exclusive modes.
An unstaked position earns its observed retained share of swap fees and receives no AERO emissions.
A gauge-staked position earns conservatively discounted AERO emissions and relinquishes its swap-fee claim.
The engine never adds fees and emissions for one position.

The comparison reports retained fee APR, discounted emission APR, the selected stream, the foregone alternative, the higher adjusted mode, and any opportunity-cost difference.
The preferred mode is informational and never an instruction to change position state.
If both streams are equal, the current mode remains preferred to avoid implying needless churn.

Impermanent-loss and adverse-selection estimates are subtracted only from the selected compensation APR.
The resulting annualized estimate is divided by 365 before comparison with the daily opportunity threshold.

The fee-retention fraction is observation evidence rather than a hidden constant because Aerodrome can apply a protocol share to unstaked concentrated liquidity.
The default documented protocol share is currently 10 percent for emissions-eligible Slipstream pools, but a live adapter must read the applicable pool state instead of assuming that value.

Aerodrome's [current official economics documentation](https://aerodrome.finance/docs) states that no deposit earns swap fees and AERO concurrently.
It also states that LPs can remain unstaked for fees or stake into a gauge for emissions.

The default one-percent daily value is only an opportunity threshold.
It is not a return promise, performance objective, or instruction to trade.

## Market regimes

Only the `market_open` regime can pass the first-release policy.
`overnight` and `weekend` observations force a hold because the tokenized equity reference market is closed and price discovery may be impaired.
This conservative behavior can be revisited only with explicit policy, evidence, and tests.
