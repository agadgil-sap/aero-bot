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
