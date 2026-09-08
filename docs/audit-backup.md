# Audit-chain backup

The audit chain leaves the host every day as one encrypted bundle on a private git branch: `aero-bot-audit-backup` verifies the chain, exports it, encrypts it, and pushes it through a deploy key. The systemd timer (`deploy/systemd/aero-bot-audit-backup.timer`, `OnCalendar=daily`, `Persistent=true`) decides when; the command is a hardened oneshot like the cycle.

```
uv run aero-bot-audit-backup [--json]
```

Exit codes: zero on a pushed backup, one on failures, two on refusals - a chain that does not verify, or an incomplete configuration.

## The fixed order

1. **Verify first.** The complete hash chain must verify clean before anything is exported. A corrupt chain refuses rather than laundering tampered history into the backup; the refusal is the tamper evidence working.
2. **Export.** Every record becomes one canonical JSON line - sequence, created time, event type, payload JSON verbatim, predecessor and record hashes. The bundle carries no secret beyond what the audit already records, because the store's secret policy rejected sensitive payload fields before they were ever written.
3. **Encrypt.** AES-256-GCM with a fresh twelve-byte nonce under a sealed 32-byte key. The file is a magic header (`AEROAUDIT1`), the nonce, and the ciphertext with its authentication tag: a wrong key or any tampered byte fails closed at decryption (`decrypt_bundle`).
4. **Push.** A scratch git work tree (default beside the audit store) commits the day's `audit-chain-YYYYMMDD.bin` to the `audit-backup` branch and pushes to the private remote. The remote branch accumulates one complete self-contained snapshot per day; concurrent pushes re-sync and retry once before failing honestly.

Restore is the inverse: fetch the branch, `decrypt_bundle` any day's file with the sealed key, and every line re-validates against the record hashes it carries.

## Configuration

Everything is environment-driven; the daily timer's sealed file is `/etc/aero-bot/backup.env` (mode 0600). Errors name variables and paths, never key material.

| Variable | Meaning | Default |
| --- | --- | --- |
| `AERO_BOT_BACKUP_KEY_HEX` | The sealed 32-byte hexadecimal AES key | required |
| `AERO_BOT_BACKUP_GIT_REMOTE` | The private remote's URL (SSH) | required |
| `AERO_BOT_BACKUP_DEPLOY_KEY_PATH` | The owner-only deploy key for SSH remotes | optional; mode 0600 enforced |
| `AERO_BOT_BACKUP_BRANCH` | The branch bundles accumulate on | `audit-backup` |
| `AERO_BOT_BACKUP_WORKTREE_PATH` | The scratch work tree | beside the audit store |

The deploy key file carries the signing-key file's owner-only rule: any group or other permission bit refuses before a single git command runs, with the `chmod 600` remedy quoted. SSH remotes run with `GIT_SSH_COMMAND` pinned to the key (`IdentitiesOnly`, `accept-new` host keys); the git binary itself is invoked through its absolute path so a hijacked `PATH` cannot substitute a different git.

Generate the key once and seal it in both the timer's environment file and the captain's password manager:

```bash
openssl rand -hex 32
```

## Deploy-key setup (Phase 2, with the captain)

The private backup repository is created once; the deploy key is a read-write-write-once SSH key restricted to that repository:

```bash
ssh-keygen -t ed25519 -f /etc/aero-bot/backup-deploy.key -N "" -C aero-bot-backup
chmod 600 /etc/aero-bot/backup-deploy.key
```

The public half goes into the private repository's deploy keys (write access); `AERO_BOT_BACKUP_DEPLOY_KEY_PATH=/etc/aero-bot/backup-deploy.key` and `AERO_BOT_BACKUP_GIT_REMOTE=git@github.com:<org>/aero-bot-audit-backup.git` join the sealed environment file.
