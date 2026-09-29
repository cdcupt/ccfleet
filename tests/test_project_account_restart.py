"""An explicit account change ends every platform session, never unrelated shells."""

from __future__ import annotations

import json
import shlex
import subprocess

import pytest

from ccfleet_agent import agent

from . import test_agent_one_account as account_tests
from .test_agent_one_account import NOW, SlotFake, state_file, walk_to_code_sent

PROJECT = "p_" + "a" * 32 + "_work"
LIVE = "l_" + "b" * 32 + "_work"
slot_home = account_tests.slot_home


class RestartRunner:
    def __init__(self, output="", *, list_error=None, kill_error=None, restart_code=0):
        self.output = output
        self.list_error = list_error
        self.kill_error = kill_error
        self.restart_code = restart_code
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs["timeout"] <= 60
        if argv[1] == "list-panes":
            if isinstance(self.list_error, Exception):
                raise self.list_error
            return subprocess.CompletedProcess(argv, int(self.list_error is not None),
                                               stdout=self.output, stderr=self.list_error or "")
        if argv[1] == "kill-session":
            if isinstance(self.kill_error, Exception):
                raise self.kill_error
            return subprocess.CompletedProcess(argv, int(self.kill_error is not None),
                                               stdout="", stderr=self.kill_error or "")
        return subprocess.CompletedProcess(argv, self.restart_code, stdout="", stderr="")

    def killed(self):
        return [args[-1] for args in self.calls if args[:2] == ["tmux", "kill-session"]]

    def restarted(self):
        return any(args[:3] == ["systemctl", "--user", "restart"] for args in self.calls)


def test_refresh_closes_default_projects_and_named_claude_but_preserves_shells(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    native = str(tmp_path / ".local/bin/claude")
    fake = RestartRunner("\n".join([
        f"ccfleet\t{native} --model opus\tclaude",
        f"research\t{native} --permission-mode plan\tclaude",
        f"research\t{native} --model sonnet\tclaude",  # multiple panes, kill once
        f"{PROJECT}\t/usr/bin/python3 -I /usr/local/lib/ccfleet/project_access.py\tclaude",
        f"{LIVE}\t/usr/bin/python3 -I /usr/local/lib/ccfleet/live_access.py\tclaude",
        "shell\t/bin/bash\tbash",
        f"echo-probe\techo {shlex.quote(native)}\techo",
        "interactive\t/bin/bash\tclaude",  # not launched by this platform
        "ccfleet-backup\tsleep 9999\tsleep",
    ]))
    assert agent.restart_slot_terminal(fake)
    assert set(fake.killed()) == {"=ccfleet", "=research", "=" + PROJECT, "=" + LIVE}
    assert len(fake.killed()) == 4
    assert fake.restarted()
    assert all(args[:2] != ["tmux", "-L"] for args in fake.calls)


def test_quoted_native_path_with_spaces_is_recognized_and_echo_is_not(tmp_path, monkeypatch):
    home = tmp_path / "slot with spaces"
    monkeypatch.setenv("HOME", str(home))
    native = shlex.quote(str(home / ".local/bin/claude"))
    fake = RestartRunner(f"work\t{native} --model opus\tclaude\n"
                         f"echo\techo {native}\techo\n")
    assert agent.restart_slot_terminal(fake)
    assert set(fake.killed()) == {"=ccfleet", "=work"}


@pytest.mark.parametrize("error", ["permission denied: secret command data",
                                  "error connecting to /tmp/tmux (Permission denied)",
                                  OSError("private path"), subprocess.TimeoutExpired("tmux", 15)])
def test_list_failure_still_stops_default_and_retains_restart_debt(error, caplog):
    fake = RestartRunner(list_error=error)
    assert not agent.restart_slot_terminal(fake)
    assert fake.killed() == ["=ccfleet"]
    assert not fake.restarted()
    assert not caplog.text


@pytest.mark.parametrize("error", ["permission denied", "unexpected failure",
                                  OSError("private path"), subprocess.TimeoutExpired("tmux", 15)])
def test_kill_failure_never_marks_restart_complete(error, caplog):
    fake = RestartRunner(kill_error=error)
    assert not agent.restart_slot_terminal(fake)
    assert not fake.restarted()
    assert not caplog.text


@pytest.mark.parametrize("list_error,kill_error", [
    ("no server running on /tmp/tmux-1001/default", "no server running on /tmp/tmux-1001/default"),
    ("error connecting to /tmp/tmux-1001/default (No such file or directory)",
     "error connecting to /tmp/tmux-1001/default (No such file or directory)"),
    (None, "can't find session: =ccfleet"),
    (None, "no such session: =ccfleet"),
])
def test_absent_server_or_already_ended_session_is_idempotent(list_error, kill_error):
    fake = RestartRunner(list_error=list_error, kill_error=kill_error)
    assert agent.restart_slot_terminal(fake)
    assert fake.restarted()


@pytest.mark.parametrize("output", ["missing separators", "shell\t/bin/sh\tbash\textra",
                                   "shell\t'unclosed\tbash", "x" * (256 * 1024 + 1),
                                   "shell\t/bin/sh\tbash\n" * 4097,
                                   "shell\t" + "x" * 8200 + "\tbash"])
def test_malformed_or_oversized_pane_data_fails_closed_without_logging(output, caplog):
    fake = RestartRunner(output)
    assert not agent.restart_slot_terminal(fake)
    assert fake.killed() == ["=ccfleet"]
    assert not fake.restarted()
    assert not caplog.text


def test_invalid_project_and_session_names_cannot_be_tmux_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    native = str(tmp_path / ".local/bin/claude")
    names = ["p_" + "A" * 32 + "_work", PROJECT + ";id", "-work", "work.other", "x" * 100]
    fake = RestartRunner("\n".join(f"{name}\t{native}\tclaude" for name in names))
    assert agent.restart_slot_terminal(fake)
    assert fake.killed() == ["=ccfleet"]


def test_failed_service_restart_remains_owed():
    assert not agent.restart_slot_terminal(RestartRunner(restart_code=1))


@pytest.mark.parametrize("code", [0, 2])
def test_account_refresh_stops_live_mounts_before_finishing(tmp_path, monkeypatch, code):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/ccfleet/live").mkdir(parents=True)
    class Runner(RestartRunner):
        def __call__(self, argv, **kwargs):
            if argv[-1] == "_stop-all":
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, code, stdout="", stderr="")
            return super().__call__(argv, **kwargs)
    fake = Runner()
    assert agent.restart_slot_terminal(fake) is (code == 0)
    assert fake.calls[0][-1] == "_stop-all"
    assert fake.restarted() is (code == 0)


