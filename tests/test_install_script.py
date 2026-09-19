"""The installer runs as root on a fresh server, so its refusals matter as much as its work.

Argument validation happens before the root check precisely so it can be exercised
here as an ordinary user: every case below must refuse and change nothing.
"""

from __future__ import annotations

import pathlib
import subprocess

INSTALL = pathlib.Path(__file__).resolve().parents[1] / "node" / "install.sh"
GOOD = ["--server", "https://fleet.example.com", "--node", "alice-node",
        "--token", "a" * 64, "--owner", "alice"]


def run(args):
    return subprocess.run(["bash", str(INSTALL), *args], capture_output=True,
                          text=True, timeout=60)


def run_piped(args):
    """The documented form: curl … | bash -s -- …, where $0 is 'bash', not a path."""
    return subprocess.run(["bash", "-s", "--", *args], input=INSTALL.read_text(),
                          capture_output=True, text=True, timeout=60)


def test_script_ships_and_parses():
    assert INSTALL.is_file()
    subprocess.run(["bash", "-n", str(INSTALL)], check=True, timeout=30)


def test_no_arguments_prints_usage():
    r = run([])
    assert r.returncode == 2
    assert "--server" in (r.stdout + r.stderr)


def test_help_explains_the_lockout_guard():
    r = run(["--help"])
    assert r.returncode == 2
    out = r.stdout + r.stderr
    assert "--ssh-key" in out and "locked out" in out.lower()


def test_unknown_argument_is_refused():
    r = run([*GOOD, "--wat"])
    assert r.returncode != 0
    assert "unknown argument" in (r.stdout + r.stderr)


def swap(args, flag, value):
    out = list(args)
    out[out.index(flag) + 1] = value
    return out


def test_rejects_a_server_without_a_scheme():
    r = run(swap(GOOD, "--server", "fleet.example.com"))
    assert r.returncode != 0 and "http" in (r.stdout + r.stderr)


def test_rejects_bad_node_ids():
    for bad in ("Alice-Node", "node id", "-node", "n"):
        r = run(swap(GOOD, "--node", bad))
        assert r.returncode != 0, f"{bad!r} should have been refused"
        assert "--node" in (r.stdout + r.stderr)


def test_rejects_a_token_that_is_not_the_console_value():
    for bad in ("short", "g" * 64, "A" * 64):
        r = run(swap(GOOD, "--token", bad))
        assert r.returncode != 0, f"{bad!r} should have been refused"
        assert "--token" in (r.stdout + r.stderr)


def test_rejects_an_owner_that_could_reach_a_shell():
    for bad in ("alice; rm -rf /", "Alice", "alice owner", "-alice", "a" * 33, "_svc"):
        r = run(swap(GOOD, "--owner", bad))
        assert r.returncode != 0, f"{bad!r} should have been refused"
        assert "--owner" in (r.stdout + r.stderr)


def test_valid_arguments_get_past_validation_and_stop_at_the_root_check():
    """Proves the ordering: good arguments reach the root check, not the other way round."""
    r = run(GOOD)
    assert r.returncode != 0
    assert "root" in (r.stdout + r.stderr).lower()


def test_it_never_hardens_without_a_key_and_says_so():
    text = INSTALL.read_text()
    assert 'SKIPPED: no --ssh-key given' in text
    assert "ssh-keygen -l -f -" in text, "the key must be validated from stdin, not a temp file"
    assert "/tmp/ccfleet-key" not in text, "a predictable root-owned temp file is a symlink attack"


def test_it_refuses_to_claim_success_it_did_not_verify():
    text = INSTALL.read_text()
    assert 'fail2ban-client get sshd maxretry' in text, \
        "hardening must confirm the SSH jail is live, which also proves the service started"
    assert 'if [ -n "$FAILED" ]; then' in text, "service failures must fail the install"
    assert "exit 1" in text


def test_usage_works_when_the_script_is_piped_into_bash():
    """The documented invocation pipes this in, so $0 is 'bash' and usage must not read it."""
    r = run_piped([])
    out = r.stdout + r.stderr
    assert r.returncode == 2
    assert "--server" in out and "--owner" in out
    assert "curl -fsSL" in out, "usage should show the real invocation"


