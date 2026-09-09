"""Pin the audit-chain backup: verify, export, encrypt, and git push."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.audit_backup import (
    BACKUP_BRANCH_ENV,
    BACKUP_GIT_REMOTE_ENV,
    BACKUP_KEY_HEX_ENV,
    AuditBackupRunner,
    BackupConfigurationError,
    BackupRefusedError,
    build_backup_runner,
    decrypt_bundle,
    encrypt_bundle,
    export_chain,
    main,
    parse_backup_key,
    validate_deploy_key,
)
from aero_bot.config import Settings

# A fixture backup key, sealed-shaped but never real.
BACKUP_KEY_HEX = "9f" * 32
BACKUP_KEY = bytes.fromhex(BACKUP_KEY_HEX)
# The fixed fixture clock names the bundle deterministically.
BACKUP_NOW = datetime(2026, 9, 9, 2, 0, tzinfo=UTC)


class QuotePayload(BaseModel):
    """One minimal audited payload for seeding the chain."""

    note: str


def seeded_store(tmp_path: Path, records: int = 3) -> AuditStore:
    """Build one audit store carrying a verified fixture chain."""
    store = AuditStore(tmp_path / "audit.sqlite3")
    for index in range(records):
        store.append(
            AuditEventType.SYSTEM_STATE,
            QuotePayload(note=f"fixture-{index}"),
            BACKUP_NOW,
        )
    return store


def local_remote(tmp_path: Path) -> Path:
    """Create one bare repository standing in for the private remote."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["/usr/bin/git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )
    return remote


def make_runner(
    tmp_path: Path,
    *,
    records: int = 3,
    remote: Path | None = None,
    seed: bool = True,
) -> tuple[AuditBackupRunner, Path]:
    """Assemble one backup runner over a seeded store and local remote.

    Args:
        tmp_path: The scratch directory holding the store and remote.
        records: How many fixture records a fresh seed appends.
        remote: An existing bare remote; None creates one.
        seed: False when the caller already prepared (or corrupted) the
            store at the fixture path.
    """
    store = seeded_store(tmp_path, records) if seed else AuditStore(tmp_path / "audit.sqlite3")
    remote_path = remote if remote is not None else local_remote(tmp_path)
    runner = AuditBackupRunner(
        store=store,
        worktree_path=tmp_path / "worktree",
        git_remote=str(remote_path),
        branch="audit-backup",
        key_bytes=BACKUP_KEY,
        now=lambda: BACKUP_NOW,
    )
    return runner, remote_path


