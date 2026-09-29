"""Original local Claude launch, pinned routing and consented legacy migration."""
from __future__ import annotations

import fcntl
import json
import os
import pty
import runpy
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import live_client

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    personal = tmp_path.resolve() / "home"
    personal.mkdir()
    monkeypatch.setenv("HOME", str(personal))
    monkeypatch.setenv("CCFLEET_HOME", str(personal / ".config/ccfleet"))
    cli = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    scope = cli["cmd_local"].__globals__
    device = {"device_id": "device", "slot_id": "slot", "slot_name": "slot-name",
              "key": str(personal / "private.key"), "known_hosts": str(personal / "pin"),
              "host_alias": "slot-host", "user": "slot01"}
    cli["save_config"]({"devices": {"device": device}, "active": "device"})
    events, calls = [], []
    class Bridge:
        secret = "EPHEMERAL_NOT_PROVIDER_CREDENTIAL"
        base_url = "http://127.0.0.1:12345"

        def __init__(self, command, environment):
            self.command, self.environment = command, environment
            events.append("constructed")

        def start(self):
            events.append("started")

        def close(self):
            events.append("closed")

    helper = SimpleNamespace(Bridge=Bridge)
    monkeypatch.setitem(scope, "inference_client", lambda: helper)
    monkeypatch.setitem(scope, "check_inference", lambda *a, **kw: events.append("checked"))
    monkeypatch.setitem(scope, "check_managed_route", lambda: None)
    monkeypatch.setattr(cli["shutil"], "which", lambda name: "/native/" + name)

    def launch(command, **options):
        path = Path(command[command.index("--settings") + 1])
        calls.append((command, options, path, json.loads(path.read_text()), path.stat().st_mode & 0o777))
        return 0
    monkeypatch.setattr(subprocess, "call", launch)
    return SimpleNamespace(cli=cli, scope=scope, home=personal, device=device,
                           calls=calls, events=events, helper=helper)


def invoke(client, *flags):
    return client.cli["main"](["local", "--project", str(client.home), *flags])


def test_local_home_runs_native_cli_without_mount_or_snapshot(client, monkeypatch):
    monkeypatch.setitem(client.scope, "project_files", lambda: pytest.fail("snapshot read"))
    monkeypatch.setitem(client.scope, "live_files", lambda: pytest.fail("mount started"))
    assert invoke(client) == 0
    command, options, path, settings, mode = client.calls[0]
    assert command[0] == "/native/claude" and options["cwd"] == client.home
    assert "--dangerously-skip-permissions" in command
    assert command[command.index("--model") + 1] == "opus"
    assert options["env"]["ANTHROPIC_AUTH_TOKEN"] == client.helper.Bridge.secret
    assert settings["env"]["ANTHROPIC_BASE_URL"] == client.helper.Bridge.base_url
    assert mode == 0o600 and not path.exists()
    assert client.events == ["checked", "constructed", "started", "closed"]
    assert "live_folders" not in client.cli["load_config"]()


def test_preserves_native_history_config_and_tools_environment(client, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(client.home / "custom-native-config"))
    monkeypatch.setenv("PROJECT_TOOL_SECRET", "USER_TOOL_ENV_STAYS_LOCAL")
    monkeypatch.setenv("NO_PROXY", "internal.example,localhost")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "OLD_LOCAL_AUTH_MUST_NOT_BE_USED")
    assert invoke(client, "--continue") == 0
    command, options, _, settings, _ = client.calls[0]
    assert "--continue" in command and "--model" not in command and "--effort" not in command
    assert options["env"]["CLAUDE_CONFIG_DIR"] == str(client.home / "custom-native-config")
    assert options["env"]["PROJECT_TOOL_SECRET"] == "USER_TOOL_ENV_STAYS_LOCAL"
    assert settings["env"]["NO_PROXY"] == "internal.example,localhost,127.0.0.1"
    assert options["env"]["ANTHROPIC_API_KEY"] == ""
    assert settings["apiKeyHelper"] == ""


