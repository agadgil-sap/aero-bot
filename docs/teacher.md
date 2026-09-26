# The teacher harness

The `aero-bot-teacher` command is the intelligence layer's second surface: the Mac-side dual-seat advisory harness that observes the bot's live audited window and writes schema-validated briefs into a local corpus.
Where the shadow advisor (see [the advisor documentation](advisor.md)) is the student - one bounded model on the private inference plane - the teachers are two premium seats the operator already pays for through coding-plan logins: the `claude` seat rides the Claude Code CLI's configured model (GLM 5.3) and the `codex` seat rides the Codex CLI pinned to GPT 6 Luna at high reasoning.
No API keys exist anywhere in the surface; both CLIs authenticate as the operator.

The harness is advisory in the same strict sense as the advisor: it pulls one read-only window from the production box, asks its question, and appends one episode to the local corpus.
It never writes anything on the box, signs nothing, and never touches the cycle book.
Teachers observe and are scored; the locked policy engine keeps every trading decision.

```
uv run aero-bot-teacher <tactical|daily|news> [--seat NAME]... [--max-runs N] [--config PATH]
```

Exit codes: zero on a clean pass (absences are honest, not failures), one on configuration or corpus failures.

## The three streams

| Stream | Cadence | Question |
| --- | --- | --- |
| `tactical` | every 30 minutes | What deserves human eyes within minutes, given the fresh snapshot and the student's latest brief? |
| `daily` | once each morning | Did the desks read the last day right, and what should change tomorrow? |
| `news` | once before the trading day | Does the outside world threaten, invalidate, or improve the locked methodology? |

The tactical prompt carries the same deterministic composed facts the student sees (audited cycle summaries joined to the book) plus the student's latest audited answer under a `student_answer` key, with an explicit staleness note: the student's brief was written earlier, so drift between it and the fresher snapshot is staleness to report, not fabrication by the student.
The daily and news prompts carry a bounded digest composed from the corpus itself - the window's episode count, day P&L samples, latest equity, halt observations counted by episode, distinct actions, per-seat outcome counts, ranked anomaly labels, and the latest brief per seat.

## The window pull

Each pass reads the production box once over `gcloud compute ssh`: a base64-encoded read-only Python script opens the audit database through SQLite's `mode=ro` URI, selects the last 40 records across every event type (the same mixed window the student composes), reads the latest `advisor_reported` record separately (so the student's answer never scrolls out of the window), reads the cycle book, and prints one JSON object.
The script itself is a fixed constant with zero interpolation; its database path, book path, and record bound ride as a second base64-encoded JSON object on the script's argv, so no configured value can ever become code inside a script that runs under `sudo` (the three path fields are additionally validated to plain absolute POSIX paths at configuration load).
The pull writes nothing on the box and needs no elevated state beyond `sudo` reading the audit files.
A failed pull is honest: the episode records `window_unreachable` for every seat and asks nothing.

## The seat invocations

Both seats run headless with the full prompt on stdin:

- `claude -p --output-format json --bare --max-turns N`, with `--allowedTools WebSearch WebFetch --dangerously-skip-permissions` added only for the news stream.
The `--max-turns 3` ceiling on toolless streams exists because a model attempting a denied tool on turn one aborts with an `error_max_turns` envelope (observed live); the ceiling tolerates the stray attempt.
The `[claude-code:unrecognized_model]` stderr line is benign noise - the CLI falls back and serves the configured model.
- `codex exec - -m gpt-6-luna -c model_reasoning_effort="high" -s read-only --skip-git-repo-check --ephemeral -o <scratch>/codex-last-message.txt`.

The codex final message is read from the `-o` file, which the harness deletes before every invocation so a stale file never masquerades as this pass's answer.
The nested CLIs inherit neither this session's project directory nor the bot's `AERO_BOT_*` configuration namespace; the operator CLI logins legitimately live in the ambient environment, so nothing else is stripped.
The shared answer contract also bounds prompt injection: every fact, digest, and prior brief is declared untrusted data whose embedded instructions are never followed.

## The absence catalog

Every way an answer can be missing is a stable typed reason recorded in the corpus, never an exception:

