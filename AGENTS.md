# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Project pointers

- Validation gates before every commit: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src tests`, `uv run pytest`.
- Commit style: `gnhf N: <long summary>` continuing the repo's numbering; no co-author lines; never modify CHANGELOG.md.
- The LP execution layer's contracts, caps, refusal catalog, and live evidence live in `docs/lp_execution.md`; the swap executor's in `docs/execution.md`.
- Aerodrome's displayed emissions APR is the current-cell staked-value convention shared by runtime and rehearsal in `src/aero_bot/emissions_apr.py`; the derivation and live cross-checks are documented in `docs/lp_execution.md` under "Position status and Aerodrome's displayed emissions APR".
- Live Base reads use the app settings' public RPC; heavy sweeps (Sugar discovery ~3 min, rehearsal price paths) get rate-limited - space them out and expect 429 backoff.
- Real signing keys live in the macOS Keychain (`AERO_BOT_KEYCHAIN_SERVICE`/`AERO_BOT_KEYCHAIN_ACCOUNT`); never print or persist key material - public addresses only.
- `run/` and `data/` hold campaign artifacts: `data/aero-bot-lp-canary-campaign/timing-report.md` is the canonical canary evidence.
- The 2026-09-08 live matrix (timed cycle + five scenarios, 72 receipt-verified broadcasts, chapters gnhf 12-16) is fully evidenced in `data/aero-bot-live-cycle-2/report.md`; the LP lifecycle now ends with `execute burn` then `execute swap-back`, and every refusal code there was proven live or documented untriggerable.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
