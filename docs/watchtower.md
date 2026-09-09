# The range watchtower

The `aero-bot-watchtower` command is the always-on complement to the hourly cycle: a tiny long-running process that gives seconds-level reaction to a tracked position leaving its earning range.
A concentrated-liquidity position stops earning the moment the pool tick exits its range, and an hourly timer is too slow for that; the watchtower polls and, on a verified trip, fires the defensive close immediately.

```
uv run aero-bot-watchtower --symbol AAPLc [--poll-seconds 5] [--cooldown-seconds 900] [--max-polls N]
```

Exit codes: zero on a clean stop (a disabled watchtower is dark, not failed), one on startup failures.

## Semantics

One poll, every interval:

1. **Load the tracked position** from the cycle's self-healing book (`cycle_state.json`); flat means nothing to defend.
2. **Read the bounds once per token id** from the pool's own NFPM `positions()` view - the bounds are immutable per position - after deriving the NFPM through the pool's gauge and gauge-factory chain, the same verified view chain the known-pool fast path uses.
3. **Poll the tick**: one cheap block-pinned `slot0` read on the pool contract, the fast-path view set.
4. **Compare** with inclusive-lower and exclusive-upper tick semantics, the same convention the policy engine and executor classify ranges with.

A hard trip - the tick below `tick_lower` or at or above `tick_upper` on either side - fires the defensive close immediately through the existing audited exit surfaces: `execute_unstake` when staked, `execute_withdraw`, `execute_exit_swap`, each with `confirm_broadcast=True` inside the existing caps and refusal catalog.
The close is never gated by market windows or the reference quote; those gate entries only.
Before anything broadcasts, the audited `position_status` read must confirm the trip live - custody in the Safe or the gauge, the same range bounds, out of range, liquidity to exit - so one cheap poll trips and one verified read authorizes.

Every trigger appends the same `cycle_reported` audit record a cycle does (`action=defensive_exit`, `reason=watchtower_range_trip`, mode `live`) beside every record the executors already write, and delivers the alert email through the cycle's alert machinery with the trip line leading the alert set.
A successful close also reconciles the cycle book exactly like the cycle's exit completion, so the next hourly cycle starts flat.

### The fail-safe posture

Unreadable or contradictory state never fires an exit - it alerts and retries on the next poll:

- A failed bounds or tick read reports on stderr, emails at most one notice per cooldown window, and never acts.
- A verification that refuses, an owner outside the Safe and the gauge, bounds that disagree with the polled view, a position back in range, or an empty position all stand the trip down: the trigger records and alerts with its halt reason, and no broadcast happens.
- The watcher is read-only until a genuine trip; it loads no signing key until a trip demands the close, and a configured relayer that disagrees with the key's address refuses the close.

### The latch and the cooldown

- **One-shot latch per trip.** A trip latches; the latch resets only after a successful reconcile - a later poll observing the position back in range, the tracked position gone flat, or the close completing.
- **Cooldown.** Every close attempt stamps `last_fired_at` before anything irreversible, and a new attempt waits out the cooldown window (default 900 seconds), so a refusing or failing close can never hammer the chain in a loop.
- Both survive restarts through the self-healing `watchtower_state.json` beside the audit store (`AERO_BOT_WATCHTOWER_STATE_PATH` overrides), so a crashed watcher restarts inside its cooldown and never re-fires at once.

## Configuration

Everything rides the sealed cycle environment (`/etc/aero-bot/cycle.env`), the same file the cycle units read; nothing is configured in the repository.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AERO_BOT_WATCHTOWER_ENABLED` | `1` arms the watcher; anything else stays dark | `0` (dark) |
| `AERO_BOT_WATCHTOWER_POLL_SECONDS` | Seconds between cheap tick polls | `5` |
| `AERO_BOT_WATCHTOWER_COOLDOWN_SECONDS` | Seconds between close attempts | `900` |
| `AERO_BOT_WATCHTOWER_STATE_PATH` | Explicit latch-state path override | `watchtower_state.json` beside the audit store |

The alert transport configuration is the shared [alerts](docs/alerts.md) set; the watchtower sends trigger emails exactly like cycle alerts and fail-safe notices under the same `[aero-bot][ALERT]` subject prefix.

## Arming

The watchtower ships dark on two independent interlocks, and arming is the deploy operator's explicit step:

1. Set `AERO_BOT_WATCHTOWER_ENABLED=1` in `/etc/aero-bot/cycle.env` (mode 0600, root-owned).
2. Enable and start the unit: `sudo systemctl enable --now aero-bot-watchtower@AAPLc.service`.

The unit is installed by the deployment kit but never enabled by it; with the flag off, a started service prints its disabled line and exits zero.
A smoke check before leaving it running: `sudo systemctl start aero-bot-watchtower@AAPLc.service`, watch `journalctl -u aero-bot-watchtower@AAPLc.service -f` for the armed line and calm `in_range` polls, then `sudo systemctl stop` it until the arming decision.

## systemd wiring

`deploy/systemd/aero-bot-watchtower@.service` carries the contract, pinned by tests and mirroring the dashboard unit:

- One template unit per symbol: `systemctl enable --now aero-bot-watchtower@AAPLc.service`.
- `Type=simple`, `Restart=on-failure` with `RestartSec=10`: the watcher is meant to run forever and recover from crashes inside its persisted cooldown.
- Sealed environment through `EnvironmentFile=/etc/aero-bot/cycle.env`, read only when the file exists (`ConditionPathExists`).
- Hardening: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, state under `ReadWritePaths=/var/lib/aero-bot`.
- SIGTERM stops the loop cleanly between polls; a stop mid-close behaves like a crash, and the cycle's crash discipline covers it.

## Day-two triage

- **Watch the loop**: `journalctl -u aero-bot-watchtower@AAPLc.service -f` shows the armed line, every latch transition, and every failure line; the trigger's complete cycle report prints to stdout and lands in the journal.
- **Why did it not fire on an out-of-range price?** Check the trip was real (the tick against the position bounds), then the journal for `read_failed` lines (RPC outages alert by email too), then the cooldown: a recent close attempt suppresses retries for the window.
- **It fired but the close refused**: the trigger email and the `cycle_reported` audit record carry the refusal code against the [LP execution refusal catalog](docs/lp_execution.md); the watchtower retries after the cooldown while the trip stands, and the hourly cycle reconciles anything left partial.
- **Standing it down**: `sudo systemctl disable --now aero-bot-watchtower@AAPLc.service`, or set `AERO_BOT_WATCHTOWER_ENABLED=0` in the sealed environment to keep the unit running dark.
- **Resetting trip memory**: `watchtower_state.json` beside the audit store is self-healing - deleting it costs at most one extra close attempt, still bounded by the chain's own reconciliation.
