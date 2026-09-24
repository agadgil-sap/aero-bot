# Ubuntu deployment

The bot runs on a $0-tier always-on Linux VM (GCP e2-micro class: 0.25 shared vCPU burstable, 1 GB RAM, 10 GB disk is ample - one cycle is a few seconds of work per hour). This page is the complete kit: install, secrets, arming, the smoke checklist the captain walks before leaving, and the day-two runbook.

Everything network-facing stays closed. The repo binds loopback-only by construction (the application settings validate `bind_host` as a loopback address before the server starts), the firewall defaults to deny-inbound with OpenSSH alone, and the operator reaches the dashboard through an SSH tunnel - never an open port.

## Phase 2 order (what needs the captain)

Everything below through the install runs without any credential; from "Seal the secrets" on, the captain is present:

1. Provision the VM (Ubuntu 24.04 LTS), SSH in.
2. Clone the repository at the merged `main` and run the installer.
3. Seal the secrets (signing key, alert credentials, backup key) - the captain installs them.
4. Funding: ~20 USDC to the Safe, ~5 ETH to the relayer - the captain sends.
5. Smoke checklist (below) - every line verified green.
6. One live micro-cycle, verified end to end before departure.

## Install

From a clean checkout of merged `main` on the VM:

```bash
git clone git@github.com:agadgil-sap/aero-bot.git
cd aero-bot
sudo bash deploy/install.sh
```

The installer is idempotent and does, in order: Ubuntu check; base packages (git, curl, ufw, unattended-upgrades) with automatic security updates confirmed; uv installed system-wide; the dedicated `aero-bot` system user; the application tree into `/opt/aero-bot` (from the committed tree - run it from a clean checkout); the venv at `/opt/aero-bot/.venv` built with `uv sync --locked` (uv downloads Python 3.12 itself when the image lacks it); the state tree `/var/lib/aero-bot` (mode 0700, service-owned); the sealed environment templates under `/etc/aero-bot` (mode 0600, root-owned, never overwritten on re-run); every systemd unit installed and `daemon-reload`ed; and the firewall - default deny inbound, OpenSSH allowed, then enabled only with the SSH allow already in place so a session can never be locked out.

The installer stamps `/opt/aero-bot/DEPLOYED_COMMIT` itself with the exact commit the archive staged (`git rev-parse HEAD` under the same scoped `safe.directory` override as the archive), so the marker can never go stale the way the once-manually stamped file did - it read `a0993d1` on the production box while `/opt` already ran gnhf 21 bit-identical.
A tarball-only checkout (no `.git`) says `unversioned tarball tree installed <UTC timestamp>` honestly instead of naming a commit it cannot prove.

The installer does NOT enable any timer: nothing runs until Phase 2 arms it. The sealed cycle template defaults `AERO_BOT_BASE_RPC_URL` to `base.publicnode.com` because the official `mainnet.base.org` throttles the Sugar discovery sweeps into 429 cascades on small hosts (verified live on the e2-micro deploy); a paid endpoint raises the rate limits further.

## Seal the secrets (captain present)

```bash
# The signing key file: owned by the service user, owner-only bits - the
# FileSigningKeySource permission rule refuses anything looser.
sudo install -o aero-bot -g aero-bot -m 600 /dev/null /etc/aero-bot/signing-key.hex
sudoedit /etc/aero-bot/signing-key.hex   # paste the 64-hex key, no 0x needed
sudoedit /etc/aero-bot/cycle.env         # Safe + relayer addresses are pre-filled;
                                         # add the alert provider credentials
sudoedit /etc/aero-bot/daily-report.env  # the Resend overlay for the daily report
sudoedit /etc/aero-bot/advisor.env       # the inference-plane overlay for the shadow advisor
sudoedit /etc/aero-bot/backup.env        # openssl rand -hex 32 + git remote + deploy key
sudo install -o aero-bot -g aero-bot -m 600 <deploy-key> /etc/aero-bot/backup-deploy.key
```

