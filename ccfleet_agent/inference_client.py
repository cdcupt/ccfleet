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
import secrets
import select
import socket
import struct
import subprocess
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
            headers = {name.lower(): value for name, value in self.headers.items()
                       if name.lower() in REQUEST_HEADERS}
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
