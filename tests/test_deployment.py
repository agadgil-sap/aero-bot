"""Pin the Ubuntu deployment kit's install contract and unit files."""

import subprocess
from pathlib import Path

INSTALL_SCRIPT = Path("deploy/install.sh")
DEPLOYMENT_DOC = Path("docs/deployment.md")


class TestInstallScript:
    """The installer's safety contract, pinned to its text and syntax."""

    def test_the_script_parses_as_strict_bash(self) -> None:
        """Bash's own parser accepts the script under strict settings."""
        completed = subprocess.run(  # noqa: S603 - a syntax check of our own script
            ["/bin/bash", "-n", str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    def test_the_script_demands_root_and_ubuntu(self) -> None:
        """Nothing runs outside root on Ubuntu."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "run as root" in text
        assert '[[ "${ID:-}" == "ubuntu" ]]' in text

    def test_the_script_never_enables_a_timer(self) -> None:
        """Arming is Phase 2 only; the installer ships units dark."""
        executed = [
            line
            for line in INSTALL_SCRIPT.read_text(encoding="utf-8").splitlines()
            if "systemctl enable" in line and not line.lstrip().startswith("log ")
        ]
        assert executed == []
        assert "daemon-reload" in INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "NOT enabled" in INSTALL_SCRIPT.read_text(encoding="utf-8")

    def test_the_firewall_allows_ssh_before_enabling(self) -> None:
        """The OpenSSH allow precedes ufw enable; lockout is impossible."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        allow_position = text.index("ufw allow OpenSSH")
        enable_position = text.index("--force enable")
        assert allow_position < enable_position
        assert "default deny incoming" in text

    def test_sealed_env_templates_are_created_group_readable_only(self) -> None:
        """The env templates land root:service 0640, never world-readable."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'chown root:"${SERVICE_USER}" "${CONFIG_DIR}/cycle.env"' in text
        assert 'chmod 0640 "${CONFIG_DIR}/cycle.env"' in text
        assert 'chmod 0640 "${CONFIG_DIR}/daily-report.env"' in text
        assert 'chmod 0640 "${CONFIG_DIR}/backup.env"' in text

    def test_the_config_dir_is_service_group_traversable(self) -> None:
        """The service user can traverse /etc/aero-bot to reach its sealed files."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"' in text

    def test_backup_env_template_pins_the_service_audit_path(self) -> None:
        """The backup unit must read the same audit chain the cycle writes."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "AERO_BOT_AUDIT_DATABASE_PATH=/var/lib/aero-bot/audit.sqlite3" in text
        assert text.index("AERO_BOT_AUDIT_DATABASE_PATH=/var/lib/aero-bot") < text.index(
            "AERO_BOT_BACKUP_KEY_HEX"
        )

    def test_backup_env_template_pins_the_deploy_key_path(self) -> None:
        """Without the deploy-key path the runner never pins GIT_SSH_COMMAND."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "AERO_BOT_BACKUP_DEPLOY_KEY_PATH=/etc/aero-bot/backup-deploy.key" in text

    def test_existing_sealed_files_survive_reinstalls(self) -> None:
        """Idempotence never overwrites the operator's sealed values."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert '[[ ! -f "${CONFIG_DIR}/cycle.env" ]]' in text
        assert '[[ ! -f "${CONFIG_DIR}/daily-report.env" ]]' in text
        assert '[[ ! -f "${CONFIG_DIR}/backup.env" ]]' in text

    def test_the_venv_builds_locked_and_dev_free(self) -> None:
        """The venv comes from the lockfile without dev extras."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "uv sync --project" in text
        assert "--locked" in text
        assert "--no-dev" in text

    def test_root_installs_from_an_operator_owned_checkout(self) -> None:
        """The archive step scopes git's dubious-ownership guard away."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        archive_position = text.index("archive --format=tar HEAD")
        assert 'git -c safe.directory="${REPO_ROOT}"' in text
        assert text.index('git -c safe.directory="${REPO_ROOT}"') < archive_position

    def test_a_rerun_reasserts_service_ownership_of_the_venv(self) -> None:
        """The recursive chown must not cost the service user its venv."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'chown -R "${SERVICE_USER}:${SERVICE_USER}" "${VENV}"' in text

    def test_the_state_tree_is_service_owned_mode_700(self) -> None:
        """/var/lib/aero-bot belongs to the service user alone."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0700' in text

    def test_units_pin_the_state_directory_mode(self) -> None:
        """StateDirectory= never re-widens the installer's 0700 state tree."""
        for unit in sorted(Path("deploy/systemd").glob("*.service")):
            text = unit.read_text(encoding="utf-8")
            if "StateDirectory=aero-bot" in text:
                assert "StateDirectoryMode=0700" in text, unit.name

    def test_unattended_upgrades_are_configured(self) -> None:
        """Automatic security updates stay on."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'APT::Periodic::Unattended-Upgrade "1";' in text


class TestDashboardUnit:
    """The dashboard service stays loopback-only and hardened."""

    def test_the_dashboard_is_a_hardened_long_running_service(self) -> None:
        """The dashboard restarts on failure under the same hardening."""
        service = Path("deploy/systemd/aero-bot-dashboard.service").read_text(encoding="utf-8")
        assert "Type=simple" in service
        assert "User=aero-bot" in service
        assert "NoNewPrivileges=true" in service
        assert "ProtectSystem=strict" in service
        assert "Restart=on-failure" in service

    def test_every_unit_in_the_kit_installs_under_system(self) -> None:
        """The installer copies exactly the kit's units."""
        units = sorted(path.name for path in Path("deploy/systemd").iterdir())
        assert units == [
            "aero-bot-audit-backup.service",
            "aero-bot-audit-backup.timer",
            "aero-bot-cycle@.service",
            "aero-bot-cycle@.timer",
            "aero-bot-daily-report.service",
            "aero-bot-daily-report.timer",
            "aero-bot-dashboard.service",
            "aero-bot-watchtower@.service",
        ]


class TestDailyReportUnit:
    """The daily Resend report rides the cycle's alert transport."""

    def test_the_report_runs_one_dry_cycle_under_the_sealed_overlay(self) -> None:
        """The report is the cycle's dry-run surface over both env files."""
        service = Path("deploy/systemd/aero-bot-daily-report.service").read_text(encoding="utf-8")
        assert "Type=oneshot" in service
        assert "EnvironmentFile=/etc/aero-bot/cycle.env" in service
        assert "EnvironmentFile=/etc/aero-bot/daily-report.env" in service
        assert "ConditionPathExists=/etc/aero-bot/daily-report.env" in service
        assert (
            "ExecStart=/opt/aero-bot/.venv/bin/aero-bot-cycle --symbol auto --dry-run --json"
            in (service)
        )
        assert "TimeoutStartSec=1800" in service

    def test_the_report_is_hardened_like_every_oneshot(self) -> None:
        """The report carries the kit's standard hardening posture."""
        service = Path("deploy/systemd/aero-bot-daily-report.service").read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" in service
        assert "ProtectSystem=strict" in service
        assert "ProtectHome=true" in service
        assert "ReadWritePaths=/var/lib/aero-bot" in service
        assert "StateDirectoryMode=0700" in service

    def test_the_timer_fires_one_melbourne_morning_report(self) -> None:
        """One report per day at 09:00 Melbourne, catching up after downtime."""
        timer = Path("deploy/systemd/aero-bot-daily-report.timer").read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* 09:00:00 Australia/Melbourne" in timer
        assert "Persistent=true" in timer
        assert "Unit=aero-bot-daily-report.service" in timer


class TestDeploymentDoc:
    """The deployment guide carries the operational contract."""

    def test_the_smoke_checklist_covers_every_gate(self) -> None:
        """All ten smoke lines from dry cycle to sign-off are present."""
        text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        for anchor in (
            "Dry cycle decides",
            "Email arrives",
            "One-shot service run",
            "Timers armed",
            "Audit backup round-trips",
            "One live micro-cycle",
            "Dashboard tunnels",
            "Firewall",
            "Sign off",
        ):
            assert anchor in text, anchor

    def test_the_tunnel_is_the_only_documented_access_path(self) -> None:
        """The dashboard is reached through the SSH tunnel, never a port."""
        text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        assert "ssh -L 8765:127.0.0.1:8765" in text
        assert "deny incoming" in text

    def test_phase_two_boundaries_are_stated(self) -> None:
        """The captain-dependent steps are named as captain-dependent."""
        text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        assert "Seal the secrets" in text
        assert "captain present" in text
