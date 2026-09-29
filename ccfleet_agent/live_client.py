"""Resident local filesystem connector. No model client, local shell, or TCP listener.

The SFTP object and its handles survive replacement SSH streams. A sequenced
request is executed once; a lost response is resent from memory, never replayed
as a second filesystem mutation. Process loss needs an explicit link reset.
"""
from __future__ import annotations

import base64
import collections
import fcntl
import json
import os
import socket
import stat
import struct
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO, Callable

MAX_FRAME = 4 * 1024 * 1024


class LinkError(ValueError):
    pass


class ControlError(LinkError):
    """A missing control reply is ambiguous, not proof the connector stopped."""


def read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError("filesystem connection ended")
        data.extend(part)
    return bytes(data)


def read_frame(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack("!I", read_exact(stream, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise LinkError("invalid filesystem frame size")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LinkError("duplicate filesystem field")
            result[key] = value
        return result
    try:
        result = json.loads(read_exact(stream, size), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise LinkError("filesystem frame contains invalid JSON") from exc
    if not isinstance(result, dict):
        raise LinkError("invalid filesystem frame")
    return result


def write_frame(stream: BinaryIO, value: dict[str, Any]) -> None:
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if not 0 < len(raw) <= MAX_FRAME:
        raise LinkError("invalid filesystem frame size")
    pending = memoryview(struct.pack("!I", len(raw)) + raw)
    while pending:
        count = stream.write(pending)
        if not isinstance(count, int) or count <= 0:
            raise OSError("filesystem stream write stopped")
        pending = pending[count:]
    stream.flush()


class Requests:
    def __init__(self, server: Any):
        self.server = server
        self.sequence = 0
        self.replies: collections.OrderedDict[
            int, tuple[bytes, dict[str, Any]]] = collections.OrderedDict()

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        if (set(request) != {"version", "sequence", "data"}
                or type(request.get("version")) is not int or request["version"] != 1
                or type(request.get("sequence")) is not int
                or not 1 <= request["sequence"] < 2**63
                or not isinstance(request.get("data"), str)):
            raise LinkError("invalid filesystem request")
        sequence = request["sequence"]
        packet = base64.b64decode(request["data"], validate=True)
        if base64.b64encode(packet).decode() != request["data"]:
            raise LinkError("noncanonical filesystem packet")
        if sequence in self.replies:
            previous, reply = self.replies[sequence]
            if packet != previous:
                raise LinkError("filesystem request changed during retry")
            return reply
        if sequence != self.sequence + 1:
            raise LinkError("filesystem request sequence was lost; reset the link")
        result = self.server.handle_packet(packet)
        reply = {"version": 1, "sequence": sequence,
                 "data": base64.b64encode(result).decode("ascii")}
        self.replies[sequence] = (packet, reply)
        self.sequence = sequence
        # The slot has one outstanding request. A small replay window also
        # handles a delayed reply without keeping an unbounded file-content log.
        while len(self.replies) > 32:
            self.replies.popitem(last=False)
        return reply


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LinkError("unsafe local connector directory")


def read_state(directory: Path) -> dict[str, Any]:
    path = directory / "state.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 16384):
            raise LinkError("unsafe local connector state")
        data = json.loads(stream.read(16385))
        if not isinstance(data, dict):
            raise LinkError("invalid local connector state")
        return data


def control(directory: Path, operation: str) -> dict[str, Any]:
    if operation not in {"status", "stop"}:
        raise LinkError("invalid connector control")
    state = read_state(directory)
    address = state.get("socket")
    if not isinstance(address, str):
        return {}
    path = Path(address)
    try:
        parent, info = path.parent.lstat(), path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or parent.st_mode & 0o077 or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise LinkError("unsafe local connector socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(address)
            with connection.makefile("rwb", buffering=0) as stream:
                write_frame(stream, {"operation": operation, "instance": state.get("instance")})
                return read_frame(stream)
    except (FileNotFoundError, ConnectionRefusedError):
        return {}
    except (EOFError, ConnectionResetError, BrokenPipeError, socket.timeout) as exc:
        raise ControlError("could not confirm the old local connector's state; "
                           "retry setup after its shutdown finishes") from exc


def stopped(directory: Path) -> bool:
    """The resident keeps this lock until its child and filesystem are closed."""
    try:
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise LinkError("unsafe local connector directory")
        descriptor = os.open(directory / "connector.lock",
                             os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise LinkError("unsafe local connector lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(descriptor)


def run(directory: Path, instance: str, server: Any,
        connect: Callable[[], subprocess.Popen], *, check_pairing: Callable[[], bool]) -> int:
    private_directory(directory)
    lock = os.open(directory / "connector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        server.close()
        return 0
    # Short private path works even when CCFLEET_HOME exceeds Unix socket limits.
    socket_dir = Path(tempfile.mkdtemp(prefix="ccfleet-live-")).resolve()
    server.protect(socket_dir)
    address = socket_dir / "control"
    stop = threading.Event()
    state_lock = threading.Lock()
    state: dict[str, Any] = {"instance": instance, "socket": str(address),
                             "connected": False, "error": "connecting"}
    process: list[subprocess.Popen] = []

    def save(**fields: Any) -> None:
        with state_lock:
            state.update(fields)
            descriptor, filename = tempfile.mkstemp(prefix=".state-", dir=directory)
            try:
                with os.fdopen(descriptor, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(state, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(filename, directory / "state.json")
            finally:
                if os.path.exists(filename):
                    os.unlink(filename)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(address))
    os.chmod(address, 0o600)
    listener.listen(4)
    listener.settimeout(1)
    save()

    def controls() -> None:
        while not stop.is_set():
            if not check_pairing():
                stop.set()
                for child in process[:]:
                    if child.poll() is None:
                        child.terminate()
                return
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    with connection.makefile("rwb", buffering=0) as stream:
                        request = read_frame(stream)
                        if (set(request) != {"operation", "instance"}
                                or request.get("instance") != instance
                                or request.get("operation") not in {"status", "stop"}):
                            continue
                        with state_lock:
                            reply = dict(state)
                        write_frame(stream, reply)
                        if request["operation"] == "stop":
                            stop.set()
                            for child in process[:]:
                                if child.poll() is None:
                                    child.terminate()
                except (OSError, ValueError, EOFError):
                    continue

    controller = threading.Thread(target=controls, daemon=True)
    controller.start()
    requests = Requests(server)
    delay = 1
    fatal = False
    try:
        while not stop.is_set():
            if not check_pairing():
                save(connected=False, error="pairing_removed")
                break
            child = None
            try:
                child = connect()
                process[:] = [child]
                assert child.stdout is not None and child.stdin is not None
                hello = read_frame(child.stdout)
                if (type(hello.get("version")) is not int or hello["version"] != 1
                        or hello.get("ok") is not True
                        or hello.get("ready") is not True):
                    reason = hello.get("error")
                    known = {"stale", "disabled", "account_unavailable", "busy"}
                    reason = (reason if isinstance(reason, str) and reason in known
                              else "unavailable")
                    save(connected=False, error=reason)
                    fatal = True
                    break
                if set(hello) != {"version", "ok", "ready"}:
                    raise LinkError("invalid filesystem greeting")
                save(connected=True, error="")
                delay = 1
                while not stop.is_set():
                    try:
                        request = read_frame(child.stdout)
                        reply = requests.answer(request)
                    except ValueError as exc:
                        raise LinkError("invalid filesystem protocol") from exc
                    write_frame(child.stdin, reply)
            except LinkError:
                save(connected=False, error="protocol")
                fatal = True
                break
            except (OSError, EOFError, ValueError, subprocess.SubprocessError):
                save(connected=False, error="reconnecting")
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
                    for stream in (child.stdin, child.stdout):
                        if stream is not None:
                            stream.close()
                process.clear()
            if stop.wait(delay):
                break
            delay = min(10, delay + 1)
    finally:
        stop.set()
        listener.close()
        controller.join(timeout=3)
        server.close()
        if not fatal:
            save(connected=False, error="stopped")
        address.unlink(missing_ok=True)
        socket_dir.rmdir()
        os.close(lock)
    return 2 if fatal else 0
