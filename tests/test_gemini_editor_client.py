import contextlib
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
        raw = (ROOT / "ccfleet_agent" / (ops[request["op"]] + ".py")).read_bytes()
        assert request == {"op": request["op"], "sha256": hashlib.sha256(raw).hexdigest()}
        destination.write_bytes(raw)
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


@pytest.mark.parametrize("start", ["explicit", "home", "root", "sensitive"])
def test_live_command_uses_existing_workspace_pairing_and_pinned_transport(tmp_path, monkeypatch, start):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["cmd_gemini"].__globals__
    old = tmp_path / "ccfleet-home"
    personal = tmp_path / "home"
    (personal / ".gemini").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(personal))
    project = {"explicit": tmp_path / "checkout", "home": personal, "root": Path("/"),
               "sensitive": personal / ".gemini"}[start]
    if start == "explicit":
        project.mkdir()
    else:
        monkeypatch.chdir(project)
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
    def launch(path, argv, env, tools, files, runner, *, fleet_home):
        assert fleet_home == old
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
    target = ["--project", str(project)] if start == "explicit" else []
    assert cli["main"](["gemini", *target, "--test", "npm"]) == 0
    assert calls == ["launched"] and scope["home"]() == old


def test_start_and_transport_errors_do_not_echo_private_stderr(project, capfd):
    root, _ = project
    with pytest.raises(ValueError, match="OpenSSH installation"):
        ui.run(root, [str(root / "missing-ssh")], dict(os.environ), workspace_tools, project_files)
    fail = [sys.executable, "-I", "-c", "import sys;print('private slot detail',file=sys.stderr)"]
    with pytest.raises(ValueError, match="connection closed"):
        ui.run(root, fail, dict(os.environ), workspace_tools, project_files)
    assert "private slot detail" not in capfd.readouterr().err


def test_incomplete_turn_reason_is_visible_and_local_approval_expires(project, capsys):
    _, ed = project
    assert call(ed, "approve", path="pending", content="new")
    reasons = iter(("max_tokens", "cancelled", "refusal", "private/slot/path", "end_turn"))
    channel = SimpleNamespace(send=lambda _msg: None,
                              receive=lambda: {"type": "end", "reason": next(reasons)})
    for expected in ("token limit reached", "cancelled", "request declined", "interrupted"):
        ui.turn(channel, ed, "continue")
        output = capsys.readouterr().out
        assert expected in output and "private/slot/path" not in output
        assert ed.approval is None
    ui.turn(channel, ed, "continue again")
    assert capsys.readouterr().out == "\n"


@pytest.mark.parametrize("start", ["home", "parent", "root", "protected"])
def test_editor_accepts_any_starting_directory(tmp_path, monkeypatch, start):
    personal = tmp_path / "account"
    private = personal / ".gemini"
    private.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(personal))
    chosen = {"home": personal, "parent": tmp_path, "root": Path("/"), "protected": private}[start]
    ed = ui.Editor(chosen, workspace_tools, project_files)
    try:
        assert ed.workspace.path == chosen.resolve()
        if start == "protected":
            with pytest.raises(ValueError, match="protected"):
                call(ed, "list", path=".")
        else:
            (personal / "notes.txt").write_text("original")
            name = str((personal / "notes.txt").resolve().relative_to(chosen.resolve()))
            assert call(ed, "read", path=name) == "original"
            monkeypatch.setattr(ui, "confirm", lambda _: True)
            assert call(ed, "approve", path=name, content="edited")
            assert call(ed, "write", path=name, content="edited")
            assert (personal / "notes.txt").read_text() == "edited"
        if start != "protected":
            with pytest.raises(ValueError, match="choose a project"):
                workspace_tools.Workspace(chosen, lambda *_: False)
    finally:
        ed.workspace.close()


