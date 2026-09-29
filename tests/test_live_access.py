"""Live mounts keep one pending operation and never remount beneath a session."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ccfleet_agent import live_access as live

WORKSPACE = "a" * 32
INSTANCE = "b" * 32
ROOT = Path(__file__).resolve().parents[1]


def packet(body):
    return struct.pack("!I", len(body)) + body


def envelope(sequence, raw):
    return {"version": 1, "sequence": sequence, "data": base64.b64encode(raw).decode()}


@pytest.mark.parametrize("value", ["", "../escape", "a" * 31, "A" * 32, "a" * 32 + "\nid"])
def test_identifiers_never_accept_commands_or_paths(value):
    with pytest.raises(live.LiveError):
        live.identifier(value)


@pytest.mark.parametrize("raw", [b"", b"\0\0\0\0", struct.pack("!I", live.MAX_FRAME + 1),
                                  packet(b"[]"), packet(b'{"version":true}'),
                                  packet(b'{"version":1,"version":1}')])
def test_invalid_frames_fail_closed(raw):
    with pytest.raises((live.LiveError, EOFError)):
        live.read_frame(io.BytesIO(raw))


def test_frame_writer_handles_short_socket_writes():
    class Short(io.BytesIO):
        def write(self, data):
            return super().write(data[:3])

    stream = Short()
    live.write_frame(stream, {"version": 1, "ok": True})
    stream.seek(0)
    assert live.read_frame(stream) == {"version": 1, "ok": True}


@pytest.mark.parametrize("changes", [
    {"sequence": True}, {"sequence": 2}, {"data": "invalid base64!"},
    {"data": base64.b64encode(b"no header").decode()}, {"extra": "unexpected"},
])
def test_response_sequence_encoding_and_packet_length_are_validated(changes):
    with pytest.raises(live.LiveError):
        live.response_packet({**envelope(1, packet(b"\x02\0\0\0\x03")), **changes}, 1)


class FakeFuse:
    def __init__(self):
        read, write = os.pipe()
        self.stdout = os.fdopen(read, "rb", buffering=0)
        self.requests = os.fdopen(write, "wb", buffering=0)
        read, write = os.pipe()
        self.stdin = os.fdopen(write, "wb", buffering=0)
        self.responses = os.fdopen(read, "rb", buffering=0)
        self.dead = False

    def terminate(self):
        self.dead = True
        self.requests.close()

    def poll(self):
        return 0 if self.dead else None

    def wait(self, timeout=None):
        return 0

    kill = terminate

    def close(self):
        self.terminate()
        for stream in (self.stdout, self.stdin, self.responses):
            stream.close()


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "check_access", lambda *a: "account")
    monkeypatch.setattr(live, "cleanup", lambda home, project, process, **kw:
                        process.terminate() or True)
    process = FakeFuse()
    current = live.Supervisor(tmp_path, WORKSPACE, INSTANCE, process)
    worker = threading.Thread(target=current.relay, daemon=True)
    worker.start()
    yield current
    current.shutdown()
    worker.join(timeout=2)
    assert not worker.is_alive()
    process.close()


def attach(supervisor, instance=INSTANCE):
    server, client = socket.socketpair()
    client.settimeout(2)
    server_stream = server.makefile("rwb", buffering=0)
    client_stream = client.makefile("rwb", buffering=0)
    supervisor.attach(server, server_stream, instance)
    assert live.read_frame(client_stream) == {"version": 1, "ok": True, "ready": True}
    return client, client_stream


def test_resident_pipe_gets_one_response_after_network_rebind(supervisor):
    request = packet(b"\x01\0\0\0\x03")
    response = packet(b"\x02\0\0\0\x03")
    first, first_stream = attach(supervisor)
    supervisor.process.requests.write(request)
    pending = live.read_frame(first_stream)
    assert pending == envelope(1, request)
    # The local server executed once and cached its reply, but delivery was lost.
    cached = envelope(1, response)
    first.shutdown(socket.SHUT_RDWR)
    first_stream.close()
    first.close()
    second, second_stream = attach(supervisor)
    assert live.read_frame(second_stream) == pending
    live.write_frame(second_stream, cached)
    assert live.read_packet(supervisor.process.responses) == response
    supervisor.process.requests.write(packet(b"\x07\0\0\0\x02"))
    assert live.read_frame(second_stream)["sequence"] == 2
    assert not supervisor.process.dead
    second_stream.close()
    second.close()


def test_wrong_response_does_not_advance_or_replay_a_mutation(supervisor):
    request = packet(b"\x06\0\0\0\x01")
    first, first_stream = attach(supervisor)
    supervisor.process.requests.write(request)
    live.read_frame(first_stream)
    live.write_frame(first_stream, envelope(2, packet(b"\x65\0\0\0\x01")))
    assert first_stream.read(1) == b""
    second, second_stream = attach(supervisor)
    assert live.read_frame(second_stream) == envelope(1, request)
    assert supervisor.sequence == 1
    first_stream.close()
    first.close()
    second_stream.close()
    second.close()


def test_different_local_instance_cannot_take_over_live_mount(supervisor):
    server, client = socket.socketpair()
    with server, client, server.makefile("rwb", buffering=0) as stream:
        with pytest.raises(live.LiveError, match="another local connector") as error:
            supervisor.attach(server, stream, "c" * 32)
        assert error.value.code == "stale"
    assert not supervisor.process.dead


def test_gate_and_account_are_checked_again_when_rebinding(supervisor, monkeypatch):
    def refused(*args):
        raise live.LiveError("account_unavailable", "changed")

    monkeypatch.setattr(live, "check_access", refused)
    server, client = socket.socketpair()
    with server, client, server.makefile("rwb", buffering=0) as stream:
        with pytest.raises(live.LiveError, match="changed"):
            supervisor.attach(server, stream, INSTANCE)


def test_mount_detection_uses_kernel_table_without_accessing_offline_files(monkeypatch):
    raw = "51 20 0:42 / /home/slot/workspace/live/id rw - fuse.sshfs ccfleet rw\n"
    monkeypatch.setattr("builtins.open", lambda *a, **kw: io.StringIO(raw))
    monkeypatch.setattr(Path, "stat", lambda *a, **kw: pytest.fail("must not stat offline mount"))
    assert live.mounted(Path("/home/slot/workspace/live/id"))
    assert not live.mounted(Path("/home/slot/workspace/live/other"))


def test_socket_and_instance_records_refuse_symlinks_and_public_permissions(tmp_path):
    directory = live.state_directory(tmp_path, WORKSPACE)
    target = tmp_path / "outside"
    target.write_text(INSTANCE)
    (directory / "instance").symlink_to(target)
    with pytest.raises(OSError):
        live.marker(tmp_path, WORKSPACE)
    (directory / "instance").unlink()
    (directory / "instance").write_text(INSTANCE)
    (directory / "instance").chmod(0o644)
    with pytest.raises(live.LiveError):
        live.marker(tmp_path, WORKSPACE)
    (directory / "instance").chmod(0o600)
    assert live.marker(tmp_path, WORKSPACE) == INSTANCE
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_stale_mount_or_instance_never_restarts_sshfs_automatically(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "check_access", lambda *a: "account")
    monkeypatch.setattr(live, "connect", lambda *a: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(live, "dependencies", lambda: True)
    monkeypatch.setattr(live, "marker", lambda *a: INSTANCE)
    monkeypatch.setattr(live.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not remount"))
    with pytest.raises(live.LiveError) as error:
        live.ensure_daemon(tmp_path, WORKSPACE, INSTANCE)
    assert error.value.code == "stale"


def test_stop_unmounts_offline_filesystem_before_killing_only_its_sessions(tmp_path, monkeypatch):
    present = {"mounted": True}
    monkeypatch.setattr(live, "mounted", lambda _: present["mounted"])
    monkeypatch.setattr(live.project, "sessions", lambda _: [
        "l_" + WORKSPACE + "_main", "l_" + "c" * 32 + "_other", "ccfleet", "shell"])
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == live.FUSERMOUNT:
            present["mounted"] = False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(live.subprocess, "run", run)
    assert live.cleanup(tmp_path, WORKSPACE)
    assert calls == [[live.FUSERMOUNT, "-u", "-z", str(live.mount_path(tmp_path, WORKSPACE))],
                     [live.project.TMUX, "kill-session", "-t", "=l_" + WORKSPACE + "_main"]]


def test_failed_unmount_never_reports_success(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "mounted", lambda _: True)
    monkeypatch.setattr(live, "live_sessions", lambda *a: [])
    monkeypatch.setattr(live.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    assert not live.cleanup(tmp_path, WORKSPACE)


def test_stop_is_allowed_to_clean_up_account_restart_debt(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "check_access", lambda *a: pytest.fail("cleanup must remain possible"))
    monkeypatch.setattr(live, "connect", lambda *a: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(live, "cleanup", lambda *a: True)
    assert live.control(tmp_path, WORKSPACE, "stop") == {"version": 1, "ok": True, "stopped": True}


def test_stop_all_handles_only_valid_known_workspaces_and_fails_closed(tmp_path, monkeypatch):
    directory = live.state_directory(tmp_path, WORKSPACE)
    other = "c" * 32
    (directory.parent / other).mkdir(mode=0o700)
    (directory.parent / "unrelated").mkdir(mode=0o700)
    calls = []

    def control(home, identifier, operation):
        calls.append((identifier, operation))
        return {"stopped": identifier == WORKSPACE}

    monkeypatch.setattr(live, "control", control)
    assert not live.stop_all(tmp_path)
    assert set(calls) == {(WORKSPACE, "stop"), (other, "stop")}


def test_status_and_stop_of_unconfigured_workspace_do_not_create_state(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "check_access", lambda *a: "account")
    monkeypatch.setattr(live, "dependencies", lambda: False)
    monkeypatch.setattr(live, "mounted", lambda *a: False)
    monkeypatch.setattr(live, "live_sessions", lambda *a: [])
    assert live.control(tmp_path, WORKSPACE, "status") == {
        "version": 1, "ok": True, "ready": False, "mounted": False,
        "connected": False, "instance": "", "sessions": []}
    assert live.stop_all(tmp_path)
    assert not (tmp_path / ".config").exists()


def test_account_errors_use_the_clients_existing_error_categories():
    assert live.error_code(live.project.ProjectAccessError("account", "private")) == \
        "account_unavailable"
    assert live.error_code(live.project.ProjectAccessError("disabled", "private")) == "disabled"
    assert live.error_code(live.project.ProjectAccessError("filesystem", "private")) == "invalid"
    assert live.error_code(OSError("private")) == "unavailable"


def test_supervisor_boot_keeps_neutral_slave_mount_and_stale_marker_on_failure(monkeypatch):
    # A short isolated path keeps real AF_UNIX socket paths portable. No FUSE
    # mount or external process runs; only kernel-mount observations are faked.
    with tempfile.TemporaryDirectory(prefix="ccfl-live-test-", dir="/tmp") as temporary:
        home = Path(temporary).resolve()
        process = FakeFuse()
        calls, protected = [], []
        mounted = {"value": False}
        polls = {"value": 0}

        def poll():
            polls["value"] += 1
            if polls["value"] == 1:
                mounted["value"] = True
                return None
            process.terminate()
            return 0

        def launch(argv, **kwargs):
            calls.append((argv, kwargs))
            return process

        def run(argv, **kwargs):
            mounted["value"] = False
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(process, "poll", poll)
        monkeypatch.setattr(live, "check_access", lambda *a: "account")
        monkeypatch.setattr(live, "dependencies", lambda: True)
        monkeypatch.setattr(live, "mounted", lambda *a: mounted["value"])
        monkeypatch.setattr(live, "live_sessions", lambda *a: [])
        monkeypatch.setattr(live.subprocess, "Popen", launch)
        monkeypatch.setattr(live.subprocess, "run", run)
        monkeypatch.setattr(live.signal, "signal", lambda *a: None)
        monkeypatch.setattr(live.os, "fchmod", lambda fd, mode: protected.append(mode))
        assert live.run_daemon(home, WORKSPACE, INSTANCE) == 0
        assert protected == [0]
        assert calls[0][0][:4] == [live.SSHFS, "ccfleet:/", str(live.mount_path(home, WORKSPACE)), "-f"]
        assert "slave" in calls[0][0][-1] and "reconnect" not in calls[0][0][-1]
        assert "dir_cache=yes,dcache_timeout=1" in calls[0][0][-1]
        assert "attr_timeout=1,entry_timeout=1,negative_timeout=0" in calls[0][0][-1]
        assert "direct_io" in calls[0][0][-1] and "sshfs_sync" in calls[0][0][-1]
        assert "allow_other" not in calls[0][0][-1]
        assert calls[0][1]["env"] == live.project.slot_environment(home)
        assert live.marker(home, WORKSPACE) == INSTANCE
        assert not live.socket_path(home, WORKSPACE).exists()
        assert live.cleanup(home, WORKSPACE)
        assert live.marker(home, WORKSPACE) is None
        process.close()


class Executed(Exception):
    pass


def test_native_live_claude_uses_clean_slot_environment_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_HOST_SENTINEL", "private-local-data")
    monkeypatch.setattr(live, "check_access", lambda *a: "account")
    monkeypatch.setattr(live, "control", lambda *a: {"mounted": True, "connected": True})
    monkeypatch.setattr(live, "mounted", lambda *a: True)
    captured = {}
    monkeypatch.setattr(live.os, "chdir", lambda value: captured.update(cwd=value))
    monkeypatch.setattr(live.os, "umask", lambda *a: None)

    def execute(path, argv, environment):
        captured.update(path=path, argv=argv, env=environment)
        raise Executed

    monkeypatch.setattr(live.os, "execve", execute)
    with pytest.raises(Executed):
        live.run_claude(tmp_path, WORKSPACE, "bypassPermissions", "opus", "max")
    assert captured["cwd"] == live.mount_path(tmp_path, WORKSPACE)
    assert captured["env"] == live.project.slot_environment(tmp_path)
    assert "LOCAL_HOST_SENTINEL" not in captured["env"]
    assert captured["path"] == str(tmp_path / ".local/bin/claude")


def test_offline_filesystem_cannot_launch_claude_against_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "check_access", lambda *a: "account")
    monkeypatch.setattr(live, "control", lambda *a: {"mounted": False, "connected": False})
    monkeypatch.setattr(live.os, "chdir", lambda *a: pytest.fail("no fallback cwd"))
    with pytest.raises(live.LiveError, match="connect the local filesystem"):
        live.run_claude(tmp_path, WORKSPACE, "manual", "opus", "max")


def test_real_socket_stop_ack_is_flushed_before_daemon_returns(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="ccfl-stop-test-", dir="/tmp") as temporary:
        home = Path(temporary).resolve()
        process = FakeFuse()
        active_mount = {"value": False}
        ack_started, allow_ack, returned = (threading.Event() for _ in range(3))
        original_write = live.write_frame
        errors = []

        def launch(*args, **kwargs):
            active_mount["value"] = True
            return process

        def unmount(*args, **kwargs):
            active_mount["value"] = False
            return SimpleNamespace(returncode=0)

        def write(stream, value):
            if value.get("stopped") is True:
                ack_started.set()
                assert allow_ack.wait(3)
            original_write(stream, value)

        def daemon():
            try:
                live.run_daemon(home, WORKSPACE, INSTANCE)
            except BaseException as exc:
                errors.append(exc)
            finally:
                returned.set()

        monkeypatch.setattr(live, "check_access", lambda *a: "account")
        monkeypatch.setattr(live, "dependencies", lambda: True)
        monkeypatch.setattr(live, "mounted", lambda *a: active_mount["value"])
        monkeypatch.setattr(live, "live_sessions", lambda *a: [])
        monkeypatch.setattr(live.subprocess, "Popen", launch)
        monkeypatch.setattr(live.subprocess, "run", unmount)
        monkeypatch.setattr(live.signal, "signal", lambda *a: None)
        monkeypatch.setattr(live.os, "fchmod", lambda *a: None)
        monkeypatch.setattr(live, "write_frame", write)
        worker = threading.Thread(target=daemon)
        worker.start()
        socket_file = home / ".config/ccfleet/live-sockets" / (WORKSPACE + ".sock")
        deadline = time.monotonic() + 3
        while not socket_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        peer = live.connect(home, WORKSPACE)
        with peer, peer.makefile("rwb", buffering=0) as stream:
            original_write(stream, {"version": 1, "operation": "stop"})
            assert ack_started.wait(3), errors
            assert not returned.wait(0.7), "daemon exited before its control handler acknowledged"
            allow_ack.set()
            assert live.read_frame(stream) == {"version": 1, "ok": True, "stopped": True}
        worker.join(timeout=3)
        assert not worker.is_alive() and not errors
        assert returned.is_set() and not socket_file.exists()
        assert live.marker(home, WORKSPACE) is None
        process.close()


@pytest.mark.parametrize("remaining", ["none", "mount", "session", "marker"])
def test_lost_stop_ack_only_succeeds_after_verified_cleanup(tmp_path, monkeypatch, remaining):
    server, client = socket.socketpair()
    client.settimeout(2)

    def peer():
        with server, server.makefile("rwb", buffering=0) as stream:
            assert live.read_frame(stream)["operation"] == "stop"
            # Simulate the previous daemon's pre-ACK exit.

    worker = threading.Thread(target=peer)
    worker.start()
    monkeypatch.setattr(live, "connect", lambda *a: client)
    monkeypatch.setattr(live, "mounted", lambda *a: remaining == "mount")
    monkeypatch.setattr(live, "live_sessions", lambda *a: ["main"] if remaining == "session" else [])
    monkeypatch.setattr(live, "marker", lambda *a: INSTANCE if remaining == "marker" else None)
    if remaining == "none":
        assert live.control(tmp_path, WORKSPACE, "stop")["stopped"] is True
    else:
        with pytest.raises(live.LiveError, match="not confirmed"):
            live.control(tmp_path, WORKSPACE, "stop")
    worker.join(timeout=2)
    assert not worker.is_alive()


@pytest.mark.parametrize("command,expected", [
    (f"ccfleet-live-v1 {WORKSPACE} {INSTANCE}", ["link", WORKSPACE, INSTANCE]),
    (f"ccfleet-live-status-v1 {WORKSPACE}", ["status", WORKSPACE]),
    (f"ccfleet-live-stop-v1 {WORKSPACE}", ["stop", WORKSPACE]),
    (f"ccfleet-live-v1 {WORKSPACE} {INSTANCE}\nid", None),
    (f"ccfleet-live-v1 {WORKSPACE} {INSTANCE} extra", None),
    (f"ccfleet-live-v1 ../escape {INSTANCE}", None),
    (f"ccfleet-live-status-v1 {WORKSPACE} extra", None),
])
def test_fixed_entry_dispatches_only_valid_live_transport(tmp_path, command, expected):
    entry = tmp_path / "slot-entry.sh"
    entry.write_text((ROOT / "node/slot-entry.sh").read_text())
    package = tmp_path / "ccfleet_agent"
    package.mkdir()
    (package / "live_access.py").write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    result = subprocess.run(["bash", str(entry)], capture_output=True, text=True,
                            env={**os.environ, "SSH_ORIGINAL_COMMAND": command})
    if expected is None:
        assert result.returncode != 0 and result.stdout == ""
    else:
        assert result.returncode == 0 and json.loads(result.stdout) == expected