@pytest.mark.parametrize("failure", ["list", "kill"])
def test_account_change_keeps_durable_debt_until_named_sessions_are_gone(slot_home, failure):
    class AccountRunner(SlotFake):
        fail = True

        def __call__(self, argv, **kwargs):
            response = super().__call__(argv, **kwargs)
            if list(argv[:2]) == ["tmux", "list-panes"]:
                response.stdout = (f"work\t{slot_home}/.local/bin/claude --model opus\tclaude\n"
                                   f"{PROJECT}\t/usr/bin/python3 -I project_access.py\tclaude\n"
                                   "shell\t/bin/bash\tbash\n")
                if self.fail and failure == "list":
                    response.returncode, response.stderr = 1, "permission denied"
            if (list(argv[:2]) == ["tmux", "kill-session"] and argv[-1] == "=work"
                    and self.fail and failure == "kill"):
                response.returncode, response.stderr = 1, "permission denied"
            return response

    fake = AccountRunner(slot_home)
    wanted, _ = walk_to_code_sent(fake)
    facts = agent.slot_facts({"login": {**wanted, "code": "the-code"}}, fake, now=NOW)
    assert json.loads(state_file(slot_home).read_text())["account_restart"] == "owed"
    assert str(slot_home / ".local/bin/claude") not in json.dumps(facts)
    assert "permission denied" not in json.dumps(facts)
    fake.fail = False
    agent.slot_facts({}, fake, now=NOW)
    assert "account_restart" not in json.loads(state_file(slot_home).read_text())
    assert ["tmux", "kill-session", "-t", "=work"] in fake.calls
    assert ["tmux", "kill-session", "-t", "=" + PROJECT] in fake.calls
    assert ["tmux", "kill-session", "-t", "=shell"] not in fake.calls
