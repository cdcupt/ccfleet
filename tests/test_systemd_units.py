"""The systemd units carry decisions that are easy to undo by accident.

The one these tests exist for: a tmux server belongs to whichever unit started
it, and systemd's default KillMode kills a unit's whole control group. Sharing
one tmux server between the Remote Control unit and the owner's work session
therefore meant restarting Remote Control destroyed the owner's work. Measured
on a live node 2026-09-19.
"""
import configparser
from pathlib import Path

UNITS = Path(__file__).resolve().parents[1] / "node" / "systemd"
RC = UNITS / "claude-remote-control.service"
SHELL = UNITS / "ccfleet-shell.service"


def _service(path):
    """systemd allows repeated keys, so read them as lists rather than a dict."""
    cp = configparser.RawConfigParser(strict=False, comment_prefixes=("#",), inline_comment_prefixes=None)
    cp.optionxform = str
    cp.read_string(path.read_text())
    return {k: v for k, v in cp.items("Service")}


def test_remote_control_uses_its_own_tmux_server():
    svc = _service(RC)
    assert "-L ccfleet-rc" in svc["ExecStart"], \
        "without a private socket, stopping this unit kills the owner's work session too"
    assert "-L ccfleet-rc" in svc["ExecStop"], "the stop must target the same private server"


def test_remote_control_stop_cannot_reach_the_shared_server():
    """kill-session on the default socket was the bug; kill-server on its own is safe."""
    svc = _service(RC)
    stop = svc["ExecStop"]
    assert "kill-server" in stop
    assert "kill-session" not in stop, "session-scoped kill on a shared server is what broke this"
    # The give-away for a regression: a tmux call with no -L is on the shared server.
    for line in (svc["ExecStart"], stop):
        assert "tmux -L" in line, f"tmux call without a private socket: {line}"


def test_remote_control_takes_optional_args_from_a_file_not_an_edited_command():
    """install.sh --bypass-permissions must not rewrite this command line."""
    text = RC.read_text()
    assert "EnvironmentFile=-%h/.config/ccfleet/remote-control.env" in text, \
        "optional args come from a file; the leading dash makes it optional"
    assert "${CCFLEET_RC_ARGS}" in _service(RC)["ExecStart"]


def test_the_shell_unit_does_not_kill_the_session_it_warmed():
    svc = _service(SHELL)
    assert svc.get("KillMode") == "process", \
        "the default control-group kill takes the tmux server down on restart"
    assert "ExecStop" not in svc, "this unit must never tear the session down"


def test_the_shell_unit_still_creates_the_session_the_login_expects():
    svc = _service(SHELL)
    assert "has-session -t cc" in svc["ExecStart"] and "new-session -d -s cc" in svc["ExecStart"], \
        "node/attach.sh attaches logins to this exact session name"


def test_the_two_units_do_not_share_a_tmux_server():
    rc, shell = _service(RC)["ExecStart"], _service(SHELL)["ExecStart"]
    assert "-L ccfleet-rc" in rc and "-L ccfleet-rc" not in shell, \
        "the work session stays on the default server; Remote Control gets its own"


def test_docs_tell_people_which_server_to_attach_to():
    root = Path(__file__).resolve().parents[1]
    for rel in ("docs/runbooks.md", "docs/guidebook.html"):
        text = (root / rel).read_text()
        if "attach -t remote-control" in text:
            assert "-L ccfleet-rc attach -t remote-control" in text, \
                f"{rel} sends people to the wrong tmux server"