def test_nonce_never_in_argv_or_output_and_settings_are_deleted(client, capsys):
    assert invoke(client, "--print", "hello") == 0
    command, _, path, _, _ = client.calls[0]
    assert client.helper.Bridge.secret not in " ".join(command)
    output = capsys.readouterr()
    assert client.helper.Bridge.secret not in output.out + output.err
    assert command[-3:] == ["--print", "--", "hello"] and not path.exists()


def test_preserves_effective_settings_only_proxy_exclusions(client):
    directory = client.home / ".claude"
    directory.mkdir()
    (directory / "settings.json").write_text(json.dumps({"env": {"NO_PROXY": "internal.example"}}))
    (directory / "settings.local.json").write_text(json.dumps({"env": {"no_proxy": "private.example"}}))
    assert invoke(client) == 0
    settings = client.calls[0][3]["env"]
    assert "internal.example" in settings["NO_PROXY"]
    assert "private.example" in settings["no_proxy"]
    assert "127.0.0.1" in settings["NO_PROXY"] and "127.0.0.1" in settings["no_proxy"]


def test_native_flags_mode_and_bare_auth(client):
    assert invoke(client, "--", "--bare", "--model", "sonnet", "--permission-mode", "plan") == 0
    command, options, _, settings, _ = client.calls[0]
    assert "--dangerously-skip-permissions" not in command
    assert command.count("--model") == 1 and "sonnet" in command
    assert options["env"]["ANTHROPIC_AUTH_TOKEN"] == ""
    assert settings["env"]["ANTHROPIC_API_KEY"] == client.helper.Bridge.secret


@pytest.mark.parametrize("provider", ["BEDROCK", "VERTEX", "FOUNDRY", "ANTHROPIC_AWS",
                                      "ANTHROPIC_GOOGLE_CLOUD", "MANTLE", "GATEWAY"])
def test_all_installed_provider_selectors_are_pinned_off(client, monkeypatch, provider):
    name = "CLAUDE_CODE_USE_" + provider
    monkeypatch.setenv(name, "1")
    assert invoke(client) == 0
    _, options, _, settings, _ = client.calls[0]
    assert options["env"][name] == "0" and settings["env"][name] == "0"


def test_native_end_of_options_cannot_disable_final_settings_overlay(client):
    assert invoke(client, "--", "--", "literal prompt") == 0
    command = client.calls[0][0]
    assert command.index("--settings") < command.index("--")


def test_literal_prompt_option_names_cannot_switch_auth_or_default_model(client):
    assert invoke(client, "--", "--", "--bare", "--model", "--resume") == 0
    command, _, _, settings, _ = client.calls[0]
    assert command.index("--model") < command.index("--")
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == client.helper.Bridge.secret


@pytest.mark.parametrize("flag", ["--resume=work", "--resume", "-r", "--continue", "-c"])
def test_native_resume_flags_preserve_recorded_model_and_effort(client, flag):
    assert invoke(client, "--", flag) == 0
    command = client.calls[0][0]
    assert "--model" not in command and "--effort" not in command


