#!/usr/bin/env bash
# Serve the student seat's dedicated Ollama plane on macOS.
#
# The shadow advisor (the student seat, running on the production VM) asks
# one local model for its anomaly briefs every thirty minutes. Serving that
# seat from the Mac's shared Ollama instance cost availability two ways:
# the default five-minute keep-alive evicted the ~22 GB model after every
# window (each pass paid a full cold reload before inference), and any
# other consumer of the shared instance (web tools, bake-offs, fleet work)
# could evict or starve it under memory pressure. This wrapper therefore
# runs a SECOND Ollama server, bound to a distinct port, serving only the
# student seat, with the model pinned resident and a single inference slot:
#
#   OLLAMA_KEEP_ALIVE=-1   - nothing this server loads is ever evicted;
#                            per-request keep_alive is NOT expressible
#                            through the OpenAI-compatible chat-completions
#                            surface the advisor speaks (verified live:
#                            both numeric and string body values are
#                            ignored), so the pin must live here.
#   OLLAMA_NUM_PARALLEL=1  - one inference slot; no parallel bursts can
#                            balloon memory or starve the seat's latency.
#
# The server shares the user's default models directory with the GUI
# instance (content-addressed read-only blobs), so the student model is
# stored once. The bind defaults to the Mac's tailnet address - the same
# tailnet-only posture the shared plane uses - with a distinct port, so
# the production VM can reach the dedicated plane and nothing on the LAN
# can. Unlike the GUI instance's runtime OLLAMA_HOST state (which a Mac
# reboot wipes), this wrapper's environment is baked into its launchd
# agent, so the dedicated plane comes back correct on every boot.
#
# The binary prefers the Ollama.app bundle's server over a Homebrew CLI:
# older CLI builds break the advisor's JSON mode with thinking disabled
# (verified live: Homebrew 0.32.15 returned empty content under
# response_format json_object plus think:false, while the app's 0.34.3
# served both correctly).
set -euo pipefail

# The bind address: the Mac's tailnet address by default, overridable by
# the generated launchd agent (AERO_BOT_STUDENT_OLLAMA_HOST stamped at
# install time) or by hand for a loopback probe.
STUDENT_HOST="${AERO_BOT_STUDENT_OLLAMA_HOST:-100.106.111.37:11435}"

# Resolve the server binary: the app bundle first, PATH second.
OLLAMA_BIN=""
if [[ -x "/Applications/Ollama.app/Contents/Resources/ollama" ]]; then
    OLLAMA_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
elif command -v ollama >/dev/null 2>&1; then
    OLLAMA_BIN="$(command -v ollama)"
else
    echo "student-ollama.sh: no ollama server binary found (install the Ollama app)" >&2
    exit 1
fi

export OLLAMA_HOST="$STUDENT_HOST"
export OLLAMA_KEEP_ALIVE=-1
export OLLAMA_NUM_PARALLEL=1

exec "$OLLAMA_BIN" serve