- `dark` - the seat is disabled in the configuration.
- `cli_missing` - the seat's CLI binary is not installed.
- `timeout` - the invocation exceeded the stream's bounded timeout (600 s tactical/daily, 900 s news).
- `cli_error` - the CLI exited non-zero or answered an error envelope; the bounded detail carries the subtype.
- `empty_content` / `malformed_json` / `schema_invalid` - the answer was absent, unparseable, or violated the bounded brief schema (the same strict schema the student answers, so every desk's briefs live in one scoreable shape - including the position view: when the pulled facts show a tracked position, every seat must state a view, a verdict with a confidence band and a policy-tied reason, or decline one explicitly; never default a missing view to hold).
- `window_unreachable` - the pull itself failed; no question was asked.

## The corpus

Episodes append as JSONL lines under `~/.local/state/aero-bot/teacher/corpus.jsonl` (one per pass, schema tag `teacher_episode/1`), each carrying the stream, the window outcome, the composed facts, the student's latest audited answer, and every seat's outcome tagged by seat and model.
The corpus is the teaching material: hindsight scoring (the next section) replays episodes against what actually followed, and the student's upgrades train on it.
Malformed lines are skipped on load, never fatal.
A per-stream `reports/<stream>_last.json` is atomically rewritten beside it for quick inspection.

## Configuration

The harness works with no configuration file at all; an optional JSON file (default `~/.config/aero-bot/teacher.json`, override with `AERO_BOT_TEACHER_CONFIG`) tunes seats, the pull target, and the digest window:

```json
{
    "seats": {"codex": {"enabled": false, "timeout_seconds": 1200}},
    "pull": {"instance": "aero-bot", "zone": "us-west1-b"},
    "digest_window_hours": 24
}
```

`AERO_BOT_TEACHER_CORPUS_DIR` overrides the corpus directory (tests and manual runs use it).
Every field fails closed: an invalid file exits one naming the path, never the content.

## Deployment on the Mac

`deploy/launchd/install-mac.sh` generates the six user agents into `~/Library/LaunchAgents` - `com.aero-bot.teacher-tactical` (StartInterval 1800), `com.aero-bot.teacher-daily` (09:30, after the box's 09:00 Melbourne morning report), `com.aero-bot.teacher-news` (07:10), `com.aero-bot.teacher-hindsight` (09:50, after the daily stream drains), `com.aero-bot.teacher-upgrade` (10:10, after the hindsight report is rewritten), and `com.aero-bot.teacher-risk-manager` (10:00, between the scorer and the proposer) - and never loads any of them, mirroring the Ubuntu kit's posture.
Arming a stream is the operator's explicit act:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aero-bot.teacher-tactical.plist
launchctl kickstart gui/$(id -u)/com.aero-bot.teacher-tactical
```

macOS constraint the kit works around: launchd user agents cannot read the TCC-protected folders (`~/Documents`, `~/Desktop`, `~/Downloads`), and a checkout living there is unreadable to them - executing a wrapper from such a checkout fails with `Operation not permitted` (verified empirically; only the Terminal's own grant covers interactive runs).
The installer therefore maintains a stateless git worktree at `~/.local/state/aero-bot/teacher/repo`, recreated from the checkout's HEAD on every install, and copies the wrapper into `~/.local/state/aero-bot/teacher/bin/`; the agents run that copy with `AERO_BOT_TEACHER_REPO` pointing at the worktree, so everything they touch lives outside TCC scope.
All harness state (corpus, scratch, reports, logs) lives directly under the state directory, never inside the worktree, so reinstalling never loses an episode.
The wrapper prefers `uv` from PATH with the `~/.local/bin/uv` fallback and appends each run's output to the state directory's logs.

## The hindsight scorer

The `aero-bot-hindsight` command is the intelligence layer's third surface: it closes the loop by replaying the corpus against itself.
Every pulled episode snapshots the window facts at its timestamp, so the realized outcome of an earlier read is simply what later episodes observed - the scorer needs no live reads and no model calls; it replays deterministic facts against deterministic facts.

```
uv run aero-bot-hindsight [--corpus-dir PATH] [--horizon-hours H] [--json]
```

Three desks are scored - the `claude` seat, the `codex` seat, and the student whose audited brief rides each episode - on three axes:

- **Availability**: how many episodes that actually offered the desk a question were answered with a brief, with every typed absence counted by its stable reason.
A `dark` seat and an unreachable window are unasked, never absent.
- **Anomaly calibration**: over the window-grounded streams (tactical and daily; news judges the outside world, which has no deterministic follow-up truth here), each answered brief is scored against the stated truth rule - did any later grounded episode within the horizon (24 hours by default) observe a negative day P&L or a higher halted-cycle count.
A bad outcome scores the moment it is observed; a quiet verdict waits until the horizon has fully elapsed, and a brief stays pending until then - never guessed.
Absences are never scored at all.
The four counts (flagged-bad, flagged-quiet, unflagged-bad, unflagged-quiet) plus the pending count are the whole judgment - no model grades another model, and every number is recomputable by hand.
- **View grading (the conviction layer)**: every stated position view - the verdict, band, and reason a desk's brief carried - is graded against the same deterministic later facts, per the report's own self-describing view rule.
A hold is right when the position stayed tracked through a drained quiet horizon and wrong when a bad outcome followed while it stayed tracked; an exit is right when a bad outcome followed while the position stayed tracked and wrong when a quiet horizon drained with it still tracked; a recenter is right when a recenter action followed while tracked and wrong when a quiet horizon drained with no recenter; an enter is right when an entry followed with no bad outcome inside the window and wrong when any bad outcome fell inside it.
A view whose position left before its horizon drained is ungradeable, and an unfilled horizon stays pending - never guessed.
Explicit declines, missing views on positioned episodes, and verdicts incoherent with the facts are counted, never graded.
The confidence bands are calibrated per desk - high-confidence views must be right more often than low-confidence ones, judged only when both extreme bands hold decided views - and the view-versus-policy counterfactual is computed only where the corpus prices it: an exit view the policy declined is compared against the tracked position's committed-mark path over the same window (first order, blind to emissions, fees, gas, and slippage), a hold view the policy honored is equal, and every comparison whose counterfactual path the store never observed - a hold overridden by an exit, any recenter or enter view the policy declined - is marked uncomputable, never fabricated.

The report is one schema-validated object (`hindsight_report/1`, carrying its own horizon, truth rule, and view rule) atomically rewritten to `reports/hindsight_last.json` beside the corpus, with the latest equity, day P&L, and bounded sample series for scoreboard context and one view-score block per desk.
The daily launchd agent `com.aero-bot.teacher-hindsight` (09:50, after the 09:30 daily stream's bounded timeout has drained) makes it the daily report; the command exits zero on an honest empty corpus and one only when the report cannot be written.

## The upgrade loop

The `aero-bot-upgrade` command is the intelligence layer's fourth surface: it turns measured divergence into proposed teaching, never applied.
The doctrine calls for at-least-daily student upgrades; this surface supplies the proposals and the operator remains the seal, exactly as at every rung below full autonomy.
It reads the local corpus, asks the seats, and writes under the corpus state directory - no live reads, no signing, no writes on the production box.

```
uv run aero-bot-upgrade [--corpus-dir PATH] [--horizon-hours H] [--seat NAME]... [--config PATH] [--json]
```

Exit codes: zero on a clean pass (a gated no-divergence report is honest, not failed), one on configuration, corpus, or write failures.

**Digest first, model second.** Each pass composes a deterministic divergence digest from the corpus and the hindsight verdicts, then asks a question only if the digest is non-empty.
Five divergence classes exist; the first three draw only from episodes whose hindsight verdict came back True (a negative day P&L or a halt increment followed inside the horizon), scoped to the calibrated streams (tactical and daily):

- **Misses** - a teacher flagged anomalies, the student answered unflagged, and bad followed.
- **Availability gaps** - a teacher answered a brief the student never gave; the entry carries the student's own recorded outcome, empty when the episode carried no observation at all.
- **Label divergences** - both flagged, but the teacher named labels the student did not.
- **Posture misses** - the deterministic risk desk (see [the risk-manager documentation](risk-manager.md)) found a posture problem and the student's accepted brief stayed quiet; backed by the finding itself, realized deterministic truth, never a pending read or a hindsight verdict.
- **Conviction misses** - the conviction layer's fifth class: a teacher's stated position view graded right against realized outcomes within the horizon while the student's own view was wrong, declined, or missing; backed by the decided view grades alone, so it opens the honest gate without any bad outcome and lets the teachers' proposals cite measured conviction.

Pending and quiet episodes contribute only context counts (grounded, bad, and quiet tallies, plus the hindsight desk scores - now carrying each desk's view grading - embedded in the prompt) - never entries.
Each class keeps its most recent twenty entries, every brief snippet is whitespace-collapsed and bounded, and each label list is bounded to five, so the digest stays a bounded object.

**Honest gating.** Zero divergences asks no seat anything: the report records the gate and the seats list stays empty, the same fail-closed shape as every sibling surface.
Malformed corpus lines are surfaced, never silently folded into a gate: the digest carries a malformed-line count and the human summary prints a `corpus honesty` line whenever it is non-zero, so corruption never reads as an honest no-divergence pass.

**Asking the seats.** When divergences exist, each enabled seat receives one prompt - the upgrade contract, the digest, the hindsight desk scores, and the student's current in-repo system prompt for context - and must answer exactly one JSON object validating as `upgrade_proposal/1`:

- `teaching_block` - the complete teaching block (1-4000 characters, plain instructional text).
It appends to the student's system prompt behind a fixed separator, so the student's answer contract always survives, and it replaces any previously sealed block wholesale, so it must stand alone.
- `rationale` - one bounded paragraph (at most 2000 characters) citing digest evidence.
- `exemplars` - at most five brief exemplars drawn from the provided corpus evidence, each bounded to 600 characters; an empty list is valid.

Every way an answer can be missing is the same typed catalog the streams use (`dark`, `cli_missing`, `timeout`, `cli_error`, `empty_content`, `malformed_json`, `schema_invalid`), recorded per seat with its model tag and latency.
The seats run with no tools and a 900-second ceiling, the deepest no-tool question they answer.

**Artifacts.** One line per run appends to `proposals/upgrade-<YYYYMMDD>.jsonl` beside the corpus (the dated review trail), and `reports/upgrade_last.json` is atomically rewritten as the one-glance state.

**Sealing a proposal** is the operator's explicit act, and it stays manual until rung-4 autonomy is earned:

1. Review `~/.local/state/aero-bot/teacher/proposals/upgrade-<date>.jsonl` and edit or merge the proposed teaching blocks as judgment dictates.
2. On the VM, write the chosen block to `/etc/aero-bot/advisor-teaching.txt` (for example with `sudoedit`).
3. Uncomment `AERO_BOT_ADVISOR_TEACHING_FILE=/etc/aero-bot/advisor-teaching.txt` in `/etc/aero-bot/advisor.env`.
4. Restart nothing: the next 30-minute advisor pass reads the file, appends it to the in-repo system prompt behind a fixed separator, and fails closed (exit one naming the variable) if the file is unreadable, empty, or beyond 4000 characters.
5. To un-teach, comment the variable back out; the student reverts to the in-repo prompt alone on the next pass.

The daily launchd agent `com.aero-bot.teacher-upgrade` (10:10, after the 09:50 hindsight report has rewritten its own) makes proposing part of the daily rhythm.

## The risk manager

The `aero-bot-risk-manager` command is the intelligence layer's fifth surface: the separation-of-duties counterparty desk that independently audits the corpus's posture snapshots - the day-P&L identity, the five-percent halt line and its entry discipline, exposure against the 100 USDC hard cap and the eighty-percent sizing fraction - and records every desk whose accepted brief stayed quiet over a flagged posture.
It is deterministic and offline like the scorer, its report lands as `reports/risk_manager_last.json` beside the corpus, and the upgrade loop consumes the same pure audit as its fourth evidence class.
The full contract - the finding kinds, the contradiction rule, the upgrade wiring - lives in [the risk-manager documentation](risk-manager.md).
The daily launchd agent `com.aero-bot.teacher-risk-manager` (10:00, between the scorer's 09:50 report and the 10:10 proposer) makes the audit part of the same morning rhythm.

## Manual runs

```
uv run aero-bot-teacher tactical
AERO_BOT_TEACHER_CORPUS_DIR=/tmp/teacher-probe uv run aero-bot-teacher daily --seat codex
uv run aero-bot-hindsight --horizon-hours 48
uv run aero-bot-upgrade --seat claude
uv run aero-bot-risk-manager
```

A manual pass is identical to a scheduled one: one pull, one question per enabled seat, one corpus line.
The hindsight scorer and the upgrade proposer consume the corpus offline; nothing about the harness depends on either running on any schedule.
