# Ordinary multi-entry timeout and missing staked positions

Observed 2026-09-30T12:13Z through 12:33Z by the rollout worker.
No financial cycle, transaction, book edit, latch reset, or service interruption was initiated by this worker.

## Live reproduction

The existing timer started `aero-bot-cycle@auto.service` at 11:46:13Z on deployed `aad1bf5649133fced58196e9fec1e648f28bf9b3`.
It performed multiple entries after gas funding recovered and confirmed three mints and all three gauge deposits.
At 12:16Z systemd terminated it at the existing `TimeoutStartUSec=30min`, before a final report or portfolio-book save.
The raw journal contains `start operation timed out. Terminating`, `status=15/TERM`, and `Failed with result 'timeout'`.
This worker's deployment was waiting for the execution lock and did not cause the termination.

The next ordinary cycles started at 12:16:34.270015Z and 12:22:31.803416Z.
Both reported `hold/open_in_range`, empty `halted_reason`, `final_reconciliation_verified=true`, but only MSTRc NFT 7310376 in their tracked book.
They marked equity at 68.52842131731241469747400978 and 68.53757092478843242637082253 USDC respectively, versus 104.2348945939082132986662799 at 11:46:10Z.
Both omit the three newly staked NFTs; Safe-owned enumeration shows `inventory_live_token_ids=[]` because deposited NFTs are gauge-owned.
The saved book now carries `halted_day=2026-09-30`, anchor 104.7017073196612441548434662 and peak 105.7353328824604165332248358.
Every board entry is skipped for `daily_loss_halt_active`.
This is an active entry latch, not a whole-cycle halt or an observed unwind.
The roughly 36-USDC marked drop is NOT proven financial loss: newly funded liquidity is omitted from the book's equity.

## Confirmed mint and stake receipts

All six raw receipts are preserved as `receipt-<hash>.json` and independently returned status `0x1` from `https://mainnet.base.org` around 12:33Z.
NFPM: `0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53`.

- NFT **7311805**, mint `0x9360ba3faa621c684a7ace066d2ec8667ceac2e163dee3cf114d93782a45384e`, block 51989952.
  Gauge deposit `0x837fa25eb3f0bb38d7a00cba847118e2fdadd5773df588b42d8a7b9cadbe7f61`, block 51989990.
  Additional read-only live NFPM read around 12:33Z returned liquidity **3610260828**, owner `0xa7d474c6ba8bea20263805370fb8e3ddda6cc642`.
- NFT **7312392**, mint `0x8d9c15fdb4f2ed31339ca2718c014f0f182b374b910437453ae78a51f291cf20`, block 51990296.
  Gauge deposit `0x012fbe85baaa682114a7dcc632fcf2c5f9bb1ec44ad60a06f0eadbf66c334af7`, block 51990331.
  Additional read-only live NFPM read around 12:33Z returned liquidity **2790693399**, owner `0x536df7362915337ddc86c9b57d322905ca819d65`.
- NFT **7312807**, mint `0x7bb0eb2ae6db30a40b56a2ef1a3569c688114f24eef83f5920ac46a5f3844447`, block 51990515.
  Gauge deposit `0x2748e1d61088f2693bf9ad0a204d19f658b8ab87b5532bfc56ba74ec94738773`, block 51990559.
  Its independent latest owner read hit an RPC transport reset; latest custody/liquidity beyond the confirmed receipts remains unverified here.

## Artifact inventory

- `cycle-journal-1100-1229.txt`: full timestamped journal across pre-timeout activity, termination, post-timeout cycles and deployment lock refusal.
- `cycle-raw-and-unit.txt`: same window in raw JSON-report-friendly form plus the installed cycle unit.
- `audit-1100-1230.tsv`: every audit event in that window, sequence, UTC timestamp, type and original payload JSON.
- `pre-timeout-book-and-audit.txt`: read-only persisted book and recent audit rows captured before the timeout.
- `pre-timeout-journal.txt`: active-cycle progress and host/service evidence at 12:13Z.
- `book-live-1230.json`: raw persisted book after the timeout and entry-latch trip.
- `book-backup-122805.tar.gz`: identical-class raw book snapshot from the lock-held fresh deployment backup.
- Six `receipt-<hash>.json` files: independently fetched mint and stake receipts.
- `receipt-probe.json`: first successful read-only endpoint receipt probe.

The pre-timeout full state is represented by the captured printed book rather than an untouched original JSON file; no fabricated snapshot is supplied.
The VM also retains `/var/backups/aero-bot-rollout-20260930T122805Z` with prior release, all top-level JSON state, SQLite online backups, checksums and install evidence.
An earlier aborted backup at `...T122226Z` is incomplete and must not be mistaken for the completed backup.

## Smallest repair direction for the authorized fix lane

Source at merged `868556e` is unchanged in this path: `src/aero_bot/cycle.py:3489` executes multiple plan steps, while the book save occurs at `:1753` after the aggregate act/rebuild.
Reconciliation at `:1900-1924` only discovers untracked live IDs from Safe-owned inventory, and adoption requires an otherwise empty tracked book.
Gauge-owned freshly staked positions therefore fall outside discovery after a killed multi-entry cycle with an existing sibling.
Reproduce the exact sequence: existing funded sibling, three confirmed mint/stake pairs, interruption before aggregate save, ordinary next reconcile.
Require durable progress checkpoints and/or audit-proven gauge-custody recovery for every completed sibling before computing portfolio equity and observing the loss latch.
Preserve fail-closed handling of genuinely unknown NFTs and real loss latches.
Do not merely extend the systemd timeout, clear the live latch, fabricate a book, or clamp the apparent loss.
Raising a time budget alone does not repair crash consistency.

## Deployment relation

The independent fee-growth release `868556ea69358cd08c5460bf0281548bbe2cac5b` installed at 12:28Z through the standard script under the execution lock.
It does not alter this recovery path and is not claimed to repair the timeout defect.
The rollout worker continues ordinary scheduled-cycle observation; no manual adoption or latch reset is authorized.
