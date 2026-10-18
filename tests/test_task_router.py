"""Operator-only delegation with fake native CLIs; no provider or account calls."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccfleet_agent import local_jobs
from ccfleet_agent import task_router as router


def eventually(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("bounded synthetic task check timed out")


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    native_home = tmp_path / "native-home"
    native_home.mkdir()
    native_data = tmp_path / "native-data"
    credentials = native_data / "devin" / "credentials.toml"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("synthetic fixture, never a provider token")
    credentials.chmod(0o600)
    monkeypatch.setenv("HOME", str(native_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(native_data))
    binary = tmp_path / "fake-native"
    binary.write_text(f'''#!{sys.executable}
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
if args[-2:] == ["auth", "status"]:
    print("synthetic private account identity")
    print("synthetic private auth detail", file=sys.stderr)
    raise SystemExit(17 if Path(".auth-fail").exists() else 0)
def argument(flag):
    return args[args.index(flag) + 1]
prompt = Path(argument("--prompt-file")).read_text() if "--prompt-file" in args else sys.stdin.read()
record = {{"arguments": args, "environment": dict(os.environ)}}
with Path(".launches").open("a") as stream:
    stream.write(json.dumps(record) + "\\n")
if "--config" in args:
    record["configuration"] = json.loads(Path(argument("--config")).read_text())
    Path(".devin-config-seen").write_text(json.dumps(record["configuration"]))
if "--respect-workspace-trust" in args and Path(".untrusted").exists():
    print("synthetic workspace is untrusted", file=sys.stderr)
    raise SystemExit(12)
if "--export" in args:
    Path(argument("--export")).write_text("synthetic ATIF trace")
print("synthetic task stdout", flush=True)
print("synthetic task stderr", file=sys.stderr, flush=True)
if prompt == "large":
    print("x" * (5 * 1024 * 1024), flush=True)
if prompt == "fork":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(".grandchild").write_text(str(child.pid))
if prompt in ("sleep", "fork"):
    Path(".running").write_text("ready")
    time.sleep(60)
raise SystemExit(7 if prompt == "fail" else 0)
''')
    binary.chmod(0o700)
    config_path = tmp_path / "router.json"
    value = {
        "version": 1, "enabled": True, "state_dir": str(tmp_path / "task-state"),
        "timeout_s": 10, "workspaces": {"pilot": {"path": str(project), "trusted": True}},
        "backends": {name: {"binary": str(binary), "model": "selected-claude-model",
                            "auth_verified": True} for name in ("claude", "devin")},
    }

    def configure():
        config_path.write_text(json.dumps(value))
        config_path.chmod(0o600)
        return router.load_config(config_path)

    configure()
    launched = []

    def start(prompt="finish", backend="devin"):
        prompt_path = tmp_path / "selected-prompt.txt"
        prompt_path.write_text(prompt)
        result = router.start(config_path, router.load_config(config_path), backend,
                              "pilot", prompt_path)
        launched.append(result["job_id"])
        return result

    result = {"project": project, "value": value, "path": config_path,
              "root": Path(value["state_dir"]), "configure": configure, "start": start,
              "credentials": credentials, "binary": binary, "launched": launched}
    yield result
    for job_id in launched:
        try:
            local_jobs.stop(result["root"], job_id, timeout_s=5)
        except (OSError, ValueError):
            pass


def finished(pilot, job_id):
    return eventually(lambda: (value if value["state"] in local_jobs.TERMINAL else None)
                      if (value := local_jobs.status(pilot["root"], job_id)) else None)


@pytest.mark.parametrize("backend", ["claude", "devin"])
def test_native_success_uses_fixed_model_private_prompt_and_suppresses_auth_identity(pilot, backend):
    result = pilot["start"](backend=backend)
    status = finished(pilot, result["job_id"])
    assert status["state"] == "completed" and status["exit_code"] == 0
    assert status["cleanup_confirmed"] is True
    assert result["selected_model"] == "selected-claude-model"
    lines = (pilot["project"] / ".launches").read_text().splitlines()
    assert len(lines) == 1
    arguments = json.loads(lines[0])["arguments"]
    assert arguments[:2] == ["--model", "selected-claude-model"] if backend == "claude" else (
        arguments[:1] == ["--config"])
    assert "finish" not in arguments and "--print" in arguments
    assert not {"--dangerously-skip-permissions", "--permission-mode", "--cloud"} & set(arguments)
    if backend == "devin":
        assert arguments[arguments.index("--respect-workspace-trust") + 1] == "true"
        configuration = json.loads((pilot["project"] / ".devin-config-seen").read_text())
        assert configuration == {"auto_update": False, "subagents_enabled": False,
                                 "agent": {"model": "selected-claude-model"}}
        for name in ("devin-config.json", "prompt.txt", "trace.atif"):
            assert stat.S_IMODE((pilot["root"] / result["job_id"] / name).stat().st_mode) == 0o600
    output = local_jobs.logs(pilot["root"], result["job_id"])
    errors = local_jobs.logs(pilot["root"], result["job_id"], stream="stderr")
    assert b"synthetic task stdout" in output and b"synthetic task stderr" in errors
    assert b"account identity" not in output + errors and b"auth detail" not in output + errors
    summary = router._summary(pilot["root"], status)
    assert summary["backend"] == backend and "actual_model" not in summary
    assert not {"prompt", "project", "options", "socket", "instance", "pid"} & set(summary)


def test_native_failure_propagates_exit_without_resubmission_or_backend_fallback(pilot):
    result = pilot["start"]("fail")
    status = finished(pilot, result["job_id"])
    assert status["state"] == "failed" and status["exit_code"] == 7
    assert len((pilot["project"] / ".launches").read_text().splitlines()) == 1


def test_failed_authentication_runs_no_model_and_exposes_no_identity(pilot):
    (pilot["project"] / ".auth-fail").touch()
    result = pilot["start"]()
    status = finished(pilot, result["job_id"])
    assert status["state"] == "failed" and status["exit_code"] == 17
    assert not (pilot["project"] / ".launches").exists()
    assert b"private" not in local_jobs.logs(pilot["root"], result["job_id"], stream="stderr")


@pytest.mark.parametrize("kind", ["missing", "public", "symlink", "hardlink"])
def test_devin_auth_status_zero_without_safe_native_credentials_is_denied(pilot, kind):
    credentials = pilot["credentials"]
    if kind == "missing":
        credentials.unlink()
    elif kind == "public":
        credentials.chmod(0o644)
    elif kind == "symlink":
        moved = credentials.with_name("moved")
        credentials.rename(moved)
        credentials.symlink_to(moved)
    else:
        os.link(credentials, credentials.with_name("linked"))
    result = pilot["start"]()
    assert finished(pilot, result["job_id"])["state"] == "failed"
    assert not (pilot["project"] / ".launches").exists()


def test_devin_native_workspace_trust_failure_is_preserved(pilot):
    (pilot["project"] / ".untrusted").touch()
    result = pilot["start"]()
    assert finished(pilot, result["job_id"])["exit_code"] == 12


def test_single_concurrency_blocks_a_second_task(pilot):
    first = pilot["start"]("sleep")
    eventually((pilot["project"] / ".running").exists)
    with pytest.raises(local_jobs.JobError, match="maximum concurrent"):
        pilot["start"]("finish", backend="claude")
    assert local_jobs.stop(pilot["root"], first["job_id"])["cleanup_confirmed"] is True


def test_timeout_stops_the_native_task_without_retry(pilot):
    pilot["value"]["timeout_s"] = 1
    pilot["configure"]()
    result = pilot["start"]("sleep")
    status = finished(pilot, result["job_id"])
    assert status["state"] == "timed_out" and status["cleanup_confirmed"] is True
    assert len((pilot["project"] / ".launches").read_text().splitlines()) == 1


def test_cancel_kills_owned_process_group_including_descendant(pilot):
    result = pilot["start"]("fork")
    marker = pilot["project"] / ".grandchild"
    eventually(marker.exists)
    pid = int(marker.read_text())
    value = local_jobs.stop(pilot["root"], result["job_id"])
    assert value["state"] == "stopped" and value["cleanup_confirmed"] is True

    def gone():
        process = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3)
        return not process.stdout.strip() or process.stdout.strip().startswith("Z")

    eventually(gone)


def test_provider_and_permission_environment_overrides_are_not_inherited(pilot, monkeypatch):
    variables = {"DEVIN_MODEL": "other-model", "DEVIN_PERMISSION_MODE": "dangerous",
                 "WINDSURF_API_KEY": "synthetic", "ANTHROPIC_API_KEY": "synthetic",
                 "ANTHROPIC_BASE_URL": "https://invalid.example", "CLAUDE_CONFIG_DIR": "/tmp/other",
                 "CLAUDE_CODE_OAUTH_TOKEN": "synthetic", "HTTPS_PROXY": "http://invalid.example",
                 "PYTHONPATH": "/tmp/untrusted", "AWS_SECRET_ACCESS_KEY": "synthetic"}
    for key, value in variables.items():
        monkeypatch.setenv(key, value)
    result = pilot["start"]()
    assert finished(pilot, result["job_id"])["state"] == "completed"
    environment = json.loads((pilot["project"] / ".launches").read_text())["environment"]
    assert not set(variables) & set(environment)
    assert environment["HOME"] == os.environ["HOME"]
    assert environment["XDG_DATA_HOME"] == os.environ["XDG_DATA_HOME"]
    assert local_jobs.GUARD_FD_ENV not in environment
    assert local_jobs.LIVENESS_FD_ENV not in environment


@pytest.mark.parametrize("kind", ["disabled", "omitted-enabled", "unverified-auth", "untrusted"])
def test_operator_gates_fail_before_creating_jobs(pilot, kind):
    value = pilot["value"]
    if kind == "disabled":
        value["enabled"] = False
    elif kind == "omitted-enabled":
        value.pop("enabled")
    elif kind == "unverified-auth":
        value["backends"]["devin"]["auth_verified"] = False
    else:
        value["workspaces"]["pilot"]["trusted"] = False
    pilot["configure"]()
    with pytest.raises(router.RouterError):
        pilot["start"]()
    assert not pilot["root"].exists()


@pytest.mark.parametrize("kind", ["unknown", "relative", "symlink", "state-in-project"])
def test_workspace_escape_inputs_never_create_jobs(pilot, kind):
    if kind == "unknown":
        prompt = pilot["path"].with_name("prompt")
        prompt.write_text("finish")
        with pytest.raises(router.RouterError, match="unknown"):
            router.start(pilot["path"], router.load_config(pilot["path"]), "devin", "../escape", prompt)
    else:
        value = pilot["value"]
        if kind == "relative":
            value["workspaces"]["pilot"]["path"] = "../project"
        elif kind == "symlink":
            linked = pilot["project"].with_name("linked")
            linked.symlink_to(pilot["project"], target_is_directory=True)
            value["workspaces"]["pilot"]["path"] = str(linked)
        else:
            value["state_dir"] = str(pilot["project"] / "state")
        with pytest.raises(router.RouterError):
            pilot["configure"]()
    assert not pilot["root"].exists()


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_readonly_configuration_supports_exact_owner_modes(pilot, mode):
    pilot["path"].chmod(mode)
    before = pilot["path"].read_bytes()
    assert router.load_config(pilot["path"])["version"] == 1
    assert pilot["path"].read_bytes() == before


@pytest.mark.parametrize("mutation", ["unknown-field", "unknown-arg", "bad-version", "long-timeout",
                                      "bool-timeout", "command-string"])
def test_exact_configuration_schema_rejects_unsupported_controls(pilot, mutation):
    value = pilot["value"]
    if mutation == "unknown-field":
        value["extra"] = True
    elif mutation == "unknown-arg":
        value["backends"]["devin"]["args"] = ["--dangerous"]
    elif mutation == "bad-version":
        value["version"] = True
    elif mutation == "long-timeout":
        value["timeout_s"] = 901
    elif mutation == "bool-timeout":
        value["timeout_s"] = True
    else:
        value["backends"]["devin"]["binary"] += " --dangerous"
    with pytest.raises(router.RouterError):
        pilot["configure"]()


@pytest.mark.parametrize("kind", ["unsafe-mode", "duplicate", "symlink"])
def test_unsafe_configuration_file_is_rejected(pilot, kind):
    if kind == "unsafe-mode":
        pilot["path"].chmod(0o666)
    elif kind == "duplicate":
        pilot["path"].write_text('{"version":1,"version":1}')
    else:
        moved = pilot["path"].with_name("actual")
        pilot["path"].rename(moved)
        pilot["path"].symlink_to(moved)
    with pytest.raises((OSError, router.RouterError)):
        router.load_config(pilot["path"])


def test_changed_config_revokes_worker_authorization_and_keeps_stop_available(pilot):
    config = router.load_config(pilot["path"])
    result = pilot["start"]("sleep")
    spec = local_jobs.read_spec(pilot["root"], result["job_id"])
    assert router.authorized(pilot["path"], pilot["root"], config["binding"], spec)
    pilot["value"]["enabled"] = False
    pilot["configure"]()
    assert not router.authorized(pilot["path"], pilot["root"], config["binding"], spec)
    assert local_jobs.stop(pilot["root"], result["job_id"])["cleanup_confirmed"] is True


def test_json_cli_status_logs_list_and_stop_are_bounded_and_work_when_disabled(pilot, capsys):
    result = pilot["start"]()
    job_id = result["job_id"]
    finished(pilot, job_id)
    pilot["value"]["enabled"] = False
    pilot["configure"]()
    prefix = ["--config", str(pilot["path"])]
    for args in (["status", job_id], ["logs", job_id, "--bytes", "100"], ["list"], ["stop", job_id]):
        assert router.main([*prefix, *args]) == 0
        value = json.loads(capsys.readouterr().out)
        assert isinstance(value, (dict, list))
        assert str(pilot["project"]) not in json.dumps(value)
    assert router.main([*prefix, "logs", job_id, "--bytes", "999999999"]) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["error"] == "task_router_failed"
    assert str(pilot["path"]) not in json.dumps(error)


def test_output_is_capped_by_existing_supervisor(pilot):
    result = pilot["start"]("large")
    status = finished(pilot, result["job_id"])
    assert status["state"] == "completed" and status["log_truncated"] is True
    assert len(local_jobs.logs(pilot["root"], result["job_id"],
                               max_bytes=local_jobs.MAX_LOG_BYTES)) == local_jobs.MAX_LOG_BYTES


@pytest.mark.parametrize("kind", ["deleted", "symlink-replacement"])
def test_immediate_cancel_survives_native_binary_and_workspace_loss(pilot, capsys, kind):
    result = pilot["start"]("sleep")
    eventually((pilot["project"] / ".running").exists)
    if kind == "deleted":
        pilot["binary"].unlink()
        shutil.rmtree(pilot["project"])
    else:
        moved_binary = pilot["binary"].with_name("moved-binary")
        moved_project = pilot["project"].with_name("moved-project")
        pilot["binary"].rename(moved_binary)
        pilot["binary"].symlink_to(moved_binary)
        pilot["project"].rename(moved_project)
        pilot["project"].symlink_to(moved_project, target_is_directory=True)
    with pytest.raises(router.RouterError):
        router.load_config(pilot["path"])
    prefix = ["--config", str(pilot["path"])]
    assert router.main([*prefix, "status", result["job_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "running"
    assert router.main([*prefix, "stop", result["job_id"]]) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["state"] == "stopped" and stopped["cleanup_confirmed"] is True
