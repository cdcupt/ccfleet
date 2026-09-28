"""Opt-in, single-slot inference transport over an authenticated SSH channel.

There is no listener on the node and no caller-selected upstream or credential.
The forced SSH command runs this as the slot user. Request identities and bodies
are forwarded unchanged; only hop-by-hop headers and authentication are replaced.
This experimental transport is disabled unless root explicitly enables the slot.
It never refreshes or writes Claude's credential file: the original Claude Code
installation remains its sole owner. Expired credentials fail closed.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import pwd
import re
import select
import socket
import ssl
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable

POLICY_DIR = Path("/etc/ccfleet/local-relay")
UPSTREAM_HOST = "api.anthropic.com"
MAX_HEADER = 32 * 1024
MAX_BODY = 20 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
REQUEST_TIMEOUT = 300
HEADER_NAME = re.compile(r"^[a-zA-Z0-9-]{1,64}$")
REQUEST_HEADERS = frozenset({
    "accept", "content-type", "anthropic-version", "anthropic-beta", "user-agent", "x-app",
})
RESPONSE_HEADERS = frozenset({"content-type", "request-id", "retry-after"})
PATHS = frozenset({"/v1/messages", "/v1/messages?beta=true", "/v1/messages/count_tokens",
                   "/v1/messages/count_tokens?beta=true"})


class RelayError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def read_exact(stream: BinaryIO, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError("incomplete relay frame")
        result.extend(part)
    return bytes(result)


def read_metadata(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack("!I", read_exact(stream, 4))[0]
    if not 0 < size <= MAX_HEADER:
        raise RelayError(400, "invalid relay metadata size")
    try:
        result = json.loads(read_exact(stream, size))
    except (ValueError, UnicodeError) as exc:
        raise RelayError(400, "invalid relay metadata") from exc
    if (not isinstance(result, dict) or type(result.get("version")) is not int
            or result["version"] != 1):
        raise RelayError(400, "unsupported relay protocol")
    return result


def write_metadata(stream: BinaryIO, status: int, headers: dict[str, str]) -> None:
    encoded = json.dumps({"version": 1, "status": status, "headers": headers}).encode()
    stream.write(struct.pack("!I", len(encoded)) + encoded)
    stream.flush()


def write_chunk(stream: BinaryIO, data: bytes) -> None:
    stream.write(struct.pack("!I", len(data)) + data)
    stream.flush()


def send_error(stream: BinaryIO, status: int, message: str) -> None:
    write_metadata(stream, status, {"content-type": "application/json"})
    body = json.dumps({"type": "error", "error": {
        "type": "ccfleet_relay_error", "message": message}}).encode()
    write_chunk(stream, body)
    write_chunk(stream, b"")


def require_enabled(policy_dir: Path = POLICY_DIR) -> None:
    """A paired key alone does not opt an existing customer into a model relay."""
    name = pwd.getpwuid(os.getuid()).pw_name
    try:
        parent = policy_dir.lstat()
        marker = (policy_dir / name).lstat()
    except OSError as exc:
        raise RelayError(403, "local relay is not enabled for this slot") from exc
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022
            or not stat.S_ISREG(marker.st_mode) or marker.st_uid != 0
            or marker.st_mode & 0o022):
        raise RelayError(403, "invalid operator local-relay policy")


def read_object(path: Path, limit: int = 64 * 1024) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("oversized configuration")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (ValueError, OSError):
        pass
    raise RelayError(401, "slot authentication is unavailable; sign in on your slot page")


def bound_account(home: Path) -> str:
    profile = read_object(home / ".claude.json", limit=4 * 1024 * 1024).get("oauthAccount")
    state = read_object(home / ".config/ccfleet/slot-state.json")
    uuid = profile.get("accountUuid") if isinstance(profile, dict) else None
    if not isinstance(uuid, str) or not uuid.strip():
        raise RelayError(401, "the slot has no bound Claude account")
    fingerprint = hashlib.sha256(uuid.strip().encode()).hexdigest()[:16]
    if state.get("bound_fp") != fingerprint or state.get("account_restart"):
        raise RelayError(409, "slot account transition is pending; retry after it completes")
    return fingerprint


def credential(home: Path, now: float) -> str:
    bound_account(home)
    oauth = read_object(home / ".claude/.credentials.json").get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise RelayError(401, "sign in to Claude on your slot page")
    token, expiry = oauth.get("accessToken"), oauth.get("expiresAt")
    if (not isinstance(token, str) or not 1 <= len(token) <= 8192
            or not token.isascii() or any(c.isspace() or ord(c) < 32 or ord(c) == 127
                                         for c in token)):
        raise RelayError(401, "slot authentication is invalid; sign in on your slot page")
    if (not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
            or not math.isfinite(expiry) or expiry / 1000 <= now + 30):
        raise RelayError(401, "slot sign-in needs renewal by Claude Code; use your slot page")
    return token


def validate_request(meta: dict[str, Any]) -> tuple[str, dict[str, str], int]:
    if (meta.get("method") != "POST" or not isinstance(meta.get("path"), str)
            or meta["path"] not in PATHS):
        raise RelayError(400, "only Claude message and token-count requests are supported")
    size = meta.get("body_size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BODY:
        raise RelayError(413, "invalid or oversized Claude request")
    supplied = meta.get("headers")
    if not isinstance(supplied, dict) or len(supplied) > 64:
        raise RelayError(400, "invalid relay headers")
    headers = {}
    for name, value in supplied.items():
        if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name)
                or not isinstance(value, str) or len(value) > 8192
                or not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise RelayError(400, "invalid relay header")
        lowered = name.lower()
        if lowered in headers:
            raise RelayError(400, "duplicate relay header")
        if lowered in REQUEST_HEADERS or lowered.startswith("x-stainless-"):
            headers[lowered] = value
    if headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise RelayError(400, "Claude requests must be JSON")
    # The HTTP library supplies the real destination and size, never caller
    # input. All client authentication, cookies and proxy headers are dropped.
    headers["accept-encoding"] = "identity"
    return meta["path"], headers, size


def connect_upstream() -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(
        UPSTREAM_HOST, 443, timeout=60, context=ssl.create_default_context())


def serve_one(input_: BinaryIO, output: BinaryIO, home: Path, *,
              policy: Callable[[], None] = require_enabled,
              connect: Callable[[], Any] = connect_upstream,
              watch: bool = False) -> int:
    connection = None
    began = False
    finished = threading.Event()
    cancelled = threading.Event()
    try:
        policy()
        meta = read_metadata(input_)
        original_account = bound_account(home)
        token = credential(home, time.time())
        if bound_account(home) != original_account:
            raise RelayError(409, "slot account changed while opening the request; retry")
        if meta.get("operation") == "status":
            write_metadata(output, 200, {"content-type": "application/json"})
            write_chunk(output, b'{"ready":true,"protocol":1}')
            write_chunk(output, b"")
            return 0
        path, headers, size = validate_request(meta)
        body = read_exact(input_, size)
        try:
            if not isinstance(json.loads(body), dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeError) as exc:
            raise RelayError(400, "Claude request must be a JSON object") from exc
        connection = connect()
        headers["authorization"] = "Bearer " + token
        connection.connect()
        upstream_socket = connection.sock

        def stop_if_revoked() -> None:
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while not finished.wait(0.5):
                try:
                    policy()
                    valid = bound_account(home) == original_account
                    eof = bool(select.select([input_], [], [], 0)[0])
                except (RelayError, OSError, ValueError):
                    valid, eof = False, True
                if valid and not eof and time.monotonic() < deadline:
                    continue
                cancelled.set()
                # A closed SSH stdin is a revoked/disconnected device. Also
                # stop an in-flight request when the bound account changes.
                sock = upstream_socket
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return

        if watch:
            threading.Thread(target=stop_if_revoked, daemon=True).start()
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()
                            if name.lower() in RESPONSE_HEADERS}
        write_metadata(output, response.status, response_headers)
        began = True
        while True:
            data = response.read1(CHUNK_SIZE)
            if not data:
                if cancelled.is_set() or getattr(response, "length", None) not in (None, 0):
                    raise OSError("incomplete upstream response")
                break
            write_chunk(output, data)
        write_chunk(output, b"")
        return 0
    except RelayError as exc:
        if not began:
            send_error(output, exc.status, str(exc))
        return 2
    except (OSError, EOFError, http.client.HTTPException):
        if not began:
            send_error(output, 502, "slot relay connection failed; the relay did not retry")
        # No terminal frame after a truncated upstream response. The client
        # must not present an interrupted stream as successful completion.
        return 2
    finally:
        finished.set()
        if connection is not None:
            connection.close()


def main() -> int:
    try:
        return serve_one(sys.stdin.buffer, sys.stdout.buffer, Path.home(), watch=True)
    except (OSError, BrokenPipeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
