"""The local resident bridge retries responses, never filesystem mutations."""
import base64
import fcntl
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ccfleet_agent import live_client as live
from ccfleet_agent import live_files


def request(sequence, data=b"test"):
    return {"version": 1, "sequence": sequence, "data": base64.b64encode(data).decode()}


class Server:
    def __init__(self):
        self.calls = []

    def handle_packet(self, packet):
        self.calls.append(packet)
        return b"answer:" + packet


def test_lost_response_does_not_reexecute_a_write():
    server = Server()
    requests = live.Requests(server)
    first = requests.answer(request(1, b"write"))
    assert requests.answer(request(1, b"write")) == first
    assert server.calls == [b"write"]
    requests.answer(request(2, b"rename"))
    assert requests.answer(request(1, b"write")) == first
    assert server.calls == [b"write", b"rename"]


@pytest.mark.parametrize("bad", [request(0), request(2), request(True), request(2**63),
    {"version": True, "sequence": 1, "data": ""}, {"version": 2, "sequence": 1, "data": ""},
    {"version": 1, "sequence": 1, "data": "!"}, {**request(1), "extra": "identity"}])
def test_invalid_requests_never_reach_filesystem(bad):
    server = Server()
    with pytest.raises(ValueError):
        live.Requests(server).answer(bad)
    assert server.calls == []


def test_retry_with_changed_payload_is_refused():
    server = Server()
    requests = live.Requests(server)
    requests.answer(request(1))
    with pytest.raises(live.LinkError, match="changed"):
        requests.answer(request(1, b"different"))
    assert server.calls == [b"test"]


def test_cache_is_bounded_and_evicted_requests_never_replay():
    server = Server()
    requests = live.Requests(server)
    for sequence in range(1, 70):
        requests.answer(request(sequence))
    assert len(requests.replies) == 32
    with pytest.raises(live.LinkError):
        requests.answer(request(1))
    assert len(server.calls) == 69


@pytest.mark.parametrize("data", [b"", struct.pack("!I", 0),
    struct.pack("!I", live.MAX_FRAME + 1), struct.pack("!I", 2) + b"[]",
    struct.pack("!I", 13) + b'{"x":1,"x":2}'])
def test_malformed_framing_fails_closed(data):
    with pytest.raises((EOFError, ValueError)):
        live.read_frame(io.BytesIO(data))


def test_framing_roundtrip_has_no_implicit_identity():
    output = io.BytesIO()
    live.write_frame(output, request(1))
    assert live.read_frame(io.BytesIO(output.getvalue())) == request(1)


def test_framing_handles_short_writes():
    class Short(io.BytesIO):
        def write(self, value):
            return super().write(value[:3])
    stream = Short()
    live.write_frame(stream, request(1))
    assert live.read_frame(io.BytesIO(stream.getvalue())) == request(1)


def test_framing_refuses_zero_write():
    class Stopped(io.BytesIO):
        def write(self, value):
            return 0
    with pytest.raises(OSError):
        live.write_frame(Stopped(), request(1))


def test_state_refuses_symlink_and_public_file(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("must not read")
    state = tmp_path / "state.json"
    state.symlink_to(secret)
    with pytest.raises(OSError):
        live.read_state(tmp_path)
    state.unlink()
    state.write_text(json.dumps({"socket": "unused"}))
    state.chmod(0o644)
    with pytest.raises(live.LinkError):
        live.read_state(tmp_path)


def test_private_directory_refuses_foreign_modes_and_links(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(live.LinkError):
        live.private_directory(link)
    target.chmod(0o777)
    with pytest.raises(live.LinkError):
        live.private_directory(target)


def test_control_missing_or_dead_socket_does_not_signal_a_pid(tmp_path):
    assert live.control(tmp_path, "stop") == {}
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"pid": os.getpid(), "socket": str(tmp_path / "gone")}))
    state.chmod(0o600)
    assert live.control(tmp_path, "stop") == {}
    with pytest.raises(live.LinkError):
        live.control(tmp_path, "shell")


def test_private_state_is_bounded(tmp_path):
    state = Path(tmp_path) / "state.json"
    state.write_bytes(b" " * 16385)
    state.chmod(0o600)
    with pytest.raises(live.LinkError):
        live.read_state(tmp_path)


