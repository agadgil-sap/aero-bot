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
- `empty_content` / `malformed_json` / `schema_invalid` - the answer was absent, unparseable, or violated the bounded brief schema (the same strict schema the student answers, so every desk's briefs live in one scoreable shape).
- `window_unreachable` - the pull itself failed; no question was asked.

## The corpus

Episodes append as JSONL lines under `~/.local/state/aero-bot/teacher/corpus.jsonl` (one per pass, schema tag `teacher_episode/1`), each carrying the stream, the window outcome, the composed facts, the student's latest audited answer, and every seat's outcome tagged by seat and model.
The corpus is the teaching material: hindsight scoring (the next surface) replays episodes against what actually followed, and the student's upgrades train on it.
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

`deploy/launchd/install-mac.sh` generates the three user agents into `~/Library/LaunchAgents` - `com.aero-bot.teacher-tactical` (StartInterval 1800), `com.aero-bot.teacher-daily` (09:30, after the box's 09:00 Melbourne morning report), and `com.aero-bot.teacher-news` (07:10) - and never loads any of them, mirroring the Ubuntu kit's posture.
Arming a stream is the operator's explicit act:

```
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aero-bot.teacher-tactical.plist
launchctl kickstart gui/$(id -u)/com.aero-bot.teacher-tactical
```

macOS constraint the kit works around: launchd user agents cannot read the TCC-protected folders (`~/Documents`, `~/Desktop`, `~/Downloads`), and a checkout living there is unreadable to them - executing a wrapper from such a checkout fails with `Operation not permitted` (verified empirically; only the Terminal's own grant covers interactive runs).
The installer therefore maintains a stateless git worktree at `~/.local/state/aero-bot/teacher/repo`, recreated from the checkout's HEAD on every install, and copies the wrapper into `~/.local/state/aero-bot/teacher/bin/`; the agents run that copy with `AERO_BOT_TEACHER_REPO` pointing at the worktree, so everything they touch lives outside TCC scope.
All harness state (corpus, scratch, reports, logs) lives directly under the state directory, never inside the worktree, so reinstalling never loses an episode.
The wrapper prefers `uv` from PATH with the `~/.local/bin/uv` fallback and appends each run's output to the state directory's logs.

## Manual runs

```
uv run aero-bot-teacher tactical
AERO_BOT_TEACHER_CORPUS_DIR=/tmp/teacher-probe uv run aero-bot-teacher daily --seat codex
```

A manual pass is identical to a scheduled one: one pull, one question per enabled seat, one corpus line.
The hindsight scorer consumes the corpus offline; nothing about the harness depends on it running on any schedule.
