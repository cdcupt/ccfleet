import hashlib
import os
import runpy
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import gemini_editor_client as ui
from ccfleet_agent import project_files, workspace_tools

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "local"
    root.mkdir()
    ed = ui.Editor(root, workspace_tools, project_files)
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    try:
        yield root, ed
    finally:
        ed.workspace.close()


def call(ed, op, **fields):
    return ed.handle({"type": "request", "id": 1, "op": op, **fields})


def test_exact_file_content_and_one_shot_approval(project, monkeypatch, capsys):
    root, ed = project
    f = root / "note"
    f.write_text("local v1\n")
    with pytest.raises(ValueError, match="read again"):
        call(ed, "approve", path="note", content="new")
    assert call(ed, "read", path="note") == "local v1\n"
    assert call(ed, "approve", path="note", content="new")
    assert "-local v1" in capsys.readouterr().out
    with pytest.raises(ValueError, match="approval"):
        call(ed, "write", path="note", content="different")
    with pytest.raises(ValueError, match="approval"):
        call(ed, "write", path="note", content="new")
    assert call(ed, "approve", path="note", content="new")
    f.write_text("newer local contents")
    with pytest.raises(ValueError, match="file changed"):
        call(ed, "write", path="note", content="new")
    assert f.read_text() == "newer local contents"
    call(ed, "read", path="note")
    monkeypatch.setattr(ui, "confirm", lambda _: False)
    assert call(ed, "approve", path="note", content="new") is False


def test_creation_directory_listing_and_workspace_boundary(project):
    root, ed = project
    assert call(ed, "mkdir", path="src")
    assert call(ed, "approve", path="src/a.py", content="print(1)")
    assert call(ed, "write", path="src/a.py", content="print(1)")
    assert call(ed, "list", path="src") == ["a.py"]
    assert call(ed, "read", path="src/a.py") == "print(1)"
    assert call(ed, "approve", path="src/a.py", content="print(2)")
    assert call(ed, "write", path="src/a.py", content="print(2)")
    assert (root / "src/a.py").read_text() == "print(2)"
    assert call(ed, "approve", path="new", content="proposed")
    (root / "new").write_text("created concurrently")
    with pytest.raises(ValueError):
        call(ed, "write", path="new", content="proposed")
    (root / "redirect").symlink_to(root.parent, target_is_directory=True)
    for path in ("../outside", ".env", ".git/config", "redirect/outside"):
        with pytest.raises((ValueError, OSError)):
            call(ed, "approve", path=path, content="changed")
    assert not (root.parent / "outside").exists()


def test_local_test_choice_and_approval(project, monkeypatch, capsys):
    _, ed = project
    with pytest.raises(ValueError, match="disabled"):
        call(ed, "test")
    ed.test_command("/test")
    ed.test_command("/test invalid")
    ed.test_command("/test npm")
    assert ed.workspace.command == ("npm", "test")
    monkeypatch.setattr(ui, "confirm", lambda question: False)
    with pytest.raises(ValueError, match="approved"):
        call(ed, "test")
    with pytest.raises(ValueError):
        call(ed, "test", command="not allowed")
    monkeypatch.setattr(ed.workspace, "_test", lambda: {"exit": 0, "output": "PASS\n"})
    assert call(ed, "test") == {"exit": 0, "output": "PASS\n"}
    ed.test_command("/test off")
    assert ed.workspace.command is None
    assert "Local tests exited with 0" in capsys.readouterr().out


def test_rejects_invalid_frames_and_connection_loss():
    for raw, message in ((struct.pack(">I", 0), "size"),
                         (struct.pack(">I", 3) + b"abc", "message"),
                         (struct.pack(">I", 2) + b"[]", "message")):
        r, w = os.pipe()
        with os.fdopen(r, "rb") as stream, os.fdopen(w, "wb", buffering=0) as dest:
            dest.write(raw)
            with pytest.raises(ValueError, match=message):
                ui.Channel(SimpleNamespace(stdout=stream)).receive()
    with pytest.raises(EOFError):
        with open(os.devnull, "rb") as closed:
            ui.Channel(SimpleNamespace(stdout=closed)).receive()


def test_multi_turn_real_stdio(project, monkeypatch, capsys):
    root, _ = project
    (root / "a.txt").write_text("old")
    peer = """
import json, sys, struct
def read():
 n = struct.unpack(">I", sys.stdin.buffer.read(4))[0]
 return json.loads(sys.stdin.buffer.read(n))
def send(**m):
 b=json.dumps(m).encode(); sys.stdout.buffer.write(struct.pack(">I",len(b))+b); sys.stdout.buffer.flush()
def tool(n,op,**fields):
 send(type="request",id=n,op=op,**fields)
 return read()
assert read()=={"type":"open","version":1}
send(type="ready", model="gemini-test")
assert read()["text"]=="edit"
assert tool(1,"read",path="a.txt")["result"]=="old"
assert tool(2,"approve",path="a.txt",content="new")["result"] is True
assert tool(3,"write",path="a.txt",content="new")["result"] is True
assert "error" in tool(4,"arbitrary",command="something")
send(type="text",text="completed"); send(type="end",reason="end_turn")
assert read()["text"]=="read"
assert tool(5,"read",path="a.txt")["result"]=="new"
send(type="text",text="second turn"); send(type="end",reason="end_turn")
assert read()=={"type":"close"}
"""
    prompts = iter(("/help", "/test npm", "edit", "read", "/exit"))
    monkeypatch.setattr("builtins.input", lambda _: next(prompts))
    ui.run(root, [sys.executable, "-I", "-c", peer], dict(os.environ),
           workspace_tools, project_files)
    assert (root / "a.txt").read_text() == "new"
    assert "second turn" in capsys.readouterr().out


