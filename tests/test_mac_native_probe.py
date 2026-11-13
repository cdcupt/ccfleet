"""Native Mac safe-mode renewal uses only an owned synthetic PTY in these tests."""
from __future__ import annotations

import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from ccfleet_agent import agent, compatibility, mac_native_probe

HELP = ("\n".join(mac_native_probe.REQUIRED_FLAGS) +
        "\n--safe-mode Customizations disabled. Auth and permissions work normally.\n--session-id").encode()


@pytest.fixture
def context(tmp_path, monkeypatch):
    home = tmp_path / "home"
    probe = home / "private-probe"
    probe.mkdir(parents=True, mode=0o700)
    user_settings, managed_settings = home / "settings.json", tmp_path / "managed-settings.json"
    user_settings.write_text("{}")
    managed_settings.write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    for name in compatibility.CONFLICTING_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(compatibility, "settings_paths", lambda *a:
                        [user_settings, managed_settings])
    policy = SimpleNamespace(authenticated_owner=lambda selected: None,
                             open_directory=lambda selected, **kw: os.open(selected, os.O_RDONLY))
    modules = {"mac_policy": policy, "compatibility": compatibility, "agent": agent}
    monkeypatch.setattr(mac_native_probe, "sibling", modules.__getitem__)
    return home, probe, user_settings, managed_settings, modules


