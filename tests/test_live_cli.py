"""Live command UX uses real file-boundary helpers and only synthetic local homes."""

from __future__ import annotations

import json
import runpy
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import live_client, live_files

ROOT = Path(__file__).resolve().parents[1]


def test_published_helper_digests_load_exact_live_modules():
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    for name, constant in (("live_files", "LIVE_FILES_SHA256"),
                           ("live_client", "LIVE_CLIENT_SHA256")):
        helper = cli["packaged_helper"](name, cli[constant])
        assert Path(helper.__file__) == ROOT / "ccfleet_agent" / (name + ".py")


@pytest.fixture
def client(tmp_path, monkeypatch):
    personal = tmp_path.resolve() / "synthetic-home"
    personal.mkdir()
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setenv("CCFLEET_HOME", str(personal / ".fleet-control"))
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["cmd_local"].__globals__
    monkeypatch.setitem(scope, "live_files", lambda: live_files)
    monkeypatch.setitem(scope, "live_client", lambda: live_client)
    monkeypatch.setitem(scope, "project_files", lambda: pytest.fail("live command scanned a snapshot"))
    monkeypatch.setitem(scope, "project_rpc", lambda *a, **kw: pytest.fail("live used snapshot RPC"))
    monkeypatch.setattr(cli["shutil"], "which", lambda _: "/usr/bin/ssh")
    return cli, personal, scope


@pytest.fixture
def connected(client, monkeypatch):
    cli, personal, scope = client
    cli["ensure_home"]()
    private = cli["home"]()
    key, pin = private / "device-key", private / "host-pin"
    key.write_text("synthetic-key")
    pin.write_text("synthetic-pin")
    device = {"slot_id": "slot-test", "slot_name": "test-slot", "device_id": "device-test",
              "device_token": "synthetic-device-token", "host_alias": "ccfleet-slot-test",
              "key": str(key), "known_hosts": str(pin), "user": "slot01",
              "server": "https://fleet.invalid", "endpoint": "wss://fleet.invalid/connect"}
    cli["save_config"]({"version": 1, "devices": {device["device_id"]: device},
                        "active": device["device_id"]})
    folder = personal / "PRIVATE-HOST-FOLDER"
    folder.mkdir()
    (folder / "source.py").write_text("FILE_CONTENT_SENTINEL_NEVER_IN_CONTROL")
    events, commands = [], []

    def control(_device, project, action="status", **fields):
        events.append(("remote", project, action, fields))
        return {"version": 1, "ok": True, "ready": True, "mounted": True,
                "connected": True, "sessions": ["main", "work"], "stopped": action == "stop"}

    monkeypatch.setitem(scope, "live_control", control)
    monkeypatch.setitem(scope, "start_local_connector", lambda identifier: events.append(
        ("start", identifier)))
    monkeypatch.setitem(scope, "stop_local_connector", lambda identifier: events.append(
        ("stop", identifier)))
    monkeypatch.setattr(subprocess, "call", lambda command, **kw: (
        commands.append((command, kw)) or 0))
    return SimpleNamespace(cli=cli, home=personal, scope=scope, folder=folder, private=private,
                           device=device, events=events, commands=commands)


def launch(state, *flags, folder=None):
    return state.cli["main"](["local", "--project", str(folder or state.folder), *flags])


def record(state, folder=None):
    return state.cli["live_record"](state.device, folder or state.folder)[1]


def test_home_can_be_connected_without_snapshot_scan_or_separate_home_flag(connected, monkeypatch):
    state = connected
    (state.home / ".env").write_text("synthetic selected content")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert launch(state, "--yes", folder=state.home) == 0
    saved = record(state, state.home)
    assert saved["trusted"] is True
    assert saved["root"] == str(state.home)
    assert "projects" not in state.cli["load_config"]()
    assert any(event[0] == "start" for event in state.events)
    assert "ccfleet-live-session-v1" in state.commands[0][0]


def test_first_folder_requires_explicit_grant_before_connector_start(connected, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert launch(connected) == 2
    assert connected.commands == []
    assert all(event[0] == "remote" and event[2] == "status" for event in connected.events)
    assert "live_folders" not in connected.cli["load_config"]()


def test_declining_interactive_trust_does_not_start_access(connected, monkeypatch):
    prompts = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda text: prompts.append(text) or "no")
    assert launch(connected, folder=connected.home) == 2
    assert len(prompts) == 1 and "Trust this folder" in prompts[0]
    assert not any(event[0] == "start" for event in connected.events)
    assert connected.commands == []


