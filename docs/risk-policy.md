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

Fee APR is combined with AERO emissions only after applying the configured emissions haircut.
Impermanent-loss and adverse-selection estimates are then subtracted.
The resulting annualized estimate is divided by 365 before comparison with the daily opportunity threshold.

The default one-percent daily value is only an opportunity threshold.
It is not a return promise, performance objective, or instruction to trade.

## Market regimes

Only the `market_open` regime can pass the first-release policy.
`overnight` and `weekend` observations force a hold because the tokenized equity reference market is closed and price discovery may be impaired.
This conservative behavior can be revisited only with explicit policy, evidence, and tests.