def test_validation_still_refuses_when_piped():
    r = run_piped(swap(GOOD, "--owner", "alice; rm -rf /"))
    assert r.returncode != 0
    assert "--owner" in (r.stdout + r.stderr)


def test_usage_does_not_read_its_own_path():
    assert 'sed -n' not in INSTALL.read_text().split("Required:")[0], \
        "usage must be self-contained; $0 is 'bash' when piped"


def test_sudo_is_installed_before_the_first_call_that_needs_it():
    """A definition is not a use: what matters is the first as_owner CALL."""
    lines = INSTALL.read_text().splitlines()
    install_at = next(i for i, ln in enumerate(lines) if "apt-get install -y -q sudo" in ln)
    call_at = next(i for i, ln in enumerate(lines)
                   if ln.lstrip().startswith(("as_owner ", "as_owner'", 'as_owner"')))
    assert install_at < call_at, (
        f"sudo installed at line {install_at + 1} but first used at {call_at + 1}")


def test_remote_control_is_not_treated_as_a_readiness_gate():
    """It cannot be active before the owner signs in, so requiring it would fail every install."""
    text = INSTALL.read_text()
    check = text[text.index('CHECK="ccfleet-shell.service'):text.index("if [ -n \"$FAILED\" ]")]
    assert "claude-remote-control.service" not in check.split("for unit in")[0], \
        "remote control must not be in the list whose failure fails the install"
    assert '"$RC_ENABLED, starts after sign-in"' in text, \
        "its state should still be reported, and from the verified variable rather than a guess"
    assert "systemctl --user start claude-remote-control.service" in text, \
        "the owner must be told the one command that turns it on"
    assert "retrying every 30 seconds" not in text, \
        "Type=forking around tmux means Restart=on-failure never sees the auth failure"


def test_remote_control_is_enabled_but_not_started_during_install():
    text = INSTALL.read_text()
    assert "user_systemctl enable claude-remote-control.service" in text
    assert "enable --now claude-remote-control.service" not in text, \
        "starting it before a login exists cannot work"


def test_a_failed_remote_control_enable_fails_the_install():
    """The closing message promises it returns after a reboot, so the enable must be verified."""
    text = INSTALL.read_text()
    assert 'RC_ENABLED="$(user_systemctl is-enabled claude-remote-control.service' in text, \
        "the enable has to be read back, not assumed from the exit status we discard"
    assert '[ "$RC_ENABLED" = enabled ] || FAILED=' in text, \
        "an enable that did not take must land in FAILED, not be swallowed by || true"
    # And the promise it backs is still the one being made.
    assert "It is already enabled, so it comes back by itself after a reboot." in text


def test_fail2ban_is_restarted_after_its_jail_is_written():
    """apt starts fail2ban before the jail file exists, and `enable --now` will not restart it."""
    text = INSTALL.read_text()
    write = text.index("/etc/fail2ban/jail.d/sshd.local")
    restart = text.index("systemctl restart fail2ban")
    verify = text.index("fail2ban-client get sshd maxretry")
    assert write < restart < verify, \
        "order must be: write the jail, restart so it is read, then verify it took"
    assert "enable --now fail2ban" not in text, \
        "--now is a no-op on the already-running service and silently skips our config"


def test_fail2ban_verification_checks_the_jail_not_just_the_service():
    """A live node reported fail2ban active while running Debian's defaults, not ours."""
    text = INSTALL.read_text()
    assert 'is-active fail2ban' not in text, \
        "an active service says nothing about whether our jail was loaded"
    assert '"$(fail2ban-client get sshd maxretry 2>/dev/null)" = 4' in text, \
        "verify the value we set, which is what proves the jail file was actually read"


def test_owner_cannot_be_a_system_identity_like_nobody():
    """The owner is given NOPASSWD:ALL, so `nobody` (uid 65534) passing uid>=1000 was a hole."""
    text = INSTALL.read_text()
    assert '[ "$OWNER_UID" -le 59999 ]' in text, \
        "uid must be bounded above; nobody is 65534 and would otherwise be accepted"
    assert '*/nologin | */false | ""' in text, \
        "a service account inside the normal uid range is identified by its login shell"
    # The grant this protects is still the one being made.
    assert "ALL=(ALL) NOPASSWD:ALL" in text


