"""Exercise terminal/ACP control messages and the actual file-backed Mac grant."""

import json
import os
import pty
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ccfleet_agent import gemini_editor_client as terminal
from ccfleet_agent import project_files, workspace_tools
from tests.test_gemini_acp_client import Harness
from tests.test_gemini_shell_clients import slot_peer
from tests.test_gemini_terminal_recovery import PEER


@pytest.fixture
def navigation(tmp_path, monkeypatch):
    host = Harness(tmp_path)
    home = tmp_path.resolve() / "private-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(project_files, "_account_home", lambda: home)
    host.project = home
    access = project_files.ProjectAccess(home, host.bridge.fleet_home)
    access.enable_all()
    for project in ("first project", "second"):
        path = home / project
        path.mkdir(mode=0o700)
        (path / "pyproject.toml").write_text("[project]\n")
        (path / "note.txt").write_text(project)
    monkeypatch.setattr(terminal, "confirm", lambda *_: pytest.fail("unexpected terminal approval"))
    yield host, access


def session(host, surface, args, monkeypatch, *, test=None):
    if surface == "terminal":
        prompts = iter([*args, "/exit"])
        monkeypatch.setattr("builtins.input", lambda _: next(prompts))
        terminal.run(
            host.project,
            host.bridge.command,
            host.bridge.environment,
            workspace_tools,
            project_files,
            test,
            fleet_home=host.bridge.fleet_home,
        )
        assert next(prompts, None) is None
    else:
        try:
            sid = host.open()
            for text in args:
                assert host.recv(host.prompt(sid, text))["result"]["stopReason"] == "end_turn"
            assert "result" in host.recv(host.send("session/close", {"sessionId": sid}))
        finally:
            host.close()
        assert not any(m.get("method") == "session/request_permission" for m in host.history)
    return [json.loads(row) for row in host.log.read_text().splitlines()]


@pytest.mark.parametrize(
    "surface,at_home",
    [
        ("terminal", True),
        ("acp", True),
        ("terminal", False),
        ("acp", False),
    ],
)
def test_switch_reuses_one_conversation_and_discards_old_edits_and_test_settings(
    navigation, monkeypatch, capsys, surface, at_home
):
    host, owner = navigation
    first = owner.project / "first project"
    second = owner.project / "second"
    if not at_home:
        host.project = first
    slots = """
first = get()
assert first["projectShell"] is True and first["text"].endswith("inspect first")
assert tool(1, "read", path="note.txt") == "first project"
assert tool(2, "approve", path="draft.txt", content="previous draft")
assert not tool(3, "workspace", path="second")
put({"type":"text","text":"/workspace second"})
put({"type":"end","reason":"end_turn"})
second = get()
assert second["projectShell"] is True and second["text"].endswith("inspect second")
assert "switched" in second["text"]
assert not tool(1, "write", path="draft.txt", content="previous draft")
assert not tool(2, "approve", path="note.txt", content="stale selection")
assert not tool(3, "test")
assert tool(4, "read", path="note.txt") == "second"
assert tool(5, "approve", path="note.txt", content="edited second")
assert tool(6, "write", path="note.txt", content="edited second")
assert tool(7, "shell", command="check worktree")["output"]=="second"
put({"type":"end","reason":"end_turn"})
assert get()=={"type":"close"}
"""
    slot_peer(host, {"projectShell": True}, slots)
    monkeypatch.setattr(
        workspace_tools,
        "run_local_command",
        lambda grant, *_a, **_kw: {"exit": 0, "output": grant.project.name},
    )
    first_steps = ["Are you there?", "/workspace", "/workspace ~/first project"] if at_home else []
    commands = [
        *first_steps,
        "inspect first",
        "/workspace second",
        "/workspace",
        "inspect second",
        "/workspace ~",
        "try working at home",
    ]
    wire = session(host, surface, commands, monkeypatch, test="npm")
    triggers = [m for m in wire if m["type"] == "prompt"]
    assert len(triggers) == 2 and str(owner.project) not in json.dumps(wire)
    assert (first / "note.txt").read_text() == "first project" and not (
        first / "draft.txt"
    ).exists()
    assert (second / "note.txt").read_text() == "edited second" and not (
        second / "draft.txt"
    ).exists()
    visible = capsys.readouterr().out if surface == "terminal" else json.dumps(host.history)
    assert str(second) in visible and terminal.PICK_WORKSPACE in visible


