"""Pure, single-shot native ACP read handoff; disconnected from all transports.

The caller supplies a server-verified owner/key/generation binding and rechecks
its authorization/freshness on every frontend operation. Matching that opaque
binding here is not an account, lease, or file-access check. No tools, files,
commands, permissions or native processes are executed. Frontend text is copied
exactly; removal of any native Read formatting requires a separate verified
adapter. No ACP capabilities are advertised by this module.
"""

from __future__ import annotations

import hmac
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Union

MAX_RESULT_BYTES = 1024 * 1024
MAX_RESULT_FRAGMENTS = 128
MAX_PATH_BYTES = 4096
MAX_WAIT_SECONDS = 60
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
RPCID = Union[int, str]


class ReadBridgeError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


def _binding(value: Any) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 512 or not value.isascii()
            or any(not 32 <= ord(char) < 127 for char in value)):
        raise ReadBridgeError("invalid server binding")
    return value


def _session(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ReadBridgeError("invalid owned session ID")
    return value


def _rpc_id(value: Any) -> RPCID:
    if ((type(value) is int and abs(value) <= 1_000_000_000)
            or (isinstance(value, str) and _ID.fullmatch(value))):
        return value
    raise ReadBridgeError("invalid native request ID")


def _seconds(value: Any) -> float:
    if (type(value) not in (int, float) or not 0 < value <= MAX_WAIT_SECONDS
            or not math.isfinite(value)):
        raise ReadBridgeError("invalid remaining deadline")
    return float(value)


def _text_bytes(value: Any, maximum: int, name: str) -> int:
    if not isinstance(value, str):
        raise ReadBridgeError(f"invalid {name}")
    if len(value) > maximum:
        raise ReadBridgeError(f"{name} exceeds bound", "limit_exceeded")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ReadBridgeError(f"invalid Unicode in {name}") from exc
    if size > maximum:
        raise ReadBridgeError(f"{name} exceeds bound", "limit_exceeded")
    if any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in value):
        raise ReadBridgeError(f"invalid control character in {name}")
    return size


@dataclass(frozen=True)
class FrontendRead:
    tool_id: str
    path: str
    line: Optional[int]
    limit: Optional[int]

    def as_tool_use(self) -> dict[str, Any]:
        arguments: dict[str, Any] = {"file_path": self.path}
        if self.line is not None:
            arguments["offset"] = self.line
        if self.limit is not None:
            arguments["limit"] = self.limit
        return {"type": "tool_use", "id": self.tool_id, "name": "Read", "input": arguments}


