"""Signed CLI entry point and existing private-slot pairing for external ACP editors."""

import hashlib
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def cli_scope(tmp_path, monkeypatch):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "ccfleet"))
    return cli, cli["cmd_gemini"].__globals__


def test_acp_cli_launches_pinned_bridge_without_terminal_or_provider_credentials(
    tmp_path, monkeypatch, capsys
):
    cli, scope = cli_scope(tmp_path, monkeypatch)
    root = (tmp_path / "ccfleet").resolve()
    monkeypatch.setenv("GEMINI_API_KEY", "test-value-not-for-transport")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://invalid.example")
    device = {"server": "https://ccfleet.io", "endpoint": "wss://ccfleet.io/api/cli/connect",
              "device_id": "test", "slot_id": "only", "slot_name": "Owner"}
    monkeypatch.setitem(scope, "load_config", lambda: {"devices": {"test": device}})
    def ssh(selected, *, tty, fleet_only):
        assert selected is device and not tty and fleet_only
        assert scope["home"]() == root / "antigravity-workspace"
        return ["assigned-slot"]
    monkeypatch.setitem(scope, "ssh_transport_command", ssh)
    tools, editor, files = object(), object(), object()
    monkeypatch.setitem(scope, "project_files", lambda: files)
    def run(command, environment, editor_module, tools_module, file_module, *,
            fleet_home, test, test_folder):
        assert command == ["assigned-slot", "ccfleet-gemini-editor-v1"]
        assert (editor_module, tools_module, file_module) == (editor, tools, files)
        assert fleet_home == root and test == "npm" and test_folder == "packages/app"
        assert environment["CCFLEET_HOME"] == str(root / "antigravity-workspace")
        assert not any(k.startswith(("GEMINI_", "GOOGLE_")) for k in environment)
        return 0
    calls = []
    def component(name, digest, op, command):
        assert command == ["assigned-slot", "ccfleet-gemini-release-v1"]
        raw = (ROOT / "ccfleet_agent" / (name + ".py")).read_bytes()
        assert digest == hashlib.sha256(raw).hexdigest()
        calls.append(op)
        return {"editor-client": editor, "workspace-tools": tools,
                "acp-client": SimpleNamespace(run=run)}[op]
    monkeypatch.setitem(scope, "slot_component", component)
    assert cli["main"](["gemini", "--acp", "--slot", "only", "--test", "npm",
                        "--test-folder", "packages/app"]) == 0
    assert calls == ["editor-client", "workspace-tools", "acp-client"]
    assert Path(os.environ["CCFLEET_HOME"]) == root
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("args", "message"), [
    (["--acp"], "no Gemini workspace pairing"),
    (["--acp", "--pair"], "pair this Mac first"),
    (["--acp", "--project", "/tmp"], "T3 selects"),
    (["--acp", "--test-folder", "subfolder"], "--test-folder requires"),
    (["--acp", "--test", "npm", "--test-folder", "/"], "relative to the local project"),
    (["--acp", "--test", "npm", "--test-folder", "../path"], "relative to the local project"),
    (["--test-folder", "subfolder", "--test", "npm"], "--test-folder requires"),
])
def test_acp_preflight_errors_keep_stdout_clear(tmp_path, monkeypatch, capsys, args, message):
    cli, scope = cli_scope(tmp_path, monkeypatch)
    monkeypatch.setitem(scope, "cmd_login", lambda *a: pytest.fail("unexpected interactive login"))
    assert cli["main"](["gemini", *args]) == 2
    captured = capsys.readouterr()
    assert not captured.out and message in captured.err
    assert scope["home"]() == tmp_path / "ccfleet"


def test_preview_and_acp_are_exclusive(tmp_path, monkeypatch, capsys):
    cli, _ = cli_scope(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cli["main"](["gemini", "--preview", "--acp"])
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


def test_help_explains_t3_local_provider_and_supervised_approvals(capsys):
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    with pytest.raises(SystemExit) as exc:
        cli["main"](["gemini", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for word in ("--acp", "local environment", "Local ACP command",
                 "gemini and --acp as separate arguments", "Supervised"):
        assert word in help_text