@pytest.mark.parametrize("surface", ["terminal", "acp"])
def test_old_slot_reports_unavailable_without_falling_back_to_approval(
    navigation, monkeypatch, capsys, surface
):
    host, owner = navigation
    host.project = owner.project / "second"
    slot_peer(
        host,
        {},
        """
frame=get(); assert "projectShell" not in frame
assert tool(1,"read",path="note.txt")=="second"
assert not tool(2,"approve",path="draft.txt",content="x")
assert not tool(3,"mkdir",path="directory")
assert not tool(4,"test")
assert not tool(5,"shell",command="pwd")
put({"type":"end","reason":"end_turn"})
assert get()=={"type":"close"}
""",
    )
    session(host, surface, ["test"], monkeypatch, test="npm")
    visible = capsys.readouterr().out if surface == "terminal" else json.dumps(host.history)
    assert "Update the private Mac slot" in visible and not (host.project / "directory").exists()


@pytest.mark.parametrize("surface", ["acp", "terminal"])
def test_global_revocation_in_busy_turn_never_reprompts(navigation, monkeypatch, surface):
    host, owner = navigation
    host.project = owner.project / "second"

    def interrupt(grant, command, *, cancelled):
        owner.disable_all()
        assert cancelled()
        return {"exit": 1, "output": "stopped"}

    monkeypatch.setattr(workspace_tools, "run_local_command", interrupt)
    slot_peer(
        host,
        {"projectShell": True},
        """
assert get()["projectShell"] is True
assert tool(1, "approve", path="draft", content="pending")
tool(2, "shell", command="in flight")
assert not tool(3, "write", path="draft", content="pending")
assert not tool(4, "approve", path="late", content="stale")
assert not tool(5, "mkdir", path="late-dir")
assert not tool(6, "test")
put({"type":"end","reason":"end_turn"})
assert get()=={"type":"close"}
""",
    )
    session(host, surface, ["one instruction"], monkeypatch, test="npm")
    assert not (host.project / "draft").exists() and not (host.project / "late-dir").exists()


def test_acp_rejects_workspace_switch_via_wrapped_blocks_attachments_or_model(navigation):
    host, owner = navigation
    slot_peer(host, {"projectShell": True}, 'assert get()=={"type":"close"}')
    try:
        sid = host.open()
        text = {"type": "text", "text": "/workspace second"}
        for blocks, metadata in [
            ([text, {"type": "text", "text": ""}], {}),
            (
                [
                    text,
                    {"type": "resource_link", "uri": (owner.project / "second/note.txt").as_uri()},
                ],
                {},
            ),
            ([dict(text, annotations={"audience": ["assistant"]})], {}),
            ([dict(text, text="/workspace second\nbackground title generation")], {}),
            ([text], {"_meta": {"task": "generate title"}}),
        ]:
            reply = host.recv(
                host.send("session/prompt", {"sessionId": sid, "prompt": blocks, **metadata})
            )
            assert "error" in reply and host.bridge.session.workspace.path == owner.project
        assert "result" in host.recv(host.send("session/close", {"sessionId": sid}))
    finally:
        host.close()
    assert [json.loads(row)["type"] for row in host.log.read_text().splitlines()] == [
        "open",
        "close",
    ]


def test_acp_keeps_original_workspace_for_rejected_or_overlapping_switch(navigation, monkeypatch):
    host, owner = navigation
    host.project = owner.project / "first project"
    (owner.project / "settings").mkdir()
    (owner.project / "alias").symlink_to(owner.project / "second", target_is_directory=True)
    started = threading.Event()
    complete = threading.Event()

    def run(access, *_args, **_kwargs):
        started.set()
        assert complete.wait(5)
        return {"exit": 0, "output": access.project.name}

    monkeypatch.setattr(workspace_tools, "run_local_command", run)
    slot_peer(
        host,
        {"projectShell": True},
        """
assert get()["projectShell"] is True
assert tool(1,"shell",command="slow")["output"]=="first project"
put({"type":"end","reason":"end_turn"})
assert get()=={"type":"close"}
""",
    )
    try:
        sid = host.open()
        for arg in [str(owner.fleet_home), "alias", "settings", "../outside"]:
            assert "error" in host.recv(host.prompt(sid, "/workspace " + arg))
            assert host.bridge.session.workspace.path == host.project
        pending = host.prompt(sid, "slow")
        assert started.wait(4)
        assert "error" in host.recv(host.prompt(sid, "/workspace second"))
        assert host.bridge.session.workspace.path == host.project
        complete.set()
        assert host.recv(pending)["result"]["stopReason"] == "end_turn"
        assert "result" in host.recv(host.prompt(sid, "/workspace second"))
        assert host.bridge.session.workspace.path == owner.project / "second"
        assert "result" in host.recv(host.send("session/close", {"sessionId": sid}))
    finally:
        complete.set()
        host.close()


