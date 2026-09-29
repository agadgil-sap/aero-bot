# The risk manager

The `aero-bot-risk-manager` command is the intelligence layer's fifth surface and its separation-of-duties seat: the deterministic counterparty desk the captain sealed as decision 3a in the 2026-09-24 upgrade-loop session.
Where the other surfaces ask whether a desk's brief matched what followed, this one independently checks the capital posture itself and every desk's verdict against it, so no single desk's reading stands alone.

```
uv run aero-bot-risk-manager [--corpus-dir PATH] [--json]
```

Exit codes: zero on a produced report (findings are honest observations, not failures; an empty corpus audits to zero everywhere), one on corpus or write failures.

## The counterparty discipline

The surface is deterministic and offline in the same sense the hindsight scorer is: it replays the corpus's fact snapshots - each grounded episode carries the audited cycle summary fields and book posture pulled at its timestamp - against the locked posture arithmetic, with no live reads, no model calls, no signing, and no writes on the production box.
The report beside the corpus is the entire effect.
Nothing it finds authorizes or blocks anything by itself; the locked policy engine keeps every trading decision, and the risk manager never touches the execution path or the sealed teaching block.

## The posture checks

Every check is recomputable by hand from the facts any operator can read in the corpus, and the report carries its own rules string so a month-old report explains itself:

- **Capital posture.**
The day P&L must equal equity minus the day-start anchor - the exact identity the cycle reports compose - within one micro-USDC; anything else is a `day_pnl_contradiction` between the posture's own fields.
- **Halt state.**
A marked drawdown of at least five percent of the day-start anchor (the locked policy's latch fraction) crosses the daily-loss-halt line and records a `halt_threshold_breached` finding.
An entry-kind action (`enter`, `pool_switch`) observed later on the same anchor day - the same New York session date and the same day-start anchor - records a `halt_discipline_violation`, because the halt blocks new entries for the rest of that day while recenters stay armed as maintenance.
- **Exposure versus caps.**
Committed exposure above the 1000 USDC hard ceiling (raised from the trial's 100 by the captain's 2026-09-27 performance ruling) records a `hard_cap_breach` at any equity.
The sizing fraction and the per-name concentration cap engage only at or above the allocator's activation equity (default 1000 USDC, the locked default behind `AERO_BOT_CYCLE_CONCENTRATION_CAP_ACTIVATION_USDC`): an entry committing above eighty percent of current equity - the locked sizing fraction - records a `sizing_cap_breach` there, while below it the same reading records an informational `sizing_fraction_elevated`.
- **Portfolio posture.**
The allocator ruling (gnhf 33) adds the portfolio-level checks: the largest single position holding above the thirty-five percent concentration cap of equity records a `concentration_cap_breach` at or above the activation equity and an informational `concentration_cap_elevated` below it, and more than ten tracked positions records a `count_cap_breach` at every equity.
Both read the composed portfolio facts (position count, per-name committed values) beside the equity the halt already measures.
The informational readings exist because the captain's sub-1000 ruling authorizes the deploy-what-you-have book below the activation equity: the concentration stays visible as elevated risk on the report, never as a policy breach.
- **Contradictions.**
A desk's accepted brief that carried no anomaly flags on an episode with any breach finding records a `DeskContradiction` naming the desk: it read a flagged posture as quiet.
Informational elevated-risk findings never contradict - a desk staying quiet over the authorized sub-1000 concentration is not a contradiction.
The student is checked first, then each teacher seat.
Absences are never contradictions - an absent desk gave no verdict to contradict.

Honest parsing holds everywhere: malformed and non-finite economics strings (`NaN`, infinities) are absences, never guesses, and missing posture fields simply skip their check.

## The report

One frozen schema-validated object (`risk_manager_report/1`) is atomically rewritten to `reports/risk_manager_last.json` beside the corpus: the posture rules, the full per-kind finding counts and per-desk contradiction counts (the complete truth), and the bounded most-recent twenty findings and contradictions.
The timeline is the grounded episodes sorted by episode timestamp, never file order, exactly like the hindsight scorer.

## The upgrade wiring

The upgrade loop consumes the same pure audit (`audit_corpus_posture`) on every pass, so its digest carries a fourth evidence class beside the three teacher divergences: **posture misses**, episodes where the deterministic risk desk found a posture problem and the student's accepted brief stayed quiet.
A posture miss is backed by the finding itself - realized deterministic truth at the episode's timestamp - so it needs no hindsight verdict and opens the honest gate on its own: a corpus where no teacher ever flagged anything still asks the seats for proposals when the counterparty desk caught what the student missed.
Only breach findings gate: the informational elevated-risk findings below the activation equity stay visible in the report and its counts - and in the digest's context count - but never open a posture miss, exactly as they never contradict a quiet desk.
(The conviction layer later joined a fifth class, conviction misses, beside this one; see [the teacher documentation](teacher.md).)
The digest also carries the audit's total finding count as context, and the seats' prompt embeds the posture rules and per-kind counts so proposals can cite them.
The `upgrade_last.json` report keeps its `upgrade_report/1` shape; the new fields default to empty, so older reports stay valid.

## Deployment on the Mac

The launchd kit generates the sixth user agent, `com.aero-bot.teacher-risk-manager`, at 10:00 each morning - after the 09:50 hindsight report has rewritten its own and before the 10:10 upgrade pass composes its digest - and never loads it, like every agent in the kit.
Arming is the operator's explicit `launchctl` act (see [the teacher documentation](teacher.md)).

## Manual runs

```
uv run aero-bot-risk-manager
AERO_BOT_TEACHER_CORPUS_DIR=/tmp/teacher-probe uv run aero-bot-risk-manager --json
```

A manual pass is identical to a scheduled one: one audit over the corpus, one report rewritten beside it.
The upgrade loop recomputes the audit itself on every pass, so nothing about the wiring depends on this surface running on any schedule.
