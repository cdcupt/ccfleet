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


@pytest.mark.parametrize("prefix", ["agy1-ccf_enroll_", "agy1-ccf_pair_"])
def test_pair_without_chat_preserves_claude_and_reuses_saved_device(
        tmp_path, monkeypatch, capsys, prefix):
    cli, scope = cli_scope(tmp_path, monkeypatch)
    root = scope["home"]()
    root.mkdir()
    claude = root / "config.json"
    claude.write_text('{"devices":{"claude":{}}}')
    code = prefix + "fixture"
    monkeypatch.setitem(scope, "prompt_pairing_code", lambda prompt: code)
    calls = []

    def register(url, bearer, payload, *, fleet_only):
        assert fleet_only and url == "https://ccfleet.io/api/cli/register"
        assert bearer == code[5:] and payload["purpose"] == "antigravity-workspace"
        assert payload["device_name"] == "Work Mac" and payload["public_key"].startswith("ssh-ed25519 ")
        calls.append(url)
        return {"workspace_only": True, "slot_name": "Private Mac", "slot_id": "mini",
                "device_id": "dev_test", "device_token": "ccf_dev_fixture", "user": "slot",
                "host_key": "ssh-ed25519 fixture", "endpoint": "wss://ccfleet.io/api/cli/connect"}

    monkeypatch.setitem(scope, "api_json", register)
    for name in ("ssh_transport_command", "existing_directory", "slot_component", "installed_agent"):
        monkeypatch.setitem(scope, name, lambda *a, **k: pytest.fail("pair launched an agent"))
    assert cli["main"](["pair", "gemini", "--name", "Work Mac"]) == 0
    monkeypatch.setitem(scope, "prompt_pairing_code", lambda *a: pytest.fail("prompted twice"))
    assert cli["main"](["pair", "gemini"]) == 0
    assert len(calls) == 1 and scope["home"]() == root
    assert claude.read_text() == '{"devices":{"claude":{}}}'
    stored = (root / "antigravity-workspace" / "config.json").read_text()
    assert "ccf_dev_fixture" in stored and code[5:] not in stored
    output = capsys.readouterr().out
    assert "already saved" in output and "Local ACP command" in output and code[5:] not in output


def test_failed_explicit_replacement_keeps_existing_workspace(tmp_path, monkeypatch, capsys):
    cli, scope = cli_scope(tmp_path, monkeypatch)
    with scope["gemini_profile"]():
        scope["save_config"]({"active": "old", "devices": {"old": {"slot_id": "mini"}}})
        previous = scope["config_path"]().read_bytes()
    code = "agy1-ccf_enroll_invalid"
    monkeypatch.setitem(scope, "prompt_pairing_code", lambda prompt: code)

    def rejected(*args, **kwargs):
        raise cli["CliError"]("code disabled")

    monkeypatch.setitem(scope, "api_json", rejected)
    assert cli["main"](["pair", "gemini", "--replace"]) == 2
    with scope["gemini_profile"]():
        assert scope["config_path"]().read_bytes() == previous
        assert not list(scope["home"]().glob(".pairing-*"))
    out = capsys.readouterr()
    assert "code disabled" in out.err and code not in out.err + out.out
