"""Both Gemini clients grant tools locally for one negotiated prompt at a time."""

import json
import threading
import time
from pathlib import Path

import pytest

from ccfleet_agent import gemini_editor_client as terminal
from ccfleet_agent import project_files, workspace_tools
from tests.test_gemini_acp_client import SLOT, Harness
from tests.test_gemini_terminal_recovery import exercise as terminal_session


@pytest.fixture
def local_tools(monkeypatch):
    # The grant writer, sandbox, and publishing broker have separate tests. Here the
    # local authority is intentionally independent of the simulated slot and model.
    trusted = set()
    calls = []

    class Grant:
        def __init__(self, project, fleet_home):
            self.project = Path(project)
            self.fleet_home = fleet_home

        def enabled(self):
            return self.project in trusted

        def all_enabled(self):
            return False

        def navigation(self):
            return False

        def snapshot(self):
            return ("original", None) if self.enabled() else None

    def run(access, command, *, cancelled):
        assert access.enabled() and not cancelled()
        calls.append(("shell", command))
        return {"exit": 0, "output": "local result\n"}

    def publish(access, request, *, cancelled):
        assert access.enabled() and not cancelled()
        calls.append(("publish", request["action"]))
        return {"exit": 0, "output": "review created\n"}

    monkeypatch.setattr(project_files, "ProjectAccess", Grant)
    monkeypatch.setattr(workspace_tools, "run_local_command", run, raising=False)
    monkeypatch.setattr(workspace_tools, "publish_project", publish, raising=False)
    return trusted, calls


def slot_peer(host, caps, turns):
    setup = SLOT.split('put({"type":"ready"', 1)[0]
    ready = {"type": "ready", "model": "gemini-test", "capabilities": caps}
    host.bridge.command[4] = setup + f'put({ready!r})\n' + turns


def one_turn(host, surface, monkeypatch, prompt):
    if surface == "terminal":
        answers = iter([prompt, "/exit"])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        terminal.run(host.project, host.bridge.command, host.bridge.environment,
                     workspace_tools, project_files, fleet_home=host.root / "private")
    else:
        try:
            sid = host.open()
            assert host.recv(host.prompt(sid, prompt))["result"]["stopReason"] == "end_turn"
            assert "result" in host.recv(host.send("session/close", {"sessionId": sid}))
        finally:
            host.close()
    return [json.loads(row) for row in host.log.read_text().splitlines()]


@pytest.mark.parametrize("surface", ["terminal", "acp"])
@pytest.mark.parametrize("capability, granted", [(True, True), (True, False),
                                                 (False, True), ("true", True)])
def test_project_tools_require_local_grant_and_exact_slot_capability(
        tmp_path, monkeypatch, local_tools, capsys, surface, capability, granted):
    trusted, calls = local_tools
    host = Harness(tmp_path)
    if granted:
        trusted.add(host.project)
    enabled = granted and capability is True
    slot_peer(host, {"projectShell": capability}, f'enabled = {enabled!r}\n' + """
frame = get()
assert frame == ({"type":"prompt","text":"edit","projectShell":True} if enabled
                 else {"type":"prompt","text":"edit"})
assert bool(tool(1,"shell",command="git status")) == enabled
assert bool(tool(2,"publish",request={"action":"create_review","body":"not displayed in tool title"})) == enabled
assert not tool(3,"shell",command="git status",projectShell=True)
assert not tool(4,"publish",request="git status")
if enabled:
    assert tool(5,"approve",path="a.txt",content="approved")
    assert tool(6,"write",path="a.txt",content="approved")
    assert tool(7,"mkdir",path="generated")
put({"type":"end","reason":"end_turn"})
assert get() == {"type":"close"}
""")
    wire = one_turn(host, surface, monkeypatch, "edit")
    assert wire[0] == {"type": "open", "version": 1}
    assert len([w for w in wire if w.get("type") == "prompt"]) == 1
    assert calls == ([("shell", "git status"), ("publish", "create_review")] if enabled else [])
    assert (host.project / "a.txt").exists() == enabled
    assert (host.project / "generated").is_dir() == enabled
    if surface == "terminal":
        visible = capsys.readouterr().out
    else:
        assert not any(m.get("method") == "session/request_permission" for m in host.history)
        visible = json.dumps(host.history)
    assert ("Run project command: git status" in visible) == enabled
    assert ("local result" in visible) == enabled
    assert "not displayed in tool title" not in visible


@pytest.mark.parametrize("surface", ["terminal", "acp"])
def test_revoked_project_tool_cannot_write_an_implicitly_approved_edit(
        tmp_path, monkeypatch, local_tools, surface):
    trusted, _ = local_tools
    host = Harness(tmp_path)
    trusted.add(host.project)

    def revoke_at_boundary(access, command, *, cancelled):
        trusted.remove(host.project)
        return {"exit": 0, "output": ""}

    monkeypatch.setattr(workspace_tools, "run_local_command", revoke_at_boundary)
    slot_peer(host, {"projectShell": True}, """
assert get()["projectShell"] is True
assert tool(1,"approve",path="draft.txt",content="pending")
assert tool(2,"shell",command="git status")
assert not tool(3,"write",path="draft.txt",content="pending")
assert not tool(4,"publish",request={"action":"push"})
assert not tool(5,"shell",command="git status")
put({"type":"end","reason":"end_turn"})
assert get() == {"type":"close"}
""")
    one_turn(host, surface, monkeypatch, "edit")
    assert not (host.project / "draft.txt").exists()


