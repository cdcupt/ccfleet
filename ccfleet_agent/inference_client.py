"""Ephemeral authenticated loopback HTTP bridge to one assigned slot's relay.

Only model API requests use this bridge. It does not inspect projects, execute a
local agent, own Claude credentials, retry inference, or persist request data.
The caller supplies the fixed paired SSH command and launches native Claude.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import math
import os
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Optional

VERSION = 2
MAX_META = 32 * 1024
MAX_BODY = 64 * 1024 * 1024
MAX_CHUNK = 64 * 1024
PATHS = frozenset({"/v1/messages", "/v1/messages?beta=true",
                   "/v1/messages/count_tokens", "/v1/messages/count_tokens?beta=true"})
REQUEST_HEADERS = frozenset({"accept", "content-type", "anthropic-version", "anthropic-beta"})
RESPONSE_HEADERS = frozenset({"content-type", "request-id", "retry-after"})
IDENTITY_FIELDS = frozenset({"metadata", "user_id", "device_id", "session_id", "client_id",
                            "installation_id", "machine_id", "client_metadata",
                            "device_metadata", "fingerprint"})
Command = Callable[[], list[str]]
Environment = Optional[Callable[[], Mapping[str, str]]]


class RelayError(ValueError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def read_exact(stream: BinaryIO, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise RelayError("slot relay ended before the response completed")
        result.extend(part)
    return bytes(result)


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise RelayError("duplicate slot relay field")
        result[name] = value
    return result


def _json(raw: bytes) -> Any:
    def nonfinite(_value):
        raise RelayError("nonfinite JSON value")
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RelayError("invalid slot relay JSON") from exc


def sanitize_body(raw: bytes) -> bytes:
    """Remove structured identity before transport; never rewrite prompt/code text."""
    body = _json(raw)
    if not isinstance(body, dict):
        raise RelayError("model request must be a JSON object")
    for name in IDENTITY_FIELDS:
        body.pop(name, None)
    try:
        result = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                            allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RelayError("invalid model request JSON") from exc
    if len(result) > MAX_BODY:
        raise RelayError("model request exceeds the transport bound", 413)
    return result


def _header_value(value: Any) -> bool:
    return (isinstance(value, str) and len(value) <= 8192 and value.isascii()
            and all(32 <= ord(char) < 127 for char in value))


def _header_parts(value: str, delimiter: str) -> list[str]:
    """Split HTTP list/parameter syntax without splitting quoted private values."""
    parts, start, quoted, escaped = [], 0, False, False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == delimiter and not quoted:
            parts.append(value[start:index].strip())
            start = index + 1
    if quoted or escaped:
        raise ValueError("invalid model request headers")
    parts.append(value[start:].strip())
    return parts


def sanitize_request_headers(supplied: dict[str, str]) -> dict[str, str]:
    """Keep semantic values, not arbitrary identity-bearing header parameters.

    Mirrored in local_relay.py so the standalone laptop and slot enforce the
    same boundary independently. Parity tests cover both implementations.
    Feature names remain extensible; valid names can themselves carry identity,
    so this is minimization, not an anonymity or covert-channel guarantee.
    """
    headers = {name: value for name, value in supplied.items() if name in REQUEST_HEADERS}
    if any(not isinstance(value, str) or len(value) > 8192 or not value.isascii()
           or any(not 32 <= ord(char) < 127 for char in value) for value in headers.values()):
        raise ValueError("invalid model request headers")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ValueError("invalid model request headers")
    # Both boundaries serialize the parsed body as UTF-8 JSON. Original charset
    # and extension parameters describe neither that body nor needed API input.
    headers["content-type"] = "application/json"
    if "accept" in headers:
        ranges = _header_parts(headers["accept"], ",")
        if len(ranges) > 32:
            raise ValueError("invalid model request headers")
        normalized = []
        token = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
        quoted = r'"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\[\x20-\x7e])*"'
        for item in ranges:
            parts = _header_parts(item, ";")
            media = parts[0].lower()
            if media not in {"application/json", "text/event-stream", "application/*",
                             "text/*", "*/*"}:
                raise ValueError("invalid model request headers")
            quality = None
            for parameter in parts[1:]:
                name, separator, value = parameter.partition("=")
                name, value = name.strip().lower(), value.strip()
                if (not re.fullmatch(token, name) or not separator
                        or not re.fullmatch(f"(?:{token}|{quoted})", value)):
                    raise ValueError("invalid model request headers")
                if name == "q":
                    if quality is not None or not re.fullmatch(
                            r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", value):
                        raise ValueError("invalid model request headers")
                    quality = value.rstrip("0").rstrip(".") if "." in value else value
            normalized.append(media + (";q=" + quality if quality is not None else ""))
        headers["accept"] = ", ".join(normalized)
    if "anthropic-version" in headers:
        version = headers["anthropic-version"].strip()
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", version):
            raise ValueError("invalid model request headers")
        try:
            date.fromisoformat(version)
        except ValueError:
            raise ValueError("invalid model request headers") from None
        headers["anthropic-version"] = version
    if "anthropic-beta" in headers:
        features = [value.strip() for value in headers["anthropic-beta"].split(",")]
        if len(features) > 64 or any(not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", feature) for feature in features):
            raise ValueError("invalid model request headers")
        headers["anthropic-beta"] = ",".join(features)
    return headers


def metadata(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack("!I", read_exact(stream, 4))[0]
    if not 0 < size <= MAX_META:
        raise RelayError("invalid slot relay response size")
    result = _json(read_exact(stream, size))
    if (not isinstance(result, dict) or set(result) != {"version", "status", "headers"}
            or type(result["version"]) is not int or result["version"] != VERSION
            or type(result["status"]) is not int or not 200 <= result["status"] <= 599
            or 300 <= result["status"] < 400 or not isinstance(result["headers"], dict)):
        raise RelayError("invalid or redirected slot relay response")
    if any(name not in RESPONSE_HEADERS or not _header_value(value)
           for name, value in result["headers"].items()):
        raise RelayError("invalid slot relay response header")
    return result


def chunk(stream: BinaryIO) -> bytes:
    size = struct.unpack("!I", read_exact(stream, 4))[0]
    if size > MAX_CHUNK:
        raise RelayError("oversized slot relay response frame")
    return read_exact(stream, size)


def preamble(fields: dict[str, Any]) -> bytes:
    raw = json.dumps({"version": VERSION, **fields}, separators=(",", ":"),
                     allow_nan=False).encode()
    if not 0 < len(raw) <= MAX_META:
        raise RelayError("request headers exceed the slot relay limit")
    return struct.pack("!I", len(raw)) + raw


def _spawn(command: Command, environment: Environment):
    arguments = command()
    if (not isinstance(arguments, list) or not arguments
            or any(not isinstance(value, str) or not value or "\0" in value for value in arguments)
            or arguments[-1] != "ccfleet-inference-v1"):
        raise RelayError("invalid fixed slot relay command")
    return subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            env=dict(environment()) if environment is not None else None)


def _kill(process) -> None:
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()


def _finish(process) -> None:
    _kill(process)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        process.wait(timeout=3)
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


def _completed(process, status: int) -> None:
    # Do not close stdin until the server exits: EOF is the cancellation signal.
    code = process.wait(timeout=5)
    if code != 0 and not (code == 2 and status >= 400):
        raise RelayError("slot relay ended unexpectedly")
    if process.stdout.read(1):
        raise RelayError("unexpected trailing slot relay data")


def check_status(command: Command, environment: Environment = None,
                 timeout: float = 25) -> dict[str, Any]:
    """Verify the slot's gate/account without reading files or making a model call."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("invalid relay readiness timeout")
    process = _spawn(command, environment)
    timer = threading.Timer(timeout, _kill, args=(process,))
    timer.daemon = True
    timer.start()
    try:
        process.stdin.write(preamble({"operation": "status"}))
        process.stdin.flush()
        response = metadata(process.stdout)
        body = bytearray()
        while True:
            part = chunk(process.stdout)
            if not part:
                break
            body.extend(part)
            if len(body) > MAX_CHUNK:
                raise RelayError("oversized relay readiness response")
        _completed(process, response["status"])
        if response["status"] != 200:
            message = {
                401: "the slot's Claude sign-in needs renewal; open your slot page",
                403: "local inference is not enabled for this slot; contact your operator",
                409: "the slot is changing accounts; retry after it finishes",
            }.get(response["status"],
                  "slot inference is unavailable; retry or contact your operator")
            raise RelayError(message, response["status"])
        answer = _json(bytes(body))
        if (not isinstance(answer, dict) or set(answer) != {"ready", "protocol"}
                or answer["ready"] is not True or type(answer["protocol"]) is not int
                or answer["protocol"] != VERSION):
            raise RelayError("invalid relay readiness response")
        return answer
    except (OSError, subprocess.SubprocessError) as exc:
        raise RelayError("could not verify slot inference; check your connection and pairing") \
            from exc
    finally:
        timer.cancel()
        _finish(process)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, bridge):
        self.bridge = bridge
        self.workers = threading.BoundedSemaphore(bridge.max_requests)
        self.processes: set[Any] = set()
        self.connections: set[socket.socket] = set()
        self.guard = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request, client_address):
        if self.stopping.is_set() or not self.workers.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            self.shutdown_request(request)
            return
        with self.guard:
            self.connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.guard:
                self.connections.discard(request)
            self.workers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.guard:
                self.connections.discard(request)
            self.workers.release()

    def stop_connections(self):
        self.stopping.set()
        with self.guard:
            for process in self.processes:
                _kill(process)
            for connection in self.connections:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)

    def handle_error(self, _request, _address):
        # BaseServer's traceback can contain request data. Emit nothing.
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ccfleet"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.bridge.body_timeout)

    def log_message(self, _format, *_args):
        pass

    def fail(self, status: int, message: str):
        self.close_connection = True
        body = json.dumps({"type": "error", "error": {
            "type": "ccfleet_relay_error", "message": message}}).encode()
        if self.command == "HEAD":
            body = b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.fail(code, "unsupported or invalid local HTTP request")

    def handle_expect_100(self):
        self.fail(417, "Expect requests are not supported")
        return False

    def _method(self):
        self.fail(405, "only model POST requests are supported")

    do_GET = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_CONNECT = do_TRACE = _method

    def _validate(self) -> Optional[int]:
        server = self.server
        if (self.headers.get_all("Host") != [f"127.0.0.1:{server.server_port}"]
                or self.headers.get_all("Origin") is not None):
            self.fail(403, "loopback CLI requests only")
            return None
        auth = self.headers.get_all("Authorization", [])
        keys = self.headers.get_all("x-api-key", [])
        if len(auth) + len(keys) != 1:
            self.fail(401, "invalid local relay token")
            return None
        provided = auth[0] if auth else keys[0]
        expected = (("Bearer " if auth else "") + server.bridge.secret).encode()
        if not hmac.compare_digest(provided.encode(), expected):
            self.fail(401, "invalid local relay token")
            return None
        names = [name.lower() for name in self.headers]
        lengths = self.headers.get_all("Content-Length", [])
        if (len(names) != len(set(names)) or self.headers.get_all("Transfer-Encoding") is not None
                or self.path not in PATHS or len(lengths) != 1
                or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 10
                or any(not _header_value(value) for value in self.headers.values())):
            self.fail(400, "invalid Claude request")
            return None
        size = int(lengths[0])
        if str(size) != lengths[0]:
            self.fail(400, "ambiguous request length")
            return None
        if not 0 < size <= MAX_BODY:
            self.fail(413, "Claude request exceeds the inference transport limit")
            return None
        return size

    def _cancel_on_disconnect(self, process, complete: threading.Event):
        while not complete.wait(0.1):
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable and self.connection.recv(1, socket.MSG_PEEK) == b"":
                    _kill(process)
                    return
            except (OSError, ValueError):
                _kill(process)
                return

    def do_POST(self):
        self.close_connection = True
        size = self._validate()
        if size is None:
            return
        try:
            headers = sanitize_request_headers({name.lower(): value
                                                 for name, value in self.headers.items()})
        except ValueError:
            self.fail(400, "invalid model request headers")
            return
        try:
            body = sanitize_body(read_exact(self.rfile, size))
        except RelayError as exc:
            self.fail(exc.status or 400, "invalid model request JSON")
            return
        except OSError:
            self.fail(408, "model request body did not arrive")
            return
        server = self.server
        process = timer = watcher = None
        complete = threading.Event()
        began = False
        try:
            prefix = preamble({"operation": "request", "method": "POST", "path": self.path,
                               "headers": headers, "body_size": len(body)})
            with server.guard:
                if server.stopping.is_set():
                    raise RelayError("local relay is closing")
                process = _spawn(server.bridge.command, server.bridge.environment)
                server.processes.add(process)
            timer = threading.Timer(server.bridge.request_timeout, _kill, args=(process,))
            timer.daemon = True
            timer.start()
            # An SSH peer can stop reading during a large upload. Watch the
            # client before writing to the pipe, not only while reading SSE.
            watcher = threading.Thread(target=self._cancel_on_disconnect,
                                       args=(process, complete), daemon=True)
            watcher.start()
            process.stdin.write(prefix)
            process.stdin.write(body)
            process.stdin.flush()
            answer = metadata(process.stdout)
            self.send_response(answer["status"])
            for name, value in answer["headers"].items():
                self.send_header(name, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            began = True
            while True:
                part = chunk(process.stdout)
                if not part:
                    break
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                self.wfile.flush()
            _completed(process, answer["status"])
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (RelayError, OSError, subprocess.SubprocessError):
            if not began:
                self.fail(502, "slot relay failed; this bridge did not retry the request")
            # Once streaming starts, close without an HTTP success terminator.
        finally:
            complete.set()
            if timer is not None:
                timer.cancel()
            if process is not None:
                _finish(process)
                with server.guard:
                    server.processes.discard(process)
            if watcher is not None:
                watcher.join(timeout=1)


class Bridge:
    """Launch-scoped endpoint. The random secret is for the native local process only."""

    def __init__(self, command: Command, environment: Environment = None, *,
                 max_requests: int = 4, request_timeout: float = 900,
                 body_timeout: float = 30):
        if (type(max_requests) is not int or not 1 <= max_requests <= 32
                or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                       for value in (request_timeout, body_timeout))):
            raise ValueError("invalid relay resource bounds")
        self.command, self.environment = command, environment
        self.max_requests, self.request_timeout = max_requests, request_timeout
        self.body_timeout = body_timeout
        self.secret = secrets.token_urlsafe(32)
        self.server = _HTTPServer(self)
        self.port = self.server.server_port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.thread: Optional[threading.Thread] = None

    def start(self):
        if self.thread is not None:
            raise RelayError("local relay is already started")
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.server.stop_connections()
        if self.thread is not None:
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