def test_unresponsive_control_is_not_mistaken_for_stopped_file_access(tmp_path):
    import socket
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ccf-t-") as private, \
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        address = Path(private) / "control"
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(1)
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"socket": str(address), "instance": "test"}))
        state.chmod(0o600)
        with pytest.raises(live.ControlError) as error:
            live.control(tmp_path, "stop")
        assert isinstance(error.value.__cause__, socket.timeout)


@pytest.mark.parametrize("reply", [b"", b"\x00\x00", struct.pack("!I", 8) + b"{"])
def test_control_closed_or_truncated_reply_is_ambiguous_not_inactive(tmp_path, reply):
    import socket
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory(prefix="ccf-t-") as private, \
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        address = Path(private) / "control"
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(1)
        listener.settimeout(3)
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"socket": str(address), "instance": "test"}))
        state.chmod(0o600)

        def peer():
            try:
                connection, _ = listener.accept()
                with connection, connection.makefile("rwb", buffering=0) as stream:
                    assert live.read_frame(stream) == {"operation": "status", "instance": "test"}
                    connection.sendall(reply)
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        try:
            with pytest.raises(live.ControlError) as error:
                live.control(tmp_path, "status")
            assert isinstance(error.value.__cause__, EOFError)
        finally:
            thread.join(timeout=4)
        assert not thread.is_alive() and not failures


def test_control_reset_by_real_unix_peer_is_not_inactive(tmp_path):
    import socket
    import tempfile
    with tempfile.TemporaryDirectory(prefix="ccf-t-") as private, \
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        address = Path(private) / "control"
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen(1)
        listener.settimeout(3)
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"socket": str(address), "instance": "test"}))
        state.chmod(0o600)
        failures = []

        def peer():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                          struct.pack("ii", 1, 0))
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        try:
            with pytest.raises(live.ControlError) as error:
                live.control(tmp_path, "stop")
            # Unix implementations report an immediate peer close at either
            # write or read; none of these outcomes proves filesystem teardown.
            assert isinstance(error.value.__cause__,
                              (EOFError, ConnectionResetError, BrokenPipeError))
        finally:
            thread.join(timeout=4)
        assert not thread.is_alive() and not failures


def test_stopped_requires_released_real_lock_even_without_control_socket(tmp_path):
    lock = tmp_path / "connector.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert live.control(tmp_path, "stop") == {}
        assert live.stopped(tmp_path) is False
    finally:
        os.close(descriptor)
    assert live.stopped(tmp_path) is True


def test_stopped_missing_lock_or_directory_is_absent_without_creating_files(tmp_path):
    assert live.stopped(tmp_path) is True
    assert live.stopped(tmp_path / "missing") is True
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["permissions", "symlink", "directory", "fifo", "hardlink"])
def test_stopped_rejects_unsafe_or_nonregular_lock(tmp_path, kind):
    lock = tmp_path / "connector.lock"
    if kind == "directory":
        lock.mkdir(mode=0o700)
    elif kind == "fifo":
        os.mkfifo(lock, 0o600)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.touch(mode=0o600)
        lock.symlink_to(target)
    else:
        lock.touch(mode=0o600)
        if kind == "permissions":
            lock.chmod(0o644)
        else:
            os.link(lock, tmp_path / "duplicate")
    with pytest.raises((live.LinkError, OSError)):
        live.stopped(tmp_path)


CHILD_PREAMBLE = r'''
import base64, json, os, struct, sys
from pathlib import Path
attempt = int(sys.argv[1])
workspace = Path(sys.argv[2])
def frame(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(struct.pack("!I", len(raw)) + raw)
    sys.stdout.buffer.flush()
def exact(size):
    data = b""
    while len(data) < size:
        part = sys.stdin.buffer.read(size - len(data))
        if not part:
            raise EOFError("parent stream ended")
        data += part
    return data
def response():
    return json.loads(exact(struct.unpack("!I", exact(4))[0]))
def u32(n):
    return struct.pack("!I", n)
def u64(n):
    return struct.pack("!Q", n)
def string(value):
    return u32(len(value)) + value
def packet(kind, value):
    return u32(1 + len(value)) + bytes([kind]) + value
def query(sequence, data):
    frame({"version": 1, "sequence": sequence, "data": base64.b64encode(data).decode()})
    return base64.b64decode(response()["data"])
def hello():
    frame({"version": 1, "ok": True, "ready": True})
def opened():
    query(1, packet(1, u32(3)))
    answer = query(2, packet(3, u32(1) + string(b"live.txt") + u32(15) + u32(0)))
    assert answer[4] == 102
    length = struct.unpack("!I", answer[9:13])[0]
    return answer[13:13 + length]
'''