def test_t3_steer_cancels_active_command_and_never_replays_it(
        tmp_path, monkeypatch, local_tools):
    trusted, calls = local_tools
    host = Harness(tmp_path)
    trusted.add(host.project)
    entered = threading.Event()

    def run(access, command, *, cancelled):
        calls.append(("shell", command))
        entered.set()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not cancelled():
            time.sleep(0.01)
        assert cancelled()
        raise ValueError("cancelled")

    monkeypatch.setattr(workspace_tools, "run_local_command", run)
    slot_peer(host, {"cancelTurn": True, "projectShell": True}, """
assert get() == {"type":"prompt","text":"build","turn":1,"projectShell":True}
put({"type":"request","op":"shell","command":"make","id":1})
messages = [get(), get()]
assert {"type":"cancel","turn":1} in messages and any("error" in m for m in messages)
put({"type":"end","reason":"cancelled","turn":1})
assert get() == {"type":"prompt","text":"next","turn":2,"projectShell":True}
put({"type":"end","reason":"end_turn","turn":2})
assert get() == {"type":"close"}
""")
    try:
        sid = host.open()
        first = host.prompt(sid, "build")
        assert entered.wait(3)
        host.send("session/cancel", {"sessionId": sid}, notify=True)
        assert host.recv(first)["result"]["stopReason"] == "cancelled"
        assert host.recv(host.prompt(sid, "next"))["result"]["stopReason"] == "end_turn"
        assert "result" in host.recv(host.send("session/close", {"sessionId": sid}))
    finally:
        host.close()
    assert calls == [("shell", "make")]


def test_terminal_transport_close_interrupts_local_execution(tmp_path, local_tools, monkeypatch):
    trusted, calls = local_tools
    host = Harness(tmp_path)
    trusted.add(host.project)
    signal = tmp_path / "started"

    def run(access, command, *, cancelled):
        calls.append(("shell", command))
        signal.touch()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and not cancelled():
            time.sleep(0.01)
        assert cancelled()
        raise ValueError("cancelled")

    monkeypatch.setattr(workspace_tools, "run_local_command", run)
    slot_peer(host, {"projectShell": True}, f'flag = Path({str(signal)!r})\n' + """
assert get()["projectShell"] is True
put({"type":"request","id":1,"op":"shell","command":"make"})
while not flag.exists():
    time.sleep(.01)
os.close(1)
time.sleep(.25)
""")
    wire = one_turn(host, "terminal", monkeypatch, "build")
    assert [f["text"] for f in wire if f.get("type") == "prompt"] == ["build"]
    assert calls == [("shell", "make")]


def test_cli_does_not_accept_commands_outside_negotiated_turn(tmp_path, local_tools):
    trusted, calls = local_tools
    project = tmp_path / "project"
    project.mkdir()
    trusted.add(project)
    ed = terminal.Editor(project, workspace_tools, project_files, fleet_home=tmp_path / "private")
    try:
        ed.commands.connected({"capabilities": {"projectShell": True}})
        request = {"type": "request", "op": "shell", "id": 1, "command": "git status"}
        with pytest.raises(ValueError):
            ed.handle(request)
        frame = {"type": "prompt", "text": "check"}
        with ed.commands.turn(frame):
            assert frame["projectShell"] is True
            assert ed.handle(request)["exit"] == 0
            for bad in ("", " " * 3, "echo\x00invalid", "x" * (16 * 1024 + 1)):
                with pytest.raises(ValueError):
                    ed.handle(dict(request, command=bad))
        with pytest.raises(ValueError):
            ed.handle(request)
        assert calls == [("shell", "git status")]
    finally:
        ed.workspace.close()


def test_replaced_grant_waits_for_next_user_turn(tmp_path, local_tools, monkeypatch):
    trusted, calls = local_tools
    project = tmp_path / "project"
    project.mkdir()
    trusted.add(project)
    commands = terminal.ProjectCommands(workspace_tools, project_files, project, tmp_path / "private")
    commands.connected({"capabilities": {"projectShell": True}})
    request = {"op": "shell", "command": "git status"}
    with commands.turn({}):
        assert commands.allowed()
        monkeypatch.setattr(commands.access, "snapshot", lambda: ("replacement", None))
        with pytest.raises(ValueError):
            commands.execute(request, lambda: False)
    with commands.turn({}):
        commands.execute(request, lambda: False)
    assert calls == [("shell", "git status")]

@pytest.mark.parametrize("lose_grant", [True, False])
def test_terminal_resume_renegotiates_project_access_without_replay(
        tmp_path, monkeypatch, local_tools, lose_grant):
    trusted, calls = local_tools
    project = tmp_path / "project"
    project.mkdir()
    trusted.add(project)
    first = """
assert read() == {"type":"open","version":1}
ready(capabilities={"projectShell":True,"resumeSession":True,"cancelTurn":True})
assert read() == {"type":"enableResume"}
assert prompt()["projectShell"] is True
assert tool(1,"shell",command="git status")["result"]["exit"]==0
"""  # Link loss after the operation completed, before the end acknowledgement.
    second = f"advertise = {lose_grant!r}\n" + """
assert read() == {"type":"resume","version":1,"resumeToken":"ab"*32}
ready(resumed=True, capabilities={"projectShell":advertise,"resumeSession":True,"cancelTurn":True})
assert read() == {"type":"enableResume"}
followup = prompt()
assert "projectShell" not in followup
assert "error" in tool(1,"shell",command="git status")
end(followup)
assert read() == {"type":"close"}
"""
    if lose_grant:
        def run(access, command, *, cancelled):
            trusted.remove(project)
            calls.append(("shell", command))
            return {"exit": 0, "output": ""}
        monkeypatch.setattr(workspace_tools, "run_local_command", run)
    reqs = terminal_session(tmp_path, project, monkeypatch, ["first", "follow-up", "/exit"],
                            [first, second])
    assert [r["text"] for r in reqs if r["type"] == "prompt"] == ["first", "follow-up"]
    assert calls == [("shell", "git status")]
