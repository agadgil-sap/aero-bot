"""Daily encrypted backup of the append-only audit chain to a private git remote.

The audit chain is the bot's authoritative evidence, so it leaves the host
every day as one encrypted bundle on a private git branch reachable only
through a deploy key:

1. **Verify first.** The complete hash chain must verify clean before
   anything is exported - a corrupt chain refuses (exit two) rather than
   laundering tampered history into the backup.
2. **Export.** Every record becomes one canonical JSON line (sequence,
   created time, event type, payload JSON verbatim, predecessor and record
   hashes); the bundle carries no secret beyond what the audit already
   records, because the store's secret policy filtered every payload before
   it was ever written.
3. **Encrypt.** AES-256-GCM with a fresh nonce under a sealed 32-byte key;
   the file is a magic header, the nonce, and the ciphertext-plus-tag, so a
   wrong key or a tampered byte fails closed at decryption.
4. **Push.** A scratch git work tree commits the day's bundle to the
   ``audit-backup`` branch and pushes through the deploy key; the remote
   branch accumulates one bundle per day, each a complete self-contained
   snapshot of the append-only chain.

Everything is environment-driven: the key (``AERO_BOT_BACKUP_KEY_HEX``),
the remote (``AERO_BOT_BACKUP_GIT_REMOTE``), the deploy key path
(``AERO_BOT_BACKUP_DEPLOY_KEY_PATH``, owner-only or refused), the scratch
work tree, and the branch name. Errors name variables and paths, never key
material.
"""

import argparse
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from string import hexdigits

from pydantic import BaseModel

from aero_bot.audit import AuditStore, AuditVerificationStatus
from aero_bot.config import Settings

# Environment variable carrying the sealed 32-byte hexadecimal backup key.
BACKUP_KEY_HEX_ENV = "AERO_BOT_BACKUP_KEY_HEX"
# Environment variable carrying the private git remote URL.
BACKUP_GIT_REMOTE_ENV = "AERO_BOT_BACKUP_GIT_REMOTE"
# Environment variable carrying the owner-only deploy key path.
BACKUP_DEPLOY_KEY_PATH_ENV = "AERO_BOT_BACKUP_DEPLOY_KEY_PATH"
# Environment variable carrying an explicit scratch work-tree path.
BACKUP_WORKTREE_PATH_ENV = "AERO_BOT_BACKUP_WORKTREE_PATH"
# Environment variable carrying the backup branch name.
BACKUP_BRANCH_ENV = "AERO_BOT_BACKUP_BRANCH"
# The default backup branch on the private remote.
DEFAULT_BACKUP_BRANCH = "audit-backup"
# A backup key is exactly 32 raw bytes, written as 64 hexadecimal characters.
BACKUP_KEY_HEX_LENGTH = 64
# The all-zero key is a placeholder, never a usable encryption key.
ZERO_KEY = b"\x00" * 32
# The bundle's magic header pins the format version.
BUNDLE_MAGIC = b"AEROAUDIT1"
# AES-GCM nonces are twelve bytes.
GCM_NONCE_BYTES = 12
# Owner-only permission mask for the deploy key file.
OWNER_ONLY_PERMISSION_MASK = 0o077
# One git command gets its seconds; the push gets more.
GIT_TIMEOUT_SECONDS = 120.0
# The commit identity backups carry.
GIT_AUTHOR_NAME = "aero-bot-backup"
GIT_AUTHOR_EMAIL = "backup@aero-bot.invalid"
# The git tool has a fixed installation path; invoking it absolutely
# prevents PATH substitution, mirroring the Keychain's security tool rule.
GIT_EXECUTABLE = "/usr/bin/git"
# CLI exit codes matching the cycle's semantics.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2


class BackupConfigurationError(ValueError):
    """Signal that the backup configuration is incomplete or invalid."""


class BackupRefusedError(RuntimeError):
    """Signal that the chain refused to be backed up (it must verify first)."""


class GitBackupError(RuntimeError):
    """Signal that the git push side of the backup failed."""


def parse_backup_key(text: str) -> bytes:
    """Validate one sealed hexadecimal backup key and return 32 raw bytes.

    Args:
        text: The stripped key text; a leading ``0x`` is optional.

    Returns:
        The 32 raw key bytes.

    Raises:
        BackupConfigurationError: If the text is not exactly a 0x-optional
            64-character hexadecimal key, or is the all-zero placeholder.
            The message quotes the length, never the value.
    """
    stripped = text.strip()
    if stripped.startswith(("0x", "0X")):
        stripped = stripped[2:]
    if len(stripped) != BACKUP_KEY_HEX_LENGTH or any(
        character not in hexdigits for character in stripped
    ):
        raise BackupConfigurationError(
            f"the backup key must be a 64-character hexadecimal value; the "
            f"stored length was {len(stripped)} characters"
        )
    key = bytes.fromhex(stripped)
    if key == ZERO_KEY:
        raise BackupConfigurationError(
            "the backup key is the all-zero placeholder and cannot encrypt anything"
        )
    return key