def test_slot_components_work_from_older_signed_release_layout(tmp_path, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["slot_component"].__globals__
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path))
    def missing(*_):
        raise cli["CliError"]("absent")
    def fetch(command, request, destination, *_):
        ops = {"editor-client": "gemini_editor_client", "workspace-tools": "workspace_tools"}
        assert command == ["paired-transport"]
        destination.write_bytes((ROOT / "ccfleet_agent" / (ops[request["op"]] + ".py")).read_bytes())
    monkeypatch.setitem(scope, "packaged_helper", missing)
    from ccfleet_agent import client_release
    monkeypatch.setitem(scope, "client_release", lambda: client_release)
    monkeypatch.setitem(scope, "client_experience", lambda: SimpleNamespace(slot_artifact=fetch))
    def load(name, operation):
        raw = (ROOT / "ccfleet_agent" / (name + ".py")).read_bytes()
        return cli["slot_component"](name, hashlib.sha256(raw).hexdigest(), operation, ["paired-transport"])
    editor = load("gemini_editor_client", "editor-client")
    tools = load("workspace_tools", "workspace-tools")
    instance = editor.Editor(tmp_path, tools, project_files)
    instance.workspace.close()
    monkeypatch.setitem(scope, "client_experience", missing)
    assert load("gemini_editor_client", "editor-client").MAX_PROMPT == editor.MAX_PROMPT


def test_safe_terminal_display():
    assert ui.terminal_text("X\x1b]52;foo\x07\r\u202e!") == "X\\x1b]52;foo\\x07\\r\\u202e!"


def test_live_command_uses_existing_workspace_pairing_and_pinned_transport(tmp_path, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["cmd_gemini"].__globals__
    old = tmp_path / "ccfleet-home"
    project = tmp_path / "checkout"
    project.mkdir()
    monkeypatch.setenv("CCFLEET_HOME", str(old))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    device = {
        "server": "https://ccfleet.io", "endpoint": "wss://ccfleet.io/api/cli/connect",
        "slot_name": "private", "slot_id": "assigned", "device_id": "id",
    }
    def config():
        assert scope["home"]() == old / "antigravity-workspace"
        return {"devices": {"id": device}, "active": "id"}
    monkeypatch.setitem(scope, "load_config", config)
    def transport(d, *, tty, fleet_only):
        assert d == device and not tty and fleet_only
        return ["approved-ssh"]
    monkeypatch.setitem(scope, "ssh_transport_command", transport)
    def fail(*_):
        pytest.fail("live mode must not install or start an agent or ask for a new pairing")
    monkeypatch.setitem(scope, "cmd_login", fail)
    monkeypatch.setitem(scope, "client_experience", fail)
    monkeypatch.setitem(scope, "preview_helper", fail)
    calls = []
    def launch(path, argv, env, tools, files, runner):
        assert path == project.resolve() and argv == ["approved-ssh", "ccfleet-gemini-editor-v1"]
        assert env["CCFLEET_HOME"] == str(old / "antigravity-workspace")
        assert not any(k.startswith(("GOOGLE_", "GEMINI_")) for k in env)
        assert runner == "npm"
        assert callable(tools.Workspace) and callable(files._open_root)
        calls.append("launched")
    def component(name, digest, op, command):
        raw = (ROOT / "ccfleet_agent" / (name + ".py")).read_bytes()
        assert digest == hashlib.sha256(raw).hexdigest()
        assert command == ["approved-ssh", "ccfleet-gemini-release-v1"]
        if op == "workspace-tools":
            return workspace_tools
        assert op == "editor-client"
        return SimpleNamespace(run=launch)
    monkeypatch.setitem(scope, "slot_component", component)
    assert cli["main"](["gemini", "--project", str(project), "--test", "npm"]) == 0
    assert calls == ["launched"] and scope["home"]() == old


def test_start_and_transport_errors_do_not_echo_private_stderr(project, capfd):
    root, _ = project
    with pytest.raises(ValueError, match="OpenSSH installation"):
        ui.run(root, [str(root / "missing-ssh")], dict(os.environ), workspace_tools, project_files)
    fail = [sys.executable, "-I", "-c", "import sys;print('private slot detail',file=sys.stderr)"]
    with pytest.raises(ValueError, match="connection closed"):
        ui.run(root, fail, dict(os.environ), workspace_tools, project_files)
    assert "private slot detail" not in capfd.readouterr().err