def test_protected_operations_and_private_config_under_custom_path(tmp_path, monkeypatch):
    personal = tmp_path / "user"
    fleet = personal / "my-fleet"
    project = personal / "source"
    for d in (personal / "Library", personal / ".gemini", personal / ".local/share",
              personal / ".kube", personal / ".gnupg", personal / ".password-store",
              fleet, project, project / ".git"):
        d.mkdir(parents=True, exist_ok=True)
        (d / "record").write_text("private test data")
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setattr(ui, "confirm", lambda _: pytest.fail("private path reached approval"))
    for root in (personal, tmp_path, Path("/")):
        ed = ui.Editor(root, workspace_tools, project_files, fleet_home=fleet)
        prefix = str(personal.resolve().relative_to(root.resolve()))
        try:
            names = call(ed, "list", path=prefix)
            assert "source/" in names and "my-fleet/" not in names and "Library/" not in names
            assert ".gemini/" not in names and ".kube/" not in names
            assert "share/" not in call(ed, "list", path=str(Path(prefix) / ".local"))
            for protected in (".gemini", "LIBRARY", ".local/share", ".kube", ".gnupg",
                              ".password-store", "my-fleet", "source/.git"):
                name = str(Path(prefix) / protected)
                with pytest.raises(ValueError):
                    call(ed, "read", path=name + "/record")
                with pytest.raises(ValueError):
                    call(ed, "approve", path=name + "/new", content="unavailable")
                with pytest.raises(ValueError):
                    call(ed, "mkdir", path=name + "/dir")
                with pytest.raises(ValueError):
                    call(ed, "list", path=name)
        finally:
            ed.workspace.close()


def test_test_subfolder_is_chosen_by_local_user(tmp_path, monkeypatch):
    personal = tmp_path / "user"
    project = personal / "my project"
    project.mkdir(parents=True)
    config = personal / "private-fleet"
    config.mkdir()
    monkeypatch.setenv("HOME", str(personal))
    ed = ui.Editor(personal, workspace_tools, project_files, "npm", fleet_home=config)
    try:
        assert ed.workspace.test_directory() is None
        with pytest.raises(ValueError, match="subfolder"):
            ed.workspace.call("test_project", {})
        answers = iter([""])
        monkeypatch.setattr("builtins.input", lambda _: next(answers))
        with pytest.raises(ValueError, match="not selected"):
            call(ed, "test")
        ed.test_command("/test npm private-fleet")
        assert ed.workspace.test_directory() is None
        (personal / "alias").symlink_to(project, target_is_directory=True)
        ed.test_command("/test npm alias")
        assert ed.workspace.test_directory() is None
        ed.test_command("/test npm my project")
        assert ed.workspace.test_directory() == project.resolve()
        questions = []
        monkeypatch.setattr(ui, "confirm", lambda message: questions.append(message) or False)
        with pytest.raises(ValueError, match="approved"):
            call(ed, "test")
        assert questions == ["Run local tests on this Mac (npm test in my project)?"]
        ed.test_command("/test off")
        assert ed.workspace.command is None
    finally:
        ed.workspace.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="uses the macOS test sandbox")
def test_approved_subfolder_test_cannot_read_sibling_credentials(tmp_path, monkeypatch):
    personal = tmp_path / "home"
    project = personal / "project"
    private = personal / "private-fleet"
    project.mkdir(parents=True)
    private.mkdir()
    secret = private / "synthetic"
    secret.write_text("fixture")
    runner = project / "go"
    runner.write_text(
        '#!/bin/sh\nif /bin/cat "$PWD/../private-fleet/synthetic" 2>/dev/null; '
        'then exit 1; fi\n/bin/cat answer\n'
    )
    runner.chmod(0o700)
    (project / "answer").write_text("verified")
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setattr(ui, "confirm", lambda _: True)
    ed = ui.Editor(personal, workspace_tools, project_files, "go", fleet_home=private)
    try:
        ed.workspace.test_path = str(project)
        ed.test_command("/test go ./project")
        result = call(ed, "test")
        assert result == {"exit": 0, "output": "verified"}
    finally:
        ed.workspace.close()



