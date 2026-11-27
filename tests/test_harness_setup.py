"""Exercise harness setup through the shipped laptop CLI."""
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

CLIENT = Path(__file__).resolve().parents[1] / "laptop/ccfleet"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    for key in ("HERDR_PANE_ID", "HERDR_APP_HOME", "HERDR_BIN_PATH", "ZDOTDIR",
                "T3CODE_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CCFLEET_HOME", str(tmp_path / "fleet"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    return runpy.run_path(str(CLIENT))["main"]


def test_herdr_desktop_merges_existing_settings_and_preserves_symlink(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_PANE_ID", "pane")
    monkeypatch.setenv("HERDR_APP_HOME", str(tmp_path))
    path = tmp_path / "programs.json"
    real = tmp_path / "tracked.json"
    real.write_text('{"programs":{"claude":{"name":"Custom","run":["claude"]},"git":{}},"shells":["sh"]}')
    real.chmod(0o640)
    path.symlink_to(real)
    for _ in range(2):
        assert cli(["herdr", "setup", "--desktop"]) == 0
    assert json.loads(path.read_text()) == {
        "programs": {"claude": {"name": "Custom", "run": ["ccfleet", "claude", "--resume"]},
                     "git": {}}, "shells": ["sh"]}
    assert path.is_symlink() and real.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("content", [
    "{", "[]", '{"programs":null}', '{"programs":{"claude":[]}}', '{"p":1,"p":2}', '{"key":NaN}',
])
def test_herdr_invalid_settings_survive(cli, tmp_path, monkeypatch, content):
    monkeypatch.setenv("HERDR_APP_HOME", str(tmp_path))
    path = tmp_path / "programs.json"
    path.write_text(content)
    assert cli(["herdr", "setup"]) == 2
    assert path.read_text() == content


def test_tui_forwards_session_only_inside_herdr(cli, tmp_path, monkeypatch):
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh is required to exercise Herdr zsh panes")
    fake = tmp_path / "herdr"
    fake.write_text('#!/bin/sh\n[ "$*" = "integration install claude" ]\n')
    fake.chmod(0o700)
    monkeypatch.setenv("HERDR_BIN_PATH", str(fake))
    monkeypatch.setenv("HERDR_PANE_ID", "pane")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "native"))
    dot = tmp_path / "dotfiles"
    dot.mkdir()
    rc = dot / ".zshrc"
    rc.write_text("# my config")
    monkeypatch.setenv("ZDOTDIR", str(dot))
    assert cli(["herdr", "setup"]) == 0
    assert cli(["herdr", "setup", "--tui"]) == 0
    assert (tmp_path / "native").is_dir()
    assert rc.read_text().startswith("# my config\n")
    assert rc.read_text().count("# >>> CCFleet") == 1
    assert not (tmp_path / ".config/herdr-app").exists()
    for program in ("ccfleet", "claude"):
        exe = tmp_path / program
        exe.write_text(f'#!/bin/sh\nprintf "%s\\n" {program} "$@"\n')
        exe.chmod(0o700)
    for pane, args, expected in [
        ("pane", ["--resume", "saved id"], ["ccfleet", "claude", "--resume", "saved id"]),
        ("pane", ["--resume"], ["ccfleet", "claude", "--resume"]),
        ("pane", ["--version"], ["claude", "--version"]),
        ("", ["--resume", "a"], ["claude", "--resume", "a"]),
    ]:
        env = {**os.environ, "PATH": str(tmp_path) + ":" + os.defpath, "HERDR_PANE_ID": pane}
        result = subprocess.run([zsh, "-i", "-c", 'claude "$@"',
                                 "test", *args], env=env, capture_output=True, text=True, check=True)
        assert result.stdout.splitlines() == expected

    # A pre-existing alias or function still handles ordinary Claude launches in Herdr.
    for custom in ("alias claude='claude --custom'", 'function claude { command claude --custom "$@"; }'):
        rc.write_text(custom + "\n" + cli.__globals__["HERDR_ZSH_RESUME"] + "\n")
        env["HERDR_PANE_ID"] = "pane"
        for argv, expect in [(["--version"], ["claude", "--custom", "--version"]),
                             (["--resume", "id"], ["ccfleet", "claude", "--resume", "id"])]:
            result = subprocess.run([zsh, "-i", "-c", 'claude "$@"', "test", *argv],
                                    env=env, capture_output=True, text=True, check=True)
            assert result.stdout.splitlines() == expect


def test_tui_failed_hook_install_leaves_rc_alone(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_BIN_PATH", "/usr/bin/false")
    assert cli(["herdr", "setup", "--tui"]) == 2
    assert not (tmp_path / ".zshrc").exists()


def test_t3_builds_separate_providers_and_reuses_launcher(cli, tmp_path, monkeypatch):
    directory = tmp_path / "t3"
    monkeypatch.setenv("T3CODE_HOME", str(directory))
    path = directory / "userdata/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"theme":"dark","providerInstances":{"claude":{"driver":"claudeAgent"}}}')
    scope = cli.__globals__
    slot = "example-slot"
    route = {"slot_id": slot, "gateway": {"base_url": "https://fleet.invalid"}, "provider": "anthropic"}
    monkeypatch.setitem(scope, "local_route", lambda *args, **kwargs: route)
    monkeypatch.setitem(scope, "installed_agent", lambda _: None)
    for _ in range(2):
        assert cli(["t3", "setup", "--slot", slot]) == 0
    assert cli(["t3", "setup"]) == 0
    data = json.loads(path.read_text())
    providers = data["providerInstances"]
    assert len(providers) == 3
    assert data["theme"] == "dark" and providers["claude"] == {"driver": "claudeAgent"}
    assert providers["ccfleet-gemini"]["config"] == {
        "source": "local", "commandPath": str(CLIENT), "commandArgs": ["gemini", "--acp"]}
    custom = next(p for p in providers.values() if p.get("displayName") == f"CCFleet Claude ({slot})")
    launcher = Path(custom["config"]["binaryPath"])
    assert custom["driver"] == "claudeAgent" and os.access(launcher, os.X_OK)
    profile = json.loads((launcher.parent / "profile.json").read_text())
    assert profile["slot_id"] == slot and "__external-claude" in launcher.read_text()


def test_t3_refuses_existing_conflicting_provider(cli, tmp_path):
    path = tmp_path / ".t3/userdata/settings.json"
    path.parent.mkdir(parents=True)
    original = '{"providerInstances":{"ccfleet-gemini":{"driver":"codex"}}}'
    path.write_text(original)
    assert cli(["t3", "setup"]) == 2
    assert path.read_text() == original


def test_orca_recovers_partial_setup_and_keeps_other_commands(cli, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_USER_DATA_PATH", str(tmp_path))
    metadata = tmp_path / "orca-runtime.json"
    commands = [{"id": "my-tool", "command": "pwd"}]
    calls = []
    errors = []
    # AF_UNIX paths must fit in sockaddr_un, including under long CI workspace paths.
    with tempfile.TemporaryDirectory(dir="/tmp") as short, socket.socket(socket.AF_UNIX) as server:
        address = str(Path(short) / "orca.sock")
        server.bind(address)
        server.listen()
        server.settimeout(3)
        metadata.write_text(json.dumps({"transports": [{"kind": "unix", "endpoint": address}],
                                        "authToken": "synthetic-token"}))
        metadata.chmod(0o600)

        def serve():
            try:
                for _ in range(6):
                    conn, _ = server.accept()
                    with conn, conn.makefile("rb") as stream:
                        request = json.loads(stream.readline())
                        assert request["authToken"] == "synthetic-token"
                        method = request["method"]
                        calls.append(method)
                        ok = True
                        if method == "settings.updateTerminalQuickCommands":
                            mutation = request["params"]["mutation"]
                            assert mutation["type"] == "upsert"
                            if len(calls) == 3:
                                ok = False
                            else:
                                commands.append(mutation["command"])
                        else:
                            assert method == "settings.getTerminalQuickCommands"
                        response = {"id": request["id"], "ok": ok,
                                    "result": {"terminalQuickCommands": commands}}
                        conn.sendall(b'{"_keepalive":true}\n' + json.dumps(response).encode() + b"\n")
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        assert cli(["orca", "setup"]) == 2
        assert cli(["orca", "setup"]) == 0
        assert cli(["orca", "setup"]) == 0
        worker.join(4)
        assert not worker.is_alive() and not errors
    assert [item["command"] for item in commands] == ["pwd", "ccfleet claude --resume", "ccfleet gemini"]
    assert commands[1]["scope"] == {"type": "global"} and commands[1]["appendEnter"]


def test_public_help_works_without_pairing(tmp_path):
    args = [sys.executable, "-I", str(CLIENT), "orca", "setup", "--help"]
    result = subprocess.run(args, env={"HOME": str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0 and "orca setup" in result.stdout
    assert not list(tmp_path.iterdir())