def remote_tree(remote: Path, branch: str = "audit-backup") -> list[str]:
    """List one file tree on the remote branch; empty when absent."""
    completed = subprocess.run(
        [
            "/usr/bin/git",
            f"--git-dir={remote}",
            "ls-tree",
            "--name-only",
            "-r",
            branch,
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line]


class TestKeyParsing:
    """The sealed backup key's validation."""

    def test_valid_bare_and_prefixed_keys_parse(self) -> None:
        """Bare and 0x-prefixed 64-character keys return the raw bytes."""
        assert parse_backup_key(BACKUP_KEY_HEX) == BACKUP_KEY
        assert parse_backup_key("0x" + BACKUP_KEY_HEX) == BACKUP_KEY

    def test_wrong_length_and_non_hex_refuse_without_values(self) -> None:
        """Malformed keys fail quoting lengths, never content."""
        with pytest.raises(BackupConfigurationError, match="64-character") as error:
            parse_backup_key("abcd")
        assert "abcd" not in str(error.value)
        with pytest.raises(BackupConfigurationError, match="64-character"):
            parse_backup_key("z" * 64)

    def test_the_zero_placeholder_refuses(self) -> None:
        """The all-zero key cannot encrypt anything."""
        with pytest.raises(BackupConfigurationError, match="all-zero"):
            parse_backup_key("0" * 64)

    def test_missing_environment_variable_names_it(self) -> None:
        """An unset key variable fails naming the variable."""
        with pytest.raises(BackupConfigurationError, match=BACKUP_KEY_HEX_ENV):
            from aero_bot.audit_backup import load_backup_key

            load_backup_key({})


class TestBundleEncryption:
    """The AES-256-GCM bundle format."""

    def test_round_trip_preserves_the_export_bytes(self) -> None:
        """Encryption and decryption restore the exact plaintext."""
        plaintext = b'{"sequence":1}\n{"sequence":2}\n'
        blob = encrypt_bundle(plaintext, BACKUP_KEY)
        assert blob.startswith(b"AEROAUDIT1")
        assert decrypt_bundle(blob, BACKUP_KEY) == plaintext

    def test_two_encryptions_use_fresh_nonces(self) -> None:
        """Every bundle carries a distinct nonce under the same key."""
        first = encrypt_bundle(b"same plaintext", BACKUP_KEY)
        second = encrypt_bundle(b"same plaintext", BACKUP_KEY)
        assert first != second

    def test_a_wrong_key_fails_authentication(self) -> None:
        """A wrong key fails closed at GCM verification."""
        blob = encrypt_bundle(b"secret chain", BACKUP_KEY)
        with pytest.raises(ValueError, match="authentication"):
            decrypt_bundle(blob, bytes.fromhex("11" * 32))

    def test_a_tampered_byte_fails_authentication(self) -> None:
        """Any flipped ciphertext byte fails closed."""
        blob = bytearray(encrypt_bundle(b"secret chain", BACKUP_KEY))
        blob[-1] ^= 1
        with pytest.raises(ValueError, match="authentication"):
            decrypt_bundle(bytes(blob), BACKUP_KEY)

    def test_a_non_bundle_blob_refuses_on_the_magic_header(self) -> None:
        """Random bytes never decrypt as a bundle."""
        with pytest.raises(ValueError, match="magic header"):
            decrypt_bundle(b"not a bundle at all" + b"0" * 40, BACKUP_KEY)

    def test_malformed_key_lengths_refuse(self) -> None:
        """Both directions refuse keys that are not exactly 32 bytes."""
        with pytest.raises(BackupConfigurationError, match="32 raw bytes"):
            encrypt_bundle(b"x", b"short")
        with pytest.raises(BackupConfigurationError, match="32 raw bytes"):
            decrypt_bundle(b"AEROAUDIT1" + b"0" * 40, b"short")


class TestChainExport:
    """The canonical JSONL export."""

    def test_export_carries_every_record_verbatim(self, tmp_path: Path) -> None:
        """Each record becomes one canonical line with its payload intact."""
        store = seeded_store(tmp_path, records=3)
        export, count, head = export_chain(store)
        assert count == 3
        lines = export.strip().splitlines()
        assert len(lines) == 3
        first = json.loads(lines[0])
        assert first["sequence"] == 1
        assert first["event_type"] == "system_state"
        assert "fixture-0" in first["payload_json"]
        assert head == store.read_records(3)[-1].record_hash

    def test_an_empty_chain_refuses(self, tmp_path: Path) -> None:
        """Nothing to back up is an honest refusal, not an empty bundle."""
        store = AuditStore(tmp_path / "empty.sqlite3")
        with pytest.raises(BackupRefusedError, match="empty"):
            export_chain(store)


class TestCorruptChainRefusal:
    """A tampered chain never reaches the backup."""

    def test_a_corrupt_chain_refuses_before_export(self, tmp_path: Path) -> None:
        """One tampered payload row makes the whole backup refuse."""
        import sqlite3

        seeded_store(tmp_path, records=3)
        connection = sqlite3.connect(tmp_path / "audit.sqlite3")
        # The schema's append-only trigger is dropped first: corruption tests
        # bypass exactly the guard real tampering would have to bypass.
        connection.execute("DROP TRIGGER audit_records_no_update")
        connection.execute(
            "UPDATE audit_records SET payload_json = '{\"tampered\": true}' WHERE sequence = 2"
        )
        connection.commit()
        connection.close()
        runner, remote = make_runner(tmp_path, seed=False)
        with pytest.raises(BackupRefusedError, match="does not verify"):
            runner.run()
        assert remote_tree(remote) == []


class TestGitPushFlow:
    """The bundle push against a real local bare remote."""

    def test_one_run_pushes_one_dated_bundle(self, tmp_path: Path) -> None:
        """A first run commits and pushes the day's bundle."""
        runner, remote = make_runner(tmp_path)
        report = runner.run()
        assert report.record_count == 3
        assert report.pushed is True
        assert report.bundle_name == "audit-chain-20260909.bin"
        assert remote_tree(remote) == [report.bundle_name]
        committed = subprocess.run(
            [
                "/usr/bin/git",
                f"--git-dir={remote}",
                "log",
                "-1",
                "--format=%s",
                "audit-backup",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert committed.startswith("audit backup audit-chain-20260909.bin")
        assert "3 records" in committed

    def test_the_pushed_bundle_decrypts_to_the_chain(self, tmp_path: Path) -> None:
        """The remote's bundle restores the exact JSONL export."""
        runner, _ = make_runner(tmp_path)
        report = runner.run()
        blob = subprocess.run(
            [
                "/usr/bin/git",
                f"--git-dir={tmp_path / 'remote.git'}",
                "show",
                f"audit-backup:{report.bundle_name}",
            ],
            check=True,
            capture_output=True,
        ).stdout
        export, _, _ = export_chain(runner._store)
        assert decrypt_bundle(blob, BACKUP_KEY).decode("utf-8") == export

    def test_daily_bundles_accumulate_on_the_branch(self, tmp_path: Path) -> None:
        """A second day's run adds its bundle beside the first."""
        runner, remote = make_runner(tmp_path)
        first = runner.run()
        later = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)
        second_runner = AuditBackupRunner(
            store=runner._store,
            worktree_path=runner._worktree,
            git_remote=str(remote),
            branch="audit-backup",
            key_bytes=BACKUP_KEY,
            now=lambda: later,
        )
        second = second_runner.run()
        assert second.bundle_name == "audit-chain-20260910.bin"
        assert sorted(remote_tree(remote)) == sorted({first.bundle_name, second.bundle_name})


class TestSshRemoteKeyPinning:
    """GIT_SSH_COMMAND must cover every non-HTTP remote form."""

    def test_scp_style_remote_pins_the_deploy_key(self, tmp_path: Path) -> None:
        """The documented git@host:path remote carries the key pin."""
        key = tmp_path / "deploy.key"
        key.write_text("fixture", encoding="ascii")
        environments: list[dict[str, str]] = []

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environments.append(dict(cast("dict[str, str]", kwargs.get("env"))))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner = AuditBackupRunner(
            store=AuditStore(tmp_path / "audit.sqlite3"),
            worktree_path=tmp_path / "worktree",
            git_remote="git@github.com:agadgil-sap/aero-bot.git",
            branch="audit-backup",
            key_bytes=BACKUP_KEY,
            deploy_key_path=key,
            runner=fake_runner,
        )
        runner._git("status")
        assert environments[-1]["GIT_SSH_COMMAND"] == (
            f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )

    def test_https_remote_never_pins_ssh(self, tmp_path: Path) -> None:
        """An HTTPS remote ignores the deploy key entirely."""
        key = tmp_path / "deploy.key"
        key.write_text("fixture", encoding="ascii")
        environments: list[dict[str, str]] = []

        def fake_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environments.append(dict(cast("dict[str, str]", kwargs.get("env"))))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner = AuditBackupRunner(
            store=AuditStore(tmp_path / "audit.sqlite3"),
            worktree_path=tmp_path / "worktree",
            git_remote="https://github.com/agadgil-sap/aero-bot.git",
            branch="audit-backup",
            key_bytes=BACKUP_KEY,
            deploy_key_path=key,
            runner=fake_runner,
        )
        runner._git("status")
        assert "GIT_SSH_COMMAND" not in environments[-1]


class TestDeployKeyDiscipline:
    """The owner-only deploy key rule."""

    def test_a_mode_600_key_validates(self, tmp_path: Path) -> None:
        """An owner-only key file passes the permission gate."""
        key = tmp_path / "deploy.key"
        key.write_text("ssh-ed25519 AAAA fixture\n", encoding="ascii")
        os.chmod(key, 0o600)
        assert validate_deploy_key(str(key)) == key

    def test_loose_permissions_refuse_before_any_git(self, tmp_path: Path) -> None:
        """A group-readable key refuses with the chmod remedy."""
        key = tmp_path / "deploy.key"
        key.write_text("ssh-ed25519 AAAA fixture\n", encoding="ascii")
        os.chmod(key, 0o644)
        with pytest.raises(BackupConfigurationError, match="chmod 600"):
            validate_deploy_key(str(key))

    def test_a_missing_key_file_refuses(self, tmp_path: Path) -> None:
        """A missing key file names the path."""
        with pytest.raises(BackupConfigurationError, match="cannot be inspected"):
            validate_deploy_key(str(tmp_path / "absent.key"))


class TestBuildRunner:
    """The environment-driven runner assembly."""

    def test_complete_configuration_wires_every_boundary(self, tmp_path: Path) -> None:
        """Every variable lands in its boundary; defaults apply."""
        seeded_store(tmp_path)
        settings = Settings.model_construct(audit_database_path=tmp_path / "audit.sqlite3")
        runner = build_backup_runner(
            settings,
            {
                BACKUP_KEY_HEX_ENV: BACKUP_KEY_HEX,
                BACKUP_GIT_REMOTE_ENV: str(tmp_path / "remote.git"),
            },
        )
        assert runner._branch == "audit-backup"
        assert runner._worktree == tmp_path / "audit-backup-repo"

    def test_missing_remote_names_the_variable(self, tmp_path: Path) -> None:
        """A missing remote variable fails naming it, never any value."""
        seeded_store(tmp_path)
        settings = Settings.model_construct(audit_database_path=tmp_path / "audit.sqlite3")
        with pytest.raises(BackupConfigurationError, match=BACKUP_GIT_REMOTE_ENV):
            build_backup_runner(settings, {BACKUP_KEY_HEX_ENV: BACKUP_KEY_HEX})

    def test_branch_and_worktree_overrides_apply(self, tmp_path: Path) -> None:
        """Branch and worktree variables override the defaults."""
        seeded_store(tmp_path)
        settings = Settings.model_construct(audit_database_path=tmp_path / "audit.sqlite3")
        runner = build_backup_runner(
            settings,
            {
                BACKUP_KEY_HEX_ENV: BACKUP_KEY_HEX,
                BACKUP_GIT_REMOTE_ENV: str(tmp_path / "remote.git"),
                BACKUP_BRANCH_ENV: "evidence",
                "AERO_BOT_BACKUP_WORKTREE_PATH": str(tmp_path / "scratch"),
            },
        )
        assert runner._branch == "evidence"
        assert runner._worktree == tmp_path / "scratch"


class TestCli:
    """The backup command's exit semantics."""

    def test_missing_configuration_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No environment means a refusal exit, nothing pushed."""
        exit_code = main([])
        assert exit_code == 2
        assert BACKUP_KEY_HEX_ENV in capsys.readouterr().err

    def test_a_complete_run_exits_zero_and_prints(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured run pushes and summarizes its commit."""
        seeded_store(tmp_path)
        remote = local_remote(tmp_path)
        monkeypatch.setenv(BACKUP_KEY_HEX_ENV, BACKUP_KEY_HEX)
        monkeypatch.setenv(BACKUP_GIT_REMOTE_ENV, str(remote))
        monkeypatch.setenv("AERO_BOT_BACKUP_WORKTREE_PATH", str(tmp_path / "worktree"))
        monkeypatch.setattr(
            "aero_bot.audit_backup.Settings",
            lambda: Settings.model_construct(audit_database_path=tmp_path / "audit.sqlite3"),
        )
        assert main([]) == 0
        output = capsys.readouterr().out
        assert "3 records" in output
        assert "audit-chain-" in output

    def test_a_refused_chain_exits_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corrupt chain refuses through the CLI as well."""
        import sqlite3

        seeded_store(tmp_path)
        connection = sqlite3.connect(tmp_path / "audit.sqlite3")
        connection.execute("DROP TRIGGER audit_records_no_update")
        connection.execute(
            "UPDATE audit_records SET payload_json = '{\"tampered\": true}' WHERE sequence = 1"
        )
        connection.commit()
        connection.close()
        monkeypatch.setenv(BACKUP_KEY_HEX_ENV, BACKUP_KEY_HEX)
        monkeypatch.setenv(BACKUP_GIT_REMOTE_ENV, str(local_remote(tmp_path)))
        monkeypatch.setenv("AERO_BOT_BACKUP_WORKTREE_PATH", str(tmp_path / "worktree"))
        monkeypatch.setattr(
            "aero_bot.audit_backup.Settings",
            lambda: Settings.model_construct(audit_database_path=tmp_path / "audit.sqlite3"),
        )
        assert main([]) == 2
        assert "does not verify" in capsys.readouterr().err


class TestSystemdUnits:
    """The backup timer contract."""

    def test_the_timer_runs_daily_with_persistence(self) -> None:
        """One bundle per day, catching up after downtime."""
        timer = Path("deploy/systemd/aero-bot-audit-backup.timer").read_text(encoding="utf-8")
        assert "OnCalendar=daily" in timer
        assert "Persistent=true" in timer
        assert "Unit=aero-bot-audit-backup.service" in timer

    def test_the_service_is_a_hardened_oneshot_with_sealed_env(self) -> None:
        """The backup runs as the dedicated user over a 0600 env file."""
        service = Path("deploy/systemd/aero-bot-audit-backup.service").read_text(encoding="utf-8")
        assert "Type=oneshot" in service
        assert "User=aero-bot" in service
        assert "EnvironmentFile=/etc/aero-bot/backup.env" in service
        assert "NoNewPrivileges=true" in service
        assert "aero-bot-audit-backup --json" in service
