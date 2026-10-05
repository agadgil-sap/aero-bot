#!/usr/bin/env bash
# Run one aero-bot-teacher stream, the daily hindsight scoring pass, the
# daily upgrade-proposal pass, or the daily risk-manager audit, from a
# launchd user agent.
#
# The harness repository resolves from AERO_BOT_TEACHER_REPO first (the
# launchd kit points this at a worktree outside macOS's TCC-protected
# folders, because launchd agents cannot read ~/Documents), then from this
# script's own location. The wrapper prefers the user's uv on PATH and
# appends each run's output to the harness state directory so launchd's own
# logs stay small.
set -euo pipefail

REPO_ROOT="${AERO_BOT_TEACHER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STREAM="${1:?usage: teacher-run.sh <tactical|daily|news|hindsight|upgrade|risk-manager|publish-advice>}"
STATE_DIR="${AERO_BOT_TEACHER_STATE_DIR:-$HOME/.local/state/aero-bot/teacher}"
LOG_DIR="$STATE_DIR/logs"

mkdir -p "$LOG_DIR"

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi
if [[ -z "$UV_BIN" ]]; then
    echo "teacher-run: uv not found on PATH or at ~/.local/bin/uv" \
        >>"$LOG_DIR/${STREAM}.log" 2>&1
    exit 1
fi

cd "$REPO_ROOT"
if [[ "$STREAM" == "hindsight" ]]; then
    # The scorer replays the corpus offline; it is not a teacher stream.
    exec "$UV_BIN" run aero-bot-hindsight >>"$LOG_DIR/${STREAM}.log" 2>&1
fi
if [[ "$STREAM" == "upgrade" ]]; then
    # The upgrade loop proposes teaching blocks from scored divergence;
    # it is not a teacher stream either. The pass also emails its digest
    # through the sealed alert transport when the operator has sealed the
    # overlay below (the existing Resend configuration reused, never a
    # new provider; owner-only delivery until a sending domain is
    # verified - see docs/teacher.md). --email only warns when the
    # overlay is absent, so the pass stays clean while unconfigured.
    EMAIL_ENV="$STATE_DIR/upgrade-email.env"
    if [[ -f "$EMAIL_ENV" ]]; then
        # A key-bearing overlay must be owner-only: any group or other
        # access bit refuses the pass (stat spells differ between macOS
        # and Linux, so both are tried before giving up open).
        EMAIL_MODE="$(stat -f '%Lp' "$EMAIL_ENV" 2>/dev/null \
            || stat -c '%a' "$EMAIL_ENV" 2>/dev/null || true)"
        if [[ "$EMAIL_MODE" =~ ^[0-7]+$ ]] && (( (8#$EMAIL_MODE & 8#007) != 0 )); then
            echo "teacher-run: refusing group/world-readable $EMAIL_ENV (mode $EMAIL_MODE); chmod 600 it" \
                >>"$LOG_DIR/${STREAM}.log" 2>&1
            exit 1
        fi
        set -a
        # shellcheck disable=SC1090
        . "$EMAIL_ENV"
        set +a
    fi
    exec "$UV_BIN" run aero-bot-upgrade --email >>"$LOG_DIR/${STREAM}.log" 2>&1
fi
if [[ "$STREAM" == "risk-manager" ]]; then
    # The risk manager audits posture as the counterparty desk; it is not
    # a teacher stream either.
    exec "$UV_BIN" run aero-bot-risk-manager >>"$LOG_DIR/${STREAM}.log" 2>&1
fi
if [[ "$STREAM" == "publish-advice" ]]; then
    # The publisher writes one bounded advice artifact to the production
    # box for the daily digest (firstmate 028's minimal one-way transfer);
    # it is not a teacher stream either. A failed publish only logs - the
    # digest states the honest missing/malformed/stale marker instead.
    exec "$UV_BIN" run aero-bot-teacher-publish >>"$LOG_DIR/${STREAM}.log" 2>&1
fi
exec "$UV_BIN" run aero-bot-teacher "$STREAM" >>"$LOG_DIR/${STREAM}.log" 2>&1