class ResidentHarness:
    """Real run/control sockets and child pipes; only the SSH peer is synthetic."""

    def __init__(self, directory, root, script, workspace):
        self.directory, self.root = directory, root
        self.script, self.workspace = script, workspace
        self.children = []
        self.results, self.failures = [], []
        self.paired = threading.Event()
        self.paired.set()
        outer = self

        class RecordingFilesystem(live_files.SFTPServer):
            def __init__(self):
                super().__init__(root, [])
                self.packets = []
                self.closed = False

            def handle_packet(self, data):
                self.packets.append(data)
                return super().handle_packet(data)

            def close(self):
                self.closed = True
                super().close()

        self.server = RecordingFilesystem()

        def connect():
            child = subprocess.Popen(
                [sys.executable, "-I", str(script), str(len(outer.children) + 1), str(workspace)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            outer.children.append(child)
            return child

        def run():
            try:
                result = live.run(directory, "synthetic-instance", outer.server, connect,
                                  check_pairing=outer.paired.is_set)
                outer.results.append(result)
            except BaseException as exc:
                outer.failures.append(exc)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, predicate, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert not self.failures, self.failures
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        pytest.fail("resident connector test deadline expired")

    def connected(self):
        return self.wait(lambda: live.control(self.directory, "status").get("connected"))

    def joined(self):
        self.thread.join(timeout=6)
        assert not self.thread.is_alive(), "connector did not terminate within the test deadline"
        assert not self.failures, self.failures
        assert self.server.closed
        assert all(child.poll() is not None for child in self.children)

    def cleanup(self):
        try:
            live.control(self.directory, "stop")
        except (OSError, ValueError, EOFError):
            pass
        self.paired.clear()
        for child in self.children:
            if child.poll() is None:
                child.kill()
        self.thread.join(timeout=6)
        for child in self.children:
            child.wait(timeout=3)


@pytest.fixture
def resident(tmp_path):
    running = []

    def start(body):
        workspace = tmp_path.resolve() / f"attempt-{len(running)}"
        workspace.mkdir()
        root = workspace / "selected"
        root.mkdir()
        script = workspace / "synthetic-ssh-peer.py"
        script.write_text(CHILD_PREAMBLE + "\n" + body)
        harness = ResidentHarness(workspace / "connector", root, script, workspace)
        running.append(harness)
        return harness

    yield start
    for harness in running:
        harness.cleanup()


def test_resident_initial_connection_and_private_control_stop_close_real_handles(resident):
    daemon = resident('''hello()
handle = opened()
(workspace / "opened").write_text("ready")
sys.stdin.buffer.read()
''')
    daemon.connected()
    daemon.wait(lambda: (daemon.workspace / "opened").exists())
    assert len(daemon.server.handles) == 1
    state = live.control(daemon.directory, "status")
    socket_path = Path(state["socket"])
    assert socket_path.stat().st_mode & 0o777 == 0o600
    assert socket_path.parent.stat().st_mode & 0o777 == 0o700
    reply = live.control(daemon.directory, "stop")
    assert reply["instance"] == "synthetic-instance"
    daemon.joined()
    assert daemon.results == [0]
    assert daemon.server.handles == {}
    assert not socket_path.exists()
    assert live.control(daemon.directory, "status") == {}
    assert live.read_state(daemon.directory)["connected"] is False


def test_resident_lock_remains_held_until_filesystem_teardown_finishes(resident, monkeypatch):
    daemon = resident('''hello()
handle = opened()
(workspace / "opened").write_text("ready")
sys.stdin.buffer.read()
''')
    daemon.connected()
    daemon.wait(lambda: (daemon.workspace / "opened").exists())
    closing, release = threading.Event(), threading.Event()
    original_close = daemon.server.close

    def delayed_close():
        closing.set()
        assert release.wait(timeout=6), "test did not release filesystem teardown"
        original_close()

    monkeypatch.setattr(daemon.server, "close", delayed_close)
    try:
        assert live.control(daemon.directory, "stop")["instance"] == "synthetic-instance"
        assert closing.wait(timeout=6)
        assert all(child.poll() is not None for child in daemon.children)
        assert daemon.server.handles and not daemon.server.closed
        assert live.stopped(daemon.directory) is False
        # The control listener closes before filesystem handles. Its absence
        # must not be used as evidence that local file access has ended.
        assert live.control(daemon.directory, "status") == {}
    finally:
        release.set()
    daemon.joined()
    assert daemon.server.handles == {}
    assert live.stopped(daemon.directory) is True


def test_resident_reconnect_retains_server_handles_and_deduplicates_uncertain_append(resident):
    daemon = resident('''hello()
pending = workspace / "pending.json"
if attempt == 1:
    handle = opened()
    data = packet(6, u32(2) + string(handle) + u64(0) + string(b"X"))
    request = {"version": 1, "sequence": 3, "data": base64.b64encode(data).decode()}
    pending.write_text(json.dumps({"request": request, "handle": base64.b64encode(handle).decode()}))
    frame(request)
    os._exit(0)  # Lose the response after delivering the mutating request.
saved = json.loads(pending.read_text())
frame(saved["request"])
assert response()["sequence"] == 3
handle = base64.b64decode(saved["handle"])
answer = query(4, packet(5, u32(3) + string(handle) + u64(0) + u32(10)))
assert answer[4] == 103
length = struct.unpack("!I", answer[9:13])[0]
(workspace / "read-result").write_bytes(answer[13:13 + length])
sys.stdin.buffer.read()
''')
    daemon.wait(lambda: (daemon.workspace / "read-result").exists())
    daemon.connected()
    assert (daemon.root / "live.txt").read_bytes() == b"X"
    assert (daemon.workspace / "read-result").read_bytes() == b"X"
    assert len(daemon.children) == 2
    assert len(daemon.server.handles) == 1
    assert [data[4] for data in daemon.server.packets] == [1, 3, 6, 5]
    live.control(daemon.directory, "stop")
    daemon.joined()
    assert daemon.results == [0]


def test_pairing_removal_stops_an_idle_child_without_a_new_remote_request(resident):
    daemon = resident('''hello()
handle = opened()
(workspace / "idle").write_text("ready")
sys.stdin.buffer.read()
''')
    daemon.connected()
    daemon.wait(lambda: (daemon.workspace / "idle").exists())
    daemon.paired.clear()
    daemon.joined()
    assert daemon.results == [0]
    assert daemon.server.handles == {}
    assert live.read_state(daemon.directory)["connected"] is False


@pytest.mark.parametrize("greeting, expected", [
    ({"version": 1, "ok": False, "error": {"private": "must-not-be-recorded"}}, "unavailable"),
    ({"version": 1, "ok": False, "error": "disabled"}, "disabled"),
    ({"version": True, "ok": True, "ready": True}, "unavailable"),
    ({"version": 1, "ok": True, "ready": True, "extra": "bad"}, "protocol"),
    ([], "protocol"),
])
def test_invalid_greeting_or_error_object_produces_clean_fatal_state(resident, greeting, expected):
    daemon = resident(f"frame({greeting!r})\nsys.stdin.buffer.read()\n")
    daemon.joined()
    state = live.read_state(daemon.directory)
    assert daemon.results == [2]
    assert state["connected"] is False and state["error"] == expected
    assert "must-not-be-recorded" not in json.dumps(state)
    assert daemon.server.packets == []


def test_a_new_resident_process_refuses_lost_sequence_before_any_mutation(resident):
    daemon = resident('''hello()
data = packet(6, u32(2) + string(b"lost-handle") + u64(0) + string(b"never replay"))
frame({"version": 1, "sequence": 3, "data": base64.b64encode(data).decode()})
sys.stdin.buffer.read()
''')
    daemon.joined()
    assert daemon.results == [2]
    assert live.read_state(daemon.directory)["error"] == "protocol"
    assert daemon.server.packets == []
    assert not list(daemon.root.iterdir())


@pytest.mark.parametrize("raw", [b"{invalid-json", b'{"version": "\xff"}'])
def test_malformed_encoded_greeting_is_fatal_without_reconnect(resident, raw):
    daemon = resident(f'''raw = {raw!r}
sys.stdout.buffer.write(u32(len(raw)) + raw)
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
''')
    daemon.joined()
    assert daemon.results == [2]
    assert live.read_state(daemon.directory)["error"] == "protocol"
    assert len(daemon.children) == 1
    assert daemon.server.packets == []
