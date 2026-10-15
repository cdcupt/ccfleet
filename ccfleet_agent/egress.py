"""Opt-in node egress: verified HTTPS through a credential-free local CONNECT proxy.

Only the root service reads upstream credentials. Slot processes receive a fixed
loopback URL; they never receive the upstream address, password or response text.
Network policy must independently prevent a slot from bypassing this proxy.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import http.client
import ipaddress
import json
import os
import re
import select
import signal
import socket
import socketserver
import ssl
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

POLICY_FILE = Path("/etc/ccfleet/egress.json")
UPSTREAM_FILE = Path("/etc/ccfleet/egress-upstream.json")
MANAGED_SETTINGS_FILE = Path("/etc/claude-code/managed-settings.d/90-ccfleet-egress.json")
UNPRIVILEGED_PORT_FILE = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
ROOT_UID = 0
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 912
PROXY_URL = "http://127.0.0.1:912"
MAX_CONFIG = 16 * 1024
MAX_HEADERS = 16 * 1024
MAX_SESSIONS = 32
CONNECT_TIMEOUT = 20
IDLE_TIMEOUT = 300
ERROR = "execution node egress is unavailable"
_HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
_HEADER = re.compile(rb"[A-Za-z0-9-]{1,64}\Z")
_FAILURE = b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"


class EgressError(OSError):
    def __init__(self) -> None:
        super().__init__(ERROR)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise EgressError()
        result[key] = value
    return result


def _constant(_):
    raise EgressError()


def _parents(path: Path) -> None:
    """No symlink or replaceable directory may redirect a protected setting."""
    if not path.is_absolute():
        raise EgressError()
    for parent in reversed(path.parents):
        info = parent.lstat()
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, ROOT_UID)
                or info.st_mode & 0o022 and not sticky_root):
            raise EgressError()


def _read(path: Path, *, private: bool = False) -> dict[str, Any]:
    fd = None
    try:
        _parents(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_UID or info.st_nlink != 1
                or info.st_mode & (0o077 if private else 0o022)
                or not 0 < info.st_size <= MAX_CONFIG):
            raise EgressError()
        raw = os.read(fd, MAX_CONFIG + 1)
        if len(raw) > MAX_CONFIG:
            raise EgressError()
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(value, dict):
            raise EgressError()
        return value
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise EgressError() from None
    finally:
        if fd is not None:
            os.close(fd)


def enabled() -> bool:
    try:
        POLICY_FILE.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise EgressError() from None
    value = _read(POLICY_FILE)
    if (set(value) != {"version", "enabled"} or type(value["version"]) is not int
            or value["version"] != 1 or value["enabled"] is not True):
        raise EgressError()
    return True


def native_environment() -> dict[str, str]:
    if not enabled():
        return {}
    return {"HTTPS_PROXY": PROXY_URL, "HTTP_PROXY": PROXY_URL,
            "https_proxy": PROXY_URL, "http_proxy": PROXY_URL,
            "ALL_PROXY": "", "all_proxy": "", "NO_PROXY": "", "no_proxy": ""}


class _ProxiedHTTPSConnection(http.client.HTTPSConnection):
    """Keep the fixed provider identity while connecting only to loopback."""

    def __init__(self, host: str, port: int, *, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self.set_tunnel(host, port)

    def connect(self) -> None:
        try:
            if not enabled():
                raise EgressError()
            self.sock = socket.create_connection((LISTEN_HOST, LISTEN_PORT), self.timeout)
            self._tunnel()
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except Exception:
            self.close()
            raise EgressError() from None


def provider_connection(host: str, port: int, *, timeout: float,
                        context: ssl.SSLContext) -> http.client.HTTPSConnection:
    if (not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED
            or host != "api.anthropic.com" or port != 443):
        raise EgressError()
    if enabled():
        return _ProxiedHTTPSConnection(host, port, timeout=timeout, context=context)
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=context)


def _hostname(value: Any, *, literal: bool = False) -> bool:
    if (not isinstance(value, str) or not _HOST.fullmatch(value)
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                   for part in value.split("."))):
        return False
    try:
        ipaddress.ip_address(value)
        return literal
    except ValueError:
        return True


def upstream_config() -> dict[str, Any]:
    if not enabled():
        raise EgressError()
    value = _read(UPSTREAM_FILE, private=True)
    if (set(value) != {"version", "scheme", "host", "port", "username", "password",
                      "allowed_hosts"}
            or type(value["version"]) is not int or value["version"] != 1
            or value["scheme"] not in ("http", "https")
            or not _hostname(value["host"], literal=True)
            or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
            or not isinstance(value["allowed_hosts"], list)
            or not 1 <= len(value["allowed_hosts"]) <= 64
            or any(not _hostname(host) for host in value["allowed_hosts"])
            or len(set(value["allowed_hosts"])) != len(value["allowed_hosts"])):
        raise EgressError()
    for key in ("username", "password"):
        field = value[key]
        if (not isinstance(field, str) or not 1 <= len(field) <= 2048
                or any(ord(char) < 32 or ord(char) == 127 for char in field)):
            raise EgressError()
    if ":" in value["username"]:
        raise EgressError()
    return value


def _headers(stream: socket.socket) -> list[bytes]:
    data = bytearray()
    deadline = time.monotonic() + CONNECT_TIMEOUT
    while not data.endswith(b"\r\n\r\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EgressError()
        stream.settimeout(remaining)
        part = stream.recv(1)
        if not part or len(data) >= MAX_HEADERS:
            raise EgressError()
        data.extend(part)
    lines = bytes(data[:-4]).split(b"\r\n")
    if not lines or any(len(line) > 4096 for line in lines):
        raise EgressError()
    return lines


def _destination(lines: list[bytes], allowed: list[str]) -> str:
    match = re.fullmatch(rb"CONNECT ([a-zA-Z0-9.-]+):443 HTTP/1\.[01]", lines[0])
    if match is None:
        raise EgressError()
    host = match[1].decode("ascii").lower()
    if not _hostname(host) or host not in allowed:
        raise EgressError()
    seen = set()
    for line in lines[1:]:
        key, separator, raw = line.partition(b":")
        name = key.lower()
        if (not separator or not _HEADER.fullmatch(key) or name in seen
                or any(char < 32 and char != 9 or char == 127 for char in raw)
                or name in (b"proxy-authorization", b"authorization", b"transfer-encoding")
                or name == b"content-length" and raw.strip() != b"0"
                or name == b"host" and raw.strip().lower() != (host + ":443").encode()):
            raise EgressError()
        seen.add(name)
    return host


def _upstream(value: dict[str, Any], target: str) -> socket.socket:
    stream = None
    try:
        stream = socket.create_connection((value["host"], value["port"]), CONNECT_TIMEOUT)
        if value["scheme"] == "https":
            stream = ssl.create_default_context().wrap_socket(stream, server_hostname=value["host"])
        credentials = base64.b64encode(
            (value["username"] + ":" + value["password"]).encode("utf-8"))
        stream.sendall(b"CONNECT " + target.encode("ascii") + b":443 HTTP/1.1\r\nHost: "
                       + target.encode("ascii") + b":443\r\nProxy-Authorization: Basic "
                       + credentials + b"\r\n\r\n")
        lines = _headers(stream)
        if not re.fullmatch(rb"HTTP/1\.[01] 200(?: [\x20-\x7e]*)?", lines[0]):
            raise EgressError()
        return stream
    except Exception:
        if stream is not None:
            stream.close()
        raise EgressError() from None


def _pump(client: socket.socket, upstream: socket.socket) -> None:
    active = time.monotonic()
    for stream in (client, upstream):
        stream.settimeout(IDLE_TIMEOUT)
    # Native supervisors can reuse a tunnel for many requests. Progress is
    # bounded by capacity/idle limits, never a new total request deadline.
    while True:
        now = time.monotonic()
        if now - active >= IDLE_TIMEOUT:
            return
        pending = isinstance(upstream, ssl.SSLSocket) and upstream.pending() > 0
        readable, _, _ = select.select([client, upstream], [], [], 0 if pending else 1)
        if pending and upstream not in readable:
            readable.append(upstream)
        for source in readable:
            data = source.recv(64 * 1024)
            if not data:
                return
            (upstream if source is client else client).sendall(data)
            active = time.monotonic()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = None
        connected = False
        try:
            self.request.settimeout(CONNECT_TIMEOUT)
            value = upstream_config()
            target = _destination(_headers(self.request), value["allowed_hosts"])
            upstream = _upstream(value, target)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            connected = True
            _pump(self.request, upstream)
        except Exception:
            if not connected:
                with contextlib.suppress(OSError):
                    self.request.sendall(_FAILURE)
        finally:
            if upstream is not None:
                upstream.close()


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, address=(LISTEN_HOST, LISTEN_PORT)):
        self._capacity = threading.BoundedSemaphore(MAX_SESSIONS)
        super().__init__(address, _Handler)

    def process_request(self, request, client_address) -> None:
        if not self._capacity.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.settimeout(1)
                request.sendall(_FAILURE)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._capacity.release()
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()

    def handle_error(self, request, client_address) -> None:
        pass  # Never log client headers, upstream credentials or response text.


def _ensure_directory(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        _ensure_directory(path.parent)
        path.mkdir(mode=0o755)
    _parents(path / "check")


def install_native_settings(path: Path = MANAGED_SETTINGS_FILE) -> bool:
    environment = native_environment()
    if not environment:
        return False
    temporary = None
    try:
        _ensure_directory(path.parent)
        if path.exists() or path.is_symlink():
            _read(path)
        candidate = path.with_name(path.name + f".tmp.{os.getpid()}")
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        temporary = candidate
        try:
            encoded = (json.dumps({"env": environment}, sort_keys=True) + "\n").encode()
            remaining = memoryview(encoded)
            while remaining:
                count = os.write(fd, remaining)
                if count <= 0:
                    raise EgressError()
                remaining = remaining[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        return True
    except OSError:
        raise EgressError() from None
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def main(argv=None) -> int:
    global POLICY_FILE, UPSTREAM_FILE
    parser = argparse.ArgumentParser(description="Protected execution-node egress")
    parser.add_argument("--policy-file", type=Path, default=POLICY_FILE)
    parser.add_argument("--upstream-file", type=Path, default=UPSTREAM_FILE)
    parser.add_argument("action", choices=("serve", "exec", "check", "check-upstream",
                                           "install-native-settings"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if ((args.policy_file != POLICY_FILE or args.upstream_file != UPSTREAM_FILE)
                and os.geteuid() != ROOT_UID):
            raise EgressError()
        POLICY_FILE, UPSTREAM_FILE = args.policy_file, args.upstream_file
        if args.action == "exec":
            command = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            if not command:
                raise EgressError()
            os.execvpe(command[0], command, {**os.environ, **native_environment()})
        elif args.action == "check":
            native_environment()
        elif args.action == "check-upstream":
            upstream_config()
        elif args.action == "install-native-settings":
            path = Path(args.arguments[0]) if args.arguments else MANAGED_SETTINGS_FILE
            install_native_settings(path)
        else:
            upstream_config()
            # Some container images permit unprivileged binds below 1024. A
            # holder must never impersonate this listener while it is stopped.
            try:
                threshold = int(UNPRIVILEGED_PORT_FILE.read_text().strip())
            except (OSError, ValueError):
                raise EgressError() from None
            if os.geteuid() != ROOT_UID or not LISTEN_PORT < threshold <= 65536:
                raise EgressError()
            def stop(*_):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop)
            with ProxyServer() as server:
                server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(ERROR, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