def load_backup_key(environ: Mapping[str, str]) -> bytes:
    """Read and validate the sealed backup key from the environment.

    Args:
        environ: The process environment carrying the sealed key.

    Returns:
        The 32 raw key bytes.

    Raises:
        BackupConfigurationError: If the variable is unset or malformed.
    """
    if BACKUP_KEY_HEX_ENV not in environ:
        raise BackupConfigurationError(
            f"the backup key environment variable {BACKUP_KEY_HEX_ENV} is not set; "
            "seal the 64-character hexadecimal key there before backing up"
        )
    return parse_backup_key(environ[BACKUP_KEY_HEX_ENV])


def encrypt_bundle(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt one bundle with AES-256-GCM under a fresh nonce.

    Args:
        plaintext: The canonical JSONL export bytes.
        key: Exactly 32 raw key bytes.

    Returns:
        The magic header, the twelve-byte nonce, and the ciphertext with
        its authentication tag appended.

    Raises:
        BackupConfigurationError: If the key is not 32 bytes.
    """
    from Crypto.Cipher import AES
    from Crypto.Random import get_random_bytes

    if len(key) != 32:
        raise BackupConfigurationError("the backup key must be exactly 32 raw bytes")
    nonce = get_random_bytes(GCM_NONCE_BYTES)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return BUNDLE_MAGIC + nonce + ciphertext + tag


def decrypt_bundle(blob: bytes, key: bytes) -> bytes:
    """Decrypt and authenticate one bundle.

    Args:
        blob: The complete bundle file bytes.
        key: Exactly 32 raw key bytes.

    Returns:
        The original canonical JSONL export bytes.

    Raises:
        BackupConfigurationError: If the key is malformed.
        ValueError: If the magic header is wrong or authentication fails -
            a wrong key or any tampered byte fails closed here.
    """
    from Crypto.Cipher import AES

    if len(key) != 32:
        raise BackupConfigurationError("the backup key must be exactly 32 raw bytes")
    header_length = len(BUNDLE_MAGIC) + GCM_NONCE_BYTES
    if len(blob) < header_length + 16 or not blob.startswith(BUNDLE_MAGIC):
        raise ValueError("the bundle does not carry the audit-backup magic header")
    nonce = blob[len(BUNDLE_MAGIC) : header_length]
    ciphertext = blob[header_length:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    try:
        return cipher.decrypt_and_verify(ciphertext[:-16], ciphertext[-16:])
    except ValueError as error:
        raise ValueError(
            "the bundle failed AES-GCM authentication: the key is wrong or a byte was tampered with"
        ) from error


class BackupReport(BaseModel):
    """Carry one backup run's complete evidence."""

    # The number of records the bundle carried.
    record_count: int
    # The chain's head record hash, the bundle's integrity anchor.
    head_record_hash: str
    # The bundle file name committed to the branch.
    bundle_name: str
    # The branch the bundle was pushed to.
    branch: str
    # The commit hash carrying the bundle.
    commit: str
    # Whether the push reached the remote.
    pushed: bool
    # When the backup ran, timezone-aware.
    started_at: datetime
    # Empty on success; otherwise the honest diagnostic.
    diagnostic: str = ""


class AuditBackupRunner:
    """Verify, export, encrypt, and push the audit chain once."""

    def __init__(
        self,
        store: AuditStore,
        worktree_path: Path,
        git_remote: str,
        branch: str,
        key_bytes: bytes,
        deploy_key_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        """Configure one backup run over its boundaries.

        Args:
            store: The append-only audit store being backed up.
            worktree_path: The scratch git work tree committing bundles.
            git_remote: The private remote URL (SSH or a local path).
            branch: The branch bundles accumulate on.
            key_bytes: Exactly 32 raw AES key bytes.
            deploy_key_path: The owner-only deploy key for SSH remotes.
            now: Injected clock; UTC now by default.
            runner: Subprocess runner, injectable for deterministic tests.
        """
        self._store = store
        self._worktree = worktree_path
        self._git_remote = git_remote
        self._branch = branch
        self._key = key_bytes
        self._deploy_key_path = deploy_key_path
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._runner = runner

    def run(self) -> BackupReport:
        """Run one complete backup.

        Returns:
            The backup report with the commit and push evidence.

        Raises:
            BackupRefusedError: If the chain does not verify clean.
            GitBackupError: If the git side fails.
            BackupConfigurationError: If the deploy key file is unusable.
        """
        started_at = self._now()
        verification = self._store.verify_chain()
        if verification.status is not AuditVerificationStatus.VERIFIED:
            raise BackupRefusedError(
                f"the audit chain does not verify ({verification.status.value}: "
                f"{verification.diagnostic}); refusing to back up tampered history"
            )
        export, record_count, head_hash = export_chain(self._store)
        bundle = encrypt_bundle(export.encode("utf-8"), self._key)
        bundle_name = f"audit-chain-{started_at.strftime('%Y%m%d')}.bin"
        commit = self._push_bundle(bundle_name, bundle, record_count, head_hash)
        return BackupReport(
            record_count=record_count,
            head_record_hash=head_hash,
            bundle_name=bundle_name,
            branch=self._branch,
            commit=commit,
            pushed=True,
            started_at=started_at,
        )

    # ------------------------------------------------------------------
    # Git boundary
    # ------------------------------------------------------------------

    def _push_bundle(
        self, bundle_name: str, bundle: bytes, record_count: int, head_hash: str
    ) -> str:
        """Commit one bundle on the branch and push it to the remote.

        Returns:
            The commit hash that carries the bundle.

        Raises:
            GitBackupError: If any git step fails after one honest retry on
                a lost push race.
        """
        self._prepare_worktree()
        self._git("fetch", "origin", self._branch, check=False, quiet_stderr=True)
        self._git("checkout", "-B", self._branch, "--quiet")
        # When the remote branch exists, reset onto it so concurrent
        # machines append rather than diverge; a missing branch starts fresh.
        self._git(
            "reset",
            "--hard",
            f"origin/{self._branch}",
            check=False,
            quiet_stderr=True,
        )
        bundle_path = self._worktree / bundle_name
        bundle_path.write_bytes(bundle)
        self._git("add", bundle_name)
        self._git(
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            f"audit backup {bundle_name} ({record_count} records, head {head_hash[:16]})",
            "--allow-empty",
        )
        commit = self._git("rev-parse", "HEAD").stdout.strip()
        push = self._git("push", "origin", f"{self._branch}:{self._branch}", check=False)
        if push.returncode != 0:
            # One honest retry after re-syncing onto the remote branch.
            self._git("fetch", "origin", self._branch, check=False, quiet_stderr=True)
            self._git("reset", "--hard", f"origin/{self._branch}", check=False, quiet_stderr=True)
            self._git("add", bundle_name)
            self._git(
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "-m",
                f"audit backup {bundle_name} ({record_count} records, head {head_hash[:16]})",
                "--allow-empty",
            )
            commit = self._git("rev-parse", "HEAD").stdout.strip()
            retry = self._git("push", "origin", f"{self._branch}:{self._branch}")
            if retry.returncode != 0:
                raise GitBackupError(
                    "the audit-backup push was rejected twice; investigate the "
                    f"remote branch {self._branch} before the next run"
                )
        return commit

    def _prepare_worktree(self) -> None:
        """Ensure the scratch work tree exists tracking the remote."""
        if (self._worktree / ".git").exists():
            self._git("remote", "set-url", "origin", self._git_remote)
            return
        self._worktree.mkdir(parents=True, exist_ok=True)
        self._git("init", "--quiet")
        self._git("remote", "add", "origin", self._git_remote)

    def _git(
        self,
        *arguments: str,
        check: bool = True,
        quiet_stderr: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run one git command inside the work tree with the sealed identity.

        Args:
            *arguments: The git arguments in order.
            check: Whether a nonzero exit raises.
            quiet_stderr: Whether stderr is captured for expected misses.

        Returns:
            The completed process with decoded output.

        Raises:
            GitBackupError: When ``check`` is set and git exits nonzero.
        """
        environment = dict(os.environ)
        # Any non-HTTP remote reaches git over SSH - the ssh:// scheme and
        # the SCP-style git@host:path form alike - so the deploy key is
        # pinned for both; a startswith("ssh") check silently skipped the
        # documented SCP form and left git offering no identity at all.
        if self._deploy_key_path is not None and not self._git_remote.startswith(
            ("http://", "https://")
        ):
            environment["GIT_SSH_COMMAND"] = (
                f"ssh -i {self._deploy_key_path} -o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new"
            )
        completed = self._runner(
            [
                GIT_EXECUTABLE,
                "-C",
                str(self._worktree),
                "-c",
                f"user.name={GIT_AUTHOR_NAME}",
                "-c",
                f"user.email={GIT_AUTHOR_EMAIL}",
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
        if check and completed.returncode != 0:
            raise GitBackupError(
                f"git {' '.join(arguments)} failed with exit status "
                f"{completed.returncode}: {completed.stderr.strip()[:300]}"
            )
        return completed


def export_chain(store: AuditStore) -> tuple[str, int, str]:
    """Export the complete audit chain as canonical JSON lines.

    Args:
        store: The append-only store being exported.

    Returns:
        The JSONL text, the record count, and the head record hash.

    Raises:
        BackupRefusedError: If any page read fails unexpectedly.
    """
    from aero_bot.audit import AuditRecord

    records: list[AuditRecord] = []
    while True:
        page = store.read_records(1_000, offset=len(records))
        records.extend(page)
        if len(page) < 1_000:
            break
    if not records:
        raise BackupRefusedError("the audit chain is empty; there is nothing to back up yet")
    lines = [
        json.dumps(
            {
                "sequence": record.sequence,
                "created_at": record.created_at.isoformat(),
                "event_type": record.event_type.value,
                "payload_json": record.payload_json,
                "previous_hash": record.previous_hash,
                "record_hash": record.record_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for record in records
    ]
    return "\n".join(lines) + "\n", len(records), records[-1].record_hash


def validate_deploy_key(path_text: str) -> Path:
    """Validate one deploy key path's owner-only permission discipline.

    Args:
        path_text: The configured deploy key file path.

    Returns:
        The resolved path.

    Raises:
        BackupConfigurationError: If the file is missing, not a regular
            file, or readable by group or others - the same owner-only
            rule the signing-key file carries.
    """
    path = Path(path_text).expanduser()
    try:
        file_stat = path.stat()
    except OSError as error:
        raise BackupConfigurationError(
            f"the deploy key file {path} cannot be inspected: {error}"
        ) from error
    if not stat.S_ISREG(file_stat.st_mode):
        raise BackupConfigurationError(f"the deploy key path {path} is not a regular file")
    if file_stat.st_mode & OWNER_ONLY_PERMISSION_MASK:
        raise BackupConfigurationError(
            f"the deploy key file {path} is readable by group or others "
            f"(mode {stat.filemode(file_stat.st_mode)}); refusing to use it - "
            "run `chmod 600` on the file first"
        )
    return path


def build_backup_runner(
    settings: Settings, environ: Mapping[str, str] = os.environ
) -> AuditBackupRunner:
    """Assemble the backup runner from settings and the environment.

    Args:
        settings: The application settings naming the audit database.
        environ: The environment carrying the backup configuration.

    Returns:
        The fully wired runner; nothing has been read yet.

    Raises:
        BackupConfigurationError: If any required variable is missing or
            malformed; the message names the variable, never a value.
    """
    key_bytes = load_backup_key(environ)
    if BACKUP_GIT_REMOTE_ENV not in environ:
        raise BackupConfigurationError(
            f"the backup remote variable {BACKUP_GIT_REMOTE_ENV} is not set; "
            "point it at the private git repository's URL"
        )
    git_remote = environ[BACKUP_GIT_REMOTE_ENV].strip()
    if not git_remote:
        raise BackupConfigurationError(f"{BACKUP_GIT_REMOTE_ENV} must not be empty")
    branch = environ.get(BACKUP_BRANCH_ENV, "").strip() or DEFAULT_BACKUP_BRANCH
    deploy_key: Path | None = None
    raw_deploy_key = environ.get(BACKUP_DEPLOY_KEY_PATH_ENV, "").strip()
    if raw_deploy_key:
        deploy_key = validate_deploy_key(raw_deploy_key)
    worktree_text = environ.get(BACKUP_WORKTREE_PATH_ENV, "").strip()
    worktree = (
        Path(worktree_text)
        if worktree_text
        else settings.audit_database_path.parent / "audit-backup-repo"
    )
    return AuditBackupRunner(
        store=AuditStore(settings.audit_database_path),
        worktree_path=worktree,
        git_remote=git_remote,
        branch=branch,
        key_bytes=key_bytes,
        deploy_key_path=deploy_key,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one audit backup.

    Args:
        argv: Command-line arguments; None reads sys.argv.

    Returns:
        The process exit code: zero on a pushed backup, one on failures,
        two on refusals (a corrupt chain or incomplete configuration).
    """
    parser = argparse.ArgumentParser(
        prog="aero-bot-audit-backup",
        description=(
            "Verify the audit chain, export it as canonical JSON lines, "
            "encrypt it with AES-256-GCM under a sealed key, and push the "
            "day's bundle to a private git branch through a deploy key."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the backup report as JSON instead of a summary.",
    )
    arguments = parser.parse_args(argv)
    try:
        runner = build_backup_runner(Settings())
    except BackupConfigurationError as error:
        print(f"backup refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        report = runner.run()
    except BackupRefusedError as error:
        print(f"backup refused: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except (GitBackupError, OSError, RuntimeError) as error:
        print(f"backup failed: {error}", file=sys.stderr)
        return EXIT_FAILURE
    if arguments.json:
        print(report.model_dump_json(indent=2))
    else:
        print(
            f"backed up {report.record_count} records (head {report.head_record_hash[:16]}) "
            f"as {report.bundle_name} on {report.branch} at commit {report.commit[:16]}"
        )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
