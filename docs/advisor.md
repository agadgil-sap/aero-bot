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
| `AERO_BOT_ADVISOR_URL` | The inference plane's base URL; the client posts to `{url}/v1/chat/completions`. Empty keeps the surface dark. |
| `AERO_BOT_ADVISOR_MODEL` | The model the plane serves for this surface; empty keeps the surface dark. |
| `AERO_BOT_ADVISOR_TIMEOUT_SECONDS` | The bounded wall-clock request timeout (default 15, range 1-120). A cold reasoning-model pass thinks for tens of seconds - one live 35B-A3B brief measured 55 s - so a sealed plane deployment sets 120. |
| `AERO_BOT_ADVISOR_MAX_TOKENS` | The bounded generation budget (default 4096, range 200-32768). |
| `AERO_BOT_ADVISOR_DISABLE_THINKING` | `1` asks reasoning models not to think (Ollama's `think` parameter); default off. |
| `AERO_BOT_ADVISOR_TEACHING_FILE` | Optional sealed teaching block (an absolute path, normally `/etc/aero-bot/advisor-teaching.txt`); unset keeps the in-repo system prompt alone. See [the teaching block](#the-teaching-block). |
| `AERO_BOT_ADVISOR_REPORT_PATH` | Overrides the persisted report path (default `advisor_last_report.json` beside the audit store). |

The Mac-hosted plane (see the project's inference-plane notes) binds tailnet-only, so the URL is a `100.x.y.z` address only the tailnet can reach; the VM reaches it as `http://100.106.111.37:11434` once it joins the tailnet.

## One pass

1. **Compose the facts.** The monitor reads the most recent audit records (default window 40), filters to `cycle_reported` summaries, and joins the cycle book: the tracked position and its committed value, the persisted out-of-range wait, the latest report's day economics and fee evidence, halt and action counts, the distinct decision reasons, and re-entry cooldowns.
Everything in the prompt is deterministic and grounded - the composed facts are audited or read from the self-healing book, never scraped or guessed.
2. **Request one bounded answer.** The prompt asks for exactly one JSON object - a brief of at most three sentences, at most ten anomaly flags each with a bounded confidence and one-line rationale, and (when the facts show a tracked position) a stated position view - at temperature zero.
3. **Validate fail-closed.** The answer must parse and validate against the strict schema; a reasoning model's separate `reasoning` field is ignored, and the token budget must cover thinking plus answer (a length-cutoff plane answering empty content is a typed absence, not a hang).
4. **Record.** The pass prints a human summary, atomically rewrites `advisor_last_report.json` beside the audit store, and appends one `advisor_reported` audit record carrying the accepted brief or the stable absence reason, the model, the latency, and the window size.

## The position view

The conviction layer (the captain's answer to desks that describe state and defer) extends the shared brief schema backward-compatibly: every desk - the student here, the teacher seats in the harness - may carry a position view beside its prose.
When the composed facts show a tracked position the view is required: either a verdict (hold, exit, recenter, or enter), a coarse confidence band (low, medium, high), and a one-sentence reason of at most 400 characters tied to the locked policy's own rules or naming the deviation from them - or an explicit `view_declined` saying why no view can be formed.
A desk that cannot form a view says so; it never defaults a missing view to hold.
While the book is flat the view is optional (an enter view is the coherent form).
The fields are optional in the schema itself so every brief already recorded in the audit chain or the teacher corpus keeps parsing; older briefs over a tracked position surface as honest view gaps in the hindsight scorer's measurement, never as parse failures.
The accepted view rides the `advisor_reported` audit record beside the brief, so the corpus and every offline surface see the same stated conviction.
Advisory only, like everything here: a view is a measurement input, not an instruction - the locked policy engine keeps every trading decision, and nothing in this layer can authorize, block, or execute anything.

## The teaching block

The student learns to be taught through one optional sealed file, proposed by the teacher harness's upgrade loop and written by the operator (the checklist lives in [the teacher documentation](teacher.md#the-upgrade-loop)).
When `AERO_BOT_ADVISOR_TEACHING_FILE` is set, every pass reads the file and appends its content to the in-repo system prompt behind a fixed separator - **append, never replace**, so the JSON answer contract inlined in the default prompt can never be dropped by a bad block.
Validation fails closed: the path must be absolute (checked at configuration load), and the file must exist, decode as UTF-8, be non-empty after stripping, and stay within 4000 characters; any violation fails the pass with exit one naming the variable, never a silent fallback to the untaught prompt.
The block replaces any previously sealed block wholesale (the proposals are written as complete stand-alone blocks), and commenting the variable back out un-teaches on the next pass - no restart, since the file is read fresh every 30-minute run.

## The absence catalog

Every way an answer can be missing is a stable typed reason, never an exception into a caller and never a guess:

- `dark` - no URL or model sealed; the surface never touched the network.
- `unreachable` / `timeout` - the plane could not be reached or exceeded the bounded timeout.
- `http_status` - the plane answered a non-200 status.
- `empty_content` / `malformed_json` / `schema_invalid` - the answer was absent, unparseable, or violated the bounded schema.

A missing brief is audited exactly like a present one: the record's `outcome` field carries the reason, so an offline plane leaves an honest trail rather than a silent gap.

## Deployment

The kit ships `aero-bot-advisor.service` (a hardened oneshot gated by `ConditionPathExists=/etc/aero-bot/advisor.env`) and `aero-bot-advisor.timer` (one pass every thirty minutes, `Persistent=true` for catch-up).
The installer installs both dark, like every unit: arming is the deploy operator's Phase 2 act, and the command inside stays dark until the plane values are uncommented and sealed.
Because the surface reads only the audit chain and the book and writes one report file and one audit record, it needs no extra permissions beyond the kit's standard `ReadWritePaths=/var/lib/aero-bot`.

## Manual runs and the bake-off

A one-shot operator run works from the checkout with the same environment:

```
AERO_BOT_ADVISOR_URL=http://100.106.111.37:11434 \
AERO_BOT_ADVISOR_MODEL=qwen3.6:35b-a3b \
uv run aero-bot-advisor --max-runs 1
```

The sibling harness `tools/bakeoff.py` exercises candidate models over committed scenario fixtures and scores them against the deterministic baseline; its reports land under `run/bakeoff/` and inform which model deserves the sealed `AERO_BOT_ADVISOR_MODEL` slot.
The bake-off never touches live state: it replays committed fixtures only.
