"""Upgrade compatibility checks use only synthetic CLI/process/account fixtures."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import agent, compatibility, inference_client, local_relay

NOW = 1_800_000_000.0
USAGE = "Current session\n3% used\nResets soon\nCurrent week (all models)\n15% used\nResets later\n"


@pytest.fixture
def slot(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "slot"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(compatibility, "settings_paths", lambda h, p, c=None: [
        (c or h / ".claude") / "settings.json",
        (c or h / ".claude") / "settings.local.json",
        c / ".claude.json" if c else h / ".claude.json", h / ".mcp.json",
        Path(p) / ".claude/settings.json" if p else h / ".claude/settings.json"])
    for key in compatibility.CONFLICTING_ENV:
        monkeypatch.delenv(key, raising=False)
    (home / ".claude").mkdir()
    (home / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "SYNTHETIC_PRIVATE_ACCESS", "refreshToken": "SYNTHETIC_PRIVATE_REFRESH",
        "expiresAt": (NOW + 7200) * 1000}}))
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": "synthetic-private-account", "emailAddress": "private@example.test"}}))
    binary = home / ".local/bin/claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("synthetic CLI; a fake runner interprets this, never executes it")
    monkeypatch.setattr(agent, "find_claude", lambda: str(binary))
    path = home / ".config/ccfleet/slot-state.json"
    state = {"bound_fp": agent.account_fingerprint(home / ".claude.json")}
    assert agent.write_state(path, state)
    clock = [NOW]
    monkeypatch.setattr(agent.time, "time", lambda: clock[0])
    monkeypatch.setattr(agent.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls = []
    options = {"version": "2.1.300", "signed_in": True, "source": "claude.ai",
               "provider": "firstParty", "usage": USAGE, "captures": 0, "callback": None}

    def run(argv, **kwargs):
        calls.append(list(argv))
        output = ""
        if argv[1:] == ["--version"]:
            output = options["version"] + " (Claude Code)"
        elif argv[1:] == ["auth", "--help"]:
            output = "Commands: login logout status"
        elif argv[1:] == ["auth", "status"]:
            output = json.dumps({"loggedIn": options["signed_in"], "authMethod": options["source"],
                                 "apiProvider": options["provider"], "email": "private@example.test",
                                 "token": "SYNTHETIC_PRIVATE_NATIVE_OUTPUT"})
        elif argv[0] == "tmux":
            if "new-session" in argv:
                options["captures"] = 0
            elif "capture-pane" in argv:
                options["captures"] += 1
                if options["captures"] == 1:
                    output = "ready"
                else:
                    output = options["usage"]
                    if options["callback"]:
                        options["callback"]()
        else:
            pytest.fail("unexpected synthetic command")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="PRIVATE_UNUSED_STDERR")

    real_serve = local_relay.serve_one
    modules = SimpleNamespace(connect_upstream=local_relay.connect_upstream,
                              serve_one=lambda *a, **k: real_serve(*a, policy=lambda: None, **k))
    monkeypatch.setattr(agent, "_compatibility_relay_modules", lambda: (modules, inference_client))
    return SimpleNamespace(home=home, binary=binary, path=path, state=state, clock=clock,
                           calls=calls, options=options, runner=run, relay=modules)


def check(slot, *, request=None, state=None, installed=None):
    return agent.reconcile_compatibility(
        {"refresh_quota": True} if request is None else request,
        agent.read_state(slot.path) if state is None else state,
        slot.options["version"] if installed is None else installed, slot.runner, slot.clock[0])


def test_new_generation_checks_real_status_framing_and_fresh_usage_without_post(slot):
    credentials = (slot.home / ".claude/.credentials.json").read_bytes()
    state, report, attempted = check(slot)
    assert attempted and report["state"] == "passed"
    assert all(report["checks"][key] is True for key in compatibility.CHECKS)
    assert report["usage_observed_at"] >= report["checked_at"]
    assert report["account_fp"] == slot.state["bound_fp"]
    assert report["native_version"] == "2.1.300"
    assert len(report["runtime_fp"]) == 64
    assert (slot.home / ".claude/.credentials.json").read_bytes() == credentials
    assert "PRIVATE" not in json.dumps(state) and "private@example" not in json.dumps(report)
    assert "synthetic-private-account" not in json.dumps(report)
    assert not any("install" in call or "--print" in call or "-p" in call
                   for call in slot.calls if call[0] != "tmux")


def test_success_is_cached_without_repeating_native_commands_or_usage(slot):
    state, first, _ = check(slot)
    agent.write_state(slot.path, state)
    calls = len(slot.calls)
    slot.clock[0] += 60
    state, second, attempted = check(slot)
    assert not attempted and second == first and len(slot.calls) == calls


def test_external_version_change_cannot_borrow_cached_quota_success(slot):
    state, _, _ = check(slot)
    agent.write_state(slot.path, state)
    slot.clock[0] += 60
    slot.options["version"] = "2.1.301"
    slot.binary.write_text("changed synthetic native binary")
    state, report, attempted = check(slot)
    assert attempted and report["state"] == "passed" and report["native_version"] == "2.1.301"
    assert report["checked_at"] >= NOW + 60
    assert sum("new-session" in call for call in slot.calls) == 2


def test_runtime_change_rechecks_even_without_native_version_change(slot, monkeypatch):
    state, _, _ = check(slot)
    agent.write_state(slot.path, state)
    monkeypatch.setattr(compatibility, "runtime_fingerprint", lambda *a: "f" * 64)
    state, report, attempted = check(slot)
    assert attempted and report["state"] == "passed" and report["runtime_fp"] == "f" * 64


def test_cached_quota_is_not_usage_interface_evidence(slot):
    state = {**slot.state, "quota": {"ts": NOW, "session": {"used_pct": 1}}}
    _, report, attempted = check(slot, request={"refresh_quota": False}, state=state)
    assert not attempted and report["state"] == "pending" and report["reason"] == "usage_pending"
    assert report["checks"].get("usage_parser") is not True
    assert "last_success_at" not in report


def test_usage_parse_failure_has_durable_backoff_and_never_passes(slot):
    slot.options["usage"] = "unsupported native usage layout PRIVATE"
    state, report, attempted = check(slot)
    assert attempted and report["state"] == "failed" and report["reason"] == "native_timeout"
    assert report["checks"]["usage_parser"] is False and "last_success_at" not in report
    agent.write_state(slot.path, state)
    calls = len(slot.calls)
    slot.clock[0] += 60
    _, cached, attempted = check(slot)
    assert not attempted and cached == report and len(slot.calls) == calls
    assert "PRIVATE" not in json.dumps(state)


@pytest.mark.parametrize("change,reason", [
    ({"bound_fp": None}, "account_unbound"), ({"account_restart": "owed"}, "account_transition"),
    ({"login": {"requested_at": NOW}}, "sign_in_pending"),
])
def test_account_and_signin_guards_do_not_invoke_native(slot, change, reason):
    state = {**slot.state, **change}
    agent.write_state(slot.path, state)
    _, report, attempted = check(slot)
    assert not attempted and report["state"] == "blocked" and report["reason"] == reason
    assert slot.calls == []


def test_same_account_relogin_clears_compatibility_generation(slot):
    state, first, _ = check(slot)
    reset = agent._moved_on(state)
    assert "compatibility" not in reset
    agent.write_state(slot.path, reset)
    slot.clock[0] += 300
    _, report, attempted = check(slot)
    assert attempted and report["checked_at"] > first["checked_at"]


def test_account_change_during_probe_preserves_new_binding_and_blocks_success(slot):
    changed = hashlib.sha256(b"another synthetic account").hexdigest()[:16]
    slot.options["callback"] = lambda: agent.write_state(slot.path, {
        **agent.read_state(slot.path), "bound_fp": changed})
    state, report, attempted = check(slot)
    assert attempted and report["state"] == "blocked" and report["reason"] == "account_transition"
    assert state["bound_fp"] == changed and "last_success_at" not in report


def test_native_swap_during_probe_cannot_be_attributed_to_old_version(slot):
    slot.options["callback"] = lambda: slot.binary.write_text("new native code during probe")
    _, report, attempted = check(slot)
    assert attempted and report["state"] == "blocked" and report["reason"] == "check_interrupted"
    assert "last_success_at" not in report


def test_settings_auth_override_during_probe_cannot_pass_old_context(slot):
    slot.options["callback"] = lambda: (slot.home / ".claude/settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://private.invalid"}}))
    _, report, attempted = check(slot)
    assert attempted and report["state"] == "blocked" and report["reason"] == "native_auth_source"
    assert "last_success_at" not in report and "private.invalid" not in json.dumps(report)


def test_clean_settings_generation_change_requires_recheck(slot):
    slot.options["callback"] = lambda: (slot.home / ".claude/settings.json").write_text(
        json.dumps({"forceLoginMethod": "claudeai"}))
    _, report, attempted = check(slot)
    assert attempted and report["state"] == "blocked" and report["reason"] == "check_interrupted"
    assert "last_success_at" not in report


def test_unsigned_native_status_is_interface_valid_but_cannot_pass_usage(slot):
    slot.options["signed_in"] = False
    _, report, attempted = check(slot)
    assert not attempted and report["state"] == "blocked"
    assert report["checks"]["auth_interface"] is True
    assert report["reason"] == "credential_unavailable"
    assert not any("new-session" in call for call in slot.calls)


@pytest.mark.parametrize("key", ["source", "provider"])
def test_alternate_native_auth_source_cannot_be_attributed_to_bound_account(slot, key):
    slot.options[key] = "alternate"
    _, report, attempted = check(slot)
    assert not attempted and report["reason"] == "native_auth_source"
    assert "alternate" not in json.dumps(report)
    assert not any("new-session" in call for call in slot.calls)


def test_environment_or_settings_override_blocks_checks_without_recording_value(slot, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "PRIVATE_OVERRIDE")
    _, report, attempted = check(slot)
    assert not attempted and report["reason"] == "native_auth_source" and slot.calls == []
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    (slot.home / ".claude/settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://private.invalid"}}))
    _, report, attempted = check(slot)
    assert not attempted and report["reason"] == "native_auth_source"
    assert "PRIVATE" not in json.dumps(report) and "private.invalid" not in json.dumps(report)


def test_protocol_and_verified_tls_policy_fail_closed_without_network(slot):
    wrong = SimpleNamespace(VERSION=3)
    checks, reason = compatibility.relay_checks(slot.relay, wrong, slot.home)
    assert reason == "relay_protocol_mismatch"
    import http.client
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    relay = SimpleNamespace(connect_upstream=lambda: http.client.HTTPSConnection(
        "api.anthropic.com", 443, context=context))
    checks, reason = compatibility.relay_checks(relay, inference_client, slot.home)
    assert checks["tls_policy"] is False and reason == "relay_tls_policy"


def test_nonzero_successful_status_process_is_not_compatible(slot):
    actual = slot.relay.serve_one
    def nonzero(*args, **kwargs):
        actual(*args, **kwargs)
        return 1
    slot.relay.serve_one = nonzero
    checks, reason = compatibility.relay_checks(slot.relay, inference_client, slot.home)
    assert checks["relay_protocol"] is False and reason == "relay_protocol_mismatch"


def test_renewal_and_terminal_guard_do_not_launch_second_or_futile_usage(slot):
    state, report, attempted = agent.reconcile_compatibility(
        {"refresh_quota": True}, slot.state, "2.1.300", slot.runner, NOW, usage_allowed=False)
    assert not attempted and report["state"] == "pending"
    assert not any("new-session" in call for call in slot.calls)


@pytest.mark.parametrize("settings,reason", [
    ({"env": None}, "native_auth_source"), ({"env": ["PRIVATE"]}, "native_auth_source"),
    ({"hooks": {"SessionStart": [{"command": "PRIVATE"}]}}, "native_extensions"),
    ({"enabledPlugins": {"private_plugin": True}}, "native_extensions"),
    ({"mcpServers": {"private_server": {"command": "PRIVATE"}}}, "native_extensions"),
])
def test_effective_settings_and_extensions_block_without_private_values(slot, settings, reason):
    target = slot.home / ".claude/settings.json"
    target.write_text(json.dumps(settings))
    _, report, attempted = check(slot)
    assert not attempted and report["reason"] == reason and slot.calls == []
    assert "PRIVATE" not in json.dumps(report) and "private_plugin" not in json.dumps(report)


def test_fifo_settings_are_rejected_without_blocking(slot):
    target = slot.home / ".claude/settings.json"
    os.mkfifo(target)
    began = time.monotonic()
    _, report, attempted = check(slot)
    assert not attempted and report["reason"] == "native_auth_source"
    assert time.monotonic() - began < 1


def test_usage_launch_has_explicit_clean_env_not_stale_tmux_auth(slot):
    _, report, attempted = check(slot)
    assert attempted and report["state"] == "passed"
    launch = next(call for call in slot.calls if "new-session" in call)
    assert "/usr/bin/env" in launch and "-i" in launch and "DISABLE_AUTOUPDATER=1" in launch
    assert all(not any(arg.startswith(name + "=") for arg in launch)
               for name in compatibility.CONFLICTING_ENV)
    assert launch[:3] == ["tmux", "-f", "/dev/null"]
    assert agent.QUOTA_TMUX_SOCKET == "ccfleet-quota-safe-v1"


def test_all_quota_consumers_block_unsafe_context_and_use_clean_direct_exec(slot):
    (slot.home / ".claude/settings.json").write_text(json.dumps({"hooks": {"SessionStart": []}}))
    observed = {}
    assert agent.read_quota(slot.runner, probe_result=observed) is None
    assert observed["outcome"] == "native_extensions" and observed["probe_started"] is False
    assert slot.calls == []
    (slot.home / ".claude/settings.json").unlink()
    assert agent.read_quota(slot.runner) is not None
    launch = next(call for call in slot.calls if "new-session" in call)
    assert "/usr/bin/env" in launch and "-i" in launch and "DISABLE_AUTOUPDATER=1" in launch


def test_owner_explicit_config_namespace_is_preserved_and_conflicts_block(slot, monkeypatch):
    config = slot.home / ".claude-work"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    observed = {}
    assert agent.read_quota(slot.runner, probe_result=observed, expected_config_dir=config) is not None
    launch = next(call for call in slot.calls if "new-session" in call)
    assert "HOME=" + str(slot.home) in launch and "CLAUDE_CONFIG_DIR=" + str(config) in launch
    slot.calls.clear()
    assert agent.read_quota(slot.runner, expected_config_dir=slot.home / ".claude") is None
    assert slot.calls == []
    assert agent.read_quota(slot.runner) is None, "shared slots must not inherit the owner override"


def test_owner_config_without_inherited_override_still_selects_intended_store(slot):
    config = slot.home / ".claude-work"
    config.mkdir()
    assert agent.read_quota(slot.runner, expected_config_dir=config) is not None
    launch = next(call for call in slot.calls if "new-session" in call)
    assert "CLAUDE_CONFIG_DIR=" + str(config) in launch
    assert "HOME=" + str(slot.home) in launch


def test_owner_default_store_does_not_gain_a_different_explicit_global_profile(slot):
    assert agent.read_quota(slot.runner, expected_config_dir=slot.home / ".claude") is not None
    launch = next(call for call in slot.calls if "new-session" in call)
    assert not any(arg.startswith("CLAUDE_CONFIG_DIR=") for arg in launch)


def test_owner_auth_status_uses_agent_only_config_and_keeps_default_namespace(slot):
    config = slot.home / ".claude-work"
    config.mkdir()
    observed = []
    def native(argv, **kwargs):
        observed.append(kwargs.get("env"))
        return subprocess.CompletedProcess(argv, 0, stdout='{"loggedIn":true}')
    assert agent.auth_status(native, expected_config_dir=config)["logged_in"] is True
    assert observed[0]["HOME"] == str(slot.home)
    assert observed[0]["CLAUDE_CONFIG_DIR"] == str(config)
    agent.auth_status(native, expected_config_dir=slot.home / ".claude")
    assert "CLAUDE_CONFIG_DIR" not in observed[1]


@pytest.mark.parametrize("config", [{"managedMcpServers": {"private": {}}}, {"mcpServers": {"private": {}}}])
def test_managed_mcp_and_probe_project_extensions_block_usage(slot, config, monkeypatch):
    managed = slot.home / "managed-mcp.json"
    managed.write_text(json.dumps(config))
    monkeypatch.setattr(compatibility, "settings_paths", lambda *a: [managed])
    _, report, attempted = check(slot)
    assert not attempted and report["reason"] == "native_extensions"
    assert "private" not in json.dumps(report)
    probe, _ = agent.quota_probe_dir()
    (slot.home / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "accountUuid": "synthetic-private-account"}, "projects": {probe: {"mcpServers": {"private": {}}}}}))
    monkeypatch.setattr(compatibility, "settings_paths", lambda *a: [slot.home / ".claude.json"])
    _, report, attempted = check(slot)
    assert not attempted and report["reason"] == "native_extensions"


def test_custom_owner_post_probe_cache_cannot_borrow_sibling_account(slot, monkeypatch):
    config = slot.home / ".claude-work"
    config.mkdir()
    def cache(used):
        return {"oauthAccount": {"accountUuid": "synthetic-owner"},
                "cachedUsageUtilization": {"accountUuid": "synthetic-owner", "fetchedAtMs": NOW * 1000,
                  "utilization": {"five_hour": {"utilization": used}}}}
    (slot.home / ".claude-work.json").write_text(json.dumps(cache(99)))
    def native_probe(*args, **kwargs):
        assert kwargs["expected_config_dir"] == config
        (config / ".claude.json").write_text(json.dumps(cache(7)))
        return {"session": {"used_pct": 8}}
    monkeypatch.setattr(agent, "read_quota", native_probe)
    report, _ = agent.quota_summary({}, slot.runner, NOW, config_dir=config)
    assert report["session"]["used_pct"] == 7


def test_auto_reaping_signal_context_never_spawns_command(monkeypatch):
    monkeypatch.setattr(signal, "getsignal", lambda *args: signal.SIG_IGN)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("unsafe process ownership"))
    assert compatibility.command("synthetic", ["--version"], subprocess.run) == (
        None, None, "native_unavailable")


def test_report_rejects_false_pass_and_never_reflects_untrusted_fields():
    assert compatibility.report({"state": "passed", "checked_at": NOW}) == {}
    assert compatibility.report({"state": [], "checked_at": NOW}) == {}
    report = compatibility.report({"state": "failed", "checked_at": NOW,
                                   "reason": ["PRIVATE"], "raw_stdout": "PRIVATE",
                                   "checks": {"native_version": False, "private": "PRIVATE"}})
    assert "PRIVATE" not in json.dumps(report) and report["checks"] == {"native_version": False}


def test_native_output_timeout_and_size_are_fixed_diagnostics():
    def oversized(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout="PRIVATE" * compatibility.MAX_OUTPUT)
    assert compatibility.command("synthetic", ["--version"], oversized) == (
        None, None, "native_interface_changed")
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(a, 10, output="PRIVATE", stderr="PRIVATE")
    assert compatibility.command("synthetic", ["--version"], timeout) == (
        None, None, "native_timeout")


def test_real_controlled_process_output_bound_and_owned_cleanup(tmp_path, monkeypatch):
    script = tmp_path / "synthetic-native"
    script.write_text(f"#!{sys.executable}\nimport sys,time\nsys.stdout.write('x'*65537)\n"
                      "sys.stdout.flush()\ntime.sleep(30)\n")
    script.chmod(0o700)
    began = time.monotonic()
    assert compatibility.command(str(script), ["--version"], subprocess.run) == (
        None, None, "native_interface_changed")
    assert time.monotonic() - began < 3
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    monkeypatch.setattr(compatibility, "COMMAND_TIMEOUT", 0.1)
    began = time.monotonic()
    assert compatibility.command(str(script), ["--version"], subprocess.run) == (
        None, None, "native_timeout")
    assert time.monotonic() - began < 3


@pytest.mark.parametrize("redirected", [False, True])
def test_owned_group_descendants_are_stopped_after_leader_exits(tmp_path, monkeypatch, redirected):
    marker = tmp_path / "synthetic-child-pid"
    script = tmp_path / "synthetic-native"
    script.write_text(f"#!{sys.executable}\nimport subprocess,sys,pathlib\n"
                      "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                      + ("stdout=subprocess.DEVNULL" if redirected else "stdout=None") + ")\n"
                      f"pathlib.Path({str(marker)!r}).write_text(str(child.pid))\n"
                      "print('2.1.300',flush=True)\n")
    script.chmod(0o700)
    monkeypatch.setattr(compatibility, "COMMAND_TIMEOUT", 2)
    code, output, reason = compatibility.command(str(script), ["--version"], subprocess.run)
    if redirected:
        assert code == 0 and output == "2.1.300\n" and reason is None
    else:
        assert code is None and output is None and reason == "native_timeout"
    pid = int(marker.read_text())
    def running():
        status = Path(f"/proc/{pid}/stat")
        if status.exists() and status.read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    deadline = time.monotonic() + 2
    while running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not running(), "the checker leaked its own synthetic descendant"


def test_installer_private_output_never_enters_error_or_update_state(slot):
    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, stdout="PRIVATE_TOKEN", stderr="PRIVATE_EMAIL")
    result = agent.reconcile_version({"claude_version": "2.1.301"}, "2.1.300", {}, failing, NOW)
    assert result["error"] == "native_install_failed" and result["exit_code"] == 3
    assert "PRIVATE" not in json.dumps(result)
