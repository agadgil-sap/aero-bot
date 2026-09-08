# Live cycle 2 - blocked before broadcast: residual NFT refuses the mint

Ordered 2026-09-08 ~21:40 AEST (11:40Z): one full LP cycle on the known AAPLc/USDC pool through the productized `aero-bot-lp execute` commands on the new fast path, timing every action.
The cycle did not execute.
Step 2 (mint) is refused by the executor's own fail-closed gate `untracked_existing_positions`, because the first canary's residual empty position NFT 5703026 still sits at the Safe, and no productized execute path can burn it.
Nothing was broadcast this session: zero transactions, zero signatures with real key material (the only signing was a throwaway ephemeral key in one dry run), and the audit chain shows read-only events only.
This is the refusal catalog working exactly as documented, not a machinery fault, and per the brief the run stops and reports rather than working around it.

## Outcome in one paragraph

The Safe enters and exits this session unchanged: 8.972790 USDC, 0 AAPLc, 1.839255877811365502 AERO, 0.0001 ETH, Safe nonce 25, relayer EOA 0.000975 ETH.
Step 1 (the pre-mint balancing swap) was deliberately not executed once the mint blocker was confirmed: the shipped swap executor converts USDC to stock only (1.00 USDC per-swap cap), the stock-to-USDC reverse direction exists only in the uncommitted canary driver `run/swap_exit.py` (gitignored, absent here), so a pre-swap would have stranded stock in a blocked cycle.
Every later step is unreachable without the mint.

## The blocker, precisely

- The canary's exit (2026-09-08 04:31Z) left empty position NFT 5703026 at the Safe - zero liquidity, zero checkpointed fees, unstaked, above range - documented verbatim in `data/aero-bot-lp-canary-campaign/timing-report.md` as "burnable by a future recenter".
- Live read-only proof this session: `ownerOf(5703026)` = the Safe, NFPM `balanceOf(safe)` = 1 (probe at block ~51036406).
- The mint gate (`docs/lp_execution.md`, execution-layer cap table) refuses entry whenever the Safe holds any position NFT: without a live position-value read the total-exposure cap cannot be evaluated honestly, so entry fails closed as `untracked_existing_positions`.
- The productized CLI offers execute forms for mint/stake/unstake/withdraw/collect only.
The one composed burn path - `recenter`, which burns the emptied NFT and recycles it into a fresh mint - is dry-run-only by design ("the recenter action has no execute form"), and the withdraw action's own refusal text concedes the gap: "burn it directly once an execute path exists".
- Proof the recenter batch itself builds fine: `dry-run recenter --symbol AAPLc --token-id 5703026 --width-ticks 1 --amount 8.5 --ephemeral-key` composed the full five-transaction batch at Safe nonces 25-29 (`nfpm_burn`, `balancing_swap` 6,139,489 raw USDC for at least 1,906,352 raw AAPLc, both exact NFPM approvals, `mint` range [-11610,-11590) with 2,366,643 + 1,923,686 raw units), exit 0, signatures honestly rejected because the key is ephemeral.
Only the broadcast surface is missing.

What would unblock a rerun, in the captain's court: either ship the recenter's execute form (or a bare burn action), or ship the live position-value read the gate's own message names, or rule an alternative disposal for NFT 5703026.
All three are code changes this brief did not authorize, so none was attempted.

## Per-action timings (wall clock, this session)

Wall times from `run/exec_markers.log` (the canary's marker pattern); every action below is read-only - no key material, no broadcast.
The app-settings RPC is `https://mainnet.base.org`; its public endpoint rate-limits (429) with the backend's bounded exponential backoff, which is the cause of every >60s action below.

| action | wall | outcome |
| --- | --- | --- |
| live state probe (balances, custody, nonce) | ~5s | Safe state as above; NFT 5703026 confirmed at the Safe |
| `plan mint` - cold (no pin on this machine's canonical pin store) | **198s - over budget, cause: the one-time full Sugar sweep over the rate-limited app RPC; it wrote the AAPLc pin at block 51036544** | refused `untracked_existing_positions`, audit seq 153 |
| `plan mint` - fast path (pinned) | 33s | same refusal, audit seq 154 |
| `plan mint` - fast path again | 28s | same refusal, audit seq 155 |
| `status --token-id 5703026` (fast path) | 34s | empty, unstaked, above range, 0 USDC value; audit seq 156 |
| `dry-run recenter --token-id 5703026 --ephemeral-key` | 53s (build 52.3s) | full burn+swap+approve+mint batch built at nonces 25-29; audit seqs 157-163 |
| steps 1-7 of the ordered cycle | not started | blocked at step 2; step 1 deliberately skipped (see above) |

Speed-pass verdict, honestly measured: the fast path removes the ~200s per-action sweep permanently after one cold resolution, but on this endpoint the residual 28-53s per action is dominated by rate-limit backoff, not discovery - the docs' 3.8s figure was measured over `base.publicnode.com`, and raw `eth_blockNumber` calls here answer in 0.34s.
With a calmer endpoint or after the limiter cools, these actions land in single-digit seconds; no action this session spent its time on discovery except the one cold sweep.

## Emissions and fees

None earned, none collected - nothing was staked this session.
The Safe's AERO balance is unchanged from the canary's close: 1,839,255,877,811,365,502 raw (18 decimals) = 1.839255877811365502 AERO, of which ~0.0015410 was the canary position's own emissions and 1.837715 pre-existed the campaign.
No trading fees were collected (no collect or withdraw ran).

## Transactions

None broadcast.
There are no tx hashes to report because every productized surface refused or stopped at dry run before any broadcast confirmation was reached.
The audit chain (`~/Library/Application Support/Aero Bot/audit.sqlite3`) carries this session as sequences 153-163: three `lp_refused` (mint, `untracked_existing_positions`), one `lp_status_reported`, one `lp_mint_planned` plus `lp_recenter_planned` with its five `lp_transaction_built` records - all read-only evidence, no `lp_execute_sent`, no `execution_sent`.

## Final Safe balances (unchanged)

| asset | raw | units |
| --- | --- | --- |
| USDC | 8,972,790 | 8.972790 |
| AAPLc | 0 | 0 |
| AERO | 1,839,255,877,811,365,502 | 1.839255877811365502 |
| ETH (Safe) | 100,000,000,000,000 | 0.0001 |
| ETH (relayer EOA) | 974,614,963,677,242 | ~0.000975 |

## Refusals encountered

| surface | refusal | why |
| --- | --- | --- |
| `aero-bot-lp plan/execute mint` | `untracked_existing_positions` | residual canary NFT 5703026 at the Safe; no productized burn path exists (the structural blocker, audited at seq 153-155) |
| `aero-bot-lp execute withdraw --token-id 5703026` (by inspection of the gate, not run) | `position_empty` | the residual NFT holds no liquidity and no fees, so even the exit path refuses it; its message points at recenter-or-future-burn |
| stock -> USDC and AERO -> USDC (ordered step 7) | no shipped surface | the swap executor is USDC-to-stock only; AERO is outside the USDC-plus-B20-registry token whitelist; the reverse driver is uncommitted canary tooling |

## Session artifacts

- `run/exec_markers.log` - wall-clock markers (verbatim above).
- `run/logs/*.log` - full stdout/stderr of every action.
- `run/live_state_probe.py`, `run/audit_read.py`, `run/rpc_latency_probe.py` - the read-only probes.
- Canonical pin store gained the AAPLc pin (block 51036544) - the fast path is armed for the rerun.
