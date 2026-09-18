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
    assert 'is-active fail2ban' in text, "hardening must confirm fail2ban actually started"
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
    assert "activates after sign-in" in text, "its state should still be reported, just not gated"
    assert "retrying every 30 seconds" in text, "the owner should know it comes up by itself"
