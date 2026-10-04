#!/usr/bin/env bash
# Aero Bot Ubuntu deployment installer.
#
# Idempotent: safe to re-run. Installs the application under /opt/aero-bot,
# the dedicated service user, uv and the virtual environment, the sealed
# environment-file templates under /etc/aero-bot, and every systemd unit -
# but NEVER enables the cycle or backup timers: those arm only in Phase 2,
# after the captain installs the real secrets and funds the Safe.
#
# Run as root from the repository checkout:
#   sudo bash deploy/install.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/aero-bot"
SERVICE_USER="aero-bot"
STATE_DIR="/var/lib/aero-bot"
CONFIG_DIR="/etc/aero-bot"
UNIT_DIR="/etc/systemd/system"
VENV="${APP_DIR}/.venv"

log() { printf '[install] %s\n' "$*"; }
die() { printf '[install] FATAL: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo bash deploy/install.sh)"

# ---------------------------------------------------------------- platform
if [[ ! -f /etc/os-release ]]; then
    die "this installer targets Ubuntu; /etc/os-release is missing"
fi
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "this installer targets Ubuntu (found ${ID:-unknown})"
log "Ubuntu ${VERSION_ID:-?} confirmed"

# ---------------------------------------------------------------- packages
log "installing base packages (git, curl, ca-certificates, ufw, unattended-upgrades)"
export DEBIAN_FRONTEND=noninteractive
apt-get update --yes
apt-get install --yes --no-install-recommends git curl ca-certificates ufw unattended-upgrades

# Automatic security updates stay enabled; the cycle's email hook is the
# operational signal that matters, but the host should patch itself.
log "ensuring unattended-upgrades automatic config"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# ---------------------------------------------------------------- uv
if ! command -v uv >/dev/null 2>&1; then
    log "installing uv system-wide"
    curl -LsSf https://astral.sh/uv/install.sh \
        | env UV_INSTALL_DIR="/usr/local/bin" UV_UNMANAGED_INSTALL=1 sh
fi
command -v uv >/dev/null 2>&1 || die "uv did not install"
log "uv $(uv --version | awk '{print $2}') present"

# ---------------------------------------------------------------- service user
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    log "creating the dedicated ${SERVICE_USER} system user"
    useradd --system --create-home --home-dir "${STATE_DIR}" \
        --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

# ---------------------------------------------------------------- application
log "installing the application tree into ${APP_DIR}"
mkdir -p "${APP_DIR}"
# The full checkout travels so docs and metadata stay on the box; git
# internals ride along and cost nothing. The archive runs with a scoped
# safe.directory so root may install from an operator-owned checkout (the
# normal cloud flow: SSH as your user, sudo the installer) without git's
# dubious-ownership guard refusing the very first step.
if [[ -d "${REPO_ROOT}/.git" ]]; then
    git -c safe.directory="${REPO_ROOT}" -C "${REPO_ROOT}" \
        archive --format=tar HEAD | tar -x -C "${APP_DIR}"
    # The deployment marker names the exact commit the archive staged, written
    # by the installer itself so it cannot go stale the way the once-manually
    # stamped file did (it read a0993d1 while /opt already ran gnhf 21).
    git -c safe.directory="${REPO_ROOT}" -C "${REPO_ROOT}" \
        rev-parse HEAD >"${APP_DIR}/DEPLOYED_COMMIT"
else
    tar -C "${REPO_ROOT}" \
        --exclude=.venv --exclude=__pycache__ --exclude="*.pyc" \
        --exclude=.pytest_cache --exclude=.mypy_cache -c . | tar -x -C "${APP_DIR}"
    printf 'unversioned tarball tree installed %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"${APP_DIR}/DEPLOYED_COMMIT"
fi
chown -R "root:${SERVICE_USER}" "${APP_DIR}"
chmod -R u=rwX,g=rX,o= "${APP_DIR}"

log "building the virtual environment (uv downloads Python 3.12 when needed)"
# The venv lives inside the root-owned tree but belongs to the service user,
# which is the only writer the build and later reinstalls need. A re-run's
# recursive chown above sweeps an existing venv back to root, so ownership
# is re-asserted here: without it uv cannot replace the entry points and
# the idempotent reinstall dies halfway.
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${VENV}"
[[ -d "${VENV}" ]] && chown -R "${SERVICE_USER}:${SERVICE_USER}" "${VENV}"
sudo -u "${SERVICE_USER}" env HOME="${STATE_DIR}" \
    UV_PYTHON_INSTALL_DIR="${STATE_DIR}/.uv-python" \
    uv sync --project "${APP_DIR}" --locked --no-dev
[[ -x "${VENV}/bin/aero-bot-cycle" ]] || die "the venv is missing the cycle entry point"

# ---------------------------------------------------------------- state
log "preparing the state tree under ${STATE_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700 "${STATE_DIR}"

# ---------------------------------------------------------------- sealed env
log "installing the sealed environment templates under ${CONFIG_DIR}"
# root:service 0640: systemd reads these as root before dropping
# privileges, and the service user may read them for manual operator runs -
# writes stay root-only, and the only group is the bot itself. The directory
# itself is root:service 0750 for the same reason: the cycle service runs as
# the service user and must traverse it to read the signing-key file.
install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_DIR}/cycle.env" ]]; then
    cat >"${CONFIG_DIR}/cycle.env" <<'EOF'
