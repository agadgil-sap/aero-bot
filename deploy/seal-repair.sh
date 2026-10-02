#!/usr/bin/env bash
# Repair the known seal drift that no deploy can fix, idempotently and
# guarded (see docs/deployment.md, "Seal drift and repair").
#
# Why this exists: the installer never rewrites an existing seal (its
# idempotence design), so the 2026-09-28 misconfiguration - the advisor
# sealed against the dead shared plane with no fallback, alerts sealed
# silent at the provider line despite working Resend credentials -
# survived every deploy while the student seat went dark for 36 hours.
#
# Repairs, each idempotent (a converged seal is a no-op):
#   1. /etc/aero-bot/advisor.env: AERO_BOT_ADVISOR_URL repointed to the
#      dedicated student plane, AERO_BOT_ADVISOR_FALLBACK_URL set to the
#      shared plane behind it, then `systemctl try-restart
#      aero-bot-advisor` (restarts only an already-active unit; every
#      later pass reads the repaired seal at start).
#   2. /etc/aero-bot/cycle.env: AERO_BOT_ALERT_PROVIDER=resend enabled
#      ONLY when the Resend key, FROM, and TO are already sealed -
#      nothing is provisioned and a missing credential is a refusal,
#      never a guess.
#
# Every modified file is backed up beside itself (.bak-<timestamp>) with
# its ownership and mode preserved.
#
# Usage on the VM, as root, from the deployed checkout:
#   sudo bash deploy/seal-repair.sh --check   # report drift, change nothing
#   sudo bash deploy/seal-repair.sh --apply   # repair and restart
#
# Exit codes: 0 no drift (or fully applied), 3 drift found (check mode),
# 4 a repair was refused (missing sealed prerequisites), 1 misuse.
set -euo pipefail

CONFIG_DIR="${AERO_BOT_CONFIG_DIR:-/etc/aero-bot}"
SYSTEMCTL="${AERO_BOT_SYSTEMCTL:-systemctl}"
ADVISOR_ENV="$CONFIG_DIR/advisor.env"
CYCLE_ENV="$CONFIG_DIR/cycle.env"
ADVISOR_UNIT="${AERO_BOT_ADVISOR_UNIT:-aero-bot-advisor.service}"
# The documented planes (docs/advisor.md, "The student plane"); the
# installer template carries the same pair.
DEDICATED_PLANE="${AERO_BOT_SEAL_DEDICATED_URL:-http://100.106.111.37:11435}"
SHARED_PLANE="${AERO_BOT_SEAL_SHARED_URL:-http://100.106.111.37:11434}"

log() { printf '[seal-repair] %s\n' "$*"; }
die() { printf '[seal-repair] FATAL: %s\n' "$*" >&2; exit 1; }

# Fixture mode: the root guard protects the real seal directory; an
# overridden CONFIG_DIR is how the tests exercise the script off-box.
if [[ "$CONFIG_DIR" == "/etc/aero-bot" && $EUID -ne 0 ]]; then
    die "run as root against $CONFIG_DIR (or point AERO_BOT_CONFIG_DIR at a fixture)"
fi

# The last effective (uncommented) assignment to one variable, else empty.
effective_value() {
    { grep -E "^[[:space:]]*${2}=" "$1" 2>/dev/null || true; } \
        | tail -n 1 | cut -d= -f2- | tr -d '\r'
}

# Idempotently set one variable's effective assignment; a commented or
# absent line is appended, an existing effective line is replaced.
# The rewrite avoids `sed -i` because its in-place suffix argument differs
# between GNU and BSD sed: the filtered text lands in a sibling temporary
# and is copied back over the original inode, preserving owner and mode.
set_env_var() {
    local file="$1" var="$2" value="$3"
    local temporary="${file}.seal-repair-new"
    sed -E "\|^[[:space:]]*${var}=|d" "$file" >"$temporary"
    # A seal whose last line lacks a newline must not merge with the append.
    if [[ -n "$(tail -c 1 "$temporary")" ]]; then
        printf '\n' >>"$temporary"
    fi
    printf '%s=%s\n' "$var" "$value" >>"$temporary"
    cat "$temporary" >"$file"
    rm "$temporary"
}

# Back one seal up beside itself before its first mutation this run,
# preserving ownership and mode (cp -p).
rewrite_seal() {
    local file="$1"
    local stamp
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$file" "${file}.bak-${stamp}"
    log "backed up ${file} to ${file}.bak-${stamp}"
}

MODE="${1:-}"
[[ "$MODE" == "--check" || "$MODE" == "--apply" ]] \
    || die "usage: seal-repair.sh --check | --apply"
[[ -f "$ADVISOR_ENV" ]] || die "missing $ADVISOR_ENV"
[[ -f "$CYCLE_ENV" ]] || die "missing $CYCLE_ENV"

DRIFT=0
REFUSED=0
ADVISOR_CHANGED=0

