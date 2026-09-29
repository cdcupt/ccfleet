"""Real local processes and private sockets; no provider, account or model calls."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccfleet_agent import local_jobs as jobs

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def harness(tmp_path):
    root = tmp_path / "jobs"
    project = tmp_path / "project"
    project.mkdir()
    allowed = tmp_path / "authorization"
    allowed.write_text("allow")
    worker = tmp_path / "worker.py"
    worker.write_text(f'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from ccfleet_agent import local_jobs as jobs
jobs.AUTH_INTERVAL_S = 0.1
jobs.AUTH_TIMEOUT_S = 0.3
jobs.STOP_GRACE_S = 0.2
jobs.MAX_LOG_BYTES = 1024
mode, root, authorization, job_id = sys.argv[1:]
root, authorization = Path(root), Path(authorization)
if mode == "run":
    def authorize(spec):
        value = authorization.read_text()
        if value == "hang": time.sleep(60)
        if value == "error": raise RuntimeError("sensitive-account-detail")
        return value == "allow"
    def command(spec):
        return [sys.executable, __file__, "child", str(root), str(authorization), job_id]
    raise SystemExit(jobs.run(root, job_id, command=command, check_authorization=authorize))
if mode == "child":
    with jobs.parent_guard():
        spec = jobs.read_spec(root, job_id)
        assert str(Path.cwd()) == spec["project"]
        child = subprocess.Popen([sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])
        Path("processes-" + job_id).write_text(json.dumps({{
            "supervisor": os.getppid(), "child": os.getpid(), "grandchild": child.pid,
            "group": os.getpgrp()}}))
        print("synthetic stdout", flush=True)
        print("synthetic stderr", file=sys.stderr, flush=True)
        if spec["prompt"] == "large":
            print("x" * 100000, flush=True)
        if spec["prompt"] == "ignore":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if spec["prompt"] in ("sleep", "ignore"):
            time.sleep(60)
        else:
            time.sleep(0.15)
        if spec["prompt"] == "fail":
            raise SystemExit(7)
        Path("result-" + job_id).write_text("synthetic result")
''')
    live = []

    def start(prompt="sleep", *, device="device-a", timeout=10, max_jobs=4, **options):
        result = jobs.start(root, {
            "device_id": device, "slot_id": "slot-one", "project": str(project),
            "prompt": prompt, "options": options},
            [sys.executable, str(worker), "run", str(root), str(allowed)],
            timeout_s=timeout, max_jobs=max_jobs, startup_timeout=5)
        live.append(result["job_id"])
        return result

    yield root, project, allowed, worker, start
    for job_id in live:
        try:
            jobs.stop(root, job_id, timeout_s=2)
        except (jobs.JobError, OSError):
            pass


def eventually(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.03)
    raise AssertionError("bounded local job check timed out")


def finished(root, job_id):
    def check():
        value = jobs.status(root, job_id)
        return value if value["state"] in jobs.TERMINAL else None
    return eventually(check)


def processes(project, job_id):
    path = project / ("processes-" + job_id)
    eventually(path.exists)
    return json.loads(path.read_text())


def alive(pid):
    process = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=3)
    # An orphan zombie may await the OS reaper, but cannot run or access data.
    return bool(process.stdout.strip()) and not process.stdout.strip().startswith("Z")


def test_real_job_completes_with_local_output_and_no_secret_summary(harness):
    root, project, _, _, start = harness
    launched = start("finish")
    job_id = launched["job_id"]
    value = finished(root, job_id)
    assert value["state"] == "completed" and value["exit_code"] == 0
    assert value["cleanup_confirmed"] is True
    assert (project / ("result-" + job_id)).read_text() == "synthetic result"
    assert b"synthetic stdout" in jobs.logs(root, job_id)
    assert b"synthetic stderr" in jobs.logs(root, job_id, stream="stderr")
    summary = jobs.list_jobs(root)
    assert len(summary) == 1
    assert not ({"prompt", "options", "project", "socket", "instance", "pid"} & set(summary[0]))
    assert str(project) not in json.dumps(summary)
    info = processes(project, job_id)
    eventually(lambda: not alive(info["grandchild"]))


def test_real_stop_waits_for_whole_owned_process_group(harness):
    root, project, _, _, start = harness
    launched = start()
    info = processes(project, launched["job_id"])
    assert info["child"] == info["group"]
    result = jobs.stop(root, launched["job_id"])
    assert result["state"] == "stopped" and result["cleanup_confirmed"] is True
    eventually(lambda: not alive(info["child"]) and not alive(info["grandchild"]))
    assert jobs.stop(root, launched["job_id"]) == result


def test_job_survives_starting_terminal_process_exit(harness, tmp_path):
    root, project, allowed, worker, _ = harness
    spec = {"device_id": "device-a", "slot_id": "slot-one", "project": str(project),
            "prompt": "sleep", "options": {}}
    starter = tmp_path / "starter.py"
    starter.write_text(f'''
import json, sys
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from ccfleet_agent import local_jobs as jobs
result = jobs.start(Path({str(root)!r}), {spec!r},
    [sys.executable, {str(worker)!r}, "run", {str(root)!r}, {str(allowed)!r}], timeout_s=10)
print(json.dumps(result))
''')
    result = subprocess.run([sys.executable, str(starter)], capture_output=True,
                            text=True, timeout=8)
    assert result.returncode == 0, result.stderr
    job_id = json.loads(result.stdout)["job_id"]
    try:
        assert jobs.status(root, job_id)["state"] == "running"
    finally:
        jobs.stop(root, job_id)


def test_crashed_supervisor_cancels_child_without_autoresurrection(harness):
    root, project, _, worker, start = harness
    job_id = start()["job_id"]
    info = processes(project, job_id)
    os.kill(info["supervisor"], signal.SIGKILL)
    eventually(lambda: not alive(info["child"]) and not alive(info["grandchild"]))
    assert jobs.status(root, job_id)["state"] == "interrupted"
    with pytest.raises(jobs.JobError, match="cannot be confirmed"):
        jobs.stop(root, job_id)
    with pytest.raises(jobs.JobError, match="concurrent"):
        start(max_jobs=1)
    assert worker.exists()  # records and local files are preserved, never restarted/deleted


@pytest.mark.parametrize("prompt", ["sleep", "ignore"])
def test_runtime_deadline_terminates_even_an_uncooperative_child(harness, prompt):
    root, project, _, _, start = harness
    job_id = start(prompt, timeout=1)["job_id"]
    info = processes(project, job_id)
    value = finished(root, job_id)
    assert value["state"] == "timed_out" and value["cleanup_confirmed"] is True
    eventually(lambda: not alive(info["child"]) and not alive(info["grandchild"]))


@pytest.mark.parametrize("value,reason", [("deny", "authorization_denied"),
                                         ("hang", "authorization_unavailable"),
                                         ("error", "authorization_unavailable")])
def test_authorization_is_required_before_any_child_start(harness, value, reason):
    root, project, allowed, _, start = harness
    allowed.write_text(value)
    job_id = start()["job_id"]
    result = finished(root, job_id)
    assert result["reason"] == reason and result["cleanup_confirmed"] is True
    assert not (project / ("processes-" + job_id)).exists()
    assert "sensitive-account-detail" not in json.dumps(result)


def test_revocation_stops_an_idle_job_between_model_requests(harness):
    root, project, allowed, _, start = harness
    job_id = start()["job_id"]
    info = processes(project, job_id)
    allowed.write_text("deny")
    result = finished(root, job_id)
    assert result["reason"] == "authorization_denied"
    eventually(lambda: not alive(info["child"]) and not alive(info["grandchild"]))


def test_device_boundary_and_device_logout_cleanup(harness):
    root, _, _, _, start = harness
    one, two = start(device="device-a"), start(device="device-b")
    assert len(jobs.list_jobs(root, device_id="device-a")) == 1
    for action in (jobs.status, jobs.stop, jobs.logs):
        with pytest.raises(jobs.JobError, match="another paired device"):
            action(root, one["job_id"], device_id="device-b")
    result = jobs.stop_device(root, "device-a")
    assert result[0]["job_id"] == one["job_id"]
    assert jobs.status(root, two["job_id"])["state"] == "running"


def test_concurrency_limit_is_enforced_before_spawning(harness):
    root, _, _, _, start = harness
    one = start(max_jobs=1)
    with pytest.raises(jobs.JobError, match="concurrent"):
        start(max_jobs=1)
    assert len(jobs.list_jobs(root)) == 1
    assert jobs.status(root, one["job_id"])["state"] == "running"


def test_logs_are_private_bounded_and_only_returned_on_explicit_read(harness):
    root, _, _, _, start = harness
    job_id = start("large")["job_id"]
    result = finished(root, job_id)
    assert result["log_truncated"] is True
    assert len(jobs.logs(root, job_id)) == 1024
    assert len(jobs.logs(root, job_id, max_bytes=10)) == 10
    assert "synthetic stdout" not in json.dumps(jobs.list_jobs(root))


def test_files_and_socket_are_owner_only(harness):
    root, _, _, _, start = harness
    job_id = start()["job_id"]
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    directory = root / job_id
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for file in directory.iterdir():
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert file.stat().st_uid == os.getuid()
    state = json.loads((directory / "state.json").read_text())
    address = Path(state["socket"])
    assert stat.S_IMODE(address.stat().st_mode) == 0o600
    assert stat.S_IMODE(address.parent.stat().st_mode) == 0o700
    serialized = (directory / "spec.json").read_text()
    for prohibited in ("device_token", "known_hosts", "private_key", "api_key", "authorization"):
        assert prohibited not in serialized
    jobs.stop(root, job_id)
    assert not address.exists() and not address.parent.exists()


def test_lost_stop_reply_does_not_hide_successful_confirmed_cleanup(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start()["job_id"]
    real = jobs._control

    def eof(value, operation, **options):
        real(value, operation, **options)
        raise EOFError("socket closed while daemon shut down")

    monkeypatch.setattr(jobs, "_control", eof)
    assert jobs.stop(root, job_id)["cleanup_confirmed"] is True


def test_unresponsive_control_never_means_stopped(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start()["job_id"]
    with monkeypatch.context() as scoped:
        scoped.setattr(jobs, "_control", lambda *a, **k: (_ for _ in ()).throw(EOFError()))
        with pytest.raises(jobs.JobError, match="unconfirmed"):
            jobs.stop(root, job_id, timeout_s=0.2)
        assert jobs.status(root, job_id)["state"] == "unresponsive"
    assert jobs.status(root, job_id)["state"] == "running"


def test_saved_pid_is_never_used_for_shutdown(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    path = root / job_id / "state.json"
    data = json.loads(path.read_text())
    data.update(state="running", cleanup_confirmed=False, pid=os.getpid())
    path.write_text(json.dumps(data))

    def forbidden(*args):
        raise AssertionError("must never signal a saved/reused PID")

    monkeypatch.setattr(jobs.os, "kill", forbidden)
    monkeypatch.setattr(jobs.os, "killpg", forbidden)
    assert jobs.status(root, job_id)["state"] == "interrupted"
    with pytest.raises(jobs.JobError):
        jobs.stop(root, job_id)


def test_completed_job_cannot_be_restarted(harness):
    root, _, allowed, worker, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    result = subprocess.run([sys.executable, str(worker), "run", str(root), str(allowed), job_id],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "only be started once" in result.stderr


def test_failed_native_command_is_not_retried(harness):
    root, project, _, _, start = harness
    job_id = start("fail")["job_id"]
    result = finished(root, job_id)
    assert result["state"] == "failed" and result["exit_code"] == 7
    assert result["reason"] == "command_failed"
    assert not (project / ("result-" + job_id)).exists()


@pytest.mark.parametrize("name", ["../outside", "../../etc", "a" * 31, "A" * 32, "a" * 32 + "\n"])
def test_invalid_job_paths_are_refused(tmp_path, name):
    with pytest.raises((jobs.JobError, FileNotFoundError)):
        jobs.status(tmp_path, name)


@pytest.mark.parametrize("field,value", [("device_token", "private"), ("environment", {}),
                                        ("key", "/private/key")])
def test_job_spec_cannot_duplicate_credentials(tmp_path, field, value):
    spec = {"device_id": "d", "slot_id": "s", "project": str(tmp_path),
            "prompt": "hi", "options": {}, field: value}
    with pytest.raises(jobs.JobError, match="fields"):
        jobs.start(tmp_path / "jobs", spec, ["not-started"])


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), 86401])
def test_job_timeout_is_finite_and_bounded(tmp_path, timeout):
    with pytest.raises(jobs.JobError, match="timeout"):
        jobs.start(tmp_path, {}, [], timeout_s=timeout)


@pytest.mark.parametrize("count", [0, -1, True, 17, 1.5])
def test_job_concurrency_is_bounded(tmp_path, count):
    with pytest.raises(jobs.JobError, match="concurrent"):
        jobs.start(tmp_path, {}, [], max_jobs=count)


def test_symlinked_root_or_ancestor_is_not_followed(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    for path in (alias, alias / "jobs"):
        with pytest.raises(OSError):
            jobs.list_jobs(path)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("file", ["state.json", "spec.json", "stdout.log"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "fifo"])
def test_unsafe_files_are_never_read(harness, file, kind, tmp_path):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    path = root / job_id / file
    backup = path.read_bytes()
    path.unlink()
    private = tmp_path / "must-not-read"
    private.write_bytes(backup)
    private.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(private)
    elif kind == "hardlink":
        os.link(private, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(backup)
        path.chmod(0o644)
    action = {"state.json": jobs.status, "spec.json": jobs.read_spec, "stdout.log": jobs.logs}[file]
    with pytest.raises((jobs.JobError, OSError)):
        action(root, job_id)
    assert private.read_bytes() == backup


def test_parent_guard_requires_live_supervisor_not_manual_environment(monkeypatch):
    monkeypatch.delenv(jobs.GUARD_FD_ENV, raising=False)
    monkeypatch.delenv(jobs.LIVENESS_FD_ENV, raising=False)
    with pytest.raises(jobs.JobError, match="supervisor"):
        with jobs.parent_guard():
            raise AssertionError("must not execute")


def test_empty_job_listing_does_not_create_state(tmp_path):
    path = tmp_path / "not-created"
    assert jobs.list_jobs(path) == []
    assert not path.exists()


@pytest.mark.parametrize("change", [{"instance": "0" * 64}, {"job_id": "0" * 32},
                                    {"version": True}, {"operation": []},
                                    {"operation": "execute", "command": "not allowed"}])
def test_invalid_control_requests_cannot_stop_or_execute(harness, change):
    root, _, _, _, start = harness
    job_id = start()["job_id"]
    state = json.loads((root / job_id / "state.json").read_text())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(state["socket"])
        jobs._send(connection, {"version": 1, "job_id": job_id,
                                "instance": state["instance"], "operation": "stop", **change})
        assert connection.recv(1) == b""
    assert jobs.status(root, job_id)["state"] == "running"


def test_control_response_identity_is_checked(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start()["job_id"]
    state = json.loads((root / job_id / "state.json").read_text())
    receive = jobs._receive

    def wrong_instance(connection, deadline=None):
        result = receive(connection, deadline)
        result["instance"] = "0" * 64
        return result

    with monkeypatch.context() as scope:
        scope.setattr(jobs, "_receive", wrong_instance)
        with pytest.raises(jobs.JobError, match="identity"):
            jobs._control(state, "status")
    assert jobs.status(root, job_id)["state"] == "running"


def test_device_stop_still_stops_other_jobs_after_an_unconfirmed_crash(harness):
    root, project, _, _, start = harness
    crashed = start()["job_id"]
    info = processes(project, crashed)
    os.kill(info["supervisor"], signal.SIGKILL)
    eventually(lambda: not alive(info["child"]))
    active = start()["job_id"]
    with pytest.raises(jobs.JobError, match="unconfirmed"):
        jobs.stop_device(root, "device-a")
    assert finished(root, active)["state"] == "stopped"


def test_unowned_state_file_is_rejected(harness, monkeypatch):
    from types import SimpleNamespace

    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    inode = (root / job_id / "state.json").stat().st_ino
    real = os.fstat

    def foreign(fd):
        info = real(fd)
        if info.st_ino != inode:
            return info
        return SimpleNamespace(st_uid=os.getuid() + 1, st_mode=info.st_mode,
                               st_nlink=info.st_nlink, st_size=info.st_size)

    with monkeypatch.context() as scope:
        scope.setattr(jobs.os, "fstat", foreign)
        with pytest.raises(jobs.JobError, match="unsafe"):
            jobs.status(root, job_id)


def test_log_cleanup_io_failure_cannot_skip_process_group_shutdown(harness):
    root, project, allowed, worker, start = harness
    # The injected error exists only in this private synthetic worker, not the
    # running test process, and fires on log writes rather than state writes.
    source = worker.read_text()
    source = source.replace('if mode == "run":', '''if mode == "run":
    original_write = jobs._write_all
    def broken_log(fd, data):
        if b"synthetic" in data: raise OSError("simulated full log disk")
        return original_write(fd, data)
    jobs._write_all = broken_log
''')
    worker.write_text(source)
    job_id = start()["job_id"]
    info = processes(project, job_id)
    result = finished(root, job_id)
    assert result["state"] == "failed" and result["reason"] == "runner_failed"
    assert result["cleanup_confirmed"] is True
    eventually(lambda: not alive(info["child"]) and not alive(info["grandchild"]))
    assert allowed.exists()


def test_guard_remains_armed_after_context_exit_until_process_exit(harness):
    root, project, _, worker, start = harness
    source = worker.read_text()
    source += '\nif mode == "child":\n    Path("outside-guard").touch()\n    time.sleep(60)\n'
    worker.write_text(source)
    job_id = start("finish")["job_id"]
    info = processes(project, job_id)
    eventually((project / "outside-guard").exists)
    os.kill(info["supervisor"], signal.SIGKILL)
    eventually(lambda: not alive(info["child"]) and not alive(info["grandchild"]))
    assert jobs.status(root, job_id)["state"] == "interrupted"


def test_confirmed_archive_preserves_every_file_privately_and_frees_capacity(harness):
    root, _, _, _, start = harness
    job_id = start("finish", max_jobs=1)["job_id"]
    finished(root, job_id)
    directory = root / job_id
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    report = jobs.archive(root, job_id, device_id="device-a")
    archived = root / "archive" / job_id
    assert report["archived"] is True and report["cleanup_confirmed"] is True
    assert report["acknowledged_unconfirmed"] is False
    assert not directory.exists() and jobs.list_jobs(root) == []
    assert before == {p.name: p.read_bytes() for p in archived.iterdir()}
    assert (root / "archive").stat().st_mode & 0o777 == 0o700
    assert archived.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in archived.iterdir())
    assert start(max_jobs=1)["state"] == "running"


@pytest.mark.parametrize("acknowledge", [False, True])
def test_live_job_cannot_be_archived_even_with_acknowledgement(harness, acknowledge):
    root, _, _, _, start = harness
    job_id = start()["job_id"]
    with pytest.raises(jobs.JobError, match="active"):
        jobs.archive(root, job_id, acknowledge_unconfirmed=acknowledge)
    assert jobs.status(root, job_id)["state"] == "running"
    assert (root / job_id).is_dir()


def test_crash_record_archive_requires_ack_and_does_not_claim_cleanup(harness, monkeypatch):
    root, project, _, _, start = harness
    job_id = start()["job_id"]
    info = processes(project, job_id)
    os.kill(info["supervisor"], signal.SIGKILL)
    eventually(lambda: not alive(info["child"]))
    with pytest.raises(jobs.JobError, match="acknowledgement"):
        jobs.archive(root, job_id)
    with monkeypatch.context() as scope:
        scope.setattr(jobs.os, "kill", lambda *a: pytest.fail("saved PID signal"))
        scope.setattr(jobs.os, "killpg", lambda *a: pytest.fail("saved group signal"))
        result = jobs.archive(root, job_id, acknowledge_unconfirmed=True)
    assert result["archived"] is True and result["cleanup_confirmed"] is False
    assert result["acknowledged_unconfirmed"] is True
    assert jobs.list_jobs(root) == []
    stored = json.loads((root / "archive" / job_id / "state.json").read_text())
    assert stored["state"] == "running" and stored["cleanup_confirmed"] is False


def test_archive_device_boundary_is_checked_before_any_move(harness):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    with pytest.raises(jobs.JobError, match="another paired device"):
        jobs.archive(root, job_id, device_id="device-b")
    assert (root / job_id).is_dir()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_archive_never_replaces_a_preexisting_target(harness, kind, tmp_path):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    archived = root / "archive"
    archived.mkdir(mode=0o700)
    target = archived / job_id
    if kind == "directory":
        target.mkdir()
    elif kind == "file":
        target.write_text("preserve target")
    else:
        target.symlink_to(tmp_path)
    before = target.lstat()
    with pytest.raises((jobs.JobError, OSError)):
        jobs.archive(root, job_id)
    assert (root / job_id).is_dir()
    assert target.lstat().st_ino == before.st_ino


def test_archive_target_race_is_exclusive_not_check_then_overwrite(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    real = jobs._rename_exclusive

    def raced(source, target, name):
        os.mkdir(name, 0o700, dir_fd=target)
        return real(source, target, name)

    with monkeypatch.context() as scope:
        scope.setattr(jobs, "_rename_exclusive", raced)
        with pytest.raises(jobs.JobError, match="already has an archive"):
            jobs.archive(root, job_id)
    assert (root / job_id).is_dir()
    assert list((root / "archive" / job_id).iterdir()) == []


def test_archive_refuses_symlinked_storage_and_source(harness, tmp_path):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    target = tmp_path / "outside"
    target.mkdir(mode=0o700)
    (root / "archive").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        jobs.archive(root, job_id)
    assert not list(target.iterdir())
    (root / "archive").unlink()
    real = root / ("moved-" + job_id)
    (root / job_id).rename(real)
    (root / job_id).symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        jobs.archive(root, job_id)
    assert real.exists()


def test_archive_refuses_public_archive_directory(harness):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    (root / "archive").mkdir(mode=0o755)
    with pytest.raises(jobs.JobError, match="unsafe"):
        jobs.archive(root, job_id)
    assert (root / job_id).is_dir()


def test_archive_requires_inactive_control_even_when_record_claims_completed(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    path = root / job_id / "state.json"
    state = json.loads(path.read_text())
    state["socket"] = "/synthetic-private/control"
    path.write_text(json.dumps(state))
    for control in (lambda *a, **k: {}, lambda *a, **k: (_ for _ in ()).throw(TimeoutError())):
        with monkeypatch.context() as scope:
            scope.setattr(jobs, "_control", control)
            with pytest.raises(jobs.JobError, match="control"):
                jobs.archive(root, job_id, acknowledge_unconfirmed=True)
        assert (root / job_id).is_dir()


def test_archive_rechecks_resident_lock_identity_after_control_check(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    directory = root / job_id
    state_path = directory / "state.json"
    state = json.loads(state_path.read_text())
    state["socket"] = "/synthetic-private/control"
    state_path.write_text(json.dumps(state))

    def replaced(*args, **kwargs):
        (directory / "run.lock").unlink()
        (directory / "run.lock").write_text("")
        (directory / "run.lock").chmod(0o600)
        raise ConnectionRefusedError

    with monkeypatch.context() as scope:
        scope.setattr(jobs, "_control", replaced)
        with pytest.raises(jobs.JobError, match="identity changed"):
            jobs.archive(root, job_id)
    assert directory.is_dir()


def test_archive_holds_global_and_resident_locks_through_atomic_move(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    finished(root, job_id)
    real = jobs._rename_exclusive

    def checked(source, target, name):
        for path in (root / "jobs.lock", root / job_id / "run.lock"):
            with path.open("r+") as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return real(source, target, name)

    with monkeypatch.context() as scope:
        scope.setattr(jobs, "_rename_exclusive", checked)
        assert jobs.archive(root, job_id)["archived"] is True


def test_device_stop_uses_one_deadline_not_one_timeout_per_record(harness, monkeypatch):
    root, _, _, _, start = harness
    ids = [start()["job_id"] for _ in range(3)]
    calls = []

    def bounded(root, job_id, *, device_id, deadline):
        calls.append((job_id, deadline))
        time.sleep(max(0, deadline - time.monotonic()))
        raise jobs.JobError("unresponsive")

    began = time.monotonic()
    with monkeypatch.context() as scope:
        scope.setattr(jobs, "list_jobs", lambda *a, **k: pytest.fail("per-job status probes"))
        scope.setattr(jobs, "_stop_until", bounded)
        with pytest.raises(jobs.JobError) as error:
            jobs.stop_device(root, "device-a", timeout_s=0.2)
    assert time.monotonic() - began < 0.7
    assert len(calls) == 1
    assert all(job_id in str(error.value) for job_id in ids)


@pytest.mark.parametrize("timeout", [0, -1, True, 15.1, float("inf")])
def test_device_stop_budget_is_at_most_fifteen_seconds(tmp_path, timeout):
    with pytest.raises(jobs.JobError, match="timeout"):
        jobs.stop_device(tmp_path, "device-a", timeout_s=timeout)


def test_control_frame_deadline_shrinks_across_partial_reads():
    connection, peer = socket.socketpair()
    with connection, peer:
        peer.sendall(b"\x00")
        began = time.monotonic()
        with pytest.raises((TimeoutError, socket.timeout)):
            jobs._receive(connection, time.monotonic() + 0.1)
        assert time.monotonic() - began < 0.4


def test_damaged_record_does_not_prevent_stopping_other_verifiable_jobs(harness):
    root, _, _, _, start = harness
    bad = start("finish")["job_id"]
    finished(root, bad)
    active = start()["job_id"]
    (root / bad / "state.json").write_text("not json")
    with pytest.raises(jobs.JobError) as error:
        jobs.stop_device(root, "device-a", timeout_s=2)
    assert bad in str(error.value)
    assert finished(root, active)["state"] == "stopped"


def test_atomic_state_replacement_after_open_is_a_safe_unlinked_snapshot(harness, monkeypatch):
    root, _, _, _, start = harness
    job_id = start("finish")["job_id"]
    original = finished(root, job_id)
    state_path = root / job_id / "state.json"
    replacement = json.loads(state_path.read_text())
    replacement["exit_code"] = 7
    real = os.open
    exchanged = []

    def replace_after_open(path, flags, *args, **kwargs):
        fd = real(path, flags, *args, **kwargs)
        if path == "state.json" and not exchanged:
            exchanged.append(True)
            temporary = state_path.with_name("replacement.json")
            temporary.write_text(json.dumps(replacement))
            temporary.chmod(0o600)
            os.replace(temporary, state_path)
            assert os.fstat(fd).st_nlink == 0
        return fd

    with monkeypatch.context() as scope:
        scope.setattr(jobs.os, "open", replace_after_open)
        assert jobs.status(root, job_id) == original
    assert exchanged and jobs.status(root, job_id)["exit_code"] == 7
