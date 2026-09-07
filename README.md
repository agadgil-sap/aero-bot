# Aero Bot

Aero Bot is a local, wallet-free Aerodrome LP analysis application for Base.
The current foundation exposes a loopback-only FastAPI dashboard and health endpoint.
It never requests a private key and has no transaction broadcast capability.

## Install and run on macOS

Install [`uv`](https://docs.astral.sh/uv/) and use Python 3.12 or newer.

```bash
uv sync --extra dev
uv run aero-bot
```

Open <http://127.0.0.1:8765> in a browser.
The server rejects non-loopback bind addresses by design.
Immutable records default to `~/Library/Application Support/Aero Bot/audit.sqlite3`.
Set `AERO_BOT_AUDIT_DATABASE_PATH` to another absolute path only when its dedicated parent directory is private to the current user.

## Verify

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --cov
```

## Security boundary

This release is analysis and simulation software only.
Do not place wallet seed phrases, private keys, signing material, or other secrets in this repository or its environment files.
The only venue enabled by the product is Aerodrome on Base.
Transaction signing and broadcasting are intentionally absent.

## Official B20 identities

The packaged B20 registry is sourced from the [official Base stocks list](https://www.base.org/stocks).
The source states that its list is the complete set of Coinbase-issued tokenized stocks and advises matching contract addresses before interaction.
Each bundled identity retains the official BaseScan link and the date the source was observed.
The application validates the entire registry and rejects duplicate or malformed identities before exposing any address.
If the evidence resource is missing or invalid, the API and dashboard show an explicit blocked diagnostic and expose no token identities.

## Chainlink health

The application models Chainlink Coinbase B20 total-return rounds, Coinbase multiplier pause state, and the Base sequencer recovery boundary as deterministic health gates.
Only a market-open observation that passes every gate is healthy.
The current release has no reviewed B20 proxy-address snapshot or read-only observation backend, so `/api/oracles/chainlink` and the dashboard report an explicit unavailable diagnostic instead of making live feed claims.
See [the oracle health policy](docs/oracle-health.md) for evidence sources and exact gate ordering.

## Live yield screen

The dashboard and `/api/market-data/aerodrome-yields` perform a bounded read-only query against DefiLlama's public yield dataset.
The scanner accepts only Base records from `aerodrome-slipstream` whose underlying contracts are exactly native USDC and one address in the verified official B20 registry.
Positive reward yield must identify the official AERO token contract.
Fee APY and AERO emissions APY remain separate, and the displayed daily screen compares fee APY with fifty-percent-haircut emissions APY without adding them.
This secondary-source screen is not eligible for execution until onchain pool identity, oracle, exit-depth, concentrated-liquidity, IL, and adverse-selection checks also pass.

## Wallet-free transaction planning

The application can plan deterministic unsigned exact allowances and can pass a revalidated plan only to a read-only simulation interface.
The default policy is emergency-halted with empty transaction allowlists and no simulation backend.
Wallet onboarding, private-key input, signing, and broadcasting remain structurally unavailable through both the API and dashboard.
See [the transaction simulation boundary](docs/transaction-simulation.md) for exact allowance and backend evidence rules.

## Concentrated-liquidity analysis

The position analyzer implements Slipstream's inherited square-root-price inventory formulas with exact Decimal arithmetic.
It distinguishes below-range, active, and above-range positions, reports current asset concentration, and compares current LP inventory with holding the entry assets.
Fees and AERO emissions remain separate from the conservative impermanent-loss estimate.
See [the concentrated-liquidity policy](docs/concentrated-liquidity.md) for formulas, assumptions, and adapter requirements.

## Fee versus AERO compensation

The deterministic risk engine models unstaked swap fees and staked AERO emissions as mutually exclusive Aerodrome compensation modes.
It compares retained fee APR with conservatively discounted emission APR but credits only the selected mode before subtracting impermanent-loss and adverse-selection costs.
The `/api/risk/evaluate` endpoint exposes complete hold or eligible evidence without enabling a transaction.

## Immutable local audit storage

The local persistence boundary uses a versioned SQLite database with append-only triggers, canonical model payloads, contiguous sequences, and a verifiable SHA-256 hash chain.
It rejects credential-shaped fields before persistence and uses restrictive user-only filesystem permissions.
The dashboard, process health route, and `/api/audit/health` expose complete chain verification.
Every risk response is persisted with its validated input, active policy, and exact decision before it is returned.
Every exact-allowance planning response is persisted with its public request, active policy, and exact result before it is returned.
Every read-only simulation response is persisted with its submitted unsigned plan, active revalidation policy, and complete result before it is returned.
See [the audit log design](docs/audit-log.md) for guarantees, limitations, and integration status.
