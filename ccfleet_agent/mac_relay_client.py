"""Private UDS framing between an SSH shim and the owner's GUI relay service.

Only model protocol bytes or a closed read-only status object cross this socket.
Provider credentials remain inside the GUI worker and never enter this protocol.
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import select
import socket
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from . import mac_policy
except ImportError:
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_mac_policy", Path(__file__).with_name("mac_policy.py"))
    assert _spec is not None and _spec.loader is not None
    mac_policy = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mac_policy)

SOCKET_NAME = "relay.sock"
CHUNK = 64 * 1024
BUFFER = 256 * 1024
HEADER = struct.Struct("!cI")
ROLES = frozenset({b"D", b"G", b"S"})


class RelayClientError(ValueError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def peer_uid(sock: socket.socket) -> Optional[int]:
    if hasattr(sock, "getpeereid"):
        return int(sock.getpeereid()[0])
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        function = libc.getpeereid
        function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                             ctypes.POINTER(ctypes.c_uint)]
        function.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if function(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise RelayClientError("private relay peer identity is unavailable")
        return int(uid.value)
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", raw)[1])
    return None


def require_peer(sock: socket.socket, uid: int) -> None:
    observed = peer_uid(sock)
    if observed != uid:
        raise RelayClientError("private relay peer identity did not match")


def socket_path(home: Path) -> Path:
    return mac_policy.directory(home) / SOCKET_NAME


def connect(home: Path, role: bytes, timeout: float = 12) -> socket.socket:
    if role not in ROLES:
        raise RelayClientError("unsupported private relay role")
    owner = mac_policy.authenticated_owner(home)
    directory = mac_policy.open_directory(mac_policy.directory(home), private=True)
    sock = None
    try:
        info = os.stat(SOCKET_NAME, dir_fd=directory, follow_symlinks=False)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != owner.pw_uid
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RelayClientError("private relay socket is unsafe")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(socket_path(home)))
        require_peer(sock, owner.pw_uid)
        sock.sendall(role)
        answer = sock.recv(1)
        if answer != b"O":
            status = 503 if answer == b"B" else 403 if answer == b"D" else 502
            raise RelayClientError("private relay service is unavailable", status)
        return sock
    except (OSError, ValueError):
        if sock is not None:
            sock.close()
        raise
    finally:
        os.close(directory)


def frame(kind: bytes, data: bytes) -> bytes:
    if kind not in (b"D", b"X") or len(data) > CHUNK or kind == b"X" and len(data) != 4:
        raise RelayClientError("invalid private relay frame")
    return HEADER.pack(kind, len(data)) + data


def take_frame(buffer: bytearray):
    if len(buffer) < HEADER.size:
        return None
    kind, size = HEADER.unpack(buffer[:HEADER.size])
    if kind not in (b"D", b"X") or size > CHUNK or kind == b"X" and size != 4:
        raise RelayClientError("invalid private relay frame")
    if len(buffer) < HEADER.size + size:
        return None
    payload = bytes(buffer[HEADER.size:HEADER.size + size])
    del buffer[:HEADER.size + size]
    return kind, payload


def status(home: Optional[Path] = None) -> dict:
    home = Path(mac_policy.authenticated_owner(home).pw_dir)
    sock = connect(home, b"S")
    buffer, data = bytearray(), bytearray()
    try:
        deadline = time.monotonic() + 15
        terminal = False
        while not terminal:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RelayClientError("private relay status exceeded its deadline")
            sock.settimeout(remaining)
            incoming = sock.recv(CHUNK)
            if not incoming:
                raise RelayClientError("private relay status ended without completion")
            buffer.extend(incoming)
            if len(buffer) > BUFFER:
                raise RelayClientError("private relay status exceeds its bound")
            while True:
                parsed = take_frame(buffer)
                if parsed is None:
                    break
                kind, payload = parsed
                if kind == b"X":
                    if struct.unpack("!i", payload)[0] != 0 or buffer:
                        raise RelayClientError("private relay status failed")
                    terminal = True
                    break
                data.extend(payload)
                if len(data) > 4096:
                    raise RelayClientError("private relay status exceeds its bound")
        result = json.loads(data)
        if (not isinstance(result, dict) or set(result) != {
                "available", "expires_in_seconds", "binding_matches", "policy_enabled"}
                or any(type(result[key]) is not bool for key in (
                    "available", "binding_matches", "policy_enabled"))
                or result["expires_in_seconds"] is not None
                and type(result["expires_in_seconds"]) is not int):
            raise RelayClientError("private relay status is invalid")
        return result
    except (OSError, ValueError):
        raise RelayClientError("private relay status is unavailable") from None
    finally:
        sock.close()


def _error(role: bytes, status_code: int, output_fd: int) -> None:
    metadata = {"version": 2, "status": status_code,
                "headers": {"content-type": "application/json"}}
    if status_code == 503:
        metadata["headers"]["retry-after"] = "3"
    if role == b"G":
        metadata["retryable"] = True
    body = {"type": "error", "error": {"type": "ccfleet_relay_error",
            "message": "The private Mac relay service is unavailable; no request was replayed."}}
    for value in (json.dumps(metadata).encode(), json.dumps(body).encode(), b""):
        data = memoryview(struct.pack("!I", len(value)) + value)
        while data:
            written = os.write(output_fd, data)
            if written <= 0:
                raise OSError("private relay output closed")
            data = data[written:]


def pump(sock: socket.socket, *, input_fd: int = 0, output_fd: int = 1,
         timeout: float = 920) -> int:
    original = {fd: os.get_blocking(fd) for fd in (input_fd, output_fd)}
    received, outgoing, output = bytearray(), bytearray(), bytearray()
    terminal = None
    peer_closed = False
    deadline = time.monotonic() + timeout
    try:
        for fd in original:
            os.set_blocking(fd, False)
        sock.setblocking(False)
        output_watch = select.poll()
        output_watch.register(output_fd, select.POLLOUT)
        while time.monotonic() < deadline:
            while len(output) <= BUFFER - CHUNK:
                parsed = take_frame(received)
                if parsed is None:
                    if peer_closed and received:
                        raise RelayClientError("private relay ended during a frame")
                    break
                kind, payload = parsed
                if terminal is not None:
                    raise RelayClientError("private relay sent trailing data")
                if kind == b"X":
                    terminal = struct.unpack("!i", payload)[0]
                    outgoing.clear()
                else:
                    output.extend(payload)
            if terminal is not None and not output:
                if received:
                    raise RelayClientError("private relay sent trailing data")
                return terminal if terminal in (0, 2) else 255
            if peer_closed and terminal is None and not received and not output:
                raise RelayClientError("private relay ended without completion")
            if not output and dict(output_watch.poll(0)).get(output_fd, 0) & (
                    select.POLLERR | select.POLLHUP | select.POLLNVAL):
                return 255
            poller = select.poll()
            poller.register(input_fd, select.POLLIN if terminal is None and not peer_closed
                            and len(outgoing) < BUFFER else 0)
            poller.register(output_fd, select.POLLOUT if output else 0)
            events = (select.POLLIN if len(received) < BUFFER and terminal is None else 0)
            events |= select.POLLOUT if outgoing else 0
            if not peer_closed and events:
                poller.register(sock.fileno(), events)
            ready = dict(poller.poll(100))
            if ready.get(input_fd, 0) & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                return 255
            if ready.get(output_fd, 0) & (select.POLLHUP | select.POLLERR | select.POLLNVAL):
                return 255
            if output and ready.get(output_fd, 0) & select.POLLOUT:
                try:
                    count = os.write(output_fd, output[:CHUNK])
                    del output[:count]
                except BlockingIOError:
                    pass
            if (terminal is None and not peer_closed and len(outgoing) < BUFFER
                    and ready.get(input_fd, 0) & select.POLLIN):
                try:
                    data = os.read(input_fd, min(CHUNK, BUFFER - len(outgoing)))
                except BlockingIOError:
                    data = None
                if data == b"":
                    return 255
                if data:
                    outgoing.extend(data)
            event = ready.get(sock.fileno(), 0)
            if outgoing and event & select.POLLOUT:
                try:
                    count = sock.send(outgoing[:CHUNK])
                    del outgoing[:count]
                except BlockingIOError:
                    pass
            if (terminal is None and not peer_closed and len(received) < BUFFER
                    and event & (select.POLLIN | select.POLLHUP)):
                try:
                    data = sock.recv(min(CHUNK, BUFFER - len(received)))
                except BlockingIOError:
                    data = None
                if data == b"":
                    peer_closed = True
                    outgoing.clear()
                if data:
                    received.extend(data)
            if event & (select.POLLERR | select.POLLNVAL):
                raise RelayClientError("private relay connection failed")
        return 255
    except (OSError, ValueError):
        return 255
    finally:
        for fd, blocking in original.items():
            try:
                os.set_blocking(fd, blocking)
            except OSError:
                pass
        sock.close()


def forward(role: bytes, *, home: Optional[Path] = None, input_fd: int = 0,
            output_fd: int = 1) -> int:
    if role not in (b"D", b"G"):
        return 2
    try:
        selected = Path(mac_policy.authenticated_owner(home).pw_dir)
        sock = connect(selected, role)
    except (OSError, ValueError) as exc:
        try:
            _error(role, exc.status if isinstance(exc, RelayClientError) else 502, output_fd)
        except OSError:
            pass
        return 2
    return pump(sock, input_fd=input_fd, output_fd=output_fd)
