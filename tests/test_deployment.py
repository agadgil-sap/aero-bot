"""Pin the Ubuntu deployment kit's install contract and unit files."""

import subprocess
from pathlib import Path

INSTALL_SCRIPT = Path("deploy/install.sh")
CONVERGE_SCRIPT = Path("deploy/converge-check.sh")
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
        assert 'chmod 0640 "${CONFIG_DIR}/advisor.env"' in text
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
        assert '[[ ! -f "${CONFIG_DIR}/advisor.env" ]]' in text
        assert '[[ ! -f "${CONFIG_DIR}/backup.env" ]]' in text

    def test_advisor_env_template_stays_dark_until_sealed(self) -> None:
        """The advisor template ships commented plane values and hardening knobs."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "#AERO_BOT_ADVISOR_URL=http://100.106.111.37:11435" in text
        assert "#AERO_BOT_ADVISOR_FALLBACK_URL=http://100.106.111.37:11434" in text
        assert "#AERO_BOT_ADVISOR_MODEL=" in text
        assert "#AERO_BOT_ADVISOR_TIMEOUT_SECONDS=90" in text
        assert "#AERO_BOT_ADVISOR_DISABLE_THINKING=1" in text
        assert "#AERO_BOT_ADVISOR_JSON_MODE=1" in text

    def test_advisor_env_template_documents_the_hardened_posture(self) -> None:
        """The template's comments state the pin, the fallback, and the budget."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "OLLAMA_KEEP_ALIVE=-1" in text
        assert "measured warm brief latency" in text
        assert "availability outranks deliberation" in text
        assert "eliminated at the source" in text

    def test_advisor_env_template_names_the_teaching_seal(self) -> None:
        """The upgrade loop's seal ships commented with its bounds stated."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "#AERO_BOT_ADVISOR_TEACHING_FILE=/etc/aero-bot/advisor-teaching.txt" in text
        assert "never replaces it" in text
        assert "fails closed" in text

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

    def test_the_installer_stamps_the_deployed_commit_itself(self) -> None:
        """The deployment marker names the archived commit and cannot go stale."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        archive_position = text.index("archive --format=tar HEAD")
        stamp_position = text.index('rev-parse HEAD >"${APP_DIR}/DEPLOYED_COMMIT"')
        assert archive_position < stamp_position
        # The rev-parse carries the same scoped ownership override as the archive.
        assert (
            'git -c safe.directory="${REPO_ROOT}" -C "${REPO_ROOT}" \\\n'
            "        rev-parse HEAD" in text
        )
        # The tarball fallback branch has no commit to name and says so.
        assert "unversioned tarball tree installed" in text

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


