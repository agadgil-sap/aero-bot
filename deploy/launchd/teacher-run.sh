#!/usr/bin/env bash
# Run one aero-bot-teacher stream from a launchd user agent.
#
# The script resolves the repository from its own location so the generated
# plists never hard-code a checkout path beyond this wrapper, prefers the
# user's uv on PATH, and appends each run's output to the harness state
# directory so launchd's own logs stay small.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STREAM="${1:?usage: teacher-run.sh <tactical|daily|news>}"
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
exec "$UV_BIN" run aero-bot-teacher "$STREAM" >>"$LOG_DIR/${STREAM}.log" 2>&1
