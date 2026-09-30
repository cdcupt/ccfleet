"""Public CLI integration for local UX, private diagnostics and supervised jobs."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import client_experience, inference_client, local_jobs
from tests.test_native_local_cli import client as client

PRIVATE = "PRIVATE_FIXTURE_INFORMATION_MUST_NOT_APPEAR"
PROMPT = "PRIVATE_BACKGROUND_PROMPT_ONLY_ON_STDIN"


@pytest.fixture
def platform(client, monkeypatch):
    client.device.update(server="https://broker.invalid", device_token=PRIVATE,
                         endpoint="wss://broker.invalid/api/cli/connect")
    for field in ("key", "known_hosts"):
        Path(client.device[field]).write_text("synthetic " + field)
        Path(client.device[field]).chmod(0o600)
    client.cli["save_config"]({"devices": {"device": client.device}, "active": "device"})
    monkeypatch.setitem(client.scope, "client_experience", lambda: client_experience)
    client.helper.RelayError = inference_client.RelayError
    monkeypatch.setattr(client_experience, "native_version", lambda *a, **kw: "2.1.284")
    client.context = {
        "authenticated": True, "protocol": 2, "device": {"active": True},
        "slot": {"state": "active", "ready": True, "health": "ready",
                 "account_generation": "a" * 24},
        "ignored_private_debug": PRIVATE,
    }
    monkeypatch.setitem(client.scope, "device_context", lambda device: client.context)
    return client


def preferences(platform):
    return client_experience.Preferences(platform.cli["home"]())


def invoke(platform, *arguments):
    return platform.cli["main"](list(arguments))


def invoke_job_exec(platform, *, generation="a" * 24):
    token_generation = hashlib.sha256(PRIVATE.encode()).hexdigest()
    return invoke(platform, "__job-exec", "a" * 32, "--generation", generation,
                  "--device-generation", token_generation)


def test_preferences_new_sessions_only_and_explicit_overrides(platform, capsys):
    assert invoke(platform, "preferences", "set", "--project", str(platform.home),
                  "--model", "sonnet", "--effort", "high", "--mode", "plan") == 0
    assert json.loads(capsys.readouterr().out) == {"model": "sonnet", "effort": "high", "mode": "plan"}
    assert invoke(platform, "local", "--project", str(platform.home)) == 0
    command = platform.calls[-1][0]
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert invoke(platform, "local", "--project", str(platform.home), "--model", "opus",
                  "--effort", "low", "--mode", "acceptEdits") == 0
    command = platform.calls[-1][0]
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "low"
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert preferences(platform).get(platform.home)["model"] == "sonnet"


@pytest.mark.parametrize("flags", [["--continue"], ["--resume"], ["--resume", "work"],
                                    ["--", "--resume=work"], ["--", "--continue"]])
def test_saved_defaults_do_not_overwrite_resumed_native_modes(platform, flags):
    preferences(platform).set(platform.home, {"model": "sonnet", "effort": "low", "mode": "plan"})
    assert invoke(platform, "local", "--project", str(platform.home), *flags) == 0
    command = platform.calls[-1][0]
    assert not {"--model", "--effort", "--permission-mode", "--dangerously-skip-permissions"} & set(command)


def test_preferences_do_not_override_explicit_native_options(platform):
    preferences(platform).set(platform.home, {"model": "sonnet", "effort": "high", "mode": "plan"})
    assert invoke(platform, "local", "--project", str(platform.home), "--", "--model", "opus",
                  "--effort", "low", "--permission-mode", "acceptEdits") == 0
    command = platform.calls[-1][0]
    assert command.count("--model") == command.count("--effort") == command.count("--permission-mode") == 1
    assert "sonnet" not in command and "high" not in command and "plan" not in command


def test_preferences_are_private_hashed_and_clearable(platform, capsys):
    assert invoke(platform, "preferences", "set", "--project", str(platform.home),
                  "--model", "sonnet") == 0
    path = platform.cli["home"]() / "preferences.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert str(platform.home) not in path.read_text()
    assert PRIVATE not in path.read_text()
    capsys.readouterr()
    assert invoke(platform, "preferences", "clear", "--project", str(platform.home)) == 0
    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize("command,expected", [("new", "--model"), ("continue", "--continue"),
                                              ("resume", "--resume")])
def test_guided_explicit_actions_delegate_to_original_local_client(platform, command, expected):
    preferences(platform).set(platform.home, {"model": "sonnet"})
    assert invoke(platform, "start", command, "--project", str(platform.home)) == 0
    native = platform.calls[-1][0]
    assert native[0] == "/native/claude" and expected in native
    if command != "new":
        assert "--model" not in native


def test_menu_entry_delegates_with_consent_protections_not_implicit_launch(platform, monkeypatch):
    calls = []

    def menu(callbacks, **options):
        calls.append((callbacks, options))
        assert "launch_local" in callbacks and "launch_remote" in callbacks
        return 7

    monkeypatch.setattr(client_experience, "menu", menu)
    assert invoke(platform, "menu", "--project", str(platform.home)) == 7
    assert len(calls) == 1 and not platform.calls


def test_menu_cli_passes_explicit_permission_model_and_effort(platform, monkeypatch):
    actual = client_experience.menu
    def choose_new(callbacks, **options):
        return actual(callbacks, **options, input_fn=lambda _: "1", output_fn=lambda _: None,
                      is_tty=True)
    monkeypatch.setattr(client_experience, "menu", choose_new)
    assert invoke(platform, "start", "--project", str(platform.home), "--mode", "plan",
                  "--model", "sonnet", "--effort", "low") == 0
    native = platform.calls[-1][0]
    assert native[native.index("--permission-mode") + 1] == "plan"
    assert native[native.index("--model") + 1] == "sonnet"
    assert native[native.index("--effort") + 1] == "low"
    assert "--dangerously-skip-permissions" not in native


def test_sessions_uses_native_picker_without_parsing_history(platform):
    assert invoke(platform, "sessions", "--project", str(platform.home)) == 0
    assert "--resume" in platform.calls[-1][0]
    assert "--model" not in platform.calls[-1][0]


@pytest.mark.parametrize("arguments", [[], ["remote"], ["attach"]])
def test_old_remote_commands_never_silently_change_to_local_files(platform, monkeypatch, arguments):
    recorded = []
    monkeypatch.setitem(platform.scope, "cmd_attach", lambda args: recorded.append(args) or 13)
    assert invoke(platform, *arguments) == 13
    assert len(recorded) == 1 and recorded[0].action == "open"
    assert not platform.calls


def test_snapshot_is_explicit_legacy_recovery_not_local_launch(platform, monkeypatch, capsys):
    called = []
    monkeypatch.setitem(platform.scope, "cmd_snapshot", lambda args: called.append(args) or 17)
    assert invoke(platform, "snapshot", "--project", str(platform.home)) == 17
    assert called and not platform.calls
    with pytest.raises(SystemExit) as help_exit:
        invoke(platform, "snapshot", "--help")
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "Legacy snapshot recovery" in help_text
    assert "ccfleet local" in help_text


def test_help_describes_local_background_and_explicit_remote(platform, capsys):
    with pytest.raises(SystemExit):
        invoke(platform, "--help")
    text = capsys.readouterr().out
    assert "Local Claude Code" in text and "remote Claude" in text
    with pytest.raises(SystemExit):
        invoke(platform, "local", "--help")
    text = " ".join(capsys.readouterr().out.split())
    assert "--background" in text and "terminal exits" in text


def test_doctor_json_and_private_export_never_include_raw_identity(platform, monkeypatch, capsys):
    monkeypatch.setenv("PRIVATE_ENV_FIXTURE", PRIVATE)
    monkeypatch.setitem(platform.scope, "project_files", lambda: pytest.fail("project scan"))
    monkeypatch.setitem(platform.scope, "live_files", lambda: pytest.fail("filesystem mount"))
    report_path = platform.home / "support.json"
    assert invoke(platform, "doctor", "--privacy", "--json", "--export", str(report_path)) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["ok"] is True and report["privacy"]
    assert report == json.loads(report_path.read_text())
    for value in (PRIVATE, str(platform.home), platform.device["slot_name"],
                  platform.device["server"], platform.device["key"]):
        assert value not in output.out + output.err + report_path.read_text()
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert not platform.calls


@pytest.mark.parametrize("failure", ["device", "relay", "settings", "native"])
def test_diagnostic_failures_are_fixed_categories_not_raw_errors(platform, monkeypatch, capsys, failure):
    def fail(*args, **kwargs):
        raise platform.cli["CliError"](PRIVATE + " /private/person/home", status=403)

    name = {"device": "device_context", "relay": "check_inference",
            "settings": "check_managed_route", "native": "find_local_claude"}[failure]
    monkeypatch.setitem(platform.scope, name, fail)
    assert invoke(platform, "doctor", "--privacy", "--json") == 1
    output = capsys.readouterr()
    assert PRIVATE not in output.out + output.err
    assert "/private/person/home" not in output.out + output.err
    assert json.loads(output.out)["ok"] is False
    assert not platform.calls


def test_status_makes_no_native_version_or_model_request(platform, monkeypatch, capsys):
    monkeypatch.setattr(client_experience, "native_version", lambda *a, **k: pytest.fail("native run"))
    assert invoke(platform, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "status"
    assert not platform.calls


def test_device_status_uses_authenticated_get_only(platform, monkeypatch):
    called = []
    monkeypatch.setitem(platform.scope, "api_json",
                        lambda *a, **kw: called.append((a, kw)) or platform.context)
    assert platform.cli["device_context"](platform.device) == platform.context
    assert called == [(("https://broker.invalid/api/cli/status", PRIVATE), {"method": "GET"})]


def test_version_update_and_rollback_delegate_to_signed_release_boundary(platform, monkeypatch, capsys):
    called = []
    helper = SimpleNamespace(
        status=lambda *a: called.append(("status", a)) or {"signature_verified": False},
        install_channel=lambda *a: called.append(("update", a)) or {
            "version": "0.2.0", "signature_verified": True},
        rollback=lambda *a: called.append(("rollback", a)) or {
            "version": "0.1.0", "signature_verified": True})
    monkeypatch.setitem(platform.scope, "client_release", lambda: helper)
    for args, action in [(["version", "--json"], "status"),
                         (["update", "--json"], "update"),
                         (["update", "--rollback", "--json"], "rollback")]:
        assert invoke(platform, *args) == 0
        report = json.loads(capsys.readouterr().out)
        assert called[-1][0] == action
        assert platform.scope["RELEASE_PUBLIC_KEY"] in called[-1][1]
        assert PRIVATE not in json.dumps(report)
    assert not platform.calls


def test_setup_stage_rerun_keeps_pairing_and_native_history(platform, monkeypatch, capsys):
    for name in ("project_files", "live_files", "live_client"):
        monkeypatch.setitem(platform.scope, name, lambda: object())
    monkeypatch.setitem(platform.scope, "cmd_login", lambda *a: pytest.fail("paired again"))
    history = platform.home / "native-history"
    history.write_text("preserve")
    original = platform.cli["load_config"]()
    attempts = []

    def ready(device, *, newly_paired):
        attempts.append(newly_paired)
        if len(attempts) == 1:
            raise platform.cli["CliError"]("temporary slot unavailability")

    monkeypatch.setitem(platform.scope, "wait_for_setup", ready)
    assert invoke(platform, "setup") == 2
    assert platform.cli["load_config"]() == original
    assert invoke(platform, "setup") == 0
    output = capsys.readouterr()
    for stage in range(1, 6):
        assert f"[{stage}/5]" in output.err
    assert attempts == [False, False]
    assert platform.cli["load_config"]() == original
    assert history.read_text() == "preserve" and not platform.calls


@pytest.fixture
def job_capture(platform, monkeypatch):
    started = []

    def start(root, spec, command, **options):
        started.append((root, spec, command, options))
        return {"job_id": "a" * 32, "state": "running"}

    helper = SimpleNamespace(start=start, _spec=local_jobs._spec, _number=local_jobs._number,
                             MAX_TIMEOUT_S=local_jobs.MAX_TIMEOUT_S,
                             MAX_CONCURRENT=local_jobs.MAX_CONCURRENT,
                             MAX_SPEC_BYTES=local_jobs.MAX_SPEC_BYTES)
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: helper)
    return started, helper


@pytest.mark.parametrize("arguments", [["jobs", "start", "--prompt", PROMPT],
                                       ["local", "--background", "--print", PROMPT]])
def test_background_jobs_keep_prompt_local_and_supervisor_argv_clean(platform, job_capture,
                                                                   arguments, capsys):
    started, _ = job_capture
    assert invoke(platform, *arguments, "--project", str(platform.home)) == 0
    root, spec, command, options = started[0]
    assert root == platform.cli["home"]() / "jobs"
    assert set(spec) == {"device_id", "slot_id", "project", "prompt", "options"}
    assert spec["prompt"] == PROMPT and spec["device_id"] == "device" and spec["slot_id"] == "slot"
    assert command[-1] == "__job-run" and "-I" in command
    assert PRIVATE not in json.dumps((spec, command, options))
    assert PROMPT not in json.dumps(command)
    assert PROMPT not in capsys.readouterr().out
    assert not platform.calls


def test_background_prompt_can_arrive_from_stdin(platform, job_capture, monkeypatch):
    started, _ = job_capture
    monkeypatch.setattr(platform.cli["sys"], "stdin", io.StringIO(PROMPT))
    assert invoke(platform, "jobs", "start", "--prompt", "-", "--project", str(platform.home)) == 0
    assert started[0][1]["prompt"] == PROMPT


@pytest.mark.parametrize("flags", [["--resume", "work"], ["--continue"],
                                    ["--", "--resume=work"], ["--", "--continue"]])
def test_background_resumed_jobs_do_not_inject_saved_new_session_defaults(platform, job_capture,
                                                                         flags):
    started, _ = job_capture
    preferences(platform).set(platform.home, {"model": "sonnet", "effort": "high", "mode": "plan"})
    assert invoke(platform, "jobs", "start", "--prompt", PROMPT,
                  "--project", str(platform.home), *flags) == 0
    options = started[0][1]["options"]
    assert not {"model", "effort", "mode"} & set(options)


def test_background_never_implicitly_authorizes_migration(platform, job_capture, monkeypatch):
    started, _ = job_capture
    attempts = []

    def migration(device, *, yes):
        attempts.append(yes)
        raise platform.cli["CliError"]("finish or explicitly stop old work first")

    monkeypatch.setitem(platform.scope, "retire_live_grants", migration)
    assert invoke(platform, "jobs", "start", "--prompt", PROMPT,
                  "--project", str(platform.home)) == 2
    assert attempts == [False] and not started


def test_native_bg_flag_remains_rejected_not_mistaken_for_managed_background(platform):
    assert invoke(platform, "local", "--project", str(platform.home), "--", "--bg") == 2
    assert not platform.calls


def test_background_interactive_without_prompt_is_refused(platform, job_capture):
    started, _ = job_capture
    assert invoke(platform, "local", "--background", "--project", str(platform.home)) == 2
    assert not started and not platform.calls


def test_logout_revokes_before_draining_local_jobs(platform, monkeypatch):
    order = []
    monkeypatch.setitem(platform.scope, "api_json", lambda *a, **kw: order.append("revoke") or {})
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: SimpleNamespace(
        stop_device=lambda root, device: order.append("drain")))
    assert invoke(platform, "logout") == 0
    assert order == ["revoke", "drain"]
    assert platform.cli["load_config"]()["devices"] == {}
    assert not Path(platform.device["key"]).exists()


def test_logout_server_failure_preserves_pairing_but_still_drains_jobs(platform, monkeypatch):
    order = []
    original = platform.cli["load_config"]()

    def unavailable(*args, **kwargs):
        order.append("revoke")
        raise platform.cli["CliError"]("private server error " + PRIVATE, status=503)

    monkeypatch.setitem(platform.scope, "api_json", unavailable)
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: SimpleNamespace(
        stop_device=lambda root, device: order.append("drain")))
    assert invoke(platform, "logout") == 2
    assert order == ["revoke", "drain"]
    assert platform.cli["load_config"]() == original
    assert Path(platform.device["key"]).exists()


def test_uncertain_local_job_cleanup_does_not_prevent_server_revocation(platform, monkeypatch):
    order = []

    def drain(*args):
        order.append("drain")
        raise ValueError("unconfirmed crashed job")

    monkeypatch.setitem(platform.scope, "api_json", lambda *a, **kw: order.append("revoke") or {})
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: SimpleNamespace(stop_device=drain))
    assert invoke(platform, "logout") == 0
    assert order == ["revoke", "drain"]


def test_hidden_job_execution_uses_guard_and_keeps_prompt_out_of_native_argv(platform, monkeypatch):
    events, calls = [], []
    spec = {"device_id": "device", "slot_id": "slot", "project": str(platform.home),
            "prompt": PROMPT, "options": {}}

    @contextlib.contextmanager
    def guard():
        events.append("guarded")
        yield
        events.append("drained")

    helper = SimpleNamespace(parent_guard=guard, read_spec=lambda *a: spec)
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: helper)

    def call(command, **options):
        calls.append((command, options))
        return 0

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "call", call)
    monkeypatch.setattr(subprocess, "run", run)
    assert invoke_job_exec(platform) == 0
    assert events == ["guarded", "drained"] and len(calls) == 1
    command, options = calls[0]
    assert PROMPT not in command and PRIVATE not in command
    assert options.get("input") in (PROMPT, PROMPT.encode())
    assert "--print" in command
    assert platform.events[-1] == "closed"


def test_hidden_job_rejects_a_changed_slot_before_native_launch(platform, monkeypatch):
    helper = SimpleNamespace(parent_guard=contextlib.nullcontext, read_spec=lambda *a: {
        "device_id": "device", "slot_id": "different-slot", "project": str(platform.home),
        "prompt": PROMPT, "options": {}})
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: helper)
    assert invoke_job_exec(platform) == 2
    assert not platform.calls


@pytest.mark.parametrize("change", ["removed", "slot", "revoked", "unauthenticated", "offline"])
def test_background_authorization_fails_closed_on_assignment_or_endpoint_failure(platform,
                                                                               monkeypatch,
                                                                               change):
    spec = {"device_id": "device", "slot_id": "slot"}
    if change == "removed":
        platform.cli["save_config"]({"devices": {}, "active": ""})
    elif change == "slot":
        spec["slot_id"] = "different-slot"
    elif change == "revoked":
        platform.context["device"]["active"] = False
    elif change == "unauthenticated":
        platform.context["authenticated"] = False
    else:
        def unavailable(*args):
            raise platform.cli["CliError"](PRIVATE, status=503)
        monkeypatch.setitem(platform.scope, "device_context", unavailable)
    assert platform.cli["_job_authorized"](spec) is False
    assert not platform.calls


@pytest.mark.parametrize("change", ["account", "credential"])
def test_supervisor_pins_its_initial_account_and_device_credential(platform, monkeypatch, change):
    spec = {"device_id": "device", "slot_id": "slot", "project": str(platform.home),
            "prompt": PROMPT, "options": {}}
    results = []

    def run(root, job_id, *, command, check_authorization):
        assert check_authorization(spec) is True
        arguments = command(spec)
        assert arguments[arguments.index("__job-exec") + 1] == job_id
        assert arguments[arguments.index("--generation") + 1] == "a" * 24
        assert arguments[arguments.index("--device-generation") + 1] == hashlib.sha256(
            PRIVATE.encode()).hexdigest()
        assert PROMPT not in arguments and PRIVATE not in arguments
        if change == "account":
            platform.context["slot"]["account_generation"] = "b" * 24
        else:
            config = platform.cli["load_config"]()
            config["devices"]["device"]["device_token"] = "replacement-device-token"
            platform.cli["save_config"](config)
        results.append(check_authorization(spec))
        return 0

    monkeypatch.setitem(platform.scope, "local_jobs", lambda: SimpleNamespace(run=run))
    assert invoke(platform, "__job-run", "a" * 32) == 0
    assert results == [False]
    assert "account_generation" not in spec and "device_token" not in spec


@pytest.mark.parametrize("generation", [None, "", "account@example.com", "a" * 23, "A" * 24])
def test_background_authorization_rejects_missing_or_invalid_generation(platform, generation):
    platform.context["slot"]["account_generation"] = generation
    assert platform.cli["_job_authorized"]({"device_id": "device", "slot_id": "slot"}) is False


def test_hidden_job_refuses_changed_account_generation_before_launch(platform, monkeypatch):
    helper = SimpleNamespace(parent_guard=contextlib.nullcontext, read_spec=lambda *a: {
        "device_id": "device", "slot_id": "slot", "project": str(platform.home),
        "prompt": PROMPT, "options": {}})
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: helper)
    assert invoke_job_exec(platform, generation="b" * 24) == 2
    assert not platform.calls and "constructed" not in platform.events


def test_background_rechecks_account_generation_before_each_model_transport(platform, monkeypatch):
    spec = {"device_id": "device", "slot_id": "slot", "project": str(platform.home),
            "prompt": PROMPT, "options": {}}
    helper = SimpleNamespace(parent_guard=contextlib.nullcontext, read_spec=lambda *a: spec)
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: helper)
    bridges = []
    original_bridge = platform.helper.Bridge

    class Bridge(original_bridge):
        def __init__(self, command, environment):
            super().__init__(command, environment)
            bridges.append(self)

    platform.helper.Bridge = Bridge
    transports = []
    monkeypatch.setitem(platform.scope, "inference_command",
                        lambda device: transports.append(device["device_id"]) or ["fake-fixed-ssh"])

    def native(command, **options):
        assert bridges[0].command() == ["fake-fixed-ssh"]
        platform.context["slot"]["account_generation"] = "b" * 24
        with pytest.raises(inference_client.RelayError):
            bridges[0].command()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", native)
    assert invoke_job_exec(platform) == 0
    assert transports == ["device"]
    assert platform.events[-1] == "closed"


def test_real_version_probe_uses_clean_environment_and_only_version_argument(tmp_path, monkeypatch):
    binary, details = tmp_path / "claude", tmp_path / "probe.json"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", PRIVATE)
    monkeypatch.setenv("CCFLEET_NODE_TOKEN", PRIVATE)
    binary.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        f"open({str(details)!r}, 'w').write(json.dumps({{'args':sys.argv[1:],"
        "'keys':sorted(os.environ)}))\nprint('2.1.284 (Claude Code)')\n")
    binary.chmod(0o700)
    assert client_experience.native_version(str(binary)) == "2.1.284"
    report = json.loads(details.read_text())
    assert report["args"] == ["--version"]
    assert "ANTHROPIC_API_KEY" not in report["keys"] and "CCFLEET_NODE_TOKEN" not in report["keys"]


def test_real_version_probe_cleans_a_descendant_holding_stdout_after_parent_exit(tmp_path):
    binary, details = tmp_path / "claude", tmp_path / "probe.json"
    child_code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
    binary.write_text(
        f"#!{sys.executable}\nimport json,os,subprocess,sys\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}])\n"
        f"open({str(details)!r}, 'w').write(json.dumps({{'child':child.pid,"
        "'supervisor':os.getppid()}))\nprint('2.1.284',flush=True)\n")
    binary.chmod(0o700)
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        client_experience.native_version(str(binary), timeout=2)
    assert time.monotonic() - began < 5
    report = json.loads(details.read_text())

    def running(pid):
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=2)
        return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(running(pid) for pid in report.values()):
        time.sleep(0.05)
    assert not any(running(pid) for pid in report.values())


@pytest.mark.parametrize("acknowledged", [False, True])
def test_jobs_archive_requires_explicit_unconfirmed_acknowledgement(platform, monkeypatch,
                                                                  capsys, acknowledged):
    calls = []

    def archive(root, job_id, *, acknowledge_unconfirmed=False):
        calls.append((root, job_id, acknowledge_unconfirmed))
        return {"job_id": job_id, "archived": True,
                "cleanup_confirmed": not acknowledge_unconfirmed,
                "acknowledged_unconfirmed": acknowledge_unconfirmed}

    monkeypatch.setitem(platform.scope, "local_jobs", lambda: SimpleNamespace(archive=archive))
    flags = ["--acknowledge-unconfirmed"] if acknowledged else []
    job_id = "a" * 32
    assert invoke(platform, "jobs", "archive", job_id, *flags) == 0
    assert calls == [(platform.cli["home"]() / "jobs", job_id, acknowledged)]
    assert json.loads(capsys.readouterr().out)["cleanup_confirmed"] is not acknowledged
    assert not platform.calls


def test_refused_job_archive_does_not_fall_back_to_stop_or_delete(platform, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError("job is still active; stop it first")

    helper = SimpleNamespace(archive=refuse,
                             stop=lambda *a, **k: pytest.fail("implicit shutdown"))
    monkeypatch.setitem(platform.scope, "local_jobs", lambda: helper)
    assert invoke(platform, "jobs", "archive", "a" * 32) == 2
    assert not platform.calls


def test_transport_master_is_closed_if_bridge_initialization_fails(platform, monkeypatch):
    events = []

    class Transport:
        def __init__(self, *args):
            pass

        def start(self):
            events.append("started")
            return self

        def close(self):
            events.append("closed")

    def fail(*args):
        raise OSError("controlled bridge failure")

    monkeypatch.setitem(platform.scope, "inference_client", lambda: SimpleNamespace(
        TransportSession=Transport, Bridge=fail))
    assert invoke(platform, "local", "--reuse-transport") == 2
    assert events == ["started", "closed"]
    assert not platform.calls
