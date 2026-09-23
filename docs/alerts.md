# Email alerts

The cycle delivers its report by email: the per-cycle performance summary (position state, P&L vs entry, gas spent, actions with transaction hashes) and - always, whatever the summary setting - an alerting email when anything deserves eyes: an out-of-band condition, a halted cycle, a refused or failed action, or a balance below its floor (the relayer's ETH gas tank, the Safe's USDC working capital).

Delivery never crashes the cycle it describes: a failed transport exchange or a misconfiguration warns on stderr and the cycle's report, audit record, and exit code stand on their own.

## Configuration

Everything is environment-driven; credentials live only in the sealed environment (the systemd `EnvironmentFile` or an operator's shell), never in the repository, never in logs, and never in error text - configuration problems name the missing variable, never its value.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AERO_BOT_ALERT_PROVIDER` | `smtp`, `resend`, or `none` | `none` (silent) |
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

Alerts derive from the cycle report alone, in fixed order: out-of-band first, the halt reason, every refused action with its catalog code, every failed action, then the balance floors (an unknown relayer address - a dry run without `AERO_BOT_RELAYER_ADDRESS` - never alerts, it reports zero honestly instead). Alerting emails carry the `[aero-bot][ALERT]` subject prefix and lead with the first alert; summaries carry `[aero-bot]` and the verdict.

Both floors default above the executor's own hard floors: 0.0005 ETH of relayer headroom over the 0.0002-ETH broadcast floor gives warning before the next cycle refuses, and 5 USDC of Safe working capital suits a pilot funded around twenty - tune both to the deployment through the environment.

## Wiring

`aero-bot-cycle` calls the hook automatically after printing its report, in dry runs and live cycles alike. The systemd unit's sealed environment file carries the provider variables; the deployment guide's smoke checklist includes one forced test email before the captain leaves.

## The daily report

Beyond the per-cycle emails, the kit ships `aero-bot-daily-report.timer`: one dry selector-mode cycle each morning (09:00 Melbourne by default) whose summary email - sent through the same transport, typically Resend with `AERO_BOT_ALERT_PROVIDER=resend` sealed in `/etc/aero-bot/daily-report.env` overlaid on `cycle.env` - serves as the captain's daily portfolio report. The unit runs the identical `aero-bot-cycle --symbol auto --dry-run --json` surface, so the report is exactly what a manual dry run prints, and it stays dark until the overlay file is sealed (`ConditionPathExists`).
