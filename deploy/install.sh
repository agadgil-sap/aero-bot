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
# internals ride along and cost nothing.
if [[ -d "${REPO_ROOT}/.git" ]]; then
    git -C "${REPO_ROOT}" archive --format=tar HEAD | tar -x -C "${APP_DIR}"
else
    tar -C "${REPO_ROOT}" \
        --exclude=.venv --exclude=__pycache__ --exclude="*.pyc" \
        --exclude=.pytest_cache --exclude=.mypy_cache -c . | tar -x -C "${APP_DIR}"
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
# The cycle's symbol scope: unset or "auto" runs the cross-board selector
# over every verified B20 pool (the default); an explicit symbol pins one
# pool for operator runs. The systemd template's instance name (--symbol %i)
# feeds the same resolution, so aero-bot-cycle@auto.timer selects too.
#AERO_BOT_CYCLE_SYMBOL=auto
# The cross-board switch margin: another pool must beat the held pool's
# qualifying emissions APR by more than this fraction before a switch fires
# (default 0.30, the captain's 2026-09-09 trial ruling).
#AERO_BOT_CYCLE_SWITCH_MARGIN_FRACTION=0.30
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

# ---------------------------------------------------------------- systemd
log "installing the systemd units"
for unit in "${REPO_ROOT}"/deploy/systemd/*.service "${REPO_ROOT}"/deploy/systemd/*.timer; do
    install -o root -g root -m 0644 "${unit}" "${UNIT_DIR}/"
done
systemctl daemon-reload

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
log "  1. seal the real values into ${CONFIG_DIR}/cycle.env and backup.env"
log "  2. run the smoke checklist (docs/deployment.md)"
log "  3. arm one cycle timer: aero-bot-cycle@auto.timer for dynamic B20 selection, or @AAPLc.timer to pin Apple; also arm aero-bot-audit-backup.timer"