class ReadHandoff:
    """One pending read and one delivery; a new object requires a new caller transaction."""

    def __init__(self, *, binding: str, session_id: str,
                 max_result_bytes: int = MAX_RESULT_BYTES):
        self._binding = _binding(binding)
        self._session = _session(session_id)
        if type(max_result_bytes) is not int or not 1 <= max_result_bytes <= MAX_RESULT_BYTES:
            raise ReadBridgeError("invalid result byte bound")
        self._max_result_bytes = max_result_bytes
        self._condition = threading.Condition()
        self._state = "new"
        self._request_id: Optional[RPCID] = None
        self._frontend: Optional[FrontendRead] = None
        self._deadline = 0.0
        self._response: Optional[dict[str, Any]] = None

    def _context(self, binding: str, session_id: str, request_id: RPCID) -> None:
        if not hmac.compare_digest(self._binding, _binding(binding)):
            raise ReadBridgeError("server binding mismatch", "binding_mismatch")
        if _session(session_id) != self._session:
            raise ReadBridgeError("native session mismatch", "session_mismatch")
        value = _rpc_id(request_id)
        if type(value) is not type(self._request_id) or value != self._request_id:
            raise ReadBridgeError("native request ID mismatch", "request_mismatch")

    def _expire(self) -> None:
        if self._state in ("pending", "resolved") and time.monotonic() >= self._deadline:
            self._state = "timed_out"
            self._response = None
            self._condition.notify_all()

    def _pending_state(self) -> None:
        self._expire()
        if self._state == "timed_out":
            raise ReadBridgeError("read handoff timed out", "timeout")
        if self._state == "cancelled":
            raise ReadBridgeError("read handoff cancelled", "cancelled")
        if self._state != "pending":
            raise ReadBridgeError("read handoff is not pending", "replay")

    def begin(self, request: dict[str, Any], *, remaining_seconds: float) -> FrontendRead:
        """Accept only a genuine ACP client-read RPC, never a tool-status notification."""
        seconds = _seconds(remaining_seconds)
        if (not isinstance(request, dict) or set(request) != {"jsonrpc", "id", "method", "params"}
                or request["jsonrpc"] != "2.0" or request["method"] != "fs/read_text_file"):
            raise ReadBridgeError("only native fs/read_text_file requests are supported")
        request_id = _rpc_id(request["id"])
        params = request["params"]
        if (not isinstance(params, dict) or not {"sessionId", "path"} <= params.keys()
                or params.keys() - {"sessionId", "path", "line", "limit"}):
            raise ReadBridgeError("invalid native read parameters")
        if _session(params["sessionId"]) != self._session:
            raise ReadBridgeError("native session mismatch", "session_mismatch")
        path = params["path"]
        _text_bytes(path, MAX_PATH_BYTES, "read path")
        if (not path.startswith("/") or path.startswith("//")
                or any(piece in (".", "..") for piece in path.split("/"))
                or any(ord(char) < 32 for char in path)):
            raise ReadBridgeError("read path must be an unambiguous absolute POSIX path")
        for name, maximum in (("line", 1_000_000_000), ("limit", 10000)):
            if name in params and (type(params[name]) is not int
                                   or not 1 <= params[name] <= maximum):
                raise ReadBridgeError("invalid read range")
        with self._condition:
            if self._state != "new":
                raise ReadBridgeError("read handoff cannot be reused", "replay")
            self._request_id = request_id
            self._frontend = FrontendRead("toolu_devin_read_" + secrets.token_hex(16), path,
                                          params.get("line"), params.get("limit"))
            self._deadline = time.monotonic() + seconds
            self._state = "pending"
            self._condition.notify_all()
            return self._frontend

    def pending(self, *, binding: str, session_id: str, request_id: RPCID) -> FrontendRead:
        with self._condition:
            self._context(binding, session_id, request_id)
            self._pending_state()
            return self._frontend

    def resolve(self, tool_result: dict[str, Any], *, binding: str,
                session_id: str, request_id: RPCID) -> None:
        """Resolve exactly one matching frontend result; never execute or authorize a read."""
        with self._condition:
            self._context(binding, session_id, request_id)
            self._pending_state()
            if (not isinstance(tool_result, dict)
                    or not {"type", "tool_use_id", "content"} <= tool_result.keys()
                    or tool_result.keys() - {"type", "tool_use_id", "content", "is_error"}
                    or tool_result["type"] != "tool_result"
                    or tool_result["tool_use_id"] != self._frontend.tool_id):
                raise ReadBridgeError("frontend tool result does not match pending read",
                                      "tool_mismatch")
            if "is_error" in tool_result and type(tool_result["is_error"]) is not bool:
                raise ReadBridgeError("invalid frontend error flag")
            content = tool_result["content"]
            if isinstance(content, str):
                pieces = [content]
            elif isinstance(content, list):
                if len(content) > MAX_RESULT_FRAGMENTS:
                    raise ReadBridgeError("read result fragment count exceeds bound",
                                          "limit_exceeded")
                pieces = []
                for block in content:
                    if (not isinstance(block, dict) or set(block) != {"type", "text"}
                            or block["type"] != "text"):
                        raise ReadBridgeError("only plain frontend result text is supported")
                    pieces.append(block["text"])
            else:
                raise ReadBridgeError("invalid frontend read content")
            size = 0
            for piece in pieces:
                size += _text_bytes(piece, self._max_result_bytes - size, "read result")
            self._pending_state()  # Validation must not consume the remaining deadline.
            response: dict[str, Any] = {"jsonrpc": "2.0", "id": self._request_id}
            if tool_result.get("is_error", False):
                response["error"] = {"code": -32000, "message": "Frontend read failed"}
            else:
                response["result"] = {"content": "".join(pieces)}
            self._response = response
            self._state = "resolved"
            self._condition.notify_all()

    def wait(self) -> dict[str, Any]:
        """Wait in the future native-controller thread; return its single RPC response."""
        with self._condition:
            if self._state == "new":
                raise ReadBridgeError("begin the native read before waiting", "invalid_state")
            self._expire()
            while self._state == "pending":
                self._expire()
                if self._state == "pending":
                    self._condition.wait(max(0, self._deadline - time.monotonic()))
            if self._state == "resolved":
                response, self._response = self._response, None
                self._state = "delivered"
                return response
            self._pending_state()
            raise ReadBridgeError("read response was already delivered", "replay")

    def cancel(self, *, binding: str, session_id: str, request_id: RPCID) -> None:
        with self._condition:
            self._context(binding, session_id, request_id)
            self._expire()
            if self._state not in ("pending", "resolved"):
                self._pending_state()
            self._response = None
            self._state = "cancelled"
            self._condition.notify_all()