def _transport_artifacts(parent: int, child: int, name: str,
                         expected: Optional[tuple[int, int]]) -> bool:
    """Remove only the owned transport directory and its private socket."""
    try:
        own = os.fstat(child)
        try:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if (not TransportSession._trusted(own) or not TransportSession._trusted(current)
                or (own.st_dev, own.st_ino) != (current.st_dev, current.st_ino)):
            return False
        try:
            sock = os.stat("s", dir_fd=child, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if (not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != os.getuid()
                    or sock.st_mode & 0o077 or sock.st_nlink != 1
                    or expected != (sock.st_dev, sock.st_ino)):
                return False
            os.unlink("s", dir_fd=child)
        os.rmdir(name, dir_fd=parent)
        return True
    except OSError:
        return False


def _transport_supervisor(arguments: list[str]) -> int:
    """Private entry point; the live supervisor pins its own isolated PGID."""
    if len(arguments) != 5 or os.getpid() != os.getpgrp():
        return 2
    try:
        alive, status, parent, child = map(int, arguments[:4])
        name = arguments[4]
        if (len({alive, status, parent, child}) != 4 or min(alive, status, parent, child) < 3
                or len(name) != 9 or name[0] != "m"
                or any(c not in "0123456789abcdef" for c in name[1:])
                or not stat.S_ISFIFO(os.fstat(alive).st_mode)
                or not stat.S_ISFIFO(os.fstat(status).st_mode)
                or not TransportSession._trusted(os.fstat(parent))
                or not TransportSession._trusted(os.fstat(child))):
            return 2
    except (ValueError, OSError):
        return 2
    stopped = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *_: stopped.set())
    for descriptor in (alive, status, parent, child):
        os.set_inheritable(descriptor, False)
    master = None
    socket_identity = None
    try:
        raw = bytearray()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not stopped.is_set():
            readable = select.select([0, alive], [], [], 0.1)[0]
            if alive in readable:
                return 2
            if 0 not in readable:
                continue
            block = os.read(0, 4096)
            if not block:
                break
            raw.extend(block)
            if len(raw) > 16 * 1024:
                return 2
        else:
            return 2
        command = _json(bytes(raw))
        if (not isinstance(command, list) or not command
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)):
            return 2
        # Same group as this live owner, never the caller's foreground group.
        master = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=False)
        os.write(status, b"R")
        while not stopped.is_set() and master.poll() is None:
            if socket_identity is None:
                with contextlib.suppress(FileNotFoundError):
                    info = os.stat("s", dir_fd=child, follow_symlinks=False)
                    if (stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
                            and not info.st_mode & 0o077 and info.st_nlink == 1):
                        socket_identity = (info.st_dev, info.st_ino)
                        os.write(status, b"K")
            if select.select([alive], [], [], 0.05)[0]:
                break  # EOF is wrapper exit, including SIGKILL of that one PID.
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    finally:
        with contextlib.suppress(OSError):
            os.write(status, b"S")
        # This process remains alive throughout both signals. Its own TERM
        # handler only sets an event, so the PGID cannot be recycled underneath
        # the final kill, even if SSH exited and every child became an orphan.
        with contextlib.suppress(OSError):
            os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(0.3)
        clean = _transport_artifacts(parent, child, name, socket_identity)
        with contextlib.suppress(OSError):
            os.write(status, b"C" if clean else b"U")
        for descriptor in (alive, parent, child):
            with contextlib.suppress(OSError):
                os.close(descriptor)
        # Includes this live leader and all still-grouped SSH/proxy descendants.
        os.killpg(os.getpgrp(), signal.SIGKILL)
    return 2  # Unreachable after successful group cleanup.


