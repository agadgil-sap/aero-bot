#!/usr/bin/env bash
# Run one aero-bot-teacher stream, the daily hindsight scoring pass, or the
# daily upgrade-proposal pass, from a launchd user agent.
#
# The harness repository resolves from AERO_BOT_TEACHER_REPO first (the
# launchd kit points this at a worktree outside macOS's TCC-protected
# folders, because launchd agents cannot read ~/Documents), then from this
# script's own location. The wrapper prefers the user's uv on PATH and
# appends each run's output to the harness state directory so launchd's own
# logs stay small.
set -euo pipefail

REPO_ROOT="${AERO_BOT_TEACHER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STREAM="${1:?usage: teacher-run.sh <tactical|daily|news|hindsight|upgrade>}"
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
    # it is not a teacher stream either.
    exec "$UV_BIN" run aero-bot-upgrade >>"$LOG_DIR/${STREAM}.log" 2>&1
fi
exec "$UV_BIN" run aero-bot-teacher "$STREAM" >>"$LOG_DIR/${STREAM}.log" 2>&1