The env files are `root:aero-bot 0640`: systemd reads them as root, the service user can source them for manual operator runs, and writes stay root-only.

Discipline, enforced in code: the key file refuses any group/other permission bit; the env files are root-owned 0600; nothing secrets-shaped is ever committed, logged, or echoed - public addresses only. The reference quote (`AERO_BOT_CYCLE_REFERENCE_PRICE_USDC`) is the operator's honest duty: an open position defensively exits without a quote, and a stale constant is a stale quote - update it on the cadence the captain sets until the live oracle feed lands. Pinned cycles seal one bare number; the cross-board selector (see [the cycle documentation](docs/cycle.md)) seals per-symbol `SYMBOL=PRICE` pairs.

## Arm

```bash
sudo systemctl enable --now aero-bot-dashboard.service
sudo systemctl enable --now aero-bot-cycle@auto.timer aero-bot-audit-backup.timer
sudo systemctl enable --now aero-bot-daily-report.timer
systemctl list-timers | grep aero-bot
```

The production selector timer uses `aero-bot-cycle@auto.timer`; pin a pool such as `@AAPLc.timer` only for an explicit operator override. The timer cadence is set by the installed template/drop-ins. The backup runs daily, and the daily report (one dry selector cycle whose summary email is the captain's portfolio report - see [the alerts documentation](docs/alerts.md)) fires at 09:00 Melbourne and stays dark until `/etc/aero-bot/daily-report.env` is sealed. Cycles are hardened oneshots: they never overlap, never auto-retry - the next tick reconciles.

The range watchtower ships dark and arms separately, on its own explicit decision: set `AERO_BOT_WATCHTOWER_ENABLED=1` in `/etc/aero-bot/cycle.env`, then `sudo systemctl enable --now aero-bot-watchtower@AAPLc.service`.
The shadow advisor ships dark the same way: seal the inference plane's URL and model in `/etc/aero-bot/advisor.env` (see [the advisor documentation](docs/advisor.md) for the full overlay and the VM-tailnet prerequisite), then `sudo systemctl enable --now aero-bot-advisor.timer` for one bounded advisory pass every thirty minutes - advisory-only, fail-closed, never touching the cycle book.
See [the watchtower documentation](docs/watchtower.md) for the trip semantics, the fail-safe posture, and the day-two triage.

## Smoke checklist (captain present, every line green before departure)

1. **Dry cycle decides.** `sudo -u aero-bot bash -c 'set -a; . /etc/aero-bot/cycle.env; set +a; /opt/aero-bot/.venv/bin/aero-bot-cycle --symbol AAPLc --dry-run --json'` - exit 0, a complete report with balances, custody, and the honest verdict (a `hold (reference_stale)` without a sealed quote is correct behavior; with `AERO_BOT_CYCLE_REFERENCE_PRICE_USDC` sealed, the full gate chain runs). The first run on a fresh install pays one full Sugar sweep - several minutes over the public RPC - while printing one `lp sugar enumeration: N pools` progress line per page on stderr; it pins the verified pool, and every later cycle resolves in seconds through the known-pool fast path. Wait for the report; the progress lines mean it is working, not hung.
2. **The sealed key file obeys its discipline.** `sudo stat -c '%U:%a' /etc/aero-bot/signing-key.hex` prints `aero-bot:600`.
3. **Email arrives.** With the alert provider sealed, force one email by running the cycle dry-run again and confirming the summary lands in the captain's inbox (the delivery warning on stderr is the failure signal).
4. **One-shot service run.** `sudo systemctl start aero-bot-cycle@AAPLc.service` then `journalctl -u aero-bot-cycle@AAPLc.service -n 40` shows the JSON report; exit status `SUCCESS`.
5. **Timers armed.** `systemctl list-timers` lists both, with future next-runs.
6. **Audit backup round-trips.** `sudo systemctl start aero-bot-audit-backup.service` - success, and the private repository's `audit-backup` branch carries today's encrypted bundle; decrypt it once on the captain's laptop (`decrypt_bundle`) and re-verify the chain from the JSONL.
7. **One live micro-cycle.** With the reference quote sealed and the captain watching: run the cycle live. Whatever the locked engine orders is the correct product - an entry within the caps (mint + stake hashes on Base, book tracking the token id, next cycle reports the position and P&L), or an honest hold. Verify every transaction hash on BaseScan, the Safe's balances moved by exactly the plan, and one more dry cycle reconciles the new state cleanly.
8. **Dashboard tunnels.** `ssh -L 8765:127.0.0.1:8765 <vm>` then `http://127.0.0.1:8765` locally - and from the VM itself, `ss -tlnp | grep 8765` shows the bind on 127.0.0.1 only.
9. **Firewall.** `sudo ufw status verbose`: deny incoming, allow outgoing, OpenSSH only.
10. **Sign off.** The captain leaves; the first unattended cycle's email arrives on schedule.

## Day-two runbook

- **The cycle's journal:** `journalctl -u aero-bot-cycle@AAPLc.service --since "1 hour ago"`.
- **A halted cycle:** the email carries the refusal code - the catalog lives in `docs/lp_execution.md`; out-of-band halts need eyes, everything else self-corrects on the next tick.
- **Resetting the book** (only after reconciling by hand): stop the timer, inspect `/var/lib/aero-bot/cycle_state.json`, and let the next cycle's reconciliation rebuild truth - the chain, never memory, is the source.
- **Upgrading:** `cd aero-bot && git fetch && git checkout main && git pull --ff-only && sudo bash deploy/install.sh` - the venv rebuilds, units refresh, sealed files survive.
- **Proving convergence:** from the laptop checkout, `bash deploy/converge-check.sh` hashes a local `git archive HEAD` extraction against the deployed `/opt/aero-bot` tree over SSH and prints one verdict - byte-identical, converged aside from known snapshots, or diverged with the diff.
Build artifacts (`.venv`, `__pycache__`, `*.pyc`, caches) and the per-install `DEPLOYED_COMMIT` marker are excluded on both sides; content, not the stamp, is the proof.
Remote-only `<file>.pre-<tag>-<timestamp>` files are the rollback snapshots the 2026-09-20 on-box hotfix sessions left in `/opt/aero-bot` - they are reported as known extras, not divergence, and should be removed during the next deploy (`sudo find /opt/aero-bot -name '*.pre-*'` lists them).
The SSH transport and remote path default to the production box (`gcloud compute ssh aero-bot --zone us-west1-b`, `/opt/aero-bot`) and override through `AERO_BOT_SSH` / `AERO_BOT_REMOTE_APP`.
- **The relayer gas tank:** the alert floor (default 0.0005 ETH) warns before the executor's own 0.0002-ETH floor refuses broadcasts.

## The Mac-side teacher kit

The teacher harness (see [the teacher documentation](teacher.md)) runs on the operator's Mac, not this box: its launchd agents pull one read-only window over `gcloud compute ssh` and ask the advisory seats, so the box itself needs nothing new - the pull only reads the audit database and cycle book through the existing `sudo` path.
Generate the four user agents from the Mac checkout:

```
bash deploy/launchd/install-mac.sh
```

The installer writes `com.aero-bot.teacher-{tactical,daily,news,hindsight,upgrade}.plist` into `~/Library/LaunchAgents` and never loads them; arming each job is the operator's own `launchctl bootstrap` line (printed by the installer, documented in the teacher guide).
Because launchd user agents cannot read macOS's TCC-protected folders, the installer also maintains a stateless worktree at `~/.local/state/aero-bot/teacher/repo` (recreated from the checkout's HEAD on every install) and points the agents at it - see the teacher guide for the full story.
The `gcloud` credential the pull rides is the operator's own login on the Mac.