class TestConvergeCheckScript:
    """The convergence check's compare-by-content contract."""

    def test_the_script_parses_as_strict_bash(self) -> None:
        """Bash's own parser accepts the script under strict settings."""
        completed = subprocess.run(  # noqa: S603 - a syntax check of our own script
            ["/bin/bash", "-n", str(CONVERGE_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    def test_both_sides_hash_the_same_content_inventory(self) -> None:
        """Local git-archive and remote find hash identical exclusions."""
        text = CONVERGE_SCRIPT.read_text(encoding="utf-8")
        local_find = text.index("find . -type f")
        remote_find = text.index("cd ${REMOTE_APP} && find . -type f")
        for exclusion in (
            "'./.venv/*'",
            "'*__pycache__*'",
            "'*.pyc'",
            "'*.pytest_cache*'",
            "'*.mypy_cache*'",
            "'*.ruff_cache*'",
            "DEPLOYED_COMMIT",
        ):
            assert exclusion in text, exclusion
        # Every exclusion the local side applies appears on the remote side too.
        local_block = text[local_find:remote_find]
        remote_block = text[remote_find:]
        for token in (
            ".venv",
            "__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "DEPLOYED_COMMIT",
        ):
            assert token in local_block and token in remote_block, token

    def test_the_remote_sweep_runs_elevated_for_the_0750_tree(self) -> None:
        """The whole remote pipeline is elevated; the SSH user cannot traverse."""
        text = CONVERGE_SCRIPT.read_text(encoding="utf-8")
        assert "sudo sh -c 'cd ${REMOTE_APP}" in text

    def test_the_local_side_hashes_the_git_archive_not_the_worktree(self) -> None:
        """Uncommitted working-tree edits can never read as converged."""
        text = CONVERGE_SCRIPT.read_text(encoding="utf-8")
        assert "archive --format=tar HEAD" in text
        assert text.index('git -C "${REPO_ROOT}" archive') < text.index("find . -type f")

    def test_snapshot_extras_are_reported_not_treated_as_divergence(self) -> None:
        """Known .pre-* rollback snapshots explain themselves in the verdict."""
        text = CONVERGE_SCRIPT.read_text(encoding="utf-8")
        assert "known .pre-* rollback snapshot" in text
        assert "\\.pre-" in text or ".pre-" in text

    def test_divergence_exits_nonzero_with_the_diff_shown(self) -> None:
        """A real divergence is a failure, not a warning."""
        text = CONVERGE_SCRIPT.read_text(encoding="utf-8")
        assert "VERDICT: DIVERGED" in text
        assert "exit 1" in text


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

    def test_the_dashboard_reads_the_sealed_audit_store_path(self) -> None:
        """The dashboard verifies the production chain, never its own empty store.

        The gnhf 34 fix: without the sealed environment file the service
        fell back to Settings' platform default and its audit-health
        endpoint reported an initialized empty store (record_count 0)
        while the production chain held nineteen thousand records.
        """
        service = Path("deploy/systemd/aero-bot-dashboard.service").read_text(encoding="utf-8")
        assert "EnvironmentFile=/etc/aero-bot/dashboard.env" in service
        assert "ConditionPathExists=/etc/aero-bot/dashboard.env" in service
        installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "${CONFIG_DIR}/dashboard.env" in installer
        # The template pins the production store path before any other knob.
        heredoc = installer.index("${CONFIG_DIR}/dashboard.env")
        template_start = installer.index("<<'EOF'", heredoc) + len("<<'EOF'")
        template_end = installer.index("\nEOF\n", template_start)
        template = installer[template_start:template_end]
        assert "AERO_BOT_AUDIT_DATABASE_PATH=/var/lib/aero-bot/audit.sqlite3" in template

    def test_the_dashboard_env_template_is_sealed_and_idempotent(self) -> None:
        """The seal lands root:service 0640 and reinstalls never overwrite it."""
        installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'chown root:"${SERVICE_USER}" "${CONFIG_DIR}/dashboard.env"' in installer
        assert 'chmod 0640 "${CONFIG_DIR}/dashboard.env"' in installer
        assert '[[ ! -f "${CONFIG_DIR}/dashboard.env" ]]' in installer

    def test_the_installer_restarts_active_long_running_services(self) -> None:
        """A deploy never leaves yesterday's process serving the old environment.

        The gnhf 35 postmortem: the gnhf 34 deploy refreshed the dashboard
        unit and code, but nothing restarted the already-running service,
        so the process started two days earlier kept serving with no sealed
        environment - its audit-health endpoint reported an empty store
        until a manual restart. try-restart restarts only active units, so
        a stopped service still stays stopped and the installer still
        never arms anything.
        """
        installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "systemctl try-restart aero-bot-dashboard.service" in installer
        assert "'aero-bot-watchtower@*.service'" in installer
        # The restart is a refresh of already-active units only: no start,
        # no enable, no restart of the oneshot services whose every run
        # re-executes with the current environment anyway.
        executed = [
            line.strip() for line in installer.splitlines() if line.strip().startswith("systemctl ")
        ]
        assert all(
            command
            in (
                "systemctl daemon-reload",
                "systemctl try-restart aero-bot-dashboard.service 'aero-bot-watchtower@*.service'",
            )
            for command in executed
        )

    def test_the_upgrade_path_documents_the_restart(self) -> None:
        """The day-two runbook names the restart-on-upgrade behavior."""
        doc = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        assert "try-restart" in doc
        assert "yesterday's process serving the old environment" in doc

    def test_every_unit_in_the_kit_installs_under_system(self) -> None:
        """The installer copies exactly the kit's units."""
        units = sorted(path.name for path in Path("deploy/systemd").iterdir())
        assert units == [
            "aero-bot-advisor.service",
            "aero-bot-advisor.timer",
            "aero-bot-audit-backup.service",
            "aero-bot-audit-backup.timer",
            "aero-bot-cycle@.service",
            "aero-bot-cycle@.timer",
            "aero-bot-daily-report.service",
            "aero-bot-daily-report.timer",
            "aero-bot-dashboard.service",
            "aero-bot-watchtower@.service",
        ]


class TestCycleUnit:
    """The scheduled cycle service: one reconcile-decide-act pass per tick."""

    def test_the_unit_selects_the_retain_reward_posture(self) -> None:
        """The shipped invocation passes the retain selector explicitly.

        The command-line selector wins over the sealed environment, so the
        scheduled unit retains claimed AERO regardless of any sealed
        AERO_BOT_CYCLE_REWARD_POSTURE value; manual CLI runs that omit the
        flag keep the sealed or default posture.
        """
        service = Path("deploy/systemd/aero-bot-cycle@.service").read_text(encoding="utf-8")
        assert (
            "ExecStart=/opt/aero-bot/.venv/bin/aero-bot-cycle --symbol %i "
            "--reward-posture retain --json" in service
        )
        # The selector rides the command line only: the unit never states
        # the posture through its own Environment= lines, so precedence
        # rests on the parser's flag-over-environment rule alone.
        assert "Environment=AERO_BOT_CYCLE_REWARD_POSTURE" not in service


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


class TestAdvisorUnit:
    """The shadow advisor is a dark, bounded, advisory-only oneshot."""

    def test_the_advisor_runs_one_bounded_pass_under_the_sealed_overlay(self) -> None:
        """One advisor invocation over both env files, gated on the overlay."""
        service = Path("deploy/systemd/aero-bot-advisor.service").read_text(encoding="utf-8")
        assert "Type=oneshot" in service
        assert "EnvironmentFile=/etc/aero-bot/cycle.env" in service
        assert "EnvironmentFile=/etc/aero-bot/advisor.env" in service
        assert "ConditionPathExists=/etc/aero-bot/advisor.env" in service
        assert "ExecStart=/opt/aero-bot/.venv/bin/aero-bot-advisor --max-runs 1" in service
        assert "TimeoutStartSec=300" in service
        assert "Restart=no" in service

    def test_the_advisor_is_hardened_like_every_oneshot(self) -> None:
        """The advisor carries the kit's standard hardening posture."""
        service = Path("deploy/systemd/aero-bot-advisor.service").read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" in service
        assert "ProtectSystem=strict" in service
        assert "ProtectHome=true" in service
        assert "ReadWritePaths=/var/lib/aero-bot" in service
        assert "StateDirectoryMode=0700" in service

    def test_the_timer_fires_one_pass_every_thirty_minutes(self) -> None:
        """A bounded advisory cadence catches up after downtime."""
        timer = Path("deploy/systemd/aero-bot-advisor.timer").read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* *:00/30" in timer
        assert "Persistent=true" in timer
        assert "Unit=aero-bot-advisor.service" in timer


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

    def test_the_dashboard_env_file_is_documented(self) -> None:
        """The installer-written dashboard seal and its purpose are stated."""
        text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        assert "/etc/aero-bot/dashboard.env" in text
        assert "audit-health" in text

    def test_phase_two_boundaries_are_stated(self) -> None:
        """The captain-dependent steps are named as captain-dependent."""
        text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
        assert "Seal the secrets" in text
        assert "captain present" in text


MAC_INSTALL_SCRIPT = Path("deploy/launchd/install-mac.sh")
MAC_RUN_SCRIPT = Path("deploy/launchd/teacher-run.sh")
MAC_STUDENT_OLLAMA_SCRIPT = Path("deploy/launchd/student-ollama.sh")


class TestMacTeacherKit:
    """The macOS launchd kit mirrors the Ubuntu posture: generate, never arm."""

    def test_the_installer_parses_as_strict_bash(self) -> None:
        """Bash's own parser accepts the installer under strict settings."""
        completed = subprocess.run(  # noqa: S603 - a syntax check of our own script
            ["/bin/bash", "-n", str(MAC_INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    def test_the_run_script_parses_as_strict_bash(self) -> None:
        """Bash's own parser accepts the wrapper under strict settings."""
        completed = subprocess.run(  # noqa: S603 - a syntax check of our own script
            ["/bin/bash", "-n", str(MAC_RUN_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    def test_the_installer_never_loads_an_agent(self) -> None:
        """Arming stays the operator's explicit act; only hints are printed."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "NEVER loads" in text
        assert "launchctl bootstrap" in text
        assert "launchctl kickstart" in text
        executable = [
            line.strip()
            for line in text.splitlines()
            if "launchctl" in line and not line.lstrip().startswith(("#", "echo"))
        ]
        assert executable == []

    def test_the_installer_generates_all_six_jobs(self) -> None:
        """Tactical, daily, news, hindsight, upgrade, and risk-manager each get their own job."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        for label in (
            "teacher-tactical",
            "teacher-daily",
            "teacher-news",
            "teacher-hindsight",
            "teacher-upgrade",
            "teacher-risk-manager",
        ):
            assert f"com.aero-bot.{label}" in text, label

    def test_the_student_plane_wrapper_pins_the_model_resident(self) -> None:
        """The dedicated plane's wrapper carries the keep-alive pin and one slot."""
        text = MAC_STUDENT_OLLAMA_SCRIPT.read_text(encoding="utf-8")
        assert "export OLLAMA_KEEP_ALIVE=-1" in text
        assert "export OLLAMA_NUM_PARALLEL=1" in text
        # The bind defaults to the Mac's tailnet address with a distinct
        # port, overridable by the generated agent.
        assert "AERO_BOT_STUDENT_OLLAMA_HOST:-100.106.111.37:11435" in text
        # The binary prefers the app bundle: older Homebrew builds break
        # JSON mode with thinking off (verified live, 0.32.15 versus 0.34.3).
        assert '"/Applications/Ollama.app/Contents/Resources/ollama"' in text
        assert text.index("/Applications/Ollama.app") < text.index("command -v ollama")

    def test_the_student_plane_is_generated_as_an_always_alive_server(self) -> None:
        """The plane starts at login and launchd keeps it alive, unlike the passes."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "com.aero-bot.student-ollama.plist" in text
        assert "<key>RunAtLoad</key>" in text
        assert "<key>KeepAlive</key>" in text
        # The bind is stamped into the agent so a reboot restores the plane
        # without the GUI instance's fragile runtime OLLAMA_HOST state.
        assert "<key>AERO_BOT_STUDENT_OLLAMA_HOST</key>" in text
        assert "<string>${STUDENT_OLLAMA_BIND}</string>" in text

    def test_the_student_plane_wrapper_is_copied_beside_the_teacher_wrapper(self) -> None:
        """The installer ships the plane's wrapper into the state directory."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'cp "$STUDENT_OLLAMA_SRC" "$STATE_STUDENT_OLLAMA"' in text

    def test_the_student_plane_arm_hint_names_both_seal_variables(self) -> None:
        """The printed instructions wire the VM seat to the dedicated plane."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "AERO_BOT_ADVISOR_URL=http://$STUDENT_OLLAMA_HOST" in text
        assert "AERO_BOT_ADVISOR_FALLBACK_URL=http://100.106.111.37:11434" in text

    def test_the_cadences_are_thirty_minutes_daily_and_morning_news(self) -> None:
        """Tactical runs each half hour; daily and news ride calendar times."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "<integer>1800</integer>" in text
        assert "<key>Hour</key>\n        <integer>9</integer>" in text
        assert "<key>Hour</key>\n        <integer>7</integer>" in text

    def test_the_hindsight_scorer_runs_after_the_daily_stream_drains(self) -> None:
        """The daily report scores at 09:50, past the daily stream's ceiling."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "<key>Minute</key>\n        <integer>50</integer>" in text
        assert text.index("<integer>30</integer>") < text.index("<integer>50</integer>")

    def test_the_upgrade_proposer_runs_after_the_scorer_rewrites(self) -> None:
        """Proposals land at 10:10, after the 09:50 hindsight report."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        upgrade_block = (
            "<key>Hour</key>\n"
            "        <integer>10</integer>\n"
            "        <key>Minute</key>\n"
            "        <integer>10</integer>"
        )
        assert upgrade_block in text
        assert text.index("<integer>50</integer>") < text.index(upgrade_block)

    def test_the_risk_manager_audits_between_the_scorer_and_the_proposer(self) -> None:
        """The counterparty audit lands at 10:00: after 09:50, before 10:10."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        risk_block = (
            "<key>Hour</key>\n"
            "        <integer>10</integer>\n"
            "        <key>Minute</key>\n"
            "        <integer>0</integer>"
        )
        assert risk_block in text
        # The generation order mirrors the morning cadence: scorer,
        # then the audit it feeds, then the proposer.
        assert (
            text.index('generate_plist "com.aero-bot.teacher-hindsight"')
            < text.index('generate_plist "com.aero-bot.teacher-upgrade"')
            < text.index('generate_plist "com.aero-bot.teacher-risk-manager"')
        )

    def test_the_jobs_run_as_background_priority(self) -> None:
        """Advisory passes never compete with interactive work."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "<string>Background</string>" in text
        assert "<integer>10</integer>" in text

    def test_the_run_script_resolves_the_repo_and_falls_back_to_uv(self) -> None:
        """The wrapper works from launchd's bare environment."""
        text = MAC_RUN_SCRIPT.read_text(encoding="utf-8")
        assert 'REPO_ROOT="${AERO_BOT_TEACHER_REPO:-' in text
        assert 'cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd' in text
        assert "$HOME/.local/bin/uv" in text
        assert 'run aero-bot-teacher "$STREAM"' in text

    def test_the_run_script_serves_the_hindsight_scorer(self) -> None:
        """The hindsight argument runs the scorer, not a teacher stream."""
        text = MAC_RUN_SCRIPT.read_text(encoding="utf-8")
        assert "<tactical|daily|news|hindsight|upgrade|risk-manager>" in text
        assert '"$STREAM" == "hindsight"' in text
        assert 'run aero-bot-hindsight >>"$LOG_DIR/${STREAM}.log"' in text

    def test_the_run_script_serves_the_upgrade_proposer(self) -> None:
        """The upgrade argument runs the proposer, not a teacher stream."""
        text = MAC_RUN_SCRIPT.read_text(encoding="utf-8")
        assert '"$STREAM" == "upgrade"' in text
        assert 'run aero-bot-upgrade --email >>"$LOG_DIR/${STREAM}.log"' in text

    def test_the_run_script_serves_the_risk_manager(self) -> None:
        """The risk-manager argument runs the audit, not a teacher stream."""
        text = MAC_RUN_SCRIPT.read_text(encoding="utf-8")
        assert "<tactical|daily|news|hindsight|upgrade|risk-manager>" in text
        assert '"$STREAM" == "risk-manager"' in text
        assert 'run aero-bot-risk-manager >>"$LOG_DIR/${STREAM}.log"' in text

    def test_the_run_script_streams_into_the_state_logs(self) -> None:
        """Each run appends to the stream's log inside the state directory."""
        text = MAC_RUN_SCRIPT.read_text(encoding="utf-8")
        assert '>>"$LOG_DIR/${STREAM}.log" 2>&1' in text
        assert "AERO_BOT_TEACHER_STATE_DIR" in text

    def test_the_installer_runs_outside_the_tcc_protected_folders(self) -> None:
        """Everything the agents touch lives outside ~/Documents and friends."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "TCC" in text and "Operation not permitted" in text
        assert 'WORKTREE="${AERO_BOT_TEACHER_REPO:-$STATE_DIR/repo}"' in text
        assert 'git -C "$REPO_ROOT" worktree add --detach --quiet "$WORKTREE" HEAD' in text

    def test_the_worktree_is_stateless_and_recreated_from_head(self) -> None:
        """The worktree carries no state; every install resets it to HEAD."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'worktree remove --force "$WORKTREE"' in text
        assert "worktree prune" in text
        assert "stateless" in text

    def test_the_agents_run_the_copied_wrapper_over_the_worktree(self) -> None:
        """The plist points at the state-dir wrapper and exports the worktree."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'cp "$WRAPPER_SRC" "$STATE_WRAPPER"' in text
        assert 'wrapper="$(xml_escape "$STATE_WRAPPER")"' in text
        assert "<string>${wrapper}</string>" in text
        assert 'worktree="$(xml_escape "$WORKTREE")"' in text
        assert "<key>AERO_BOT_TEACHER_REPO</key>" in text
        assert "<string>${worktree}</string>" in text

    def test_the_agents_carry_a_path_that_finds_the_teacher_clis(self) -> None:
        """The bare launchd PATH lacks ~/.local/bin and Homebrew; the plist bakes both."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "<string>${path_value}</string>" in text
        assert "teacher CLIs (claude, codex, uv) live there" in text

    def test_the_installer_never_rips_the_worktree_from_a_live_pass(self) -> None:
        """Reinstalling mid-run is refused; a live pass keeps its code."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'pgrep -f "aero-bot-(teacher|hindsight|upgrade|risk-manager)"' in text
        assert "wait for it to finish before reinstalling" in text

    def test_the_installer_refuses_paths_it_does_not_manage(self) -> None:
        """A foreign AERO_BOT_TEACHER_REPO directory is refused, not removed."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert '[[ -e "$WORKTREE" && ! -f "$WORKTREE/.git" ]]' in text
        assert "not a worktree this installer manages" in text

    def test_the_plist_paths_are_xml_escaped(self) -> None:
        """Metacharacters in configured paths cannot corrupt the plist."""
        text = MAC_INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert 'value="${value//&/&amp;}"' in text
        assert 'value="${value//</&lt;}"' in text
        assert 'value="${value//>/&gt;}"' in text


SEAL_REPAIR_SCRIPT = Path("deploy/seal-repair.sh")


class TestSealRepairScript:
    """The guarded idempotent seal repair, exercised behaviorally off-box."""

    def _run(
        self,
        mode: str,
        config_dir: Path,
        system_ctl: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Run the script against a fixture seal directory."""
        return subprocess.run(  # noqa: S603 - our own script, fixture-scoped
            [
                "/bin/bash",
                str(SEAL_REPAIR_SCRIPT),
                mode,
            ],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "AERO_BOT_CONFIG_DIR": str(config_dir),
                "AERO_BOT_SYSTEMCTL": str(system_ctl),
            },
        )

    def _fixture_config(
        self,
        tmp_path: Path,
        *,
        advisor_url: str | None,
        fallback_url: str | None,
        provider: str | None,
        resend_key: str | None = "re_sealed_key",
    ) -> Path:
        """Build one drifted seal pair matching the 2026-09-28 defect."""
        config_dir = tmp_path / "aero-bot"
        config_dir.mkdir()
        advisor_lines = ["# advisor seal"]
        if advisor_url:
            advisor_lines.append(f"AERO_BOT_ADVISOR_URL={advisor_url}")
        if fallback_url:
            advisor_lines.append(f"AERO_BOT_ADVISOR_FALLBACK_URL={fallback_url}")
        (config_dir / "advisor.env").write_text("\n".join(advisor_lines) + "\n", encoding="utf-8")
        cycle_lines = ["# cycle seal", "AERO_BOT_SAFE_ADDRESS=0x0"]
        if provider:
            cycle_lines.append(f"AERO_BOT_ALERT_PROVIDER={provider}")
        if resend_key:
            cycle_lines.append(f"AERO_BOT_ALERT_RESEND_API_KEY={resend_key}")
            cycle_lines.append("AERO_BOT_ALERT_FROM=on@resend.dev")
            cycle_lines.append("AERO_BOT_ALERT_TO=capn@example.com")
        (config_dir / "cycle.env").write_text("\n".join(cycle_lines) + "\n", encoding="utf-8")
        return config_dir

    def _recording_systemctl(self, tmp_path: Path) -> Path:
        """A fake systemctl that records its arguments."""
        recorder = tmp_path / "systemctl-fixture"
        recorder.write_text(
            f'#!/bin/bash\nprintf "%s\\n" "$@" >>"{tmp_path / "systemctl.log"}"\n',
            encoding="utf-8",
        )
        recorder.chmod(0o755)
        return recorder

    def test_the_script_parses_as_strict_bash(self) -> None:
        """Bash's own parser accepts the repair script under strict settings."""
        completed = subprocess.run(  # noqa: S603 - a syntax check of our own script
            ["/bin/bash", "-n", str(SEAL_REPAIR_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    def test_check_reports_the_drift_without_touching_anything(self, tmp_path: Path) -> None:
        """The 2026-09-28 drift is visible and check mode is read-only."""
        config_dir = self._fixture_config(
            tmp_path,
            advisor_url="http://100.106.111.37:11434",
            fallback_url=None,
            provider="none",
        )
        before = {
            name: (config_dir / name).read_text(encoding="utf-8")
            for name in ("advisor.env", "cycle.env")
        }
        completed = self._run("--check", config_dir, self._recording_systemctl(tmp_path))
        assert completed.returncode == 3
        assert "DRIFT" in completed.stdout
        assert "11435" in completed.stdout
        for name, text in before.items():
            assert (config_dir / name).read_text(encoding="utf-8") == text
        assert not (tmp_path / "systemctl.log").exists()

    def test_check_passes_a_converged_seal(self, tmp_path: Path) -> None:
        """A converged seal reports no drift and exits zero."""
        config_dir = self._fixture_config(
            tmp_path,
            advisor_url="http://100.106.111.37:11435",
            fallback_url="http://100.106.111.37:11434",
            provider="resend",
        )
        completed = self._run("--check", config_dir, self._recording_systemctl(tmp_path))
        assert completed.returncode == 0
        assert "no drift" in completed.stdout

    def test_apply_repairs_the_drift_idempotently_and_restarts_the_advisor(
        self, tmp_path: Path
    ) -> None:
        """Apply repoints the plane, enables alerts, restarts once; re-run is a no-op."""
        config_dir = self._fixture_config(
            tmp_path,
            advisor_url="http://100.106.111.37:11434",
            fallback_url=None,
            provider="none",
        )
        system_ctl = self._recording_systemctl(tmp_path)
        completed = self._run("--apply", config_dir, system_ctl)
        assert completed.returncode == 0
        advisor = (config_dir / "advisor.env").read_text(encoding="utf-8")
        assert "AERO_BOT_ADVISOR_URL=http://100.106.111.37:11435" in advisor
        assert "AERO_BOT_ADVISOR_FALLBACK_URL=http://100.106.111.37:11434" in advisor
        # The stale shared-plane assignment is gone, not duplicated.
        assert advisor.count("AERO_BOT_ADVISOR_URL=") == 1
        cycle = (config_dir / "cycle.env").read_text(encoding="utf-8")
        assert "AERO_BOT_ALERT_PROVIDER=resend" in cycle
        assert cycle.count("AERO_BOT_ALERT_PROVIDER=") == 1
        restarts = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
        assert restarts == ["try-restart", "aero-bot-advisor.service"]
        backups = list(config_dir.glob("*.bak-*"))
        assert len(backups) == 2
        # No stray sed suffix files or leftover temporaries: the seal
        # directory carries exactly the two seals and their backups.
        assert sorted(path.name for path in config_dir.iterdir()) == sorted(
            [
                "advisor.env",
                "cycle.env",
                *(path.name for path in backups),
            ]
        )
        # The second apply converges without a restart or new backups.
        again = self._run("--apply", config_dir, system_ctl)
        assert again.returncode == 0
        assert "already converged" in again.stdout
        assert len(list(config_dir.glob("*.bak-*"))) == 2

    def test_apply_refuses_to_enable_alerts_without_sealed_credentials(
        self, tmp_path: Path
    ) -> None:
        """A missing Resend key is a refusal, never a guessed enable."""
        config_dir = self._fixture_config(
            tmp_path,
            advisor_url="http://100.106.111.37:11435",
            fallback_url="http://100.106.111.37:11434",
            provider=None,
            resend_key=None,
        )
        system_ctl = self._recording_systemctl(tmp_path)
        completed = self._run("--apply", config_dir, system_ctl)
        assert completed.returncode == 4
        assert "REFUSED" in completed.stdout
        assert "AERO_BOT_ALERT_PROVIDER" not in (config_dir / "cycle.env").read_text(
            encoding="utf-8"
        )
        # A converged advisor seal means no restart on a refused apply.
        assert not (tmp_path / "systemctl.log").exists()

    def test_a_foreign_provider_is_a_manual_decision_never_a_rewrite(self, tmp_path: Path) -> None:
        """An smtp provider line is reported, not stomped."""
        config_dir = self._fixture_config(
            tmp_path,
            advisor_url="http://100.106.111.37:11435",
            fallback_url="http://100.106.111.37:11434",
            provider="smtp",
        )
        system_ctl = self._recording_systemctl(tmp_path)
        completed = self._run("--apply", config_dir, system_ctl)
        assert completed.returncode == 4
        assert "MANUAL" in completed.stdout
        assert "AERO_BOT_ALERT_PROVIDER=smtp" in (config_dir / "cycle.env").read_text(
            encoding="utf-8"
        )

    def test_the_root_guard_protects_the_real_seal_directory(self) -> None:
        """Without root, the real /etc/aero-bot is refused even in check mode."""
        completed = subprocess.run(  # noqa: S603 - our own script, guard-probed
            ["/bin/bash", str(SEAL_REPAIR_SCRIPT), "--check"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        assert completed.returncode == 1
        assert "run as root" in completed.stderr

    def test_bare_invocations_are_refused(self, tmp_path: Path) -> None:
        """No mode means no action; misuse never mutates."""
        config_dir = self._fixture_config(
            tmp_path,
            advisor_url="http://100.106.111.37:11435",
            fallback_url="http://100.106.111.37:11434",
            provider="resend",
        )
        completed = self._run("--nonsense", config_dir, self._recording_systemctl(tmp_path))
        assert completed.returncode == 1
        assert "usage" in completed.stderr


class TestSealDriftWarning:
    """The installer names drift it refuses to fix, every deploy."""

    def test_the_installer_warns_about_a_drifted_advisor_seal(self) -> None:
        """The dedicated plane and fallback are checked, loudly."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "seal drift" in text
        assert (
            "advisor.env primary plane is '$SEALED_URL', not the documented "
            "dedicated plane http://100.106.111.37:11435" in text
        )
        assert "no effective AERO_BOT_ADVISOR_FALLBACK_URL" in text

    def test_the_installer_warns_about_silent_alerts_with_sealed_credentials(self) -> None:
        """Provider none while a Resend key is sealed is named every deploy."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        assert "alerts compute and never email" in text
        assert "seal-repair.sh --check" in text

    def test_the_warning_never_mutates_a_seal(self) -> None:
        """Drift detection is read-only: no writes into the config dir."""
        text = INSTALL_SCRIPT.read_text(encoding="utf-8")
        drift_block = text[text.index("seal drift") : text.index("systemd units")]
        assert "seal_effective_value" in drift_block
        assert "sed -i" not in drift_block
