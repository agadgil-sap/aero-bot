# The shadow advisor

The `aero-bot-advisor` command is the intelligence layer's first surface: a bounded, fail-closed observer that reads the bot's own audited history and asks one language model - served from the operator's private inference plane - for a prose brief and anomaly flags.
It is advisory in the strictest sense: it sends nothing, signs nothing, and never writes the cycle book; the report is the entire effect, and trading authority stays with the locked policy engine exactly as before.

```
uv run aero-bot-advisor [--max-runs N] [--interval-seconds S]
```

Exit codes: zero on a clean pass (an unconfigured advisor is dark, not failed), one on configuration or store failures.

## Sealed environment

The surface is dark until the operator seals both values, normally in `/etc/aero-bot/advisor.env` (the installer ships a commented template, never overwriting a sealed file):

| Variable | Meaning |
| --- | --- |
| `AERO_BOT_ADVISOR_URL` | The inference plane's base URL; the client posts to `{url}/api/chat` (Ollama's native protocol). Empty keeps the surface dark. |
| `AERO_BOT_ADVISOR_FALLBACK_URL` | A second plane's base URL, consulted exactly once when the primary is unreachable; empty keeps the single-endpoint posture (see [the student plane](#the-student-plane)). |
| `AERO_BOT_ADVISOR_MODEL` | The model the plane serves for this surface; empty keeps the surface dark. |
| `AERO_BOT_ADVISOR_TIMEOUT_SECONDS` | The bounded wall-clock request timeout (default 15, range 1-120). Measured warm brief latency is 2.1-3.8 s and a cold load 3.3 s page-cached, so a sealed plane deployment sets 90 - a >20x warm margin that fully covers a post-reboot cold window. |
| `AERO_BOT_ADVISOR_MAX_TOKENS` | The bounded generation budget (default 4096, range 200-32768). |
| `AERO_BOT_ADVISOR_DISABLE_THINKING` | `1` (the default) asks reasoning models not to think through Ollama's native `think` switch; `0` re-enables deliberation with eyes open. |
| `AERO_BOT_ADVISOR_JSON_MODE` | `1` (the default) constrains generation to valid JSON through Ollama's native `format` field so `malformed_json` absences are eliminated at the source; `0` leaves generation free. |
| `AERO_BOT_ADVISOR_TEACHING_FILE` | Optional sealed teaching block (an absolute path, normally `/etc/aero-bot/advisor-teaching.txt`); unset keeps the in-repo system prompt alone. See [the teaching block](#the-teaching-block). |
| `AERO_BOT_ADVISOR_REPORT_PATH` | Overrides the persisted report path (default `advisor_last_report.json` beside the audit store). |

The Mac-hosted plane (see the project's inference-plane notes) binds tailnet-only, so the URL is a `100.x.y.z` address only the tailnet can reach; the VM reaches the dedicated student plane as `http://100.106.111.37:11435` and the shared instance as `http://100.106.111.37:11434` once it joins the tailnet.

## The student plane

The seat's availability hardened into a dedicated serving architecture, after the measured diagnosis: the shared instance's default five-minute keep-alive evicted the ~22 GB model after every thirty-minute window, and the OpenAI-compatible transport could neither pin the model nor suppress its thinking - every window paid a cold reload plus an invisible ~2.4k-token reasoning chain (observed chains to 9.7k tokens), which is what produced the timeout, `empty_content`, and `malformed_json` absences.

**The native protocol.** The transport speaks Ollama's native `/api/chat`, not the OpenAI-compatible `/v1/chat/completions`, because the native surface is the only one that expresses the two levers this seat needs (both verified live against Ollama 0.34.3):

- `keep_alive: -1` rides every primary request - the plane never evicts what this seat loads. The OpenAI-compatible surface ignores the field entirely (both numeric and string body values leave the five-minute default eviction untouched).
- `think: false` caps reasoning at zero - the anomaly brief does not need deliberation. The OpenAI-compatible surface silently ignores it too, leaving the full reasoning chain in the latency (measured: the identical request answers in ~4 s natively versus ~42-62 s through the compatibility layer).
- `format: "json"` constrains generation to valid JSON, eliminating `malformed_json` absences at the source; the strict schema gate downstream stays as the shape check, so malformed output under JSON mode remains a typed parse-failure absence, never a guess.

**The dedicated instance.** A second local Ollama server on the Mac serves only the student seat, so no other consumer (web tools, bake-offs, fleet work) can evict or starve it. It binds the Mac's tailnet address on a distinct port (`100.106.111.37:11435`) with `OLLAMA_KEEP_ALIVE=-1` (the pin in server environment - belt and braces with the per-request field) and `OLLAMA_NUM_PARALLEL=1` (one inference slot; no parallel burst can balloon memory). The Mac kit ships it as the `com.aero-bot.student-ollama` launchd agent (see [the teacher documentation](teacher.md#deployment-on-the-mac)): RunAtLoad plus KeepAlive, never loaded by the installer, its bind baked into the agent so a Mac reboot restores the plane without the GUI instance's fragile runtime `OLLAMA_HOST` state. It shares the user's models directory with the GUI instance - content-addressed read-only blobs, stored once - and its wrapper prefers the Ollama.app bundle's server binary, because older standalone builds break JSON mode with thinking off (verified live: Homebrew 0.32.15 answered empty content where the app's 0.34.3 answered correctly).

**The fallback.** `AERO_BOT_ADVISOR_FALLBACK_URL` seals the shared instance behind the dedicated plane. When the dedicated plane is unreachable the seat retries exactly once against the fallback - the same payload, the same model, so a fallback window can never serve a silent wrong-model call - minus the residency pin, so one answered window never permanently claims the shared instance's VRAM. A timeout does not retry (the bounded clock is already spent); both planes unreachable stays the typed `unreachable` absence.

**Measured latencies** (qwen3.6:35b-a3b, native protocol, thinking off, JSON mode on, three replayed real windows): warm 2.1 s, 3.4 s, 3.8 s; cold load 3.3 s page-cached (NVMe-bounded single digits after a reboot); pin verified - the model stays resident with a far-future expiry and zero evictions across windows. One operational caution the measurements surfaced: two Ollama servers must not hold the same ~22 GB model at once (44 GB into a 37.4 GB GPU wedges Metal into command-buffer failures that surface as `http_status` error envelopes), which is exactly why the student model's home is the dedicated plane alone and the fallback leaves the shared instance's eviction policy untouched.

## One pass

1. **Compose the facts.** The monitor reads the most recent audit records (default window 40), filters to `cycle_reported` summaries, and joins the cycle book: the tracked position and its committed value, the persisted out-of-range wait, the latest report's day economics and fee evidence, halt and action counts, the distinct decision reasons, and re-entry cooldowns.
Everything in the prompt is deterministic and grounded - the composed facts are audited or read from the self-healing book, never scraped or guessed.
2. **Request one bounded answer.** The prompt asks for exactly one JSON object - a brief of at most three sentences and at most ten anomaly flags, each with a bounded confidence and one-line rationale - at temperature zero, with generation constrained to valid JSON and the residency pin riding the request (see [the student plane](#the-student-plane)).
3. **Validate fail-closed.** The answer must parse and validate against the strict schema; a reasoning model's separate `reasoning` field is ignored, an error envelope answered under a 200 status is a plane failure (`http_status`), and the token budget must cover thinking plus answer (a length-cutoff plane answering empty content is a typed absence, not a hang).
4. **Record.** The pass prints a human summary, atomically rewrites `advisor_last_report.json` beside the audit store, and appends one `advisor_reported` audit record carrying the accepted brief or the stable absence reason, the model, the latency, and the window size.

## The teaching block

The student learns to be taught through one optional sealed file, proposed by the teacher harness's upgrade loop and written by the operator (the checklist lives in [the teacher documentation](teacher.md#the-upgrade-loop)).
When `AERO_BOT_ADVISOR_TEACHING_FILE` is set, every pass reads the file and appends its content to the in-repo system prompt behind a fixed separator - **append, never replace**, so the JSON answer contract inlined in the default prompt can never be dropped by a bad block.
Validation fails closed: the path must be absolute (checked at configuration load), and the file must exist, decode as UTF-8, be non-empty after stripping, and stay within 4000 characters; any violation fails the pass with exit one naming the variable, never a silent fallback to the untaught prompt.
The block replaces any previously sealed block wholesale (the proposals are written as complete stand-alone blocks), and commenting the variable back out un-teaches on the next pass - no restart, since the file is read fresh every 30-minute run.

## The absence catalog

Every way an answer can be missing is a stable typed reason, never an exception into a caller and never a guess:

- `dark` - no URL or model sealed; the surface never touched the network.
- `unreachable` / `timeout` - the plane could not be reached (after the one sealed fallback retry) or exceeded the bounded timeout.
- `http_status` - the plane answered a non-200 status, or answered an error envelope under a 200 (an internal plane failure such as a Metal compute error under memory pressure).
- `empty_content` / `malformed_json` / `schema_invalid` - the answer was absent, unparseable, or violated the bounded schema.

A missing brief is audited exactly like a present one: the record's `outcome` field carries the reason, so an offline plane leaves an honest trail rather than a silent gap.

## Deployment

The kit ships `aero-bot-advisor.service` (a hardened oneshot gated by `ConditionPathExists=/etc/aero-bot/advisor.env`) and `aero-bot-advisor.timer` (one pass every thirty minutes, `Persistent=true` for catch-up).
The installer installs both dark, like every unit: arming is the deploy operator's Phase 2 act, and the command inside stays dark until the plane values are uncommented and sealed.
Because the surface reads only the audit chain and the book and writes one report file and one audit record, it needs no extra permissions beyond the kit's standard `ReadWritePaths=/var/lib/aero-bot`.

## Manual runs and the bake-off

A one-shot operator run works from the checkout with the same environment:

```
AERO_BOT_ADVISOR_URL=http://100.106.111.37:11435 \
AERO_BOT_ADVISOR_FALLBACK_URL=http://100.106.111.37:11434 \
AERO_BOT_ADVISOR_MODEL=qwen3.6:35b-a3b \
uv run aero-bot-advisor --max-runs 1
```

The sibling harness `tools/bakeoff.py` exercises candidate models over committed scenario fixtures and scores them against the deterministic baseline; its reports land under `run/bakeoff/` and inform which model deserves the sealed `AERO_BOT_ADVISOR_MODEL` slot.
The bake-off never touches live state: it replays committed fixtures only.