# Aero Bot cycle environment - seal real values here (mode 0600, root-owned).
# The Safe whose positions and balances the cycle manages.
AERO_BOT_SAFE_ADDRESS=0xB69ab6C7E73F711D5f2d10feD8f0d09B1D028C28
# The relayer's public address (never secret); lets dry runs read its ETH.
AERO_BOT_RELAYER_ADDRESS=0x0c49cc4D53423CCd6be2Bcf115a25F418649C5C9
# The signing key arrives through the sealed source: either this variable
# (preferred under systemd) or the owner-only file below.
#AERO_BOT_KEY_SOURCE=env
#AERO_BOT_SIGNING_KEY_HEX=<64-hex-character key>
#AERO_BOT_KEY_SOURCE=file
AERO_BOT_SIGNING_KEY_FILE=/etc/aero-bot/signing-key.hex
# State lives under the service tree: audit chain, pool pins, cycle book.
AERO_BOT_AUDIT_DATABASE_PATH=/var/lib/aero-bot/audit.sqlite3
AERO_BOT_LP_POOL_PINS_PATH=/var/lib/aero-bot/lp_pool_pins.json
AERO_BOT_CYCLE_STATE_PATH=/var/lib/aero-bot/cycle_state.json
# The Base RPC endpoint: base.publicnode.com tolerates the Sugar
# pagination sweeps where the official mainnet.base.org throttles small
# hosts into 429 cascades (verified live on an e2-micro deploy); a paid
# endpoint raises the rate limits further.
AERO_BOT_BASE_RPC_URL=https://base.publicnode.com
# External references are diagnostic-only for scheduled production cycles.
# Trading authority comes from the exact resolved Aerodrome pool. Manual
# --reference-price remains available for research and diagnostic runs.
# Never seal a static reference as unattended trading authority.
# The live underlying-equity reference feed: off (the default) keeps the
# injected-constant behavior, yahoo reads the credential-free chart
# endpoint, and finnhub reads the keyed /quote endpoint (requires sealing
# AERO_BOT_STOCK_REFERENCE_TOKEN, a free finnhub.io key). Every feed quote
# carries the provider's own as-of age and stays diagnostic-only.
#AERO_BOT_CYCLE_REFERENCE_FEED=off
# The cycle's symbol scope: unset or "auto" runs the cross-board selector
# over every verified B20 pool (the default); an explicit symbol pins one
# pool for operator runs. The systemd template's instance name (--symbol %i)
# feeds the same resolution, so aero-bot-cycle@auto.timer selects too.
#AERO_BOT_CYCLE_SYMBOL=auto
# The cross-board switch margin: another pool must beat the held pool's
# qualifying emissions APR by more than this fraction before a switch fires
# (default 0.30, the captain's 2026-09-09 trial ruling).
#AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION=0.30
# The out-of-range grace window: once a staked position has sat outside its
# earning range this many minutes, the policy must act - recenter when the
# economics pass, otherwise exit - because every minute out of range forgoes
# emissions income (default 10, the captain's 2026-09-27 correction).
#AERO_BOT_CYCLE_OUT_OF_RANGE_GRACE_MINUTES=10
# The reward-conversion threshold: unclaimed AERO whose value at the last
# observed price exceeds this many USDC converts to USDC inside the cycle's
# act step (default 5, the captain's 2026-09-27 ruling).
#AERO_BOT_CYCLE_AERO_CONVERSION_MIN_USDC=5
# The reward posture (the captain's retained-AERO ruling): convert (the
# default) claims and swaps rewards to USDC; retain claims through the
# same audited collect surface but holds the AERO in the Safe as book
# equity and never invokes the conversion swap - a restart can never
# trip over the conversion surface while it is under reassessment.
#AERO_BOT_CYCLE_REWARD_POSTURE=convert
# The stray-stock dust bounds (the captain's 2026-10-03 ruling): a stray
# stock balance whose USDC value at its own pool's pinned snapshot price
# sits strictly below the floor is dust - retained in the Safe, never
# adopted as inventory, never swapped - while the sum of every ignored
# dust balance stays at or below the aggregate bound (always at least
# the floor) so many-token splitting cannot hide meaningful exposure;
# both overrides sit under the one-USDC hard ceilings, and a balance
# that cannot be priced still refuses the cycle fail-closed.
#AERO_BOT_CYCLE_STOCK_DUST_FLOOR_USDC=0.01
#AERO_BOT_CYCLE_STOCK_DUST_AGGREGATE_USDC=0.10
# The allocator's portfolio bounds (the captain's gnhf 33 ruling, as
# corrected below the activation equity by the captain's 2026-09-28
# sub-1000 ruling): every default is locked and every override stays
# under the hard ceilings - at most ten concurrent positions, a positive
# minimum position size inside the total cap, and fractions inside
# (0, 1]. Below the activation equity (default 1000 USDC) there is no
# per-name minimum and no minimum-driven reserve at all - the book
# deploys its available funds into the qualifying board; the configured
# minimum governs only at or above the activation equity.
#AERO_BOT_CYCLE_TIER_BAND_FRACTION=0.50
#AERO_BOT_CYCLE_MAX_POSITIONS=10
#AERO_BOT_CYCLE_MIN_POSITION_USDC=80
#AERO_BOT_CYCLE_CONCENTRATION_CAP_FRACTION=0.35
# The hard floor under the effective minimum position size (gnhf 36
# parameter coherence, engaged-cap regime only): the effective minimum
# is max(this floor, min(the minimum above, the per-name concentration
# clamp at the live equity)) - a small engaged book deploys at the
# clamp, never below this floor, and a book whose clamp sits under the
# floor stays cash. The floor must stay at or under the configured
# minimum.
#AERO_BOT_CYCLE_MIN_POSITION_FLOOR_USDC=30
# The book equity at or above which the per-name concentration cap
# AND the configured minimum engage (the captain's 2026-09-28 rulings):
# below it neither binds - the cap does not clamp and there is no
# per-name minimum, so the book deploys its available funds into the
# qualifying board while it funds toward the cap. The hard ceiling is
# the 1000-USDC total cap itself, so an override can only engage the
# cap sooner, never later.
#AERO_BOT_CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC=1000
# The trailing window of cycle readings the conservative income
# expectation floors itself at (the captain's 2026-09-28 correction,
# default 6 cycles = half an hour at the five-minute cadence): every
# surface that assumes an expected daily yield - the range-width solve,
# the gas cost-versus-yield checks, the income-forgone accounting -
# reads min(current, median(window)) so a transient spike never sizes
# or justifies a position. Information, never exclusion: the qualifying
# gates and the ranking keep the raw venue-convention APR, and high
# readings still deploy (the boosted yield thesis).
#AERO_BOT_CYCLE_INCOME_HISTORY_CYCLES=6
# Range monitoring is allowed to observe and alert at seconds-level, but the
# shipped systemd unit is forced into --monitor-only. It cannot trade.
AERO_BOT_WATCHTOWER_ENABLED=0
AERO_BOT_WATCHTOWER_POLL_SECONDS=5
# Email alerts (see docs/alerts.md); provider none stays silent.
#AERO_BOT_ALERT_PROVIDER=smtp
#AERO_BOT_ALERT_FROM=aero-bot@example.com
#AERO_BOT_ALERT_TO=capn@example.com
#AERO_BOT_ALERT_SMTP_HOST=smtp.example.com
#AERO_BOT_ALERT_SMTP_USER=aero-bot@example.com
#AERO_BOT_ALERT_SMTP_PASSWORD=<sealed>
EOF
    chown root:"${SERVICE_USER}" "${CONFIG_DIR}/cycle.env"
    chmod 0640 "${CONFIG_DIR}/cycle.env"