def test_buffered_tool_after_end_cannot_reach_a_different_project(navigation, monkeypatch):
    host, owner = navigation
    # Deliver a deliberately out-of-order callback after a completed turn. It must
    # never be consumed as a request against the new project's files.
    caps = {"resumeSession": True, "projectShell": True, "cancelTurn": True}
    stale = host.root / "buffered"
    sessions = [
        f"""assert read()=={{"type":"open","version":1}}
ready(capabilities={caps!r})
assert read()=={{"type":"enableResume"}}
turn=prompt(); assert turn["text"].endswith("remember first")
end(turn)
send(type="request",id=9,op="mkdir",path="expired-operation")
Path({str(stale)!r}).touch()
drain()
""",
        f"""assert read()=={{"type":"resume","version":1,"resumeToken":"ab"*32}}
ready(capabilities={caps!r}, resumed=True)
assert read()=={{"type":"enableResume"}}
turn=prompt(); assert turn["text"].endswith("inspect second")
assert tool(1,"read",path="note.txt")["result"]=="second"
end(turn)
assert read()=={{"type":"close"}}
""",
    ]
    (host.root / "attachments").write_text(json.dumps(sessions))
    host.bridge.command = [sys.executable, "-I", "-c", PEER, str(host.root)]
    try:
        sid = host.open()
        assert "result" in host.recv(host.prompt(sid, "/workspace first project"))
        assert "result" in host.recv(host.prompt(sid, "remember first"))
        for _ in range(150):
            if stale.exists():
                break
            time.sleep(0.02)
        assert stale.exists()
        assert "result" in host.recv(host.prompt(sid, "/workspace second"))
        assert "result" in host.recv(host.prompt(sid, "inspect second"))
        assert "result" in host.recv(host.send("session/close", {"sessionId": sid}))
    finally:
        host.close()
    sent = [json.loads(row) for row in (host.root / "requests").read_text().splitlines()]
    assert [r["type"] for r in sent if r["type"] in ("open", "resume")] == ["open", "resume"]
    assert not any(r.get("id") == 9 for r in sent)
    assert not (owner.project / "second" / "expired-operation").exists()
    assert not (owner.project / "first project" / "expired-operation").exists()
    assert not (host.root / "errors").exists()


def test_unavailable_test_subfolder_in_full_access_never_reads_terminal_input(
    navigation, monkeypatch
):
    host, owner = navigation
    editor = terminal.Editor(
        owner.project / "second",
        workspace_tools,
        project_files,
        "npm",
        fleet_home=host.bridge.fleet_home,
    )
    try:
        editor.commands.connected({"capabilities": {"projectShell": True}})
        monkeypatch.setattr(editor.workspace, "test_directory", lambda *_: None)
        monkeypatch.setattr(editor, "answer", lambda *_: pytest.fail("unexpected input request"))
        with editor.commands.turn({"type": "prompt", "text": "tests"}):
            with pytest.raises(terminal.FullAccessUnavailable, match="subfolder"):
                editor.handle({"type": "request", "id": 1, "op": "test"})
    finally:
        editor.workspace.close()


def test_t3_grant_enabled_during_pending_approval_expires_old_decision(navigation):
    host, owner = navigation
    owner.disable_all()
    host.project = owner.project / "second"
    slot_peer(
        host,
        {"projectShell": True},
        """
assert get().get("projectShell") is None
assert not tool(1,"approve",path="old",content="review pending")
assert not tool(2,"mkdir",path="subsequent")
assert not tool(3,"write",path="old",content="review pending")
put({"type":"end","reason":"end_turn"})
assert get()["projectShell"] is True
assert tool(1,"mkdir",path="new-turn")
put({"type":"end","reason":"end_turn"})
assert get()=={"type":"close"}
""",
    )
    try:
        sid = host.open()
        turn = host.prompt(sid, "first instruction")
        while True:
            card = host.get()
            if card.get("method") == "session/request_permission":
                break
        owner.enable_all()
        assert host.recv(turn)["result"]["stopReason"] == "end_turn"
        host.bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": card["id"],
                "result": {"outcome": {"outcome": "selected", "optionId": "allow"}},
            }
        )
        assert host.recv(host.prompt(sid, "next"))["result"]["stopReason"] == "end_turn"
        assert "result" in host.recv(host.send("session/close", {"sessionId": sid}))
    finally:
        host.close()
    assert sum(e.get("method") == "session/request_permission" for e in host.history) == 1
    assert not (host.project / "old").exists() and not (host.project / "subsequent").exists()
    assert (host.project / "new-turn").is_dir()


