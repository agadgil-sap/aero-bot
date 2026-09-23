#!/usr/bin/env bash
# Generate the aero-bot teacher harness launchd user agents on macOS.
#
# Mirrors the Ubuntu kit's posture: this installer writes the three job
# definitions into ~/Library/LaunchAgents and NEVER loads any of them.
# Arming a stream is the operator's explicit Phase 2 act (the launchctl
# bootstrap lines it prints); the harness itself is advisory-only and owns
# no trading authority of any kind.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WRAPPER="$REPO_ROOT/deploy/launchd/teacher-run.sh"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
STATE_DIR="${AERO_BOT_TEACHER_STATE_DIR:-$HOME/.local/state/aero-bot/teacher}"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "install-mac.sh targets macOS; this host runs $(uname)" >&2
    exit 1
fi
if [[ ! -x "$WRAPPER" ]]; then
    chmod +x "$WRAPPER"
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$STATE_DIR/logs"

# One job per stream: tactical every thirty minutes, daily review after the
# box's 09:00 Melbourne morning report, news before the trading day starts.
# All three stay unloaded until the operator bootstraps them.
generate_plist() {
    local label="$1"
    local stream="$2"
    local calendar_block="$3"
    cat >"$LAUNCH_AGENTS_DIR/${label}.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${WRAPPER}</string>
        <string>${stream}</string>
    </array>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
${calendar_block}
    <key>StandardOutPath</key>
    <string>${STATE_DIR}/logs/${stream}-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${STATE_DIR}/logs/${stream}-launchd.log</string>
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

generate_plist "com.aero-bot.teacher-tactical" "tactical" "$INTERVAL_BLOCK"
generate_plist "com.aero-bot.teacher-daily" "daily" "$DAILY_BLOCK"
generate_plist "com.aero-bot.teacher-news" "news" "$NEWS_BLOCK"

echo "Generated teacher launchd agents (none loaded):"
ls -1 "$LAUNCH_AGENTS_DIR"/com.aero-bot.teacher-*.plist
echo
echo "Arm a stream by loading its agent, for example:"
echo "  launchctl bootstrap gui/$(id -u) $LAUNCH_AGENTS_DIR/com.aero-bot.teacher-tactical.plist"
echo "  launchctl kickstart gui/$(id -u)/com.aero-bot.teacher-tactical"
echo
echo "The harness is advisory-only and ships no trading authority; see docs/teacher.md."
