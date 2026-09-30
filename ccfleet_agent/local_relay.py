"""Opt-in v2 inference transport for a local original Claude Code client.

Only the bound slot's current OAuth credential and one fixed upstream are used.
Native slot Claude remains the sole credential writer/refresher. Local identity
headers and structured identity metadata are removed. The relay identifies itself
truthfully; it never replays a captured client fingerprint. Prompts, system text
and tool content are not rewritten and can themselves contain identifying data.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import math
import os
import pwd
import re
import select
import signal
import socket
import ssl
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional

try:
    from . import inference_policy
except ImportError:
    # Python -I excludes cwd and PYTHONPATH. Load the sibling installed in the
    # same root-owned agent directory, never a module from a holder's project.
    _spec = importlib.util.spec_from_file_location(
        "ccfleet_inference_policy", Path(__file__).with_name("inference_policy.py"))
    assert _spec is not None and _spec.loader is not None
    inference_policy = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(inference_policy)

POLICY_DIR = Path("/etc/ccfleet/local-relay")
UPSTREAM_HOST = "api.anthropic.com"
MAX_HEADER = 32 * 1024
# An individual wire-request safety bound, not a project or file-count limit.
MAX_BODY = 64 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
REQUEST_TIMEOUT = 900
INPUT_TIMEOUT = 60
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 300
USER_AGENT = "ccfleet-slot-relay/2"
HEADER_NAME = re.compile(r"[a-zA-Z0-9-]{1,64}\Z")
REQUEST_HEADERS = frozenset({"accept", "content-type", "anthropic-version", "anthropic-beta"})
RESPONSE_HEADERS = frozenset({"content-type", "retry-after"})
MAX_RETRY_AFTER = 24 * 60 * 60
MAX_ERROR_BODY = 64 * 1024
ERROR_READ_TIMEOUT = 5
PATHS = frozenset({"/v1/messages", "/v1/messages?beta=true", "/v1/messages/count_tokens",
                   "/v1/messages/count_tokens?beta=true"})
IDENTITY_FIELDS = frozenset({"user_id", "device_id", "session_id", "client_id",
                             "installation_id", "machine_id", "client_metadata",
                             "device_metadata", "fingerprint"})


class RelayError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON number")


def decode_json(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)


def read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(min(CHUNK_SIZE, size - len(data)))
        if not chunk:
            raise EOFError("incomplete inference frame")
        data.extend(chunk)
    return bytes(data)


def write_all(stream: BinaryIO, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        count = stream.write(remaining)
        if not isinstance(count, int) or count <= 0:
            raise OSError("inference output closed")
        remaining = remaining[count:]
    stream.flush()


def read_metadata(stream: BinaryIO) -> dict[str, Any]:
    size = struct.unpack("!I", read_exact(stream, 4))[0]
    if not 0 < size <= MAX_HEADER:
        raise RelayError(400, "invalid inference metadata size")
    try:
        value = decode_json(read_exact(stream, size))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RelayError(400, "invalid inference metadata") from exc
    if (not isinstance(value, dict) or type(value.get("version")) is not int
            or value["version"] != 2):
        raise RelayError(400, "unsupported inference protocol")
    return value


def write_metadata(stream: BinaryIO, status: int, headers: dict[str, str]) -> None:
    data = json.dumps({"version": 2, "status": status, "headers": headers},
                      separators=(",", ":")).encode()
    write_all(stream, struct.pack("!I", len(data)) + data)


def write_chunk(stream: BinaryIO, data: bytes) -> None:
    if len(data) > CHUNK_SIZE:
        raise OSError("oversized inference output chunk")
    write_all(stream, struct.pack("!I", len(data)) + data)


def send_error(stream: BinaryIO, status: int, message: str) -> None:
    write_metadata(stream, status, {"content-type": "application/json"})
    body = json.dumps({"type": "error", "error": {
        "type": "ccfleet_relay_error", "message": message}}).encode()
    write_chunk(stream, body)
    write_chunk(stream, b"")


def context_overflow(response: Any, headers: dict[str, str], upstream_socket: Any = None) -> bool:
    """Recognize only native context recovery, without reflecting error text."""
    length = getattr(response, "length", None)
    if (response.status != 400 or headers.get("content-type") != "application/json"
            or (type(length) is int and length >= MAX_ERROR_BODY)):
        return False
    raw = bytearray()
    deadline = time.monotonic() + ERROR_READ_TIMEOUT
    try:
        while len(raw) < MAX_ERROR_BODY:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if upstream_socket is not None:
                upstream_socket.settimeout(remaining)
            part = response.read1(min(CHUNK_SIZE, MAX_ERROR_BODY - len(raw)))
            if not part:
                if getattr(response, "length", None) not in (None, 0):
                    return False
                value = decode_json(bytes(raw))
                error = value.get("error") if isinstance(value, dict) else None
                if not isinstance(error, dict):
                    return False
                if error.get("type") == "context_window_exceeded":
                    return True
                message = error.get("message")
                prefix = "prompt is too long"
                return (error.get("type") == "invalid_request_error"
                        and isinstance(message, str)
                        and message.startswith(prefix)
                        and (len(message) == len(prefix)
                             or message[len(prefix)] in ": \t\r\n"))
            raw.extend(part)
        # The bounded buffer may be incomplete; never guess from a prefix.
        return False
    except (ValueError, UnicodeError, RecursionError, OSError, EOFError,
            http.client.HTTPException):
        return False


def send_upstream_error(stream: BinaryIO, status: int, headers: dict[str, str], *,
                        overflow: bool = False) -> None:
    """Return actionable categories, never provider bodies or account details.

    Status selects the public message, except a bounded JSON classification for
    native context-overflow recovery. No text from the body is returned. Native
    clients lose verbose provider diagnostics, but retain status, standard error
    categories, context recovery and a bounded numeric retry delay.
    """
    errors = {
        400: ("invalid_request_error", "The model request was rejected. Check the selected "
              "model, context size, and request options."),
        401: ("authentication_error", "The slot's Claude sign-in was rejected. Open your "
              "slot page to renew it."),
        403: ("permission_error", "The slot account cannot use this model or feature. "
              "Check its access on your slot page."),
        404: ("not_found_error", "The requested model or endpoint is unavailable. "
              "Choose an available model."),
        408: ("api_error", "The upstream model request timed out; the relay did not retry it."),
        409: ("invalid_request_error", "The model request conflicted with account state. "
              "Check your slot before trying again."),
        413: ("request_too_large", "The model request is too large. Start a shorter "
              "conversation or reduce input."),
        429: ("rate_limit_error", "The slot account is rate limited. Wait before sending "
              "a new request."),
        529: ("overloaded_error", "The upstream model service is overloaded. Try again later."),
    }
    if status == 400 and overflow:
        category = "invalid_request_error"
        message = "prompt is too long: reduce conversation context or start a new session."
    elif 300 <= status < 400:
        status = 502
        category, message = "api_error", "The upstream model redirect was refused."
    else:
        default = (("api_error", "The upstream model service failed. Try again later.")
                   if status >= 500 else
                   ("invalid_request_error", "The model request was rejected. Check your "
                    "selected model and request options."))
        category, message = errors.get(status, default)
    write_metadata(stream, status, {**headers, "content-type": "application/json"})
    write_chunk(stream, json.dumps({"type": "error", "error": {
        "type": category, "message": message}}, separators=(",", ":")).encode())
    write_chunk(stream, b"")


def require_enabled(policy_dir: Path = POLICY_DIR) -> None:
    name = pwd.getpwuid(os.getuid()).pw_name
    try:
        inference_policy.require_enabled(policy_dir, name)
    except inference_policy.PolicyError as exc:
        raise RelayError(403, "local inference is not enabled for this slot") from exc


def read_object(path: Path, limit: int = 64 * 1024) -> dict[str, Any]:
    """No symlink ancestors, special files or writes, including native credentials."""
    directory: Optional[int] = None
    try:
        if not path.is_absolute():
            raise ValueError("configuration path must be absolute")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(path.anchor, flags)
        for component in path.parts[1:-1]:
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_size > limit):
                raise ValueError("invalid configuration")
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("oversized configuration")
        value = decode_json(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, OSError, RecursionError):
        pass
    finally:
        if directory is not None:
            os.close(directory)
    raise RelayError(401, "slot sign-in is unavailable; use your slot page")


def bound_account(home: Path) -> str:
    profile = read_object(home / ".claude.json", 4 * 1024 * 1024).get("oauthAccount")
    state = read_object(home / ".config/ccfleet/slot-state.json")
    account = profile.get("accountUuid") if isinstance(profile, dict) else None
    if (not isinstance(account, str) or not 1 <= len(account.strip()) <= 256
            or not account.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in account)):
        raise RelayError(401, "the slot has no bound Claude account")
    fingerprint = hashlib.sha256(account.strip().encode()).hexdigest()[:16]
    if state.get("bound_fp") != fingerprint or state.get("account_restart"):
        raise RelayError(409, "slot account transition is pending; retry after it completes")
    return fingerprint


def credential(home: Path, now: float, *, minimum_remaining: float = 30) -> tuple[str, float]:
    bound_account(home)
    oauth = read_object(home / ".claude/.credentials.json").get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise RelayError(401, "sign in to Claude on your slot page")
    token, expiry = oauth.get("accessToken"), oauth.get("expiresAt")
    if (not isinstance(token, str) or not 1 <= len(token) <= 8192 or not token.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in token)):
        raise RelayError(401, "slot sign-in is invalid; use your slot page")
    try:
        seconds = expiry / 1000 if type(expiry) in (int, float) else float("nan")
    except OverflowError:
        seconds = float("nan")
    if not math.isfinite(seconds) or seconds <= now + minimum_remaining:
        raise RelayError(401, "slot sign-in needs renewal by native Claude Code")
    return token, seconds


def validate_request(meta: dict[str, Any]) -> tuple[str, dict[str, str], int]:
    if (set(meta) != {"version", "operation", "method", "path", "headers", "body_size"}
            or meta.get("operation") != "request" or meta.get("method") != "POST"
            or not isinstance(meta.get("path"), str) or meta["path"] not in PATHS):
        raise RelayError(400, "only fixed Claude message and token-count requests are supported")
    size = meta.get("body_size")
    if type(size) is not int or not 0 < size <= MAX_BODY:
        raise RelayError(413, "inference request exceeds the 64 MiB wire limit")
    supplied = meta.get("headers")
    if not isinstance(supplied, dict) or len(supplied) > 64:
        raise RelayError(400, "invalid inference headers")
    headers, seen = {}, set()
    for name, value in supplied.items():
        if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name)
                or not isinstance(value, str) or len(value) > 8192 or not value.isascii()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise RelayError(400, "invalid inference header")
        lowered = name.lower()
        if lowered in seen:
            raise RelayError(400, "duplicate inference header")
        seen.add(lowered)
        if lowered in REQUEST_HEADERS:
            headers[lowered] = value
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise RelayError(400, "Claude requests must be JSON")
    headers.update({"accept-encoding": "identity", "user-agent": USER_AGENT, "x-app": "cli"})
    return meta["path"], headers, size


def sanitize_body(raw: bytes) -> bytes:
    try:
        value = decode_json(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        if "metadata" in value and not isinstance(value["metadata"], (dict, type(None))):
            raise ValueError("invalid metadata")
        # API metadata identifies the client, not model instructions. Do not
        # inspect/rewrite identity-looking text inside messages, system or tools.
        value.pop("metadata", None)
        for field in IDENTITY_FIELDS:
            value.pop(field, None)
        clean = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                           allow_nan=False).encode()
        if len(clean) > MAX_BODY:
            raise RelayError(413, "sanitized inference request exceeds the wire limit")
        return clean
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, RelayError):
            raise
        raise RelayError(400, "Claude request must be valid unambiguous JSON") from exc


def connect_upstream() -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(UPSTREAM_HOST, 443, timeout=CONNECT_TIMEOUT,
                                       context=ssl.create_default_context())


def response_headers(response: Any) -> dict[str, str]:
    headers, seen = {}, set()
    for name, value in response.getheaders():
        lowered = name.lower()
        if lowered not in RESPONSE_HEADERS:
            continue
        if (lowered in seen or len(value) > 8192 or not value.isascii()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise RelayError(502, "upstream returned invalid response headers")
        seen.add(lowered)
        if lowered == "content-type":
            media_type = value.split(";", 1)[0].strip().lower()
            if media_type not in {"application/json", "text/event-stream"}:
                # Error pages are never forwarded, regardless of their type.
                if response.status >= 300:
                    continue
                raise RelayError(502, "upstream returned an unsupported response type")
            headers[lowered] = media_type
        elif len(value) <= 6 and value.isdigit() and int(value) <= MAX_RETRY_AFTER:
            headers[lowered] = str(int(value))
    # request-id is deliberately not forwarded: opaque identifiers can contain
    # account/credential text even when their syntax looks superficially safe.
    return headers


def _metrics_module():
    try:
        from . import relay_metrics
        return relay_metrics
    except ImportError:
        # Optional observability on the installed -I entrypoint: only a sibling
        # root-owned module is eligible, never cwd or a slot project module.
        spec = importlib.util.spec_from_file_location(
            "ccfleet_relay_metrics", Path(__file__).with_name("relay_metrics.py"))
        if spec is None or spec.loader is None:
            raise ImportError("metrics module unavailable") from None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _record_metrics(home: Path, account: str, outcome: str, timings: dict[str, float], *,
                    usage: Optional[dict[str, int]] = None, usage_eligible: bool = False) -> None:
    try:
        if bound_account(home) != account:
            return
        _metrics_module().record(home, account, outcome, timings, time.time(),
                                 still_bound=lambda: bound_account(home) == account,
                                 usage=usage, usage_eligible=usage_eligible)
    except Exception:
        # Metrics are best effort. Do not reflect exceptions, retry inference or
        # change an already completed response for a missing/failed collector.
        pass


def _outcome(status: int) -> str:
    return ({401: "auth_errors", 403: "permission_errors", 429: "rate_limits"}.get(status)
            or ("input_errors" if status in {400, 404, 409, 413} else "upstream_errors"))


def serve_one(input_: BinaryIO, output: BinaryIO, home: Path, *,
              policy: Callable[[], None] = require_enabled,
              connect: Callable[[], Any] = connect_upstream,
              request_ready: Callable[[], None] = lambda: None,
              watch: bool = False) -> int:
    connection = upstream_socket = None
    began = False
    account = None
    request_started = None
    timings: dict[str, float] = {}
    outcome = "connection_errors"
    usage_collector = None
    usage = None
    usage_eligible = False
    finished, cancelled = threading.Event(), threading.Event()
    try:
        policy()
        meta = read_metadata(input_)
        if meta.get("operation") == "request":
            request_started = time.monotonic()
        account = bound_account(home)
        if meta == {"version": 2, "operation": "status"}:
            credential(home, time.time())
            policy()
            if bound_account(home) != account:
                raise RelayError(409, "slot account changed while checking readiness")
            write_metadata(output, 200, {"content-type": "application/json"})
            write_chunk(output, b'{"ready":true,"protocol":2}')
            write_chunk(output, b"")
            return 0
        path, headers, size = validate_request(meta)
        body = sanitize_body(read_exact(input_, size))
        credential(home, time.time())
        policy()
        if bound_account(home) != account:
            raise RelayError(409, "slot account changed while opening inference")
        request_ready()
        connection = connect()
        deadline = time.monotonic() + REQUEST_TIMEOUT

        def stop_if_revoked() -> None:
            while not finished.wait(0.25):
                try:
                    policy()
                    # Native Claude can rotate this account's credential during
                    # a long response. Observe the current file, including
                    # logout/removal, instead of pinning the original expiry.
                    credential(home, time.time(), minimum_remaining=0)
                    valid = bound_account(home) == account
                    disconnected = bool(select.select([input_], [], [], 0)[0])
                except (RelayError, OSError, ValueError):
                    valid, disconnected = False, True
                if valid and not disconnected and time.monotonic() < deadline:
                    continue
                cancelled.set()
                socket_to_close = upstream_socket or getattr(connection, "sock", None)
                if socket_to_close is not None:
                    try:
                        socket_to_close.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                return

        if watch:
            threading.Thread(target=stop_if_revoked, daemon=True).start()
        connect_started = time.monotonic()
        connection.connect()
        timings["connect_ms"] = (time.monotonic() - connect_started) * 1000
        # HTTPConnection clears .sock for Connection: close responses, while
        # HTTPResponse still owns a blocking stream. Keep its cancellation handle.
        upstream_socket = getattr(connection, "sock", None)
        if cancelled.is_set() or bound_account(home) != account:
            raise RelayError(409, "slot inference was cancelled before sending")
        token, _ = credential(home, time.time())
        policy()
        if cancelled.is_set() or bound_account(home) != account:
            raise RelayError(409, "slot inference was cancelled before sending")
        headers["authorization"] = "Bearer " + token
        if upstream_socket is not None:
            upstream_socket.settimeout(READ_TIMEOUT)
        # Exactly one upstream POST. No retry, redirect following or alternative
        # account exists, including after an ambiguous connection failure.
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        if cancelled.is_set():
            raise OSError("inference cancelled")
        if type(response.status) is not int or not 200 <= response.status <= 599:
            raise RelayError(502, "upstream returned invalid HTTP status")
        headers = response_headers(response)
        if response.status >= 300:
            outcome = _outcome(response.status)
            overflow = context_overflow(response, headers, upstream_socket)
            if cancelled.is_set():
                raise OSError("inference cancelled")
            send_upstream_error(output, response.status, headers, overflow=overflow)
            return 2
        usage_eligible = path.split("?", 1)[0] == "/v1/messages"
        if usage_eligible:
            try:
                usage_collector = _metrics_module().UsageCollector(headers.get("content-type", ""))
            except Exception:
                pass
        write_metadata(output, response.status, headers)
        began = True
        while True:
            if cancelled.is_set():
                raise OSError("inference cancelled")
            data = response.read1(CHUNK_SIZE)
            if cancelled.is_set():
                raise OSError("inference cancelled")
            if not data:
                if getattr(response, "length", None) not in (None, 0):
                    raise OSError("incomplete upstream response")
                break
            if "first_byte_ms" not in timings and request_started is not None:
                timings["first_byte_ms"] = (time.monotonic() - request_started) * 1000
            write_chunk(output, data)
            if usage_collector is not None:
                try:
                    usage_collector.feed(data)
                except Exception:
                    usage_collector = None
        write_chunk(output, b"")
        outcome = "success"
        if usage_collector is not None:
            try:
                usage = usage_collector.finish()
            except Exception:
                pass
        return 0
    except RelayError as exc:
        outcome = "cancelled" if exc.status in {409, 504} else _outcome(exc.status)
        if not began:
            send_error(output, exc.status, str(exc))
        return 2
    except (OSError, EOFError, http.client.HTTPException):
        outcome = "connection_errors" if connection is not None else "input_errors"
        if not began:
            send_error(output, 502, "slot inference connection failed; no request was retried")
        return 2
    finally:
        finished.set()
        if request_started is not None and account is not None:
            timings["total_ms"] = (time.monotonic() - request_started) * 1000
            final_outcome = "cancelled" if cancelled.is_set() else outcome
            _record_metrics(home, account, final_outcome, timings,
                            usage=usage if final_outcome == "success" else None,
                            usage_eligible=usage_eligible and final_outcome == "success")
        if connection is not None:
            connection.close()


def main(argv: Optional[list[str]] = None) -> int:
    if argv != ["--protocol-v2"]:
        print("The old local relay is retired; update CC Fleet for the inference v2 protocol.",
              file=sys.stderr)
        return 2

    def expired(*args: Any) -> None:
        raise RelayError(504, "slot inference exceeded its connection deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(INPUT_TIMEOUT)
    try:
        return serve_one(sys.stdin.buffer, sys.stdout.buffer,
                         Path(pwd.getpwuid(os.getuid()).pw_dir), watch=True,
                         request_ready=lambda: signal.alarm(REQUEST_TIMEOUT))
    except (OSError, BrokenPipeError):
        return 2
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
