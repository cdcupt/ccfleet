"""Disconnected, bounded supervision of an explicitly allowlisted native Devin ACP.

No credentials are read or logged. The caller owns account/environment isolation
and must use the context manager or close(). This is not an OS sandbox: native
agent internals and descendants need separate containment before a real prompt.
Session creation and prompts are disabled by default. Client capabilities are
empty, permission requests cancel, and delegated actions are never executed.
Notifications are opaque ACP observations, not Anthropic tools or usage counts.
Trusted profiles may explicitly ignore up to eight underscore-prefixed vendor
notification names. Their parameters are discarded; requests are never ignored.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import select
import selectors
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Optional

MAX_LINE_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_NOTIFICATIONS = 128
MAX_RPC_MESSAGES = 2048
MAX_PROMPT_BYTES = 64 * 1024
MAX_PROMPT_LIMIT = 256 * 1024
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")


class ACPError(ValueError):
    def __init__(self, message: str, code: str = "protocol_error"):
        super().__init__(message)
        self.code = code


def _text(value: Any, name: str, maximum: int = MAX_PROMPT_BYTES) -> str:
    if not isinstance(value, str) or not value:
        raise ACPError(f"invalid {name}", "invalid_config")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ACPError(f"invalid {name}", "invalid_config") from exc
    if size > maximum or any(ord(c) < 32 and c not in "\t\r\n" for c in value):
        raise ACPError(f"invalid {name}", "invalid_config")
    return value


def _limit(value: Any, low: int, high: int, name: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ACPError(f"invalid {name}", "invalid_config")
    return value


def _check_json(value: Any, depth: int = 0) -> None:
    if depth > 48:
        raise ACPError("ACP JSON nesting exceeds the bound", "limit_exceeded")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if value.bit_length() > 3402:
            raise ACPError("ACP integer exceeds the bound", "limit_exceeded")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ACPError("nonfinite ACP number")
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise ACPError("invalid ACP Unicode") from exc
    elif isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ACPError("invalid ACP JSON key")
            _check_json(key, depth + 1)
            _check_json(item, depth + 1)
    else:
        raise ACPError("invalid ACP JSON value")


def _encode(value: dict[str, Any]) -> bytes:
    _check_json(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8") + b"\n"


def _decode(raw: bytes) -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ACPError("duplicate ACP JSON field")
            result[key] = value
        return result

    def integer(value):
        if len(value) - (1 if value.startswith("-") else 0) > 1024:
            raise ACPError("ACP integer exceeds the bound", "limit_exceeded")
        return int(value)

    def nonfinite(_value):
        raise ACPError("nonfinite ACP number")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_int=integer, parse_constant=nonfinite)
        _check_json(value)
    except ACPError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ACPError("malformed ACP JSON") from exc
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise ACPError("invalid ACP JSON-RPC envelope")
    return value


class ACPController:
    """One native process, serialized requests, no shell/retry/fallback or tool execution."""

    def __init__(self, executable: str, *, model: str, cwd: str,
                 allowed_executables: Iterable[str], verified_models: Iterable[str],
                 allow_model_requests: bool = False, timeout_s: float = 5,
                 max_line_bytes: int = MAX_LINE_BYTES, max_output_bytes: int = MAX_OUTPUT_BYTES,
                 max_notifications: int = MAX_NOTIFICATIONS,
                 max_prompt_bytes: int = MAX_PROMPT_BYTES,
                 ignored_notifications: Iterable[str] = (),
                 env: Optional[Mapping[str, str]] = None):
        target = Path(executable)
        if not target.is_absolute() or ".." in target.parts:
            raise ACPError("executable must be an absolute allowlisted path", "invalid_config")
        try:
            allowed = set()
            for item in allowed_executables:
                path = Path(item)
                if not path.is_absolute() or ".." in path.parts:
                    raise ACPError("invalid executable allowlist", "invalid_config")
                allowed.add(path.resolve(strict=True))
            target = target.resolve(strict=True)
            info = target.stat()
            directory = Path(cwd)
            if not directory.is_absolute() or ".." in directory.parts:
                raise ACPError("cwd must be absolute", "invalid_config")
            directory = directory.resolve(strict=True)
        except (OSError, TypeError) as exc:
            raise ACPError("invalid native ACP paths", "invalid_config") from exc
        if (target not in allowed or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o002
                or not os.access(target, os.X_OK) or not directory.is_dir()):
            raise ACPError("native ACP executable or cwd is not allowed", "invalid_config")
        if (not isinstance(verified_models, (list, tuple, set, frozenset))
                or not verified_models or len(verified_models) > 4096
                or not isinstance(model, str)
                or not _MODEL.fullmatch(model) or model not in verified_models):
            raise ACPError("model must be an explicitly verified UID", "invalid_config")
        if type(allow_model_requests) is not bool:
            raise ACPError("invalid model request guard", "invalid_config")
        if allow_model_requests and env is None:
            raise ACPError("model-enabled ACP requires an explicit environment", "invalid_config")
        if (type(timeout_s) not in (int, float) or not 0.02 <= timeout_s <= 60
                or not math.isfinite(timeout_s)):
            raise ACPError("invalid ACP timeout", "invalid_config")
        if env is not None and (not isinstance(env, Mapping) or any(
                not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
                or not isinstance(v, str) or "\x00" in v for k, v in env.items())):
            raise ACPError("invalid native ACP environment", "invalid_config")
        if (not isinstance(ignored_notifications, (list, tuple, set, frozenset))
                or len(ignored_notifications) > 8 or any(
                    not isinstance(name, str) or not re.fullmatch(r"_[A-Za-z0-9_/$.-]{1,99}", name)
                    for name in ignored_notifications)):
            raise ACPError("invalid ignored notification allowlist", "invalid_config")
        self.executable, self.model, self.cwd = str(target), model, str(directory)
        self.allow_model_requests = allow_model_requests
        self.timeout_s = float(timeout_s)
        self.max_line_bytes = _limit(max_line_bytes, 128, MAX_LINE_BYTES, "line limit")
        self.max_output_bytes = _limit(max_output_bytes, 256, MAX_OUTPUT_BYTES, "output limit")
        self.max_notifications = _limit(max_notifications, 1, MAX_NOTIFICATIONS,
                                        "notification limit")
        self.max_prompt_bytes = _limit(max_prompt_bytes, 1, MAX_PROMPT_LIMIT, "prompt limit")
        self.env = None if env is None else dict(env)
        self._ignored_notifications = frozenset(ignored_notifications)
        self._process: Optional[subprocess.Popen] = None
        self._selector: Optional[selectors.BaseSelector] = None
        self._buffer = bytearray()
        self._total = self._messages = self._next_id = 0
        self._pending: Optional[int] = None
        self._pending_method: Optional[str] = None
        self._response: Optional[dict[str, Any]] = None
        self._answered: set[int] = set()
        self._agent_ids: set[tuple[str, Any]] = set()
        self._sessions: set[str] = set()
        self._notifications: list[dict[str, Any]] = []
        self._provisional_notifications: list[dict[str, Any]] = []
        self._provisional_session_id: Optional[str] = None
        self.rejected_methods: list[str] = []
        self._initialized = self._closed = False
        self._call_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process is not None else None

    def _start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise ACPError("ACP controller is closed", "closed")
            if self._process is not None:
                return
            try:
                process = subprocess.Popen([self.executable, "acp", "--model", self.model],
                                           cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True, bufsize=0)
            except OSError as exc:
                self._closed = True
                raise ACPError("native ACP launch failed", "launch_failed") from exc
            self._process = process
            self._selector = selectors.DefaultSelector()
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            self._selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            self._selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    def _write(self, value: dict[str, Any], deadline: float) -> None:
        raw = _encode(value)
        if len(raw) > self.max_line_bytes:
            raise ACPError("ACP request exceeds line bound", "limit_exceeded")
        descriptor = self._process.stdin.fileno()
        while raw:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [descriptor], [], max(0, remaining))[1]:
                raise ACPError("ACP write timed out", "timeout")
            try:
                size = os.write(descriptor, raw)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise ACPError("ACP input closed", "process_exited") from exc
            if not size:
                raise ACPError("ACP input closed", "process_exited")
            raw = raw[size:]

    def _handle(self, value: dict[str, Any], deadline: float) -> None:
        self._messages += 1
        if self._messages > MAX_RPC_MESSAGES:
            raise ACPError("ACP message count exceeds bound", "limit_exceeded")
        if "method" in value:
            if set(value) - {"jsonrpc", "id", "method", "params"}:
                raise ACPError("invalid ACP method envelope")
            method = value["method"]
            if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z0-9_/$.-]{1,100}", method):
                raise ACPError("invalid ACP method")
            params = value.get("params", {})
            if not isinstance(params, dict):
                raise ACPError("invalid ACP method parameters")
            if "id" in value:
                rpc_id = value["id"]
                if not ((type(rpc_id) is int and abs(rpc_id) <= 1_000_000_000)
                        or (isinstance(rpc_id, str) and _ID.fullmatch(rpc_id))):
                    raise ACPError("invalid agent request ID")
                identity = (type(rpc_id).__name__, rpc_id)
                if identity in self._agent_ids:
                    raise ACPError("duplicate agent request ID")
                self._agent_ids.add(identity)
                self.rejected_methods.append(method)
                if method == "session/request_permission":
                    self._write({"jsonrpc": "2.0", "id": rpc_id,
                                 "result": {"outcome": {"outcome": "cancelled"}}}, deadline)
                else:
                    self._write({"jsonrpc": "2.0", "id": rpc_id, "error": {
                        "code": -32601, "message": "Client method unsupported"}}, deadline)
                return
            if method in self._ignored_notifications:
                # This is a notification only: parameters were validated as an
                # object above, but are neither retained nor interpreted. Output
                # and message accounting still happened before this point.
                return
            if method != "session/update" or not self.allow_model_requests:
                raise ACPError("unsolicited or unsupported ACP notification", "unsupported_method")
            session_id = params.get("sessionId")
            if (not isinstance(session_id, str) or not _ID.fullmatch(session_id)
                    or not isinstance(params.get("update"), dict)):
                raise ACPError("notification does not belong to an active session")
            if (len(self._notifications) + len(self._provisional_notifications)
                    >= self.max_notifications):
                raise ACPError("ACP notification queue exceeds bound", "limit_exceeded")
            if session_id not in self._sessions:
                if self._pending_method != "session/new" or self._response is not None:
                    raise ACPError("notification does not belong to an active session")
                if (self._provisional_session_id is not None
                        and self._provisional_session_id != session_id):
                    raise ACPError("multiple provisional ACP session IDs")
                self._provisional_session_id = session_id
                self._provisional_notifications.append(params)
                return
            self._notifications.append(params)
            return
        if (set(value) - {"jsonrpc", "id", "result", "error"}
                or ("result" in value) == ("error" in value)):
            raise ACPError("invalid ACP response envelope")
        rpc_id = value.get("id")
        if type(rpc_id) is not int or rpc_id != self._pending or rpc_id in self._answered:
            raise ACPError("unknown or duplicate ACP response ID")
        if self._pending_method == "session/new" and "result" in value:
            result = value["result"]
            session_id = result.get("sessionId") if isinstance(result, dict) else None
            if (not isinstance(session_id, str) or not _ID.fullmatch(session_id)
                    or session_id in self._sessions):
                raise ACPError("invalid or duplicate ACP session ID")
            if (self._provisional_session_id is not None
                    and self._provisional_session_id != session_id):
                raise ACPError("provisional ACP session ID did not match response")
            # Early observations remain quarantined until the response binds
            # their single session ID. No notification creates a session itself.
            self._sessions.add(session_id)
            self._notifications.extend(self._provisional_notifications)
            self._provisional_notifications = []
            self._provisional_session_id = None
        self._answered.add(rpc_id)
        self._response = value

    def _read(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ACPError("ACP response timed out", "timeout")
        events = self._selector.select(remaining)
        if not events:
            raise ACPError("ACP response timed out", "timeout")
        for key, _mask in events:
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                self._selector.unregister(key.fileobj)
                if key.data == "stdout":
                    raise ACPError("ACP output ended before request completed", "process_exited")
                continue
            self._total += len(chunk)
            if self._total > self.max_output_bytes:
                raise ACPError("ACP output exceeds bound", "limit_exceeded")
            if key.data == "stderr":
                continue  # Count then discard; never retain potential credential/error text.
            self._buffer.extend(chunk)
            while b"\n" in self._buffer:
                position = self._buffer.index(10)
                if position + 1 > self.max_line_bytes:
                    raise ACPError("ACP output line exceeds bound", "limit_exceeded")
                raw = bytes(self._buffer[:position]).removesuffix(b"\r")
                del self._buffer[:position + 1]
                if not raw:
                    raise ACPError("empty ACP JSON line")
                self._handle(_decode(raw), deadline)
            if len(self._buffer) >= self.max_line_bytes:
                raise ACPError("ACP output line exceeds bound", "limit_exceeded")

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._call_lock.acquire(blocking=False):
            raise ACPError("concurrent ACP calls are unsupported", "busy")
        try:
            self._start()
            self._next_id += 1
            self._pending, self._response = self._next_id, None
            self._pending_method = method
            deadline = time.monotonic() + self.timeout_s
            self._write({"jsonrpc": "2.0", "id": self._pending,
                         "method": method, "params": params}, deadline)
            while self._response is None:
                self._read(deadline)
            response = self._response
            if "error" in response:
                raise ACPError("native ACP request failed", "rpc_error")
            if not isinstance(response["result"], dict):
                raise ACPError("invalid native ACP result")
            return response["result"]
        except BaseException:
            self.close()
            raise
        finally:
            if method == "session/new":
                self._provisional_notifications = []
                self._provisional_session_id = None
            self._pending = None
            self._pending_method = None
            self._call_lock.release()

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            raise ACPError("ACP was already initialized", "invalid_state")
        result = self._rpc("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                          "clientInfo": {"name": "ccfleet", "version": "0.1.0"}})
        if type(result.get("protocolVersion")) is not int or result["protocolVersion"] != 1:
            self.close()
            raise ACPError("native ACP version mismatch", "version_mismatch")
        self._initialized = True
        return result

    def _model_guard(self) -> None:
        if not self.allow_model_requests:
            raise ACPError("native ACP session/model requests are disabled",
                           "model_requests_disabled")
        if self.env is None:
            raise ACPError("model-enabled ACP requires an explicit environment", "invalid_config")
        if not self._initialized:
            raise ACPError("initialize ACP first", "invalid_state")

    def new_session(self) -> dict[str, Any]:
        self._model_guard()
        result = self._rpc("session/new", {"cwd": self.cwd, "mcpServers": []})
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
            self.close()
            raise ACPError("invalid ACP session ID")
        return result

    def prompt(self, session_id: str, text: str) -> dict[str, Any]:
        self._model_guard()
        if not isinstance(session_id, str) or session_id not in self._sessions:
            raise ACPError("unknown ACP session", "invalid_state")
        text = _text(text, "prompt", self.max_prompt_bytes)
        result = self._rpc("session/prompt", {"sessionId": session_id,
                                              "prompt": [{"type": "text", "text": text}]})
        if result.get("stopReason") not in (
                "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"):
            self.close()
            raise ACPError("invalid or unsupported ACP prompt stop reason")
        return result

    def drain_notifications(self) -> list[dict[str, Any]]:
        result, self._notifications = self._notifications, []
        return result

    def close(self) -> None:
        with self._lifecycle_lock:
            self._close_owned()

    def _close_owned(self) -> None:
        self._provisional_notifications = []
        self._provisional_session_id = None
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None:
            # Never poll/reap the leader before signalling its owned group. Even
            # an exited leader remains our unreaped PID while descendants exit.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                    time.sleep(0.05)
                    os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    stream.close()
        if self._selector is not None:
            self._selector.close()

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *_args):
        self.close()