# ---------------------------------------------------------------- advisor
ADVISOR_URL_NOW="$(effective_value "$ADVISOR_ENV" AERO_BOT_ADVISOR_URL)"
FALLBACK_URL_NOW="$(effective_value "$ADVISOR_ENV" AERO_BOT_ADVISOR_FALLBACK_URL)"
if [[ -n "$ADVISOR_URL_NOW" && "$ADVISOR_URL_NOW" == "$DEDICATED_PLANE" ]]; then
    log "advisor primary: OK ($ADVISOR_URL_NOW)"
else
    DRIFT=1
    log "advisor primary: DRIFT - sealed '${ADVISOR_URL_NOW:-<unset>}' != dedicated plane $DEDICATED_PLANE"
fi
if [[ -n "$FALLBACK_URL_NOW" && "$FALLBACK_URL_NOW" == "$SHARED_PLANE" ]]; then
    log "advisor fallback: OK ($FALLBACK_URL_NOW)"
else
    DRIFT=1
    log "advisor fallback: DRIFT - sealed '${FALLBACK_URL_NOW:-<unset>}' != shared plane $SHARED_PLANE"
fi

# ------------------------------------------------------------------ alerts
PROVIDER_NOW="$(effective_value "$CYCLE_ENV" AERO_BOT_ALERT_PROVIDER)"
RESEND_KEY="$(effective_value "$CYCLE_ENV" AERO_BOT_ALERT_RESEND_API_KEY)"
ALERT_FROM="$(effective_value "$CYCLE_ENV" AERO_BOT_ALERT_FROM)"
ALERT_TO="$(effective_value "$CYCLE_ENV" AERO_BOT_ALERT_TO)"
if [[ "$PROVIDER_NOW" == "resend" ]]; then
    log "alert provider: OK (resend)"
elif [[ -n "$PROVIDER_NOW" && "$PROVIDER_NOW" != "none" ]]; then
    log "alert provider: MANUAL - provider '$PROVIDER_NOW' is neither none nor resend; left untouched"
else
    if [[ -n "$RESEND_KEY" && -n "$ALERT_FROM" && -n "$ALERT_TO" ]]; then
        DRIFT=1
        log "alert provider: DRIFT - sealed '${PROVIDER_NOW:-none}' while Resend key, FROM, and TO are all sealed"
    else
        log "alert provider: HOLD - provider '${PROVIDER_NOW:-none}' with incomplete Resend credentials (key/FROM/TO); nothing to enable"
    fi
fi

if [[ "$MODE" == "--check" ]]; then
    if [[ $DRIFT -eq 1 ]]; then
        log "CHECK: drift found; run with --apply to repair (see docs/deployment.md)"
        exit 3
    fi
    log "CHECK: no drift"
    exit 0
fi

# ------------------------------------------------------------------- apply
if [[ -n "$ADVISOR_URL_NOW" && "$ADVISOR_URL_NOW" == "$DEDICATED_PLANE" \
    && -n "$FALLBACK_URL_NOW" && "$FALLBACK_URL_NOW" == "$SHARED_PLANE" ]]; then
    log "advisor seal already converged; no restart"
else
    rewrite_seal "$ADVISOR_ENV"
    set_env_var "$ADVISOR_ENV" AERO_BOT_ADVISOR_URL "$DEDICATED_PLANE"
    set_env_var "$ADVISOR_ENV" AERO_BOT_ADVISOR_FALLBACK_URL "$SHARED_PLANE"
    ADVISOR_CHANGED=1
    log "advisor seal repointed: primary $DEDICATED_PLANE, fallback $SHARED_PLANE"
fi

if [[ "$PROVIDER_NOW" == "resend" ]]; then
    log "alert provider already resend; no change"
elif [[ -n "$PROVIDER_NOW" && "$PROVIDER_NOW" != "none" ]]; then
    REFUSED=1
    log "REFUSED: alert provider '$PROVIDER_NOW' is neither none nor resend; a manual decision is required"
elif [[ -z "$RESEND_KEY" || -z "$ALERT_FROM" || -z "$ALERT_TO" ]]; then
    REFUSED=1
    log "REFUSED: cannot enable resend - key, FROM, and TO must all be sealed in $CYCLE_ENV first"
else
    rewrite_seal "$CYCLE_ENV"
    set_env_var "$CYCLE_ENV" AERO_BOT_ALERT_PROVIDER "resend"
    log "alert provider enabled: resend (credentials already sealed)"
fi

if [[ $ADVISOR_CHANGED -eq 1 ]]; then
    # try-restart restarts only an already-active unit; every timer-driven
    # pass also reads the repaired seal at its next start regardless.
    "$SYSTEMCTL" try-restart "$ADVISOR_UNIT"
    log "restarted $ADVISOR_UNIT (only if it was active)"
fi

if [[ $REFUSED -eq 1 ]]; then
    log "APPLY: repairs above landed, but at least one was refused; see the REFUSED lines"
    exit 4
fi
log "APPLY: converged"
exit 0