def test_the_uid_bounds_match_the_sudo_grant_they_guard():
    """Order matters: validation has to run before the account is given sudo."""
    text = INSTALL.read_text()
    # Match the grant itself, not the comment above the check that explains it.
    grant = text.index("usermod -aG sudo")
    sudoers = text.index("printf '%s ALL=(ALL) NOPASSWD:ALL")
    assert text.index('[ "$OWNER_UID" -le 59999 ]') < grant < sudoers, \
        "the uid and shell checks must happen before the account is given sudo"


def test_fail2ban_uses_the_journal_so_it_works_without_rsyslog():
    """backend=auto needs /var/log/auth.log; minimal images have none and fail2ban dies."""
    text = INSTALL.read_text()
    jail = text[text.index("printf '[sshd]"):text.index("/etc/fail2ban/jail.d/sshd.local")]
    assert "backend = systemd" in jail, \
        "without this the whole service fails to start on a journald-only image"


def test_the_systemd_backend_dependency_is_installed_explicitly():
    """python3-systemd is only a Recommends of fail2ban, so --no-install-recommends drops it."""
    text = INSTALL.read_text()
    pkgs = text[text.index("apt-get install -y -q ufw"):]
    pkgs = pkgs[:pkgs.index("\n")]
    assert "python3-systemd" in pkgs, \
        "the systemd backend cannot load without it, and it is not a hard dependency"


def test_bypass_permissions_is_opt_in():
    """Un-prompted tool calls plus passwordless sudo is un-prompted root. Never the default."""
    text = INSTALL.read_text()
    assert "BYPASS=no" in text, "must default to off"
    assert "--bypass-permissions) BYPASS=yes" in text


def test_bypass_writes_both_halves_and_can_be_turned_back_off():
    """The unit flag only covers Remote Control; a typed `claude` reads settings.json."""
    text = INSTALL.read_text()
    assert "CCFLEET_RC_ARGS=--permission-mode bypassPermissions" in text, "Remote Control half"
    assert "defaultMode" in text and "bypassPermissions" in text, "terminal half"
    # Both halves are written on every run, so dropping the flag actually reverts them.
    assert "printf 'CCFLEET_RC_ARGS=\\n'" in text, \
        "the env file must be rewritten empty when the flag is absent, not left stale"
    assert "perms.pop('defaultMode', None)" in text, \
        "settings must be cleaned when the flag is absent, not left wide open"


def test_a_reinstall_does_not_delete_settings_the_owner_chose():
    """Cleanup must only undo what a previous run of this installer set."""
    text = INSTALL.read_text()
    assert "bypass-managed" in text, "provenance marker is what makes the cleanup safe"
    assert "elif os.path.exists(marker):" in text, \
        "removal must be conditional on this installer having set the keys"
    assert "if not os.path.exists(marker):" in text, \
        "the pre-existing values are snapshotted once, not overwritten on every run"
    assert "prev['defaultMode']" in text, \
        "turning bypass off must restore the owner's own value, not just delete the key"
    assert "if perms.get('defaultMode') == 'bypassPermissions':" in text, \
        "restore only while the value is still ours; a newer choice by the owner wins"
    # And the marker must live outside Claude Code's own settings file.
    assert "~/.config/ccfleet/bypass-managed" in text, \
        "do not add non-standard keys to Claude Code's settings.json"


def test_bypass_says_so_out_loud():
    text = INSTALL.read_text()
    assert "PERMISSION PROMPTS ARE OFF" in text, \
        "an operator should not have to infer this from the absence of a prompt"


def test_dropping_the_bypass_flag_reaches_a_running_service():
    """Rewriting the env file changes nothing until the process restarts."""
    text = INSTALL.read_text()
    block = text[text.index("user_systemctl enable claude-remote-control.service"):]
    block = block[:block.index("RC_ENABLED=")]
    assert 'is-active claude-remote-control.service' in block and "restart" in block, \
        "an already-running node would keep the previous permission mode"
    # And the restart must be checked, not assumed: a service that was working
    # before the installer ran must not be left dead while it reports success.
    assert 'rc_after=' in block and 'restart-failed' in block, \
        "a restart that does not come back has to fail the install, not be swallowed"
    assert block.index("restart claude-remote-control.service") < block.index("rc_after="), \
        "the state has to be read after the restart, not before"
