#!/usr/bin/env bash
# Generate the aero-bot teacher harness launchd user agents on macOS.
#
# Mirrors the Ubuntu kit's posture: this installer writes the four job
# definitions (three teacher streams plus the daily hindsight scorer) into
# ~/Library/LaunchAgents and NEVER loads any of them.
# Arming a stream is the operator's explicit Phase 2 act (the launchctl
# bootstrap lines it prints); the harness itself is advisory-only and owns
# no trading authority of any kind.
#
# macOS constraint this kit works around: launchd user agents cannot read
# the TCC-protected folders (~/Documents, ~/Desktop, ~/Downloads), and a
# checkout living there is unreadable to them - executing a wrapper from
# such a checkout fails with "Operation not permitted". The kit therefore
# maintains a stateless git worktree at $STATE_DIR/repo (recreated from the
# checkout's HEAD on every install; all harness state lives beside it under
# $STATE_DIR, never inside the worktree) and copies the wrapper into
# $STATE_DIR/bin, so everything the agents touch lives outside TCC scope.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WRAPPER_SRC="$REPO_ROOT/deploy/launchd/teacher-run.sh"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
STATE_DIR="${AERO_BOT_TEACHER_STATE_DIR:-$HOME/.local/state/aero-bot/teacher}"
WORKTREE="${AERO_BOT_TEACHER_REPO:-$STATE_DIR/repo}"
STATE_BIN="$STATE_DIR/bin"
STATE_WRAPPER="$STATE_BIN/teacher-run.sh"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "install-mac.sh targets macOS; this host runs $(uname)" >&2
    exit 1
fi
if [[ ! -f "$WRAPPER_SRC" ]]; then
    echo "install-mac.sh: missing $WRAPPER_SRC" >&2
    exit 1
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$STATE_DIR/logs" "$STATE_BIN"

# Never rip the worktree out from under a live pass: a run holds the venv
# and lazily imported modules for minutes, so reinstalling mid-run breaks it.
if pgrep -f "aero-bot-(teacher|hindsight)" >/dev/null 2>&1; then
    echo "install-mac.sh: a teacher pass is running; wait for it to finish before reinstalling." >&2
    exit 1
fi

# Refresh the teacher worktree from the checkout's current HEAD. It carries
# no state of its own (the corpus, scratch, and logs all live directly under
# $STATE_DIR), so removing and re-adding is always safe and always leaves
# the agents running exactly the code the operator just installed. A path
# that exists but is not one of this repo's worktrees is refused, not
# removed: the installer manages only what it created.
if [[ -e "$WORKTREE" && ! -f "$WORKTREE/.git" ]]; then
    echo "install-mac.sh: refusing to touch $WORKTREE - not a worktree this installer manages" >&2
    exit 1
fi
git -C "$REPO_ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
git -C "$REPO_ROOT" worktree add --detach --quiet "$WORKTREE" HEAD

cp "$WRAPPER_SRC" "$STATE_WRAPPER"
chmod 755 "$STATE_WRAPPER"

# Escape XML metacharacters so an unusual configured path cannot corrupt
# the generated plist.
xml_escape() {
    local value="$1"
    value="${value//&/&amp;}"
    value="${value//</&lt;}"
    value="${value//>/&gt;}"
    printf '%s' "$value"
}

# One job per stream: tactical every thirty minutes, daily review after the
# box's 09:00 Melbourne morning report, news before the trading day starts,
# and the hindsight scorer after the daily stream's bounded timeout has
# drained. All four stay unloaded until the operator bootstraps them. The
# baked PATH carries ~/.local/bin and Homebrew because launchd's bare PATH
# has neither, and the teacher CLIs (claude, codex, uv) live there.
generate_plist() {
    local label="$1"
    local stream="$2"
    local calendar_block="$3"
    local wrapper log_dir worktree home path_value
    wrapper="$(xml_escape "$STATE_WRAPPER")"
    log_dir="$(xml_escape "$STATE_DIR/logs")"
    worktree="$(xml_escape "$WORKTREE")"
    home="$(xml_escape "$HOME")"
    path_value="$home/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    cat >"$LAUNCH_AGENTS_DIR/${label}.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${wrapper}</string>
        <string>${stream}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${path_value}</string>
        <key>AERO_BOT_TEACHER_REPO</key>
        <string>${worktree}</string>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
${calendar_block}
    <key>StandardOutPath</key>
    <string>${log_dir}/${stream}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${log_dir}/${stream}-launchd.log</string>
</dict>
</plist>
EOF
}

INTERVAL_BLOCK='    <key>StartInterval</key>
    <integer>1800</integer>'

DAILY_BLOCK='    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>'

NEWS_BLOCK='    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>7</integer>
        <key>Minute</key>
        <integer>10</integer>
    </dict>'

HINDSIGHT_BLOCK='    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>50</integer>
    </dict>'

generate_plist "com.aero-bot.teacher-tactical" "tactical" "$INTERVAL_BLOCK"
generate_plist "com.aero-bot.teacher-daily" "daily" "$DAILY_BLOCK"
generate_plist "com.aero-bot.teacher-news" "news" "$NEWS_BLOCK"
generate_plist "com.aero-bot.teacher-hindsight" "hindsight" "$HINDSIGHT_BLOCK"

echo "Refreshed the teacher worktree at $WORKTREE (detached at HEAD, stateless)."
echo "Generated teacher launchd agents (none loaded):"
ls -1 "$LAUNCH_AGENTS_DIR"/com.aero-bot.teacher-*.plist
echo
echo "Arm a stream by loading its agent, for example:"
echo "  launchctl bootstrap gui/$(id -u) $LAUNCH_AGENTS_DIR/com.aero-bot.teacher-tactical.plist"
echo "  launchctl kickstart gui/$(id -u)/com.aero-bot.teacher-tactical"
echo
echo "The harness is advisory-only and ships no trading authority; see docs/teacher.md."