fi
if [[ ! -f "${CONFIG_DIR}/daily-report.env" ]]; then
    cat >"${CONFIG_DIR}/daily-report.env" <<'EOF'
# Aero Bot daily-report environment - seal real values here (mode 0640).
# The daily-report unit overlays this onto cycle.env: the Resend transport
# (see docs/alerts.md) and the reporting recipient live here, while the
# cycle's own alert routing stays in cycle.env.
#AERO_BOT_ALERT_PROVIDER=resend
#AERO_BOT_ALERT_RESEND_API_KEY=<sealed>
#AERO_BOT_ALERT_TO=capn@example.com
EOF
    chown root:"${SERVICE_USER}" "${CONFIG_DIR}/daily-report.env"
    chmod 0640 "${CONFIG_DIR}/daily-report.env"
fi
if [[ ! -f "${CONFIG_DIR}/advisor.env" ]]; then
    cat >"${CONFIG_DIR}/advisor.env" <<'EOF'
# Aero Bot shadow-advisor environment - seal real values here (mode 0640).
# The advisor unit overlays this onto cycle.env: the private inference
# plane's URL and model live here (see docs/advisor.md). The command is
# dark until both values are uncommented, and it stays advisory-only no
# matter what this file carries.
# The student seat's dedicated plane: the Mac-side Ollama instance that
# pins the model resident (OLLAMA_KEEP_ALIVE=-1, one inference slot), so
# no window pays the ~22 GB cold reload and no other consumer can evict
# or starve the seat.
#AERO_BOT_ADVISOR_URL=http://100.106.111.37:11435
# The shared plane behind it: consulted ONLY when the dedicated plane is
# unreachable, so a downed plane costs availability once instead of for
# every window. The payload (and model) never change between attempts.
#AERO_BOT_ADVISOR_FALLBACK_URL=http://100.106.111.37:11434
#AERO_BOT_ADVISOR_MODEL=qwen3.6:35b-a3b
# The timeout is sized against the model's measured WARM latency plus
# margin: measured warm brief latency 2.1-3.8 s over three replayed real
# windows (native protocol, thinking off, JSON mode on) against a cold
# load of 3.3 s page-cached, so 90 s carries a >20x warm margin and fully
# covers a post-reboot cold window while a wedged plane still fails
# bounded.
#AERO_BOT_ADVISOR_TIMEOUT_SECONDS=90
# Reasoning models: the budget must cover thinking plus the JSON answer
# (default 4096).
#AERO_BOT_ADVISOR_MAX_TOKENS=4096
# Thinking is OFF by default (the hardened posture): observed thinking
# chains ran to 9.7k tokens and blew the seat's clock, while anomaly
# briefs over deterministic facts do not need deliberation. Seal 0 only
# with eyes open - availability outranks deliberation for this seat.
#AERO_BOT_ADVISOR_DISABLE_THINKING=1
# JSON mode is ON by default: the plane constrains generation to valid
# JSON so malformed_json absences are eliminated at the source. Seal 0
# only for a strict plane that rejects the native JSON format field.
#AERO_BOT_ADVISOR_JSON_MODE=1
# The upgrade loop's seal: a teaching block proposed by the teachers and
# written by the operator (see docs/teacher.md). It appends to the system
# prompt, never replaces it, and the pass fails closed if the file is
# unreadable, empty, or beyond 4000 characters.
#AERO_BOT_ADVISOR_TEACHING_FILE=/etc/aero-bot/advisor-teaching.txt
EOF
    chown root:"${SERVICE_USER}" "${CONFIG_DIR}/advisor.env"
    chmod 0640 "${CONFIG_DIR}/advisor.env"