def test_folder_trust_is_remembered_for_same_device_and_root(connected, monkeypatch):
    prompts = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda text: prompts.append(text) or "yes")
    assert launch(connected) == 0
    identifier = record(connected)["id"]
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("existing trust was not reused"))
    assert launch(connected) == 0
    assert len(prompts) == 1 and record(connected)["id"] == identifier


def test_initial_control_requests_export_no_file_content_or_host_identity(connected, monkeypatch):
    monkeypatch.setenv("HOST_PRIVATE_SENTINEL", "ENVIRONMENT_IDENTITY_SENTINEL")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://must-not-use.invalid")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "LOCAL_TOKEN_SENTINEL")
    monkeypatch.setenv("TZ", "Private/Timezone")
    assert launch(connected, "--yes") == 0
    wire = json.dumps(connected.events)
    for forbidden in (str(connected.folder), "PRIVATE-HOST-FOLDER", "FILE_CONTENT_SENTINEL",
                      "ENVIRONMENT_IDENTITY_SENTINEL", "LOCAL_TOKEN_SENTINEL", "Private/Timezone"):
        assert forbidden not in wire
    command, options = connected.commands[0]
    assert command[0] == "ssh"
    assert not any(part == "claude" or part.endswith("/claude") for part in command)
    assert "-I" in next(part for part in command if part.startswith("ProxyCommand="))
    assert options["env"]["TZ"] == "UTC"
    assert set(options["env"]) == {"PATH", "HOME", "TERM", "LANG", "LC_ALL", "TZ", "CCFLEET_HOME"}


def test_new_live_session_defaults_to_bypass_opus_max_and_warns_about_live_writes(connected, capsys):
    assert launch(connected, "--yes", "--new", "--name", "work") == 0
    assert connected.commands[0][0][-5:] == ["new", "work", "bypassPermissions", "opus", "max"]
    output = capsys.readouterr().out
    assert "Edits and deletions take effect locally immediately" in output
    assert "Hidden files and Git-ignored files are available" in output
    assert "without prompts" in output and "Access stays active after this terminal closes" in output


def test_live_mode_model_and_effort_flags_reach_slot_session(connected):
    assert launch(connected, "--yes", "--new", "--name", "research", "--mode=plan",
                  "--model", "fable", "--effort", "xhigh") == 0
    assert connected.commands[0][0][-5:] == ["new", "research", "plan", "fable", "xhigh"]


