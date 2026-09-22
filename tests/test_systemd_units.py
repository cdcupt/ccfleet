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


def test_the_unit_that_runs_claude_has_the_autoupdater_off():
    """ccfleet-agent runs `claude --version`, the call that can trigger an update."""
    agent = _service(UNITS / "ccfleet-agent.service")
    assert agent.get("Environment") == "DISABLE_AUTOUPDATER=1", \
        "an update mid-probe swaps the binary and the node looks broken"


def test_every_unit_that_invokes_claude_disables_the_autoupdater():
    for name in ("ccfleet-agent.service", "claude-remote-control.service", "ccfleet-shell.service"):
        text = (UNITS / name).read_text()
        assert "DISABLE_AUTOUPDATER=1" in text, f"{name} may run claude without staging upgrades"


MACHINE = UNITS / "ccfleet-machine.service"
MACHINE_TIMER = UNITS / "ccfleet-machine.timer"


def test_the_machine_agent_runs_as_root_and_says_why_not_otherwise():
    """It creates and removes slot users, and slot-add drops into each new one
    with sudo -u. NoNewPrivileges would forbid that sudo, so it is absent here
    on purpose — unlike every owner unit, which sets it."""
    svc = _service(MACHINE)
    assert "User" not in svc, "slot users can only be made by root"
    assert "NoNewPrivileges" not in svc, "slot-add.sh's sudo -u would be refused"


def test_the_machine_agent_is_isolated_from_the_environment_it_starts_in():
    svc = _service(MACHINE)
    assert svc["ExecStart"].startswith("/usr/bin/python3 -I "), \
        "without -I a PYTHONPATH in root's environment chooses what root imports"
    assert "/usr/local/lib/ccfleet/ccfleet_agent/machine.py" in svc["ExecStart"]


def test_the_machine_token_is_read_from_its_file_not_carried_by_the_unit():
    svc = _service(MACHINE)
    assert "--env-file /etc/ccfleet/agent.env" in svc["ExecStart"]
    assert "Environment" not in svc and "EnvironmentFile" not in svc


def test_the_machine_agent_is_bounded():
    """Slots' reports and quota reads run inside this unit; a slot must not be
    able to take the machine's memory down with the agent looking after it."""
    svc = _service(MACHINE)
    assert svc.get("MemoryMax") and svc.get("TimeoutStartSec")
    assert svc.get("Type") == "oneshot"


def test_the_machine_timer_runs_it_every_minute():
    cp = configparser.RawConfigParser(strict=False, comment_prefixes=("#",))
    cp.optionxform = str
    cp.read_string(MACHINE_TIMER.read_text())
    assert cp.get("Timer", "OnUnitActiveSec") == "1min"
    assert cp.get("Install", "WantedBy") == "timers.target"