fi
if [[ ! -f "${CONFIG_DIR}/backup.env" ]]; then
    cat >"${CONFIG_DIR}/backup.env" <<'EOF'
# Aero Bot audit-backup environment - seal real values here (mode 0600).
# The audit chain the job verifies and ships lives under the service state
# tree; without this line Settings falls back to its platform default and
# the service sees an empty store.
AERO_BOT_AUDIT_DATABASE_PATH=/var/lib/aero-bot/audit.sqlite3
#AERO_BOT_BACKUP_KEY_HEX=<openssl rand -hex 32>
#AERO_BOT_BACKUP_GIT_REMOTE=git@github.com:org/aero-bot-audit-backup.git
# The deploy key for the SSH push ships at the documented path; without
# this line the runner sets no GIT_SSH_COMMAND and git offers no identity.
AERO_BOT_BACKUP_DEPLOY_KEY_PATH=/etc/aero-bot/backup-deploy.key
#AERO_BOT_BACKUP_WORKTREE_PATH=/var/lib/aero-bot/audit-backup-repo
EOF
    chown root:"${SERVICE_USER}" "${CONFIG_DIR}/backup.env"
    chmod 0640 "${CONFIG_DIR}/backup.env"
fi
if [[ ! -f "${CONFIG_DIR}/dashboard.env" ]]; then
    cat >"${CONFIG_DIR}/dashboard.env" <<'EOF'
