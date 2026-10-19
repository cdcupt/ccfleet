"""Audit public argparse help without a native client, network or personal state."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "laptop/ccfleet"

# A malformed help path must fail rather than reach a real transport or process.
# These guards live only inside the disposable child, never in the tested client.
HELP_CHILD = """
import os, runpy, socket, ssl, subprocess, sys, urllib.request
def forbidden(*args, **kwargs):
    raise AssertionError('help attempted a transport or child process')
socket.socket = forbidden
socket.create_connection = forbidden
subprocess.Popen = forbidden
for name in ('system', 'fork', 'forkpty', 'posix_spawn', 'posix_spawnp', 'execv',
             'execve', 'execvp', 'execvpe', 'execl', 'execle', 'execlp', 'execlpe'):
    if hasattr(os, name):
        setattr(os, name, forbidden)
client, arguments = sys.argv[1], sys.argv[2:]
sys.argv = [client, *arguments]
runpy.run_path(client, run_name='__main__')
"""


def _snapshot(directory):
    return {str(path.relative_to(directory)): (
        "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest())
        for path in directory.rglob("*")}


@pytest.fixture
def client(tmp_path, monkeypatch):
    personal = tmp_path / "personal"
    personal.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setenv("CCFLEET_HOME", str(personal / "control"))
    monkeypatch.delenv("CCFLEET_SERVER", raising=False)
    cli = runpy.run_path(str(CLIENT))
    assert list(personal.iterdir()) == []
    return SimpleNamespace(cli=cli, scope=cli["main"].__globals__, personal=personal)


def _visible_children(action):
    visible = {id(action.choices[item.dest]) for item in action._choices_actions
               if item.help != argparse.SUPPRESS}
    if action.metavar is None:
        # Without a custom metavar argparse also publicly lists unnamed-help
        # choices in its generated {one,two} usage, including their aliases.
        hidden = {id(action.choices[item.dest]) for item in action._choices_actions
                  if item.help == argparse.SUPPRESS}
        visible.update(id(child) for child in action.choices.values() if id(child) not in hidden)
    return visible


def _public_parsers(parser, prefix=()):
    """Discover aliases by parser identity; argparse hides internal choice rows."""
    yield prefix, parser
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        visible = _visible_children(action)
        for name, child in action.choices.items():
            if id(child) in visible:
                yield from _public_parsers(child, (*prefix, name))


def _help_paths(parser):
    paths = {}
    for path, current in _public_parsers(parser):
        paths[path] = current
        # Some command families use positional action choices rather than
        # nested parsers (for example a management action). Audit those too.
        for action in current._actions:
            if (not action.option_strings and action.choices
                    and not isinstance(action, argparse._SubParsersAction)
                    and action.help != argparse.SUPPRESS):
                for choice in action.choices:
                    if isinstance(choice, str):
                        paths[(*path, choice)] = current
    return paths


def _hidden_tokens(parser):
    for action in parser._actions:
        if action.help == argparse.SUPPRESS:
            yield from action.option_strings
        if isinstance(action, argparse._SubParsersAction):
            visible = _visible_children(action)
            yield from (name for name, child in action.choices.items() if id(child) not in visible)


def _isolated_cli(arguments, directory):
    personal = directory / "home"
    personal.mkdir(parents=True, mode=0o700)
    before = _snapshot(directory)
    environment = {"HOME": str(personal), "CCFLEET_HOME": str(personal / "control"),
                   "PATH": "", "LANG": "C", "LC_ALL": "C", "TERM": "dumb",
                   "TMPDIR": str(directory)}
    result = subprocess.run([sys.executable, "-I", "-B", "-c", HELP_CHILD,
                             str(CLIENT), *arguments],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            cwd=directory, env=environment, timeout=10, check=False)
    assert _snapshot(directory) == before, f"{arguments!r} wrote local state"
    return result


def test_every_public_command_and_alias_has_side_effect_free_help(client, tmp_path):
    parser = client.cli["parser"]()
    paths = _help_paths(parser)
    assert () in paths and any(len(path) > 1 for path in paths)
    failures = []
    for index, (path, current) in enumerate(paths.items()):
        result = _isolated_cli([*path, "--help"], tmp_path / f"help-{index}")
        if result.returncode != 0 or result.stderr or "usage:" not in result.stdout:
            failures.append((path, result.returncode, result.stdout, result.stderr))
            continue
        assert "==SUPPRESS==" not in result.stdout, path
        for token in _hidden_tokens(current):
            pattern = r"(?<![\w-])" + re.escape(token) + r"(?![\w-])"
            assert not re.search(pattern, result.stdout), (path, token)
    assert failures == []


@pytest.mark.parametrize("parent", [[], ["jobs"]])
def test_unknown_command_rejects_without_exposing_internal_commands(tmp_path, parent):
    result = _isolated_cli([*parent, "__unknown_public_command__"], tmp_path / "unknown")
    assert result.returncode == 2 and "error:" in result.stderr
    assert "__unknown_public_command__" in result.stderr and "--help" in result.stderr
    assert result.stdout == ""
    for internal in ("proxy", "live-serve", "__job-run", "__job-exec"):
        assert not re.search(r"(?<![\w-])" + re.escape(internal) + r"(?![\w-])",
                             result.stderr)


def test_invalid_option_choice_still_lists_valid_native_modes(client, tmp_path):
    result = _isolated_cli(["local", "--permission-mode", "__invalid_mode__"],
                           tmp_path / "invalid-mode")
    assert result.returncode == 2 and "invalid choice" in result.stderr
    assert result.stdout == ""
    assert "--permission-mode" in result.stderr
    for mode in client.cli["PERMISSION_MODES"]:
        assert mode in result.stderr


def _release_helper(client, monkeypatch):
    calls = []

    def called(action, *args):
        calls.append((action, args))
        return {"version": "fixture-release", "signature_verified": True}

    helper = SimpleNamespace(
        status=lambda *args: called("status", *args),
        install_channel=lambda *args: called("install", *args),
        rollback=lambda *args: called("rollback", *args))
    monkeypatch.setitem(client.scope, "client_release", lambda: helper)
    # Native version lookup is separately optional, never an inference request.
    monkeypatch.setitem(client.scope, "find_local_claude", lambda: "/fixture/claude")
    monkeypatch.setitem(client.scope, "client_experience", lambda: SimpleNamespace(
        native_version=lambda executable: "fixture-native"))
    return calls


@pytest.mark.parametrize("rollback", [False, True])
def test_update_and_declared_aliases_delegate_once_to_signed_boundary(
        client, monkeypatch, capsys, rollback):
    calls = _release_helper(client, monkeypatch)
    parser = client.cli["parser"]()
    commands = next(action for action in parser._actions
                    if isinstance(action, argparse._SubParsersAction))
    assert "update" in commands.choices
    canonical = commands.choices["update"]
    assert commands.choices["upgrade"] is canonical
    aliases = [name for name, child in commands.choices.items() if child is canonical]
    before = _snapshot(client.personal)
    directory, key = client.cli["install_directory"](), client.cli["RELEASE_PUBLIC_KEY"]
    for name in aliases:
        args = [name, "--json"] + (["--rollback"] if rollback else [])
        count = len(calls)
        assert client.cli["main"](args) == 0
        assert len(calls) == count + 1
        assert calls[-1] == (("rollback", (directory, key)) if rollback
                             else ("install", (key, directory)))
        assert json.loads(capsys.readouterr().out) == {
            "version": "fixture-release", "signature_verified": True}
    assert _snapshot(client.personal) == before


def test_version_json_uses_status_without_install_or_rollback(client, monkeypatch, capsys):
    calls = _release_helper(client, monkeypatch)
    before = _snapshot(client.personal)
    assert client.cli["main"](["version", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["client_version"] == client.cli["CLIENT_VERSION"]
    assert report["native_claude_version"] == "fixture-native"
    assert calls == [("status", (client.cli["install_directory"](),
                                  client.cli["RELEASE_PUBLIC_KEY"]))]
    assert _snapshot(client.personal) == before


@pytest.mark.parametrize("flag", ["--version", "-v"])
def test_root_version_never_loads_helpers_or_launches_native(client, monkeypatch, capsys, flag):
    def forbidden():
        pytest.fail("root version loaded a release or native helper")

    for name in ("client_release", "client_experience", "find_local_claude"):
        monkeypatch.setitem(client.scope, name, forbidden)
    before = _snapshot(client.personal)
    try:
        result = client.cli["main"]([flag])
    except SystemExit as error:
        result = error.code
    assert result == 0
    captured = capsys.readouterr()
    assert client.cli["CLIENT_VERSION"] in captured.out and captured.err == ""
    assert _snapshot(client.personal) == before


@pytest.mark.parametrize("arguments,field,value", [
    (["-c"], "continue_session", True),
    (["-r"], "resume", ""),
    (["-r", "work"], "resume", "work"),
])
def test_local_history_shortcuts_parse_as_native_options(client, arguments, field, value):
    parsed = client.cli["parser"]().parse_args(["local", *arguments])
    assert getattr(parsed, field) == value
    assert parsed.claude_args == []


def test_all_public_permission_modes_accept_the_native_option_name(client):
    found = []
    for path, parser in _public_parsers(client.cli["parser"]()):
        for action in parser._actions:
            if "--mode" in action.option_strings:
                assert "--permission-mode" in action.option_strings, path
                assert action.dest == "mode", path
                found.append(path)
    assert found


def test_failed_signed_update_does_not_retry_or_rollback(client, monkeypatch, capsys):
    calls = []

    def failed(*args):
        calls.append(args)
        raise ValueError("synthetic signature rejection")

    helper = SimpleNamespace(install_channel=failed,
                             rollback=lambda *_: pytest.fail("unexpected rollback"))
    monkeypatch.setitem(client.scope, "client_release", lambda: helper)
    before = _snapshot(client.personal)
    assert client.cli["main"](["update", "--json"]) == 2
    assert len(calls) == 1 and capsys.readouterr().out == ""
    assert _snapshot(client.personal) == before


def _saved_gateway(client, monkeypatch):
    device = {"device_id": "device", "device_token": "SYNTHETIC_DEVICE_TOKEN",
              "server": "https://fleet.invalid", "slot_id": "slot-one", "slot_name": "one"}
    profile = {"version": 1, "device_id": "device", "base_url": "https://fleet.invalid",
               "secret": "ccf_api_" + "x" * 43, "key_id": "gsame", "mode": "sticky",
               "slot_ids": ["slot-one"], "provider": "anthropic"}
    monkeypatch.setitem(client.scope, "load_config", lambda: {
        "active": "device", "devices": {"device": device}})
    monkeypatch.setitem(client.scope, "load_gateway", lambda: dict(profile))
    monkeypatch.setitem(client.scope, "save_gateway", lambda *_: pytest.fail("rewrote saved key"))
    monkeypatch.setitem(client.scope, "find_local_claude", lambda: "/fixture/claude")
    monkeypatch.setitem(client.scope, "check_managed_route", lambda: None)
    for name in ("inference_client", "client_experience", "retire_live_grants"):
        monkeypatch.setitem(client.scope, name,
                            lambda *args, **kwargs: pytest.fail("started a session or fallback"))
    return device, profile


@pytest.mark.parametrize("provider_args", [[], ["--provider", "devin"]])
def test_setup_refreshes_website_provider_before_deciding_to_create_key(
        client, monkeypatch, capsys, provider_args):
    device, saved = _saved_gateway(client, monkeypatch)
    calls = []

    def api(url, token, payload=None, *, method="POST", **kwargs):
        calls.append((url, method))
        assert method == "GET" and payload is None, "reenrolled the same website-edited key"
        if url.endswith("/options"):
            assert token == device["device_token"]
            return {"available": True, "base_url": saved["base_url"],
                    "providers": ["anthropic", "devin"], "slots": [
                {"id": "slot-one", "name": "one", "providers": ["anthropic", "devin"]}]}
        assert url.endswith("/status") and token == saved["secret"]
        return {"ready": True, "provider": "devin", "slots": [
            {"id": "slot-one", "name": "one"}], "key": {
                "id": saved["key_id"], "slot_ids": saved["slot_ids"],
                "mode": "sticky", "provider": "devin"}}

    monkeypatch.setitem(client.scope, "api_json", api)
    before = _snapshot(client.personal)
    assert client.cli["main"](["gateway", "setup", *provider_args]) == 0
    assert sum(url.endswith("/status") for url, _ in calls) >= 1
    assert all(method == "GET" for _, method in calls)
    output = capsys.readouterr()
    assert saved["secret"] not in output.out + output.err
    assert _snapshot(client.personal) == before


@pytest.mark.parametrize("slots", [
    None, {}, [None], [{"id": []}], [{"id": "foreign-slot", "name": "foreign"}],
    [{"id": "slot-one", "name": "one"}, {"id": "slot-one", "name": "duplicate"}],
])
def test_malformed_or_unselected_status_slots_fail_before_local_launch(
        client, monkeypatch, capsys, slots):
    _, saved = _saved_gateway(client, monkeypatch)
    calls = []

    def api(url, token, payload=None, *, method="POST", **kwargs):
        calls.append((url, method))
        assert method == "GET" and payload is None and token == saved["secret"]
        return {"ready": True, "slots": slots}

    monkeypatch.setitem(client.scope, "api_json", api)
    before = _snapshot(client.personal)
    assert client.cli["main"](["local", "--slot", "one", "--check"]) == 2
    assert len(calls) == 1
    output = capsys.readouterr()
    assert "invalid selected-slot list" in output.err
    assert saved["secret"] not in output.out + output.err
    assert _snapshot(client.personal) == before
