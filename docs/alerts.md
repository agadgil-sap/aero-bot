# Email alerts

The cycle delivers its report by email: the per-cycle performance summary (position state, P&L vs entry, gas spent, actions with transaction hashes) and - always, whatever the summary setting - an alerting email when anything deserves eyes: an out-of-band condition, a halted cycle, a refused or failed action, or a balance below its floor (the relayer's ETH gas tank, the Safe's USDC working capital).

Beyond that default there is one further routing mode, the daily digest (the captain's 2026-10-05 ruling; see [The daily digest](#the-daily-digest) below): every immediate email is suppressed and the 09:00 Melbourne report carries the prior 24 hours in a single bounded email.

Delivery never crashes the cycle it describes: a failed transport exchange or a misconfiguration warns on stderr and the cycle's report, audit record, and exit code stand on their own.

## Configuration

Everything is environment-driven; credentials live only in the sealed environment (the systemd `EnvironmentFile` or an operator's shell), never in the repository, never in logs, and never in error text - configuration problems name the missing variable, never its value.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AERO_BOT_ALERT_PROVIDER` | `smtp`, `resend`, or `none` | `none` (silent) |
| `AERO_BOT_ALERT_MODE` | `per_cycle` (immediate emails, the shipped default) or `digest` (suppress every immediate email; only the daily digest sends) | `per_cycle` |
| `AERO_BOT_ALERT_FROM` | The From address for every email | required with a provider |
| `AERO_BOT_ALERT_TO` | Comma-separated recipients | required with a provider |
| `AERO_BOT_ALERT_SMTP_HOST` | SMTP server hostname (smtp provider) | required |
| `AERO_BOT_ALERT_SMTP_PORT` | STARTTLS submission port | `587` |
| `AERO_BOT_ALERT_SMTP_USER` | SMTP login user | required |
| `AERO_BOT_ALERT_SMTP_PASSWORD` | SMTP password (sealed) | required |
| `AERO_BOT_ALERT_RESEND_API_KEY` | Bearer key (resend provider, sealed) | required |
| `AERO_BOT_ALERT_RESEND_URL` | Resend-style endpoint | `https://api.resend.com/emails` |
| `AERO_BOT_ALERT_SUMMARY_EVERY_CYCLE` | `1` emails every cycle's summary, `0` alerts only | `1` |
| `AERO_BOT_ALERT_RELAYER_ETH_FLOOR_WEI` | Relayer ETH alert floor, wei | `500000000000000` (0.0005 ETH) |
| `AERO_BOT_ALERT_SAFE_USDC_FLOOR_UNITS` | Safe USDC alert floor, raw six-decimal units | `5000000` (5 USDC) |

## The two transports

- **SMTP** (`smtp`): provider-agnostic submission over STARTTLS with login - Gmail app passwords, Fastmail, any mail host. The exchange is one connection per email: STARTTLS with the default TLS context, `AUTH LOGIN`, `send_message`, quit, bounded at thirty seconds.
- **Resend** (`resend`): one bearer-keyed HTTPS POST per email in the Resend JSON shape (`from`, `to`, `subject`, `text`); any Resend-style API works by overriding the endpoint URL.

## Alert semantics

Alerts derive from the cycle report alone, in fixed order: out-of-band first, the halt reason, every refused action with its catalog code, every failed action, then the balance floors (an unknown relayer address - a dry run without `AERO_BOT_RELAYER_ADDRESS` - never alerts, it reports zero honestly instead), and finally the idle-book alert. Alerting emails carry the `[aero-bot][ALERT]` subject prefix and lead with the first alert; summaries carry `[aero-bot]` and the verdict.

Both floors default above the executor's own hard floors: 0.0005 ETH of relayer headroom over the 0.0002-ETH broadcast floor gives warning before the next cycle refuses, and 5 USDC of Safe working capital suits a pilot funded around twenty - tune both to the deployment through the environment.

### The idle-book alert (captain's ruling, gnhf 34)

A silent idle book must never happen again: the first allocator night sat 113 clean cycles at 97 percent cash while the top pool (qualifying APR 69-81) was excluded every cycle behind an opaque label. The idle-book alert fires when more than the configured fraction of equity sits as cash (default 0.80, `AERO_BOT_ALERT_IDLE_CASH_FRACTION`, a decimal fraction inside (0, 1]) while at least one pool ranks above the tier band but stays excluded by a gate or bound. It fires ONCE PER EPISODE: the cycle book carries the episode signature (the sorted symbol/reason/gate set), and only the first cycle of a changed cause alerts - the same per-cycle rate limit every alert rides, plus episode suppression so a week-long idle posture costs one email, not 2016. The alert line names the pool, the gate, the bound, and the income forgone per day at the pool's qualifying APR; the full per-gate evidence rides every cycle's decision diagnostics and the `cycle_reported` audit record.

## Wiring

`aero-bot-cycle` calls the hook automatically after printing its report, in dry runs and live cycles alike. The systemd unit's sealed environment file carries the provider variables; the deployment guide's smoke checklist includes one forced test email before the captain leaves.

## The daily report

Beyond the per-cycle emails, the kit ships `aero-bot-daily-report.timer`: one dry selector-mode cycle each morning (09:00 Melbourne by default - `OnCalendar=*-*-* 09:00:00 Australia/Melbourne`, so systemd's timezone-aware calendar keeps the local morning across both DST transitions) whose summary email - sent through the same transport, typically Resend with `AERO_BOT_ALERT_PROVIDER=resend` sealed in `/etc/aero-bot/daily-report.env` overlaid on `cycle.env` - serves as the captain's daily portfolio report. The unit runs the identical `aero-bot-cycle --symbol auto --dry-run --json` surface (plus the `--daily-digest` flag below), so the report is exactly what a manual dry run prints, and it stays dark until the overlay file is sealed (`ConditionPathExists`).

## The daily digest

The captain's 2026-10-05 ruling: only one email per day. `AERO_BOT_ALERT_MODE=digest` (sealed in `cycle.env`) suppresses EVERY immediate email path - per-cycle summaries, event alerts, watchtower range trips, and fail-safe notices - while leaving local logging, the audit chain, the watchtower monitor, and every loss/custody/gas protection exactly as they are; no trading behavior changes. The one email that remains is the daily-report tick's digest, composed because the unit's `ExecStart` carries `--daily-digest` (the flag replaces that run's own per-cycle email; without the sealed mode it sends the digest and leaves every other path firing, so the two settings compose safely in either sealing order).

The digest is derived, never invented: every line restates a durable audit record inside the prior 24 hours - the decision flow and equity path (`cycle_reported`), every broadcast with its on-chain outcome (`lp_execute_sent` joined to its receipt), reward claims and the latest yield attribution, and the failure catalog (halted cycles, reverted deliveries, broadcast-unknown submissions, and every `lp_refused` catalog code). The student's advice section restates the latest `advisor_reported` brief with its model, latency, and age, plus the window's typed-absence tally, and says so explicitly when no pass reported. The teacher section restates the bounded one-way evidence artifact the Mac-side publisher writes beside the audit store (see [the teacher documentation](teacher.md#the-advice-evidence-publisher-firstmate-028s-minimal-transfer)): the publish provenance (`generated_at`), per-episode timestamps with each seat's model, outcome, capped brief, and position view - and when the artifact is absent, unparseable, or its newest episode is older than 24 hours, the section states that explicit MISSING, MALFORMED, or STALE marker rather than inventing advice. Sections are line-bounded so one email stays readable on a noisy day.

Composition lives in `src/aero_bot/digest.py` (`aero-bot-cycle --daily-digest`); the bounded window read pages the audit store newest-page-first and stops at the page cap.