def test_readiness_check_does_not_resolve_or_scan_nonexistent_folder(connected, monkeypatch):
    monkeypatch.setitem(connected.scope, "live_files", lambda: pytest.fail("check loaded filesystem"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    missing = connected.home / "does-not-exist"
    assert launch(connected, "--check", folder=missing) == 0
    assert connected.events == [("remote", "0" * 32, "status", {})]
    assert connected.commands == []
    assert "live_folders" not in connected.cli["load_config"]()
    assert not missing.exists()


def test_disconnect_stops_local_bridge_before_remote_sessions_and_never_restarts(connected):
    assert launch(connected, "--yes") == 0
    identifier = record(connected)["id"]
    connected.events.clear()
    connected.commands.clear()
    assert launch(connected, "--disconnect") == 0
    assert connected.events == [("stop", identifier), ("remote", identifier, "stop", {})]
    assert connected.commands == []


def test_disconnect_still_revokes_folder_access_after_folder_is_moved(connected):
    assert launch(connected, "--yes") == 0
    identifier = record(connected)["id"]
    connected.folder.rename(connected.home / "moved-folder")
    connected.events.clear()
    connected.commands.clear()
    assert launch(connected, "--disconnect") == 0
    assert connected.events == [("stop", identifier), ("remote", identifier, "stop", {})]
    assert connected.commands == []


def test_reset_requires_confirmation_before_stopping_either_side(connected, monkeypatch):
    assert launch(connected, "--yes") == 0
    connected.events.clear()
    connected.commands.clear()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert launch(connected, "--reset-link") == 2
    assert connected.events == [] and connected.commands == []


def test_explicit_reset_stops_both_sides_then_reconnects_same_folder(connected, capsys):
    assert launch(connected, "--yes") == 0
    identifier = record(connected)["id"]
    connected.events.clear()
    connected.commands.clear()
    assert launch(connected, "--reset-link", "--yes") == 0
    assert connected.events[:2] == [("stop", identifier), ("remote", identifier, "stop", {})]
    assert ("start", identifier) in connected.events[2:]
    assert record(connected)["id"] == identifier
    assert "invalidates open files" in capsys.readouterr().out


def test_hour_long_live_session_gets_fresh_reconnect_budget_and_never_replays_new(connected, monkeypatch):
    clock = {"now": 0.0}
    calls = connected.commands

    def terminal(command, **kwargs):
        calls.append((command, kwargs))
        clock["now"] += 3600 if len(calls) == 1 else 1
        return 255 if len(calls) == 1 else 0

    monkeypatch.setattr(subprocess, "call", terminal)
    monkeypatch.setattr(connected.cli["time"], "monotonic", lambda: clock["now"])
    monkeypatch.setattr(connected.cli["time"], "sleep",
                        lambda delay: clock.update(now=clock["now"] + delay))
    assert launch(connected, "--yes", "--new", "--name", "work") == 0
    assert len(calls) == 2
    assert calls[0][0][-5] == "new" and calls[1][0][-5] == "open"
    assert calls[0][0][-6] == calls[1][0][-6]


def test_live_no_reconnect_flag_returns_connection_failure(connected, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda *a, **kw: calls.append(a) or 255)
    assert launch(connected, "--yes", "--no-reconnect") == 255
    assert len(calls) == 1


def sftp_open_result(server, relative, flags=1):
    raw = relative.encode()
    body = b"\x03" + struct.pack("!II", 1, len(raw)) + raw + struct.pack("!II", flags, 0)
    result = server.handle_packet(struct.pack("!I", len(body)) + body)
    return result[4], struct.unpack("!I", result[9:13])[0]


def test_client_helpers_interpreter_and_runtime_are_protected_with_real_sftp(connected, monkeypatch):
    state = connected
    executable = state.home / "bin/ccfleet"
    executable.parent.mkdir()
    executable.write_text("client code")
    helper = state.home / "bin/helper.py"
    helper.write_text("helper code")
    runtime = state.home / "python-runtime"
    (runtime / "bin").mkdir(parents=True)
    interpreter = runtime / "bin/python3"
    interpreter.write_text("python runtime")
    stdlib = runtime / "site.py"
    stdlib.write_text("protected import")
    monkeypatch.setitem(state.scope, "__file__", str(executable))
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setattr(sys, "prefix", str(runtime))
    monkeypatch.setattr(sys, "base_prefix", str(runtime))
    protected = state.cli["live_protected"](state.home, SimpleNamespace(__file__=str(helper)))
    with pytest.raises(state.cli["CliError"], match="control directory"):
        state.cli["live_protected"](runtime)
    server = live_files.SFTPServer(state.home, protected)
    try:
        server.handle_packet(struct.pack("!IBI", 5, 1, 3))
        for path in (executable, helper, interpreter, stdlib, state.private / "config.json",
                     Path(state.device["key"]), Path(state.device["known_hosts"])):
            before = path.read_bytes()
            assert sftp_open_result(server, path.relative_to(state.home).as_posix(), 3 | 16) == (
                101, live_files.DENIED)
            assert path.read_bytes() == before
        visible = state.home / ".ssh"
        visible.mkdir()
        (visible / "synthetic-key").write_text("explicitly shared dummy content")
        assert sftp_open_result(server, ".ssh/synthetic-key")[0] == 102
    finally:
        server.close()


def test_live_and_legacy_snapshot_commands_dispatch_separately(client, monkeypatch):
    cli, _, scope = client
    seen = []
    monkeypatch.setitem(scope, "cmd_local", lambda args: seen.append(("live", args.command)) or 0)
    monkeypatch.setitem(scope, "cmd_snapshot", lambda args: seen.append(("snapshot", args.command)) or 0)
    monkeypatch.setitem(scope, "cmd_project", lambda args: seen.append(("legacy", args.command)) or 0)
    assert cli["main"](["local"]) == 0
    assert cli["main"](["snapshot"]) == 0
    assert cli["main"](["project", "status"]) == 0
    assert seen == [("live", "local"), ("snapshot", "snapshot"), ("legacy", "project")]