def test_cold_component_on_old_slot_reports_required_update(tmp_path, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["slot_component"].__globals__
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path))
    def missing(*_):
        raise cli["CliError"]("missing")
    def old_slot(command, request, *_):
        assert request == {"op": "workspace-tools", "sha256": "1" * 64}
        raise ValueError("a 0.2.15 slot rejects digest requests")
    from ccfleet_agent import client_release
    monkeypatch.setitem(scope, "packaged_helper", missing)
    monkeypatch.setitem(scope, "client_release", lambda: client_release)
    monkeypatch.setitem(scope, "client_experience", lambda: SimpleNamespace(slot_artifact=old_slot))
    with pytest.raises(cli["CliError"], match="update its CCFleet slot service"):
        cli["slot_component"]("workspace_tools", "1" * 64, "workspace-tools", ["paired-slot"])
    assert not list((tmp_path / "editor-components").glob("*.py"))


def menu_session(editor, answers, frames, monkeypatch):
    sent = []
    replies = iter(frames)
    inputs = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    channel = SimpleNamespace(send=sent.append, receive=lambda **_: next(replies),
                              disconnected=lambda: False)
    with contextlib.ExitStack() as stack:
        connection = ui.TerminalSession(editor, lambda: contextlib.nullcontext(channel), stack)
        ui.session(connection)
        connection.channel.send({"type": "close"})
    assert next(inputs, None) is None
    assert next(replies, None) is None
    return sent


def test_help_model_picker_and_command_typos_stay_out_of_chat(project, monkeypatch, capsys):
    _, editor = project
    flash, pro = "gemini-flash-low", "gemini-pro-high"
    sent = menu_session(editor, [
        "/help", "/modle", "/test", "/model", "", "/model", "2", "/model", "",
        "/model " + pro, "/model unknown", "/model 0", "/model 3",
        "/model " + flash, "/model 2", "/model " + flash,
        "/example/project/app.py please review", "/quit",
    ], [
        {"type": "ready", "model": flash, "models": [flash, pro]},
        {"type": "model", "model": pro},
        {"type": "model", "model": pro, "error": "private slot diagnostic"},
        {"type": "model", "model": flash},
        {"type": "end", "reason": "end_turn"},
    ], monkeypatch)
    assert sent == [
        {"type": "open", "version": 1}, {"type": "set_model", "model": pro},
        {"type": "set_model", "model": flash},
        {"type": "set_model", "model": flash},
        {"type": "prompt", "text": "/example/project/app.py please review"}, {"type": "close"},
    ]
    out = capsys.readouterr().out
    assert all(command in out for command in ("/help", "/model <number or ID>", "/test off", "/quit"))
    assert "Unknown command /modle" in out
    assert f"1. {flash} (current)" in out and f"2. {pro} (current)" in out
    assert f"Switched to {pro}." in out and f"Using {pro}." in out
    assert out.count(f"Switched to {flash}.") == 1 and "private slot diagnostic" not in out


def test_model_on_an_older_slot_keeps_working_conversation(project, monkeypatch, capsys):
    _, editor = project
    sent = menu_session(editor, ["/help", "/model", "/model 2", "hello", "/exit"], [
        {"type": "ready", "model": "gemini-flash"},
        {"type": "end", "reason": "end_turn"},
    ], monkeypatch)
    assert sent == [{"type": "open", "version": 1}, {"type": "prompt", "text": "hello"},
                    {"type": "close"}]
    out = capsys.readouterr().out
    assert "gemini-flash" in out and "Update the private Mac slot service" in out


@pytest.mark.parametrize("catalog", [
    [], "gemini-flash", ["gemini-pro"], ["gemini-flash", "gemini-flash"],
    ["gemini-flash", "gemini-invalid\x1b"], ["gemini-flash", {}],
])
def test_model_metadata_is_checked_before_rendering(project, monkeypatch, catalog):
    _, editor = project
    with pytest.raises(ValueError, match="invalid Gemini model list"):
        menu_session(editor, [], [{"type": "ready", "model": "gemini-flash", "models": catalog}],
                     monkeypatch)


def test_model_picker_refuses_unconfirmed_model(project, monkeypatch, capsys):
    _, editor = project
    with pytest.raises(ValueError, match="did not confirm"):
        menu_session(editor, ["/model 2"], [
            {"type": "ready", "model": "gemini-a", "models": ["gemini-a", "gemini-b"]},
            {"type": "text", "text": "private response"},
        ], monkeypatch)
    assert "private response" not in capsys.readouterr().out