def test_relative_config_proxy_rules_follow_native_child_working_directory(client, monkeypatch):
    config = client.home / "relative-config"
    config.mkdir()
    (config / "settings.json").write_text(json.dumps({"env": {"NO_PROXY": "child.internal"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "relative-config")
    assert invoke(client) == 0
    assert "child.internal" in client.calls[0][3]["env"]["NO_PROXY"]


@pytest.mark.parametrize("flag", ["--settings", "--settings={}", "--client-data-url=private",
                                  "--cloud", "--teleport", "--remote-control", "--environment",
                                  "--bg", "--background"])
def test_separate_routes_cannot_silently_bypass_slot(client, flag):
    assert invoke(client, "--", flag) == 2
    assert not client.calls and not client.events


def test_check_does_not_touch_selected_path_or_start_native(client):
    assert client.cli["main"](["local", "--project", str(client.home / "missing"), "--check"]) == 0
    assert client.events == ["checked"] and not client.calls


def test_unavailable_relay_never_starts_native_or_falls_back(client, monkeypatch):
    def rejected(*a, **kw):
        raise client.cli["CliError"]("unavailable")
    monkeypatch.setitem(client.scope, "check_inference", rejected)
    assert invoke(client) == 2 and not client.calls


def legacy_record(client):
    record = {"id": "a" * 32, "device": "device", "root": str(client.home), "trusted": True}
    config = client.cli["load_config"]()
    config["live_folders"] = {"record": record}
    client.cli["save_config"](config)
    return record


def test_migration_never_treats_old_trust_as_cancel_permission(client, monkeypatch):
    legacy_record(client)
    monkeypatch.setitem(client.scope, "live_client", lambda: SimpleNamespace(control=lambda *a: {}))
    monkeypatch.setitem(client.scope, "live_control", lambda *a: {"mounted": True, "sessions": ["main"]})
    def declined(_):
        raise client.cli["CliError"]("declined")
    monkeypatch.setitem(client.scope, "confirm_legacy_retirement", declined)
    monkeypatch.setitem(client.scope, "stop_local_connector", lambda *a: pytest.fail("session stopped"))
    assert invoke(client) == 2
    assert not client.calls and "constructed" not in client.events


def test_explicit_migration_stops_exact_legacy_grant_and_keeps_files_history(client, monkeypatch):
    item = legacy_record(client)
    valuable = client.home / "valuable"
    valuable.write_text("keep")
    alive = {"yes": True}
    monkeypatch.setitem(client.scope, "live_client", lambda: SimpleNamespace(control=lambda *a: {}))
    def control(_device, identifier, action="status"):
        assert identifier == item["id"]
        client.events.append(action)
        if action == "stop":
            alive["yes"] = False
            return {"stopped": True}
        return {"mounted": alive["yes"], "connected": alive["yes"],
                "sessions": ["main"] if alive["yes"] else []}
    monkeypatch.setitem(client.scope, "live_control", control)
    monkeypatch.setitem(client.scope, "stop_local_connector",
                        lambda identifier: client.events.append("local-stop"))
    assert invoke(client, "--yes") == 0
    assert client.events.index("local-stop") < client.events.index("stop") < client.events.index("started")
    assert client.cli["load_config"]()["live_folders"]["record"]["retired"] is True
    assert valuable.read_text() == "keep"


def test_failed_legacy_cleanup_does_not_start_second_access_model(client, monkeypatch):
    legacy_record(client)
    monkeypatch.setitem(client.scope, "live_client", lambda: SimpleNamespace(control=lambda *a: {}))
    monkeypatch.setitem(client.scope, "live_control", lambda *a: {"mounted": True, "stopped": False})
    monkeypatch.setitem(client.scope, "stop_local_connector", lambda *a: None)
    assert invoke(client, "--yes") == 2 and not client.calls


@pytest.mark.parametrize("reply", ["acknowledged", "closed", "missing"])
def test_migration_waits_for_real_connector_lock_after_stop(client, monkeypatch, reply):
    item = legacy_record(client)
    directory = client.cli["live_directory"](item["id"])
    live_client.private_directory(directory)
    descriptor = os.open(directory / "connector.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stopped = threading.Event()
    local_calls = []
    def local_control(_directory, action):
        local_calls.append(action)
        if action == "stop":
            stopped.set()
            if reply == "closed":
                raise live_client.ControlError("incomplete stop response")
            return {} if reply == "missing" else {"connected": True}
        assert local_calls == ["status"] or live_client.stopped(directory)
        return {}  # No listener is not proof of a stopped resident.
    monkeypatch.setitem(client.scope, "live_client", lambda: SimpleNamespace(
        control=local_control, stopped=live_client.stopped, ControlError=live_client.ControlError))
    remote_stopped = []
    def remote_control(_device, _identifier, action="status"):
        if action == "stop":
            assert live_client.stopped(directory), "remote reset raced a live connector"
            remote_stopped.append(True)
            return {"stopped": True}
        return {"mounted": not remote_stopped, "sessions": [] if remote_stopped else ["main"]}
    monkeypatch.setitem(client.scope, "live_control", remote_control)
    def teardown():
        if stopped.wait(3):
            # Keep the real lock held across at least one shutdown poll.
            threading.Event().wait(0.05)
        os.close(descriptor)
    worker = threading.Thread(target=teardown)
    worker.start()
    try:
        assert invoke(client, "--yes") == 0
        assert client.cli["load_config"]()["live_folders"]["record"]["retired"] is True
        assert local_calls == ["status", "stop", "status"]
    finally:
        stopped.set()
        worker.join(4)
    assert not worker.is_alive()


def test_unconfirmed_stop_never_retires_record_or_launches_native(client, monkeypatch, capsys):
    legacy_record(client)
    actions = []
    def control(_directory, action):
        if action == "stop":
            raise live_client.ControlError("incomplete reply")
        return {"connected": True}
    helper = SimpleNamespace(control=control, stopped=lambda _: False,
                             ControlError=live_client.ControlError)
    monkeypatch.setitem(client.scope, "live_client", lambda: helper)
    monkeypatch.setitem(client.scope, "live_control", lambda _d, _i, action="status": (
        actions.append(action) or {"mounted": True}))
    ticks = iter([0, 0, 11])
    monkeypatch.setitem(client.scope, "time", SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=lambda _: None))
    assert invoke(client, "--yes") == 2
    assert actions == ["status"] and not client.calls
    assert "retired" not in client.cli["load_config"]()["live_folders"]["record"]
    output = capsys.readouterr()
    assert "migration is incomplete" in output.err and "Traceback" not in output.err


def test_ambiguous_migration_preflight_has_clean_error_and_preserves_grant(client, monkeypatch, capsys):
    legacy_record(client)
    def control(*args):
        raise live_client.ControlError("could not confirm the old local connector's state")
    monkeypatch.setitem(client.scope, "live_client", lambda: SimpleNamespace(control=control))
    monkeypatch.setitem(client.scope, "live_control", lambda *a: pytest.fail("remote mutation"))
    assert invoke(client, "--yes") == 2 and not client.calls
    assert "retired" not in client.cli["load_config"]()["live_folders"]["record"]
    assert "could not confirm" in capsys.readouterr().err


@pytest.mark.parametrize("policy", [{"env": {"ANTHROPIC_BASE_URL": "private"}},
                                   {"apiKeyHelper": "private-command"}, [], {"env": []}])
def test_managed_policy_conflicts_fail_closed_without_printing_values(client, tmp_path, policy):
    path = tmp_path / "managed.json"
    path.write_text(json.dumps(policy))
    actual = runpy.run_path(str(ROOT / "laptop/ccfleet"))
    with pytest.raises(actual["CliError"]):
        actual["check_managed_route"]([path])


def test_native_failure_cleans_ephemeral_bridge_and_nonce_file(client, monkeypatch):
    monkeypatch.setattr(subprocess, "call", lambda *a, **kw: (_ for _ in ()).throw(OSError("native failure")))
    assert invoke(client) == 2
    assert client.events[-1] == "closed"
    assert not list(client.cli["home"]().glob(".inference-settings-*"))


@pytest.mark.parametrize("answer,accepted", [(b"yes\n", True), (b"y\n", True), (b"no\n", False)])
def test_migration_consent_reads_real_nonseekable_tty_not_installer_stdin(client, monkeypatch, answer, accepted):
    master, slave = pty.openpty()
    def terminal(path, mode, **kwargs):
        assert path == "/dev/tty" and mode == "r+b" and kwargs == {"buffering": 0}
        return os.fdopen(os.dup(slave), mode, **kwargs)
    monkeypatch.setitem(client.scope, "open", terminal)
    try:
        os.write(master, answer)
        if accepted:
            client.cli["confirm_legacy_retirement"](False)
        else:
            with pytest.raises(client.cli["CliError"], match="cancelled"):
                client.cli["confirm_legacy_retirement"](False)
    finally:
        os.close(master)
        os.close(slave)