# Aero Bot dashboard environment - the read-only UI's sealed inputs
# (mode 0640, root-owned, created by the installer).
# The production audit chain the dashboard's audit-health endpoint
# verifies lives under the service state tree; without this line Settings
# falls back to its platform default and the dashboard opens its own
# empty store - record_count 0, status empty - instead of the real chain
# (the gnhf 34 fix).
AERO_BOT_AUDIT_DATABASE_PATH=/var/lib/aero-bot/audit.sqlite3
EOF
    chown root:"${SERVICE_USER}" "${CONFIG_DIR}/dashboard.env"
    chmod 0640 "${CONFIG_DIR}/dashboard.env"
fi

# ---------------------------------------------------------------- seal drift
log "checking the sealed environment files for drift from the documented planes"
# The installer never rewrites an existing seal (the idempotence design
# pinned above), and that is exactly how the 2026-09-28 advisor
# misconfiguration survived every deploy: the sealed primary kept
# pointing at the shared plane while the dedicated student plane sat
# unused and the student seat went dark for 36 hours. Every deploy now
# says so loudly; the repair itself stays the operator's explicit act
# (deploy/seal-repair.sh, see docs/deployment.md "Seal drift and repair").
seal_effective_value() {
    { grep -E "^[[:space:]]*${2}=" "$1" 2>/dev/null || true; } \
        | tail -n 1 | cut -d= -f2- | tr -d '\r'
}
SEAL_DRIFT=0
if [[ -f "${CONFIG_DIR}/advisor.env" ]]; then
    SEALED_URL="$(seal_effective_value "${CONFIG_DIR}/advisor.env" AERO_BOT_ADVISOR_URL)"
    SEALED_FALLBACK="$(seal_effective_value "${CONFIG_DIR}/advisor.env" AERO_BOT_ADVISOR_FALLBACK_URL)"
    if [[ -n "$SEALED_URL" && "$SEALED_URL" != "http://100.106.111.37:11435" ]]; then
        SEAL_DRIFT=1
        log "WARNING: advisor.env primary plane is '$SEALED_URL', not the documented dedicated plane http://100.106.111.37:11435"
    fi
    if [[ -z "$SEALED_FALLBACK" ]]; then
        SEAL_DRIFT=1
        log "WARNING: advisor.env carries no effective AERO_BOT_ADVISOR_FALLBACK_URL; a downed primary costs every pass"
    fi
