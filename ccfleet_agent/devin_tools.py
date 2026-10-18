"""Pure frozen Claude-tool catalog and single-shot MCP tool handoff.

The caller owns the actual MCP connection and supplies an already verified
owner/key/generation binding, native session, and server/connection identity.
Matching these opaque values is not authentication or account/lease validation;
the caller must recheck authorization on every operation. The catalog remains
fixed for that native prompt lifetime. A caller must serialize handoffs on its
connection; this object accepts one invocation and delivers at most one result.

No processes, tools, files, callbacks or transports are executed. ACP tool-status
notifications are not invocations. Only declared function tools and plain text
results are represented. Schemas/arguments are frozen as bounded JSON, not
semantically validated by a JSON Schema engine. Transport negotiation, native
builtin containment, history/system mapping and model accounting are separate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

MAX_TOOLS = 128
MAX_DESCRIPTION_BYTES = 64 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_JSON_TEXT_BYTES = 1024 * 1024
MAX_JSON_NODES = 50000
MAX_JSON_DEPTH = 48
MAX_INTEGER_BITS = 3402
MAX_RESULT_BYTES = 1024 * 1024
MAX_RESULT_FRAGMENTS = 128
MAX_WAIT_SECONDS = 60
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
RPCID = Union[int, str]


class ToolBridgeError(ValueError):
    def __init__(self, message: str, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code


def _text_size(value: Any, maximum: int, label: str) -> int:
    if not isinstance(value, str):
        raise ToolBridgeError(f"invalid {label}")
    if len(value) > maximum:
        raise ToolBridgeError(f"{label} exceeds bound", "limit_exceeded")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ToolBridgeError(f"invalid Unicode in {label}") from exc
    if size > maximum:
        raise ToolBridgeError(f"{label} exceeds bound", "limit_exceeded")
    return size


def _structure(value: Any, budget: list[int], depth: int = 0) -> None:
    budget[0] += 1
    if budget[0] > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
        raise ToolBridgeError("JSON structure exceeds bound", "limit_exceeded")
    if isinstance(value, str):
        size = _text_size(value, MAX_JSON_TEXT_BYTES, "JSON string")
        budget[1] += size
        budget[2] += size
        if budget[1] > MAX_JSON_TEXT_BYTES:
            raise ToolBridgeError("JSON text exceeds bound", "limit_exceeded")
    elif value is None or type(value) is bool:
        budget[2] += 5
    elif type(value) is int:
        if value.bit_length() > MAX_INTEGER_BITS:
            raise ToolBridgeError("JSON integer exceeds bound", "limit_exceeded")
        budget[2] += len(str(value))
    elif type(value) is float:
        if not math.isfinite(value):
            raise ToolBridgeError("nonfinite JSON number")
        budget[2] += len(repr(value))
    elif isinstance(value, list):
        for item in value:
            _structure(item, budget, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ToolBridgeError("invalid JSON object key")
            _structure(key, budget, depth + 1)
            _structure(item, budget, depth + 1)
    else:
        raise ToolBridgeError("unsupported JSON value")
    if budget[2] > MAX_JSON_BYTES:
        raise ToolBridgeError("JSON scalar data exceeds bound", "limit_exceeded")


def _snapshot(value: Any) -> str:
    _structure(value, [0, 0, 0])
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ToolBridgeError("JSON encoding exceeds bound", "limit_exceeded")
    return encoded


def _binding(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or not value.isascii()
        or any(not 32 <= ord(char) < 127 for char in value)
    ):
        raise ToolBridgeError("invalid server binding")
    return value


def _identity(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ToolBridgeError("invalid server/session identity")
    return value


def _rpc_id(value: Any) -> RPCID:
    if (type(value) is int and abs(value) <= 1_000_000_000) or (
        isinstance(value, str) and _ID.fullmatch(value)
    ):
        return value
    raise ToolBridgeError("invalid native request ID")


def _seconds(value: Any) -> float:
    if (
        type(value) not in (int, float)
        or not 0 < value <= MAX_WAIT_SECONDS
        or not math.isfinite(value)
    ):
        raise ToolBridgeError("invalid remaining deadline")
    return float(value)


@dataclass(frozen=True, init=False)
class ToolCatalog:
    """Freeze exact declaration order/content; JSON key order does not affect its digest."""

    _tools_json: str = field(repr=False)
    _names: frozenset[str] = field(repr=False)
    schema_digest: str

    def __init__(self, tools: list[dict[str, Any]]):
        if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS:
            raise ToolBridgeError("invalid declared tool count", "invalid_catalog")
        # Bound every supplied field before copying, even fields rejected below.
        _snapshot(tools)
        names, mapped = set(), []
        for tool in tools:
            if not isinstance(tool, dict) or set(tool) != {"name", "description", "input_schema"}:
                raise ToolBridgeError(
                    "only declared function tools are supported", "invalid_catalog"
                )
            name = tool["name"]
            if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
                raise ToolBridgeError("invalid or duplicate declared tool name", "invalid_catalog")
            _text_size(tool["description"], MAX_DESCRIPTION_BYTES, "tool description")
            schema = tool["input_schema"]
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ToolBridgeError(
                    "tool input schema must describe an object", "invalid_catalog"
                )
            names.add(name)
            mapped.append({"name": name, "description": tool["description"], "inputSchema": schema})
        frozen = _snapshot(mapped)
        object.__setattr__(self, "_tools_json", frozen)
        object.__setattr__(self, "_names", frozenset(names))
        object.__setattr__(
            self, "schema_digest", hashlib.sha256(frozen.encode("utf-8")).hexdigest()
        )

    def tools_list(self) -> dict[str, Any]:
        """Return a detached MCP tools/list result; the transport supplies its RPC envelope."""
        return {"tools": json.loads(self._tools_json)}


@dataclass(frozen=True)
class FrontendTool:
    tool_id: str
    name: str
    _arguments_json: str = field(repr=False)

    def as_tool_use(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "id": self.tool_id,
            "name": self.name,
            "input": json.loads(self._arguments_json),
        }


class ToolHandoff:
    """One genuine MCP call, one pending frontend result, and at most one delivery."""

    def __init__(
        self,
        catalog: ToolCatalog,
        *,
        binding: str,
        session_id: str,
        server_id: str,
        max_result_bytes: int = MAX_RESULT_BYTES,
    ):
        if not isinstance(catalog, ToolCatalog):
            raise ToolBridgeError("invalid frozen tool catalog", "invalid_catalog")
        if type(max_result_bytes) is not int or not 1 <= max_result_bytes <= MAX_RESULT_BYTES:
            raise ToolBridgeError("invalid result byte bound")
        self._catalog = catalog
        self._binding = _binding(binding)
        self._session = _identity(session_id)
        self._server = _identity(server_id)
        self._max_result_bytes = max_result_bytes
        self._condition = threading.Condition()
        self._state = "new"
        self._request_id: Optional[RPCID] = None
        self._frontend: Optional[FrontendTool] = None
        self._deadline = 0.0
        self._response_json: Optional[str] = None

    def _context(self, binding: str, session_id: str, server_id: str) -> None:
        if not hmac.compare_digest(self._binding, _binding(binding)):
            raise ToolBridgeError("server binding mismatch", "binding_mismatch")
        if _identity(session_id) != self._session:
            raise ToolBridgeError("native session mismatch", "session_mismatch")
        if _identity(server_id) != self._server:
            raise ToolBridgeError("MCP connection mismatch", "server_mismatch")

    def _request_context(
        self, binding: str, session_id: str, server_id: str, request_id: RPCID
    ) -> None:
        self._context(binding, session_id, server_id)
        value = _rpc_id(request_id)
        if type(value) is not type(self._request_id) or value != self._request_id:
            raise ToolBridgeError("native request ID mismatch", "request_mismatch")

    def _expire(self) -> None:
        if self._state in ("pending", "resolved") and time.monotonic() >= self._deadline:
            self._state = "timed_out"
            self._response_json = None
            self._condition.notify_all()

    def _pending_state(self) -> None:
        self._expire()
        if self._state == "timed_out":
            raise ToolBridgeError("tool handoff timed out", "timeout")
        if self._state == "cancelled":
            raise ToolBridgeError("tool handoff cancelled", "cancelled")
        if self._state != "pending":
            raise ToolBridgeError("tool handoff is not pending", "replay")

    def begin(
        self,
        request: dict[str, Any],
        *,
        binding: str,
        session_id: str,
        server_id: str,
        remaining_seconds: float,
    ) -> FrontendTool:
        """Accept tools/call only; an ACP tool-status notification can never reach execution."""
        deadline = time.monotonic() + _seconds(remaining_seconds)
        with self._condition:
            self._context(binding, session_id, server_id)
            if self._state != "new":
                raise ToolBridgeError("tool handoff cannot be reused", "replay")
            frozen = json.loads(_snapshot(request))
            if (
                not isinstance(frozen, dict)
                or set(frozen) != {"jsonrpc", "id", "method", "params"}
                or frozen["jsonrpc"] != "2.0"
                or frozen["method"] != "tools/call"
            ):
                raise ToolBridgeError("only genuine MCP tools/call requests are supported")
            request_id = _rpc_id(frozen["id"])
            params = frozen["params"]
            if (
                not isinstance(params, dict)
                or set(params) != {"name", "arguments"}
                or not isinstance(params["name"], str)
                or params["name"] not in self._catalog._names
                or not isinstance(params["arguments"], dict)
            ):
                raise ToolBridgeError("call must name a declared tool with object arguments")
            frontend = FrontendTool(
                "toolu_devin_mcp_" + secrets.token_hex(16),
                params["name"],
                _snapshot(params["arguments"]),
            )
            if time.monotonic() >= deadline:
                raise ToolBridgeError("tool admission deadline elapsed", "timeout")
            self._request_id = request_id
            self._frontend = frontend
            self._deadline = deadline
            self._state = "pending"
            self._condition.notify_all()
            return self._frontend

    def pending(
        self, *, binding: str, session_id: str, server_id: str, request_id: RPCID
    ) -> FrontendTool:
        with self._condition:
            self._request_context(binding, session_id, server_id, request_id)
            self._pending_state()
            return self._frontend

    def resolve(
        self,
        tool_result: dict[str, Any],
        *,
        binding: str,
        session_id: str,
        server_id: str,
        request_id: RPCID,
    ) -> None:
        """Copy matching plain text exactly; frontend tool failure stays MCP isError."""
        with self._condition:
            self._request_context(binding, session_id, server_id, request_id)
            self._pending_state()
            frozen = json.loads(_snapshot(tool_result))
            if (
                not isinstance(frozen, dict)
                or not {"type", "tool_use_id", "content"} <= frozen.keys()
                or frozen.keys() - {"type", "tool_use_id", "content", "is_error"}
                or frozen["type"] != "tool_result"
                or frozen["tool_use_id"] != self._frontend.tool_id
            ):
                raise ToolBridgeError(
                    "frontend tool result does not match pending call", "tool_mismatch"
                )
            if "is_error" in frozen and type(frozen["is_error"]) is not bool:
                raise ToolBridgeError("invalid frontend tool error flag")
            content = frozen["content"]
            if isinstance(content, str):
                pieces = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                if len(content) > MAX_RESULT_FRAGMENTS:
                    raise ToolBridgeError(
                        "tool result fragment count exceeds bound", "limit_exceeded"
                    )
                pieces = content
                if any(
                    not isinstance(piece, dict)
                    or set(piece) != {"type", "text"}
                    or piece["type"] != "text"
                    for piece in pieces
                ):
                    raise ToolBridgeError("only plain text tool results are supported")
            else:
                raise ToolBridgeError("invalid frontend tool content")
            size = 0
            for piece in pieces:
                size += _text_size(piece["text"], self._max_result_bytes - size, "tool result")
            response = _snapshot(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "result": {"content": pieces, "isError": frozen.get("is_error", False)},
                }
            )
            self._pending_state()  # All validation/copying consumes the original deadline.
            self._response_json = response
            self._state = "resolved"
            self._condition.notify_all()

    def wait(
        self, *, binding: str, session_id: str, server_id: str, request_id: RPCID
    ) -> dict[str, Any]:
        """Deliver one detached response; the caller alone writes the original MCP connection."""
        with self._condition:
            self._request_context(binding, session_id, server_id, request_id)
            self._expire()
            while self._state == "pending":
                self._condition.wait(max(0, self._deadline - time.monotonic()))
                self._expire()
            if self._state == "resolved":
                response = json.loads(self._response_json)
                self._expire()  # Decoding must not permit late delivery.
                if self._state == "resolved":
                    self._response_json = None
                    self._state = "delivered"
                    return response
            self._pending_state()
            raise ToolBridgeError("tool response was already delivered", "replay")

    def cancel(self, *, binding: str, session_id: str, server_id: str, request_id: RPCID) -> None:
        with self._condition:
            self._request_context(binding, session_id, server_id, request_id)
            self._expire()
            if self._state not in ("pending", "resolved"):
                self._pending_state()
            self._response_json = None
            self._state = "cancelled"
            self._condition.notify_all()
