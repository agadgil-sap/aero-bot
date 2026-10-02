#!/usr/bin/env bash
# Advance the teacher harness worktree to the engine the audit trail came
# from, so the teachers' vocabulary never drifts days behind the deployed
# VM (the 2026-10-01 scout measured the teacher repo ten commits behind
# main and the deployed engine because nothing ever advanced it).
#
# Target order (docs/teacher.md, "The repo sync agent"):
#   1. the DEPLOYED_COMMIT stamped on the production VM, fetched read-only
#      over the established gcloud ssh path - the teachers grade the
#      engine that actually produced the audit trail;
#   2. origin/main when the VM is unreachable - the drift kills slowly,
#      so a converged-to-main day beats a skipped day.
#
# Guards: the worktree must be clean (nothing is force-reset), the target
# must exist locally after the fetch, and the script never creates,
# removes, or re-adds the worktree itself - that stays install-mac.sh's
# explicit operator-run act.
set -euo pipefail

STATE_DIR="${AERO_BOT_TEACHER_STATE_DIR:-$HOME/.local/state/aero-bot/teacher}"
REPO_ROOT="${AERO_BOT_TEACHER_REPO:-$STATE_DIR/repo}"
LOG_DIR="$STATE_DIR/logs"
SSH_CMD="${AERO_BOT_TEACHER_SSH:-gcloud compute ssh aero-bot --zone us-west1-b --quiet}"

mkdir -p "$LOG_DIR"
log() { printf '[teacher-sync] %s\n' "$*" >>"$LOG_DIR/teacher-sync.log"; }

if [[ ! -d "$REPO_ROOT/.git" && ! -f "$REPO_ROOT/.git" ]]; then
    log "FATAL: $REPO_ROOT is not a git worktree; run install-mac.sh first"
    exit 1
fi

# A dirty worktree means someone left state inside it; the sync refuses
# rather than discarding anything.
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    log "FATAL: $REPO_ROOT is dirty; refusing to move it (clean the tree by hand)"
    exit 1
fi

BEFORE="$(git -C "$REPO_ROOT" rev-parse HEAD)"

log "fetching origin"
if ! git -C "$REPO_ROOT" fetch origin --quiet >>"$LOG_DIR/teacher-sync.log" 2>&1; then
    log "fetch failed; keeping $BEFORE"
    exit 1
fi

# The deployed SHA, read-only from the VM; any failure falls back to
# origin/main rather than skipping the day.
TARGET=""
if DEPLOYED="$("$SSH_CMD" --command='cat /opt/aero-bot/DEPLOYED_COMMIT' 2>/dev/null | tail -n 1)" \
    && [[ "$DEPLOYED" =~ ^[0-9a-f]{7,40}$ ]] \
    && git -C "$REPO_ROOT" cat-file -e "${DEPLOYED}^{commit}" 2>/dev/null; then
    TARGET="$DEPLOYED"
else
    log "deployed SHA unreadable or absent after fetch; falling back to origin/main"
    TARGET="$(git -C "$REPO_ROOT" rev-parse origin/main)"
fi

if [[ "$TARGET" == "$BEFORE" ]]; then
    log "already converged at $BEFORE"
    exit 0
fi

git -C "$REPO_ROOT" checkout --detach --quiet "$TARGET"
log "advanced $BEFORE -> $TARGET"
exit 0
