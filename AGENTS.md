# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Project pointers

- Validation gates before every commit: `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy src tests`, `uv run pytest`.
- Commit style: `gnhf N: <long summary>` continuing the repo's numbering; no co-author lines; never modify CHANGELOG.md.
- The LP execution layer's contracts, caps, refusal catalog, and live evidence live in `docs/lp_execution.md`; the swap executor's in `docs/execution.md`.
- Aerodrome's displayed emissions APR is the current-cell staked-value convention shared by runtime and rehearsal in `src/aero_bot/emissions_apr.py`; the derivation and live cross-checks are documented in `docs/lp_execution.md` under "Position status and Aerodrome's displayed emissions APR".
- Live Base reads use the app settings' public RPC; the first full Sugar sweep (~3 min over `mainnet.base.org`, one stderr progress line per page) pins the pool, after which decide/cycle/LP surfaces resolve in seconds through the known-pool fast path (`src/aero_bot/known_pool.py` and the executor's own pin path) - expect 429 backoff lines during sweeps, never silence.
- Real signing keys live in the macOS Keychain (`AERO_BOT_KEYCHAIN_SERVICE`/`AERO_BOT_KEYCHAIN_ACCOUNT`) on dev machines and in sealed sources on Linux (`AERO_BOT_KEY_SOURCE` selecting keychain/env/file - see `src/aero_bot/signing_key.py`); never print or persist key material - public addresses only.
- `run/` and `data/` hold campaign artifacts: `data/aero-bot-lp-canary-campaign/timing-report.md` is the canonical canary evidence.
- The scheduled loop is `aero-bot-cycle` (`docs/cycle.md`): reconcile-decide-act per run, the systemd timer decides when; the symbol is optional in the sealed cycle environment (`AERO_BOT_CYCLE_SYMBOL` unset/auto = the cross-board selector over every verified B20 pool, an explicit symbol pins one pool), the captain's 2026-09-09 trial rulings (cross-board selection with the 30-percent switch margin, 80-percent-of-book sizing inside the unchanged 100/100 caps, and 24/7 operation with event windows demoted to informational) live in `docs/cycle.md` and `docs/strategy.md`; the empty residual NFT 5703026 no longer blocks entry (only LIVE untracked positions refuse), and exits close through `execute exit-swap`.
- The deployed surfaces: cycle + alerts (`docs/alerts.md`), the daily Resend report timer (`deploy/systemd/aero-bot-daily-report.timer` - one dry selector cycle at 09:00 Melbourne, dark until the daily-report overlay file is sealed), the always-on range watchtower (`docs/watchtower.md` - dark until `AERO_BOT_WATCHTOWER_ENABLED` arms it), the shadow advisor (`docs/advisor.md` - a bounded fail-closed brief over the audited history, advisory-only, dark until the advisor overlay seals the plane's URL and model; `tools/bakeoff.py` scores candidate models over committed fixtures), the Mac-side teacher harness (`docs/teacher.md` - the dual-seat advisory corpus: headless Claude Code + Codex over one read-only `gcloud` window pull, three streams, typed absences, episodes into `~/.local/state/aero-bot/teacher/corpus.jsonl`; `deploy/launchd/install-mac.sh` generates the agents and never loads them), the hindsight scorer (`aero-bot-hindsight` - the daily report replaying the corpus against itself: per-desk availability and anomaly calibration over deterministic later facts, pending never guessed, `reports/hindsight_last.json` beside the corpus), encrypted daily audit backup (`docs/audit-backup.md`), and the Ubuntu kit (`deploy/install.sh` - the installer never arms a timer and stamps `DEPLOYED_COMMIT` itself, Phase 2 arms; `deploy/converge-check.sh` proves local HEAD and `/opt` byte-identical over SSH; `docs/deployment.md`).
- The advisor's inference plane is the Mac's Ollama bound tailnet-only at `100.106.111.37:11434`, and that bind is runtime state a Mac reboot wipes: before any advisor or bake-off work after a reboot, re-arm with `launchctl setenv OLLAMA_HOST "100.106.111.37:11434"` plus an Ollama restart (`open -a Ollama`, or `launchctl kickstart -k gui/$(id -u)/com.ollama.ollama`), give the Mac CLI its explicit `OLLAMA_HOST=100.106.111.37:11434` on every call, and verify with `curl -s http://100.106.111.37:11434/api/tags` - until re-armed, VM advisor passes fail closed as a typed `unreachable` absence while the bot trades on.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