def test_terminal_grant_enabled_during_pending_answer_does_not_approve(navigation, monkeypatch):
    host, owner = navigation
    owner.disable_all()
    host.project = owner.project / "second"
    attempts = []

    def enable_while_asking(question):
        assert not attempts
        attempts.append(question)
        owner.enable_all()
        return True

    monkeypatch.setattr(terminal, "confirm", enable_while_asking)
    slot_peer(
        host,
        {"projectShell": True},
        """
assert get().get("projectShell") is None
assert not tool(1,"mkdir",path="old")
assert not tool(2,"approve",path="subsequent",content="stale")
put({"type":"end","reason":"end_turn"})
assert get()["projectShell"] is True
assert tool(1,"mkdir",path="new-turn")
put({"type":"end","reason":"end_turn"})
assert get()=={"type":"close"}
""",
    )
    session(host, "terminal", ["initial", "next"], monkeypatch)
    assert len(attempts) == 1 and not (host.project / "old").exists()
    assert (host.project / "new-turn").is_dir()


def test_terminal_pending_approval_dismisses_without_keystroke_on_new_grant(navigation):
    host, owner = navigation
    owner.disable_all()
    host.project = owner.project / "second"
    slot_peer(
        host,
        {"projectShell": True},
        """
assert get().get("projectShell") is None
assert not tool(1,"mkdir",path="old")
assert not tool(2,"approve",path="subsequent",content="stale")
put({"type":"end","reason":"end_turn"})
frame=get()
assert frame.get("projectShell") is True and frame["text"]=="new command"
assert tool(1,"mkdir",path="new-turn")
put({"type":"end","reason":"end_turn"})
assert get()=={"type":"close"}
""",
    )
    code = """
import sys,json,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from ccfleet_agent import project_files as files, workspace_tools as tools, gemini_editor_client as ed
files._account_home=lambda:Path(sys.argv[2])
ed.run(Path(sys.argv[2])/'second',json.loads(sys.argv[4]),{'PATH':os.defpath},tools,files,fleet_home=Path(sys.argv[3]))
"""
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                code,
                str(Path(__file__).resolve().parents[1]),
                str(owner.project),
                str(owner.fleet_home),
                json.dumps(host.bridge.command),
            ],
            cwd=host.project,
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
    finally:
        os.close(slave)
    buffer = bytearray()

    def wait_for(word):
        until = time.monotonic() + 6
        target = word.encode()
        while target not in buffer and time.monotonic() < until:
            if select.select([master], [], [], 0.1)[0]:
                buffer.extend(os.read(master, 16384))
        assert target in buffer, bytes(buffer)[-800:]
        del buffer[: buffer.find(target) + len(target)]

    try:
        wait_for("You › ")
        os.write(master, b"first instruction\n")
        wait_for("Create directory on this Mac (old)? [y/N] ")
        os.write(master, b"y")
        owner.enable_all()
        wait_for("Project access changed.")
        wait_for("You › ")
        os.write(master, b"yes\n")
        wait_for("That approval expired.")
        wait_for("You › ")
        os.write(master, b"new command\n")
        wait_for("You › ")
        os.write(master, b"/exit\n")
        until = time.monotonic() + 6
        while proc.poll() is None and time.monotonic() < until:
            if select.select([master], [], [], 0.10)[0]:
                try:
                    os.read(master, 4096)
                except OSError:
                    break
        assert proc.wait(timeout=2) == 0
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=4)
    assert (host.project / "new-turn").is_dir()
    assert not (host.project / "old").exists()
    prompts = [
        m["text"]
        for m in (json.loads(row) for row in host.log.read_text().splitlines())
        if m.get("type") == "prompt"
    ]
    assert prompts == ["first instruction", "new command"]