class TransportSession:
    """Opt-in, foreground-owned SSH multiplexing for one captured device route.

    This does not change Bridge or retry a request. The caller may choose a cold
    connection only if start() fails before sending model traffic. Once started,
    channels use a failing ProxyCommand so a dead master cannot silently fall
    back to a fresh connection. A launch-scoped supervisor owns an isolated
    process group and an anonymous parent-liveness pipe, not a persistent daemon.
    Wrapper exit, including SIGKILL, closes the pipe and stops that owned group.
    No persisted PID, discovered descendant PID or caller group is signalled.
    """

    # OpenSSH binds a temporary pathname with a 17-character suffix before
    # publishing its control socket. Reserve that space on both macOS and Linux.
    MAX_CONTROL_PATH = 80

    def __init__(self, base_command: Command, environment: Environment,
                 directory: Path, timeout: float = 15):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 30):
            raise RelayError("invalid SSH session startup timeout")
        self.base_command = base_command
        self.environment = environment
        self.directory = Path(directory)
        self.timeout = timeout
        self._guard = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._parent: Optional[int] = None
        self._child: Optional[int] = None
        self._name = "m" + secrets.token_hex(4)
        self._socket_identity: Optional[tuple[int, int]] = None
        self._directory_identity: Optional[tuple[int, int]] = None
        self._base: list[str] = []
        self._slave_base: list[str] = []
        self._env: Optional[dict[str, str]] = None
        self._started = False
        self._closed = False
        self._path = str(self.directory / self._name / "s")
        self._liveness: Optional[int] = None
        self._status: Optional[int] = None
        self._supervisor_ready = False
        self._supervisor_socket_ready = False
        self._supervisor_ended = False
        self._supervisor_eof = False
        self._cleanup_confirmed = False
        self._cleanup_observed = False

    def _read_status(self) -> None:
        if self._status is None:
            return
        while select.select([self._status], [], [], 0)[0]:
            data = os.read(self._status, 32)
            if not data:
                self._supervisor_ended = True
                self._supervisor_eof = True
                return
            for value in data:
                if value == ord("R"):
                    self._supervisor_ready = True
                elif value == ord("K"):
                    self._supervisor_socket_ready = True
                elif value == ord("C"):
                    self._cleanup_confirmed = True
                    self._cleanup_observed = True
                elif value in (ord("S"), ord("U")):
                    self._supervisor_ended = True
                    if value == ord("U"):
                        self._cleanup_observed = True
                else:
                    self._supervisor_ended = True

    def _alive(self) -> bool:
        # Do not poll()/wait(): an unreaped child pins the owned PGID if its
        # supervisor crashes before completing cleanup.
        self._read_status()
        return self._supervisor_ready and not self._supervisor_ended

    def _launch_supervisor(self, command: list[str]) -> None:
        raw = json.dumps(command).encode()
        if len(raw) > 16 * 1024:
            raise RelayError("SSH transport command exceeds its startup bound")
        alive_read, self._liveness = os.pipe()
        self._status, status_write = os.pipe()
        try:
            assert self._parent is not None and self._child is not None
            self._process = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--transport-supervisor",
                 str(alive_read), str(status_write), str(self._parent), str(self._child),
                 self._name],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self._env, start_new_session=True,
                pass_fds=(alive_read, status_write, self._parent, self._child))
            self._process.stdin.write(raw)
            self._process.stdin.close()
        finally:
            os.close(alive_read)
            os.close(status_write)

    @staticmethod
    def _trusted(info: os.stat_result) -> bool:
        return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077)

    def _open_directory(self) -> None:
        if (not self.directory.is_absolute() or ".." in self.directory.parts
                or any(char.isspace() or ord(char) < 32 or char in "%$\0"
                       for char in str(self.directory))):
            raise RelayError("SSH reuse needs an absolute private directory "
                             "without special characters")
        if len(os.fsencode(self._path)) > self.MAX_CONTROL_PATH:
            raise RelayError("SSH reuse needs a shorter private configuration path; "
                             "use cold transport")
        flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.directory.anchor, flags)
        try:
            for part in self.directory.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            if not self._trusted(os.fstat(fd)):
                raise RelayError("SSH reuse directory must be private and owned by this user")
            self._parent, fd = fd, None
            os.mkdir(self._name, 0o700, dir_fd=self._parent)
            self._child = os.open(self._name, flags, dir_fd=self._parent)
            info = os.fstat(self._child)
            if not self._trusted(info):
                raise RelayError("SSH reuse directory could not be created safely")
            self._directory_identity = (info.st_dev, info.st_ino)
        finally:
            if fd is not None:
                os.close(fd)

    def _capture_command(self) -> None:
        base = self.base_command()
        if (not isinstance(base, list) or len(base) < 3
                or any(not isinstance(arg, str) or not arg or "\0" in arg
                       or any(ord(char) < 32 or ord(char) == 127 for char in arg) for arg in base)
                or base[-1].startswith("-") or any(char.isspace() for char in base[-1])):
            raise RelayError("invalid captured SSH device command")
        # Only the executable's existing noninteractive, config-free shape is
        # accepted. Reject ambient multiplexing, ProxyJump and daemon switches.
        slave = [base[0]]
        proxy_count = 0
        no_config = no_tty = False
        index = 1
        while index < len(base) - 1:
            arg = base[index]
            if arg == "-T":
                no_tty = True
                slave.append(arg)
                index += 1
                continue
            if arg == "-F" and index + 1 < len(base) - 1 and base[index + 1] == "/dev/null":
                no_config = True
                slave.extend(base[index:index + 2])
                index += 2
                continue
            if arg != "-o" or index + 1 >= len(base) - 1:
                raise RelayError("SSH reuse requires the fixed noninteractive device transport")
            option = base[index + 1]
            name, separator, _ = option.partition("=")
            if not separator or not name or any(char.isspace() for char in name):
                raise RelayError("invalid fixed SSH transport option")
            lowered = name.lower()
            if lowered in {"controlmaster", "controlpath", "controlpersist", "proxyjump",
                           "forkafterauthentication", "remotecommand", "localcommand"}:
                raise RelayError("SSH reuse cannot inherit a competing transport lifecycle")
            if lowered == "proxycommand":
                proxy_count += 1
            else:
                slave.extend((arg, option))
            index += 2
        if not no_config or not no_tty or proxy_count != 1:
            raise RelayError("SSH reuse requires one fixed proxy and isolated SSH settings")
        self._base = list(base)
        self._slave_base = slave
        environment = self.environment() if self.environment is not None else None
        if environment is not None:
            if (not isinstance(environment, Mapping)
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           or "\0" in key or "=" in key or "\0" in value
                           for key, value in environment.items())):
                raise RelayError("invalid SSH session environment")
            self._env = dict(environment)

    def _socket(self) -> bool:
        if self._child is None or self._parent is None or self._directory_identity is None:
            return False
        current_dir = os.stat(self._name, dir_fd=self._parent, follow_symlinks=False)
        if (not self._trusted(current_dir)
                or (current_dir.st_dev, current_dir.st_ino) != self._directory_identity):
            raise RelayError("SSH reuse directory changed; no channel was opened")
        try:
            info = os.stat("s", dir_fd=self._child, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise RelayError("SSH control socket is not private and user-owned")
        identity = (info.st_dev, info.st_ino)
        if self._socket_identity is not None and self._socket_identity != identity:
            raise RelayError("SSH control socket changed; no replacement connection was opened")
        self._socket_identity = identity
        return True

    def _control_arguments(self, action: str) -> list[str]:
        return [*self._slave_base, "-o", "ControlMaster=no", "-o", f"ControlPath={self._path}",
                "-o", "ControlPersist=no", "-o", "ProxyCommand=/bin/false",
                "-O", action, self._base[-1]]

    def _control(self, action: str, timeout: float) -> bool:
        result = subprocess.run(self._control_arguments(action), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=self._env, timeout=timeout, check=False,
                                start_new_session=False)
        return result.returncode == 0

    def start(self):
        with self._guard:
            if self._closed or self._started or self._process is not None:
                raise RelayError("SSH transport session cannot be started twice")
            try:
                self._capture_command()
                self._open_directory()
                command = [*self._base[:-1], "-M", "-N", "-o", "ControlMaster=yes",
                           "-o", f"ControlPath={self._path}", "-o", "ControlPersist=no",
                           self._base[-1]]
                self._launch_supervisor(command)
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    self._read_status()
                    if self._supervisor_ended:
                        raise RelayError("SSH reuse could not authenticate; "
                                         "no model request was sent")
                    if (self._socket() and self._supervisor_socket_ready
                            and self._control("check", max(0.05, deadline - time.monotonic()))):
                        if not self._alive():
                            raise RelayError("SSH reuse ended during startup; "
                                             "no model request was sent")
                        self._started = True
                        return self
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
                raise RelayError("SSH reuse startup timed out; no model request was sent")
            except BaseException as exc:
                with contextlib.suppress(RelayError):
                    self.close()
                if isinstance(exc, (KeyboardInterrupt, SystemExit, RelayError)):
                    raise
                raise RelayError("SSH reuse could not start safely; "
                                 "no model request was sent") from exc

    def command(self, remote_command: list[str]) -> list[str]:
        with self._guard:
            if (not isinstance(remote_command, list) or not 1 <= len(remote_command) <= 16
                    or any(not isinstance(arg, str) or not arg or len(arg) > 1024
                           or any(ord(char) < 32 or ord(char) == 127 for char in arg)
                           for arg in remote_command)):
                raise RelayError("invalid fixed SSH channel command")
            if (self._closed or not self._started or self._process is None
                    or not self._alive()):
                raise RelayError("SSH session ended; no request was retried "
                                 "or new connection opened")
            try:
                if not self._socket():
                    raise RelayError("SSH control socket is unavailable; "
                                     "no fallback connection was opened")
            except OSError as exc:
                raise RelayError("SSH control socket is unavailable; "
                                 "no fallback connection was opened") from exc
            return [*self._slave_base, "-o", "ControlMaster=no", "-o", f"ControlPath={self._path}",
                    "-o", "ControlPersist=no", "-o", "ProxyCommand=/bin/false",
                    self._base[-1], *remote_command]

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
            confirmed = True
            process = self._process
            try:
                if process is not None:
                    if self._alive():
                        try:
                            if self._socket():
                                self._control("exit", 2)
                        except (OSError, RelayError, subprocess.SubprocessError):
                            pass
                if self._liveness is not None:
                    os.close(self._liveness)
                    self._liveness = None
                if process is not None:
                    deadline = time.monotonic() + 3
                    while not self._supervisor_eof and time.monotonic() < deadline:
                        self._read_status()
                        if self._cleanup_confirmed:
                            break
                        time.sleep(0.025)
            except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
                confirmed = False
            finally:
                if process is not None:
                    # Never reap before this final signal. Even an unexpectedly
                    # dead supervisor's unreaped PID pins this owned group ID.
                    permission_denied = False
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # macOS reports EPERM for a group containing only
                        # zombies. Accept that only after the owned supervisor
                        # completed cleanup and closed its private status pipe.
                        permission_denied = True
                    except OSError:
                        confirmed = False
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        confirmed = False
                    if permission_denied:
                        self._read_status()
                        if not (self._cleanup_observed and self._supervisor_eof
                                and process.returncode == -signal.SIGKILL):
                            confirmed = False
            try:
                if self._child is not None and self._parent is not None:
                    confirmed = _transport_artifacts(self._parent, self._child, self._name,
                                                       self._socket_identity) and confirmed
            except (OSError, RelayError):
                confirmed = False
            finally:
                for name in ("_child", "_parent", "_liveness", "_status"):
                    descriptor = getattr(self, name)
                    if descriptor is not None:
                        os.close(descriptor)
                        setattr(self, name, None)
            if not confirmed:
                raise RelayError("SSH session stopped, but control/proxy cleanup "
                                 "was not fully confirmed")

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--transport-supervisor":
        raise SystemExit(_transport_supervisor(sys.argv[2:]))
    raise SystemExit(2)