fi
if [[ -f "${CONFIG_DIR}/cycle.env" ]]; then
    SEALED_PROVIDER="$(seal_effective_value "${CONFIG_DIR}/cycle.env" AERO_BOT_ALERT_PROVIDER)"
    SEALED_RESEND_KEY="$(seal_effective_value "${CONFIG_DIR}/cycle.env" AERO_BOT_ALERT_RESEND_API_KEY)"
    if [[ -z "$SEALED_PROVIDER" || "$SEALED_PROVIDER" == "none" ]] && [[ -n "$SEALED_RESEND_KEY" ]]; then
        SEAL_DRIFT=1
        log "WARNING: cycle.env seals AERO_BOT_ALERT_PROVIDER='${SEALED_PROVIDER:-none}' while a Resend key is sealed; alerts compute and never email"
    fi
fi
if [[ $SEAL_DRIFT -eq 0 ]]; then
    log "no seal drift detected"
else
    log "WARNING: seal drift above survives deploys by design; repair with 'sudo bash deploy/seal-repair.sh --check' then --apply"
fi

# ---------------------------------------------------------------- systemd
log "installing the systemd units"
for unit in "${REPO_ROOT}"/deploy/systemd/*.service "${REPO_ROOT}"/deploy/systemd/*.timer; do
    install -o root -g root -m 0644 "${unit}" "${UNIT_DIR}/"
done
systemctl daemon-reload
# The long-running services (the dashboard, any armed watchtower) keep
# serving the process they started with: an upgrade that only refreshes
# units and code leaves yesterday's process running the old environment
# forever - the gnhf 35 postmortem found the dashboard still verifying an
# empty store two days after the gnhf 34 deploy precisely because nothing
# restarted it. try-restart restarts only units that are already active,
# so the installer still never arms anything: a stopped service stays
# stopped and Phase 2's enable --now remains the only arming path.
systemctl try-restart aero-bot-dashboard.service 'aero-bot-watchtower@*.service'

# ---------------------------------------------------------------- firewall
log "configuring ufw: default-deny inbound, OpenSSH only"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
# Enable only takes effect with the SSH allow already in place, so an
# interactive session cannot be locked out by this script.
if ! ufw status | grep -q "Status: active"; then
    ufw --force enable
fi
ufw status verbose

# ---------------------------------------------------------------- done
log "install complete"
log "units installed (NOT enabled - Phase 2 arms them after secrets and funding):"
ls -1 "${REPO_ROOT}/deploy/systemd/" | sed 's/^/  /'
log "next steps, in order - see docs/deployment.md:"
log "  1. seal the real values into ${CONFIG_DIR}/cycle.env, daily-report.env, advisor.env, and backup.env"
log "  2. run the smoke checklist (docs/deployment.md)"
log "  3. arm one cycle timer: aero-bot-cycle@auto.timer for dynamic B20 selection, or @AAPLc.timer to pin Apple; also arm aero-bot-audit-backup.timer"