def test_safe_mode_flags_are_complete_and_never_disable_native_keychain():
    args = mac_native_probe.SAFE_ARGUMENTS
    assert "--safe-mode" in args and "--bare" not in args
    assert args[args.index("--setting-sources") + 1] == ""
    assert args[args.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in args
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert json.loads(args[args.index("--settings") + 1]) == {"disableAllHooks": True}
    assert args[args.index("--permission-mode") + 1] == "dontAsk"


def test_help_requires_explicit_safe_mode_authentication_support():
    assert mac_native_probe.supported_help(HELP)
    assert not mac_native_probe.supported_help(HELP.replace(b"Auth", b"Other"))
    assert not mac_native_probe.supported_help(HELP.replace(b"--strict-mcp-config", b"--other"))


def test_native_safe_mode_can_isolate_user_customizations_but_not_managed_policy(context):
    home, probe, user, managed, _modules = context
    user.write_text(json.dumps({"hooks": {"event": "never run"},
                                "mcpServers": {"local": {}}, "enabledPlugins": {"x": True}}))
    assert mac_native_probe.guard(home, probe) is None
    managed.write_text(json.dumps({"hooks": {"event": "policy"}}))
    assert mac_native_probe.guard(home, probe) == "native_extensions"


@pytest.mark.parametrize("source", ["user", "managed", "environment"])
def test_authentication_source_guard_remains_strict_with_safe_mode(context, monkeypatch, source):
    home, probe, user, managed, _modules = context
    if source == "environment":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-not-reported")
    else:
        (user if source == "user" else managed).write_text(json.dumps({"apiKeyHelper": "private"}))
    assert mac_native_probe.guard(home, probe) == "native_auth_source"


@pytest.mark.parametrize("field", ["mcpServers", "managedMcpServers", "enabledPlugins"])
def test_managed_extensions_are_never_overridden(context, field):
    home, probe, _user, managed, _modules = context
    managed.write_text(json.dumps({field: {"policy": True}}))
    assert mac_native_probe.guard(home, probe) == "native_extensions"


@pytest.mark.parametrize("allowed,help_text", [(False, HELP), (True, b"unsupported interface")])
def test_failed_binding_or_missing_capability_never_starts_native_child(context, monkeypatch,
                                                                       allowed, help_text):
    home, probe, _user, _managed, modules = context
    modules["mac_credentials"] = SimpleNamespace(bounded_output=lambda *a, **kw: (0, help_text))
    monkeypatch.setattr(mac_native_probe, "_drive", lambda *a, **kw: pytest.fail("started child"))
    result = mac_native_probe.run("/synthetic/claude", home, probe, before_start=lambda: allowed)
    assert result["probe_started"] is False


def test_native_safe_mode_launch_keeps_home_and_fixed_arguments(context, monkeypatch):
    home, probe, _user, _managed, modules = context
    modules["mac_credentials"] = SimpleNamespace(
        bounded_output=lambda *a, **kw: (0, HELP), native_environment=lambda selected:
            {"HOME": str(selected), "PATH": "/usr/bin:/bin"})
    calls = []
    monkeypatch.setattr(mac_native_probe, "_drive", lambda *a, **kw:
                        calls.append((a, kw)) or {"probe_started": True})
    mac_native_probe.run("/synthetic/claude", home, probe, before_start=lambda: True)
    argv, env, directory, timeout = calls[0][0]
    assert argv == ["/synthetic/claude", *mac_native_probe.SAFE_ARGUMENTS]
    assert env["HOME"] == str(home) and "CLAUDE_CONFIG_DIR" not in env
    assert env["CLAUDE_CODE_SAFE_MODE"] == "1"
    assert directory == probe and timeout == 45


SYNTHETIC = r'''
import os,sys,time,tty
tty.setraw(0)
if sys.argv[1] == "trust":
    os.write(1,"Do you trust this folder?\r\n  1. Yes, I trust this folder\r\n❯ 2. No, exit\r\n".encode())
    received=b""
    while len(received)<3:
        received+=os.read(0,3-len(received))
    assert received==b"\x1b[A"
    os.write(1,"\r\n❯ 1. Yes, I trust this folder\r\n  2. No, exit\r\n".encode())
    assert os.read(0,1)==b"\r"
os.write(1,"\r\n❯ ".encode())
received=b""
while len(received)<6:
    received+=os.read(0,6-len(received))
assert received==b"/usage", "only the fixed native usage command is permitted"
os.write(1,"\r\n❯ /usage".encode())
assert os.read(0,1)==b"\r"
os.write(1,b"\r\nCurrent session\r\n20% used\r\nResets later\r\n")
time.sleep(10)
'''


@pytest.mark.parametrize("mode", ["ready", "trust"])
def test_real_pty_sends_only_usage_after_prompt_and_reaps_owned_process(context, mode):
    home, probe, _user, _managed, _modules = context
    started = time.monotonic()
    result = mac_native_probe._drive([sys.executable, "-c", SYNTHETIC, mode],
                                     {"HOME": str(home), "PATH": "/usr/bin:/bin"}, probe, 3)
    assert result == {"outcome": "native_refresh_unconfirmed", "probe_started": True,
                      "cleanup_confirmed": True, "usage_seen": True}
    assert time.monotonic() - started < 3


def test_unknown_prompt_times_out_without_returning_native_output(context):
    home, probe, _user, _managed, _modules = context
    script = "import os,time;os.write(1,b'PRIVATE NATIVE OUTPUT\\r\\n1. unknown selection');time.sleep(10)"
    result = mac_native_probe._drive([sys.executable, "-c", script],
                                     {"HOME": str(home), "PATH": "/usr/bin:/bin"}, probe, 0.20)
    assert result["cleanup_confirmed"] is True and result["usage_seen"] is False
    assert "PRIVATE" not in json.dumps(result)


def test_driver_exception_never_claims_not_started_or_clean(context, monkeypatch):
    home, probe, _user, _managed, modules = context
    modules["mac_credentials"] = SimpleNamespace(
        bounded_output=lambda *a, **kw: (0, HELP), native_environment=lambda selected:
            {"HOME": str(selected), "PATH": "/usr/bin:/bin"})
    def failed(*args, **kwargs):
        raise PermissionError("synthetic cleanup failure")
    monkeypatch.setattr(mac_native_probe, "_drive", failed)
    result = mac_native_probe.run("/synthetic/claude", home, probe, before_start=lambda: True)
    assert result["probe_started"] is True and result["cleanup_confirmed"] is False


def test_group_cleanup_failure_remains_unconfirmed(context, monkeypatch):
    home, probe, _user, _managed, _modules = context
    monkeypatch.setattr(mac_native_probe.pty, "fork", lambda: (123456, 654321))
    monkeypatch.setattr(mac_native_probe.select, "select", lambda *a: ([], [], []))
    monkeypatch.setattr(mac_native_probe.os, "killpg", lambda *a:
                        (_ for _ in ()).throw(PermissionError("not permitted")))
    monkeypatch.setattr(mac_native_probe.os, "waitpid", lambda *a: (123456, 0))
    monkeypatch.setattr(mac_native_probe.os, "close", lambda *a: None)
    result = mac_native_probe._drive(["/synthetic/claude"], {}, probe, 0)
    assert result["probe_started"] is True and result["cleanup_confirmed"] is False
