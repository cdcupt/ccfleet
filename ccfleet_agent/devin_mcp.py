"""One-shot stdio MCP handoff for frozen caller-declared local tools.

The execution UID owns run-dir (0700), config.json and final session.json (0600).
config.json is immutable; session.json binds the real ACP session before prompt.
The parent must kill/reap native Devin and this shim before exposing call.json's
frontend_tool_use. No credentials or target-file contents are read by this shim.
No tools, credentials or user target files are executed/read on the node.
Stdout contains JSON-RPC only while serving. Default CLI mode prints a plan;
an explicit private --run-dir starts stdio, and --hook denies native builtins.
Hooks grant only exact catalog tool names, server-scoped discovery, and the
strict native MCP wrapper shape. Tool arguments are bounded JSON; this module
does not implement JSON Schema validation or the local client's permission UI.
The caller owns native builtin containment, connection/auth/generation checks,
readiness gating, source integrity, and process-group cleanup before delivery.
Version negotiation follows the 2025-11-25 MCP lifecycle specification:
https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import importlib.util
import io
import json
import math
import os
import re
import secrets
import select
import signal
import stat
import sys
import threading
import time
from pathlib import Path

if __package__:
    from .devin_tools import ToolBridgeError, ToolCatalog, ToolHandoff
else:
    # The native adapter runs this exact pinned file with Python -I. Import its
    # pinned sibling directly, never cwd/PYTHONPATH/user-site packages.
    _tools_spec = importlib.util.spec_from_file_location(
        "ccfleet_pinned_devin_tools", Path(__file__).resolve().with_name("devin_tools.py"))
    _tools_module = importlib.util.module_from_spec(_tools_spec)
    sys.modules[_tools_spec.name] = _tools_module
    # A source hash does not authenticate a pre-existing bytecode cache.
    _tools_source = Path(_tools_spec.origin).read_bytes()
    exec(compile(_tools_source, _tools_spec.origin, "exec"), _tools_module.__dict__)
    ToolBridgeError = _tools_module.ToolBridgeError
    ToolCatalog = _tools_module.ToolCatalog
    ToolHandoff = _tools_module.ToolHandoff

MAX_FILE = 2 * 1024 * 1024 + 4096
MAX_LINE = MAX_FILE + 4096
MAX_TOTAL = 8 * 1024 * 1024
MAX_MESSAGES = 64
MAX_NODES = 50000
MAX_DEPTH = 64
CALL_WAIT_S = 45
SERVER_LIFETIME_S = 120
VERSIONS = frozenset({"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"})
LATEST_VERSION = max(VERSIONS)
MAX_LIFECYCLE = 64 * 1024
ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
SERVER_NAME = "ccfleet"
RUN_DIR_ENV = "CCFLEET_NATIVE_RUN_DIR"
LOG_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
LIFECYCLE_FAILURE_REASONS = frozenset({"invalid_protocol_version", "invalid_protocol_metadata",
    "invalid_json", "invalid_parameters", "invalid_envelope", "invalid_request_id",
    "duplicate_request_id", "invalid_initialize", "initialization_required",
    "foreign_cancellation", "protocol_rejected", "tool_handoff_rejected", "request_rejected"})
LIFECYCLE_NORMAL_REASONS = frozenset({"started", "received", "initialize_responded", "initialized",
    "tools_list_responded", "response_sent", "ignored_notification", "tool_handoff_recorded",
    "modern_discovery_fallback"})


class Rejected(ValueError):
    pass


def _protocol_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise Rejected("invalid protocol version")
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise Rejected("invalid protocol version") from None
    # MCP lifecycle version negotiation: offer our latest supported version,
    # never claim conformance to a newer client version we do not implement.
    return value if value in VERSIONS else LATEST_VERSION


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("duplicate JSON keys")
        result[key] = value
    return result


def _integer(value):
    if len(value.lstrip("-")) > 1024:
        raise Rejected("integer bound")
    return int(value)


def _constant(_value):
    raise Rejected("nonfinite number")


def _bounded(value, budget=None, depth=0):
    if budget is None:
        budget = [0, 0]
    budget[0] += 1
    if budget[0] > MAX_NODES or depth > MAX_DEPTH:
        raise Rejected("JSON structure bound")
    if isinstance(value, str):
        budget[1] += len(value.encode("utf-8"))
        if budget[1] > MAX_FILE:
            raise Rejected("JSON text bound")
    elif isinstance(value, list):
        for item in value:
            _bounded(item, budget, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise Rejected("JSON key")
            _bounded(key, budget, depth + 1)
            _bounded(item, budget, depth + 1)
    elif type(value) is float and not math.isfinite(value):
        raise Rejected("nonfinite number")
    elif value is not None and type(value) not in (bool, int, float):
        raise Rejected("JSON value")


def _decode(raw):
    if not raw or len(raw) > MAX_FILE:
        raise Rejected("JSON byte bound")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_constant=_constant,
        )
        _bounded(value)
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError) as error:
        raise Rejected("invalid bounded JSON") from error


def _rpc_id(value):
    if type(value) is int and abs(value) <= 1_000_000_000:
        return value
    if isinstance(value, str) and ID.fullmatch(value):
        return value
    raise Rejected("invalid RPC ID")


def _candidate_wrapper(value, config, catalog):
    # The local frontend validates semantics/permissions before tool execution.
    # A different native wrapper shape or undeclared name must deny.
    names = {tool["name"] for tool in catalog.tools_list()["tools"]}
    return (
        isinstance(value, dict)
        and set(value) == {"server_name", "tool_name", "arguments"}
        and value["server_name"] == config["mcp_server_name"]
        and isinstance(value["tool_name"], str)
        and value["tool_name"] in names
        and isinstance(value["arguments"], dict)
    )


def hook_tool_names(catalog):
    """Return exact namespaced grants; native bare tool names are never included."""
    if not isinstance(catalog, ToolCatalog):
        raise Rejected("frozen tool catalog required")
    return tuple("mcp__" + SERVER_NAME + "__" + tool["name"]
                 for tool in catalog.tools_list()["tools"])


class RunDirectory:
    def __init__(self, path):
        value = Path(path)
        if (
            not value.is_absolute()
            or ".." in value.parts
            or value.resolve(strict=True) != value
        ):
            raise Rejected("plain absolute run directory required")
        self.fd = os.open(
            value, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            info = os.fstat(self.fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise Rejected("private owned run directory required")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self, name):
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=self.fd,
        )
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or not 0 < info.st_size <= MAX_FILE
            ):
                raise Rejected("private regular config file required")
            raw = bytearray()
            while len(raw) <= MAX_FILE:
                part = os.read(fd, min(65536, MAX_FILE + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
            return _decode(bytes(raw))
        finally:
            os.close(fd)

    def write_exclusive(self, name, value):
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(raw) > MAX_FILE:
            raise Rejected("record bound")
        temporary = ".pending-" + secrets.token_hex(16) + ".json"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            # link is an atomic no-replace publication of a complete record.
            os.link(
                temporary,
                name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
        finally:
            os.unlink(temporary, dir_fd=self.fd)
        os.fsync(self.fd)


def _config(directory):
    config = directory.read("config.json")
    required = {"tools", "binding", "session_id", "server_id", "mcp_server_name"}
    if (
        not isinstance(config, dict)
        or set(config) != required
    ):
        raise Rejected("invalid config fields")
    if not isinstance(config["binding"], str) or not 1 <= len(config["binding"]) <= 512:
        raise Rejected("invalid binding")
    if not config["binding"].isascii() or any(
        not 32 <= ord(c) < 127 for c in config["binding"]
    ):
        raise Rejected("invalid binding")
    for key in ("server_id", "session_id"):
        value = config[key]
        if key == "session_id" and value is None:
            continue
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise Rejected("invalid context")
    if config["mcp_server_name"] != SERVER_NAME:
        raise Rejected("invalid configured MCP server")
    catalog = ToolCatalog(config["tools"])
    return config, catalog


def _session(directory, config, catalog):
    value = directory.read("session.json")
    if (
        not isinstance(value, dict)
        or set(value) != {"binding", "session_id", "server_id", "schema_digest"}
        or value["binding"] != config["binding"]
        or value["server_id"] != config["server_id"]
        or value["schema_digest"] != catalog.schema_digest
        or not isinstance(value["session_id"], str)
        or not ID.fullmatch(value["session_id"])
        or config["session_id"] not in (None, value["session_id"])
    ):
        raise Rejected("session is not finally bound")
    return value


def process_identity():
    pid, pgid = os.getpid(), os.getpgrp()
    with open("/proc/self/stat", "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise Rejected("process identity bound")
    prefix, closing, rest = raw.rpartition(b") ")
    fields = rest.split()
    if (
        not closing
        or int(prefix.split(b" (", 1)[0]) != pid
        or len(fields) < 20
        or int(fields[2]) != pgid
    ):
        raise Rejected("process identity mismatch")
    ticks = int(fields[19])
    if ticks <= 0:
        raise Rejected("invalid process start")
    return {"pid": pid, "pgid": pgid, "startticks": ticks}


class Lines:
    def __init__(self, stream, cancelled, clock):
        self.stream, self.cancelled, self.clock = stream, cancelled, clock
        self.buffer = bytearray()
        self.total = 0
        try:
            self.fd = stream.fileno()
        except (AttributeError, io.UnsupportedOperation):
            self.fd = None

    def next(self, deadline):
        while not self.cancelled.is_set() and self.clock() < deadline:
            split = self.buffer.find(b"\n")
            if split >= 0:
                line = bytes(self.buffer[:split])
                del self.buffer[: split + 1]
                if not line or len(line) > MAX_LINE:
                    raise Rejected("line bound")
                return line
            if len(self.buffer) > MAX_LINE:
                raise Rejected("line bound")
            if self.fd is None:
                part = self.stream.read(MAX_LINE + 1)
            else:
                if not select.select(
                    [self.fd], [], [], min(0.1, deadline - self.clock())
                )[0]:
                    continue
                part = os.read(self.fd, min(65536, MAX_LINE + 1 - len(self.buffer)))
            if not part:
                if self.buffer:
                    raise Rejected("unterminated JSONL message")
                return None
            self.total += len(part)
            if self.total > MAX_TOTAL:
                raise Rejected("total input bound")
            self.buffer.extend(part)
        return None


def _send(stdout, value, *, cancelled=None, clock=time.monotonic, deadline=None):
    raw = (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    if len(raw) > MAX_LINE:
        raise Rejected("output bound")
    cancelled = threading.Event() if cancelled is None else cancelled
    deadline = clock() + 2 if deadline is None else deadline
    if cancelled.is_set() or clock() >= deadline:
        raise Rejected("output stopped")
    try:
        fd = stdout.fileno()
    except (AttributeError, io.UnsupportedOperation):
        # Only injected, test-controlled in-memory streams take this path.
        stdout.write(raw)
        stdout.flush()
        return
    was_blocking = os.get_blocking(fd)
    os.set_blocking(fd, False)
    try:
        remaining = memoryview(raw)
        while remaining:
            if cancelled.is_set() or clock() >= deadline:
                raise Rejected("output stopped")
            if not select.select([], [fd], [], max(0, min(0.1, deadline - clock())))[1]:
                continue
            if cancelled.is_set() or clock() >= deadline:
                raise Rejected("output stopped")
            try:
                written = os.write(fd, remaining)
            except (BlockingIOError, InterruptedError):
                continue
            if written <= 0:
                raise Rejected("output made no progress")
            remaining = remaining[written:]
    finally:
        os.set_blocking(fd, was_blocking)


def _lifecycle(directory, request, *, initialized, ready, reason, protocol=None):
    """Bounded protocol metadata only; no IDs, argument values or client content."""
    def keys(value):
        if not isinstance(value, dict):
            return []
        return sorted({key if isinstance(key, str) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_./-]{0,79}", key) else "[invalid]"
            for key in list(value)[:16]})

    request = request if isinstance(request, dict) else {}
    method = request.get("method")
    if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z0-9_/$.-]{1,100}", method):
        method = "[unparsed]"
    params = request.get("params")
    record = {"method": method, "keys": keys(request), "params_keys": keys(params),
              "initialized": bool(initialized), "ready": bool(ready), "reason": reason}
    if protocol is None and isinstance(params, dict):
        candidate = params.get("protocolVersion")
        try:
            _protocol_version(candidate)
        except Rejected:
            pass
        else:
            protocol = candidate  # Valid requested date, never an arbitrary string.
    if protocol is not None:
        record["protocolVersion"] = protocol
    raw = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    fd = os.open("lifecycle.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                 | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=directory.fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size + len(raw) > MAX_LIFECYCLE):
            raise Rejected("lifecycle log bound")
        if os.write(fd, raw) != len(raw):
            raise Rejected("incomplete lifecycle log")
    finally:
        os.close(fd)


def read_lifecycle(run_dir):
    """Read only safe MCP lifecycle evidence, never request IDs or argument values.

    A partial final append is not readiness proof. Error/closed records fail
    closed; callers should capture the live summary before intentional cleanup.
    Ownership/identity of the live process is checked separately by the waiter.
    """
    summary = {"phase": "missing", "ready": False, "handshake_complete": False,
               "protocol_version": None, "tools_list_count": 0, "records": 0}
    directory = RunDirectory(run_dir)
    try:
        fd = os.open("lifecycle.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory.fd)
    except FileNotFoundError:
        directory.close()
        return summary
    except BaseException:
        directory.close()
        raise
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > MAX_LIFECYCLE):
            raise Rejected("mcp_lifecycle_file_guard")
        raw = os.read(fd, MAX_LIFECYCLE + 1)
    finally:
        os.close(fd)
        directory.close()
    if len(raw) > MAX_LIFECYCLE:
        raise Rejected("mcp_lifecycle_size_bound")
    complete = not raw or raw.endswith(b"\n")
    lines = raw.split(b"\n")[:-1]
    initialized = ready = False
    summary["phase"] = "starting"
    for raw_line in lines:
        try:
            record = _decode(raw_line)
        except Rejected as error:
            raise Rejected("mcp_lifecycle_invalid_json") from error
        required = {"method", "keys", "params_keys", "initialized", "ready", "reason"}
        if (not isinstance(record, dict) or not required <= record.keys()
                or record.keys() - required - {"protocolVersion"}
                or type(record["initialized"]) is not bool or type(record["ready"]) is not bool
                or not isinstance(record["method"], str)
                or not (record["method"] == "[unparsed]" or re.fullmatch(
                    r"[A-Za-z0-9_/$.-]{1,100}", record["method"]))
                or any(not isinstance(record[name], list) or len(record[name]) > 16
                       or any(not isinstance(key, str) or not (key == "[invalid]" or re.fullmatch(
                           r"[A-Za-z_][A-Za-z0-9_./-]{0,79}", key)) for key in record[name])
                       for name in ("keys", "params_keys"))
                or not isinstance(record["reason"], str)):
            raise Rejected("mcp_lifecycle_invalid_record")
        reason = record["reason"]
        if reason == "closed":
            raise Rejected("mcp_shim_closed")
        if reason in LIFECYCLE_FAILURE_REASONS:
            raise Rejected("mcp_" + reason)  # Validated finite static reason set only.
        if reason not in LIFECYCLE_NORMAL_REASONS:
            raise Rejected("mcp_lifecycle_unknown_reason")
        protocol = record.get("protocolVersion")
        if protocol is not None:
            try:
                if (not isinstance(protocol, str)
                        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", protocol)):
                    raise ValueError
                datetime.date.fromisoformat(protocol)
            except ValueError as error:
                raise Rejected("mcp_lifecycle_invalid_protocol") from error
        if reason == "modern_discovery_fallback":
            if (initialized or ready or record["method"] != "server/discover"
                    or "id" not in record["keys"] or record["initialized"] is not False
                    or record["ready"] is not False):
                raise Rejected("mcp_discovery_fallback_invalid")
        elif reason == "initialize_responded":
            if (initialized or record["method"] != "initialize" or "id" not in record["keys"]
                    or record["initialized"] is not True or record["ready"] is not False
                    or protocol not in VERSIONS):
                raise Rejected("mcp_initialize_proof_invalid")
            initialized = True
            summary["protocol_version"] = protocol
            summary["phase"] = "initialize_responded"
        elif reason == "initialized":
            if (not initialized or ready or record["method"] != "notifications/initialized"
                    or "id" in record["keys"] or record["initialized"] is not True
                    or record["ready"] is not True or protocol != summary["protocol_version"]):
                raise Rejected("mcp_ready_proof_invalid")
            ready = True
            summary["phase"] = "ready"
        elif reason == "tools_list_responded":
            if (not ready or record["method"] != "tools/list" or "id" not in record["keys"]
                    or record["initialized"] is not True or record["ready"] is not True
                    or protocol != summary["protocol_version"]):
                raise Rejected("mcp_tools_list_proof_invalid")
            summary["tools_list_count"] += 1
        if reason != "received" and protocol is not None and initialized:
            if protocol != summary["protocol_version"]:
                raise Rejected("mcp_negotiated_protocol_changed")
        if record["initialized"] != initialized or record["ready"] != ready:
            raise Rejected("mcp_lifecycle_state_mismatch")
        summary["records"] += 1
    summary["handshake_complete"] = ready
    summary["ready"] = ready and complete
    if not complete:
        summary["phase"] = "partial"
    return summary



def serve(
    run_dir,
    stdin,
    stdout,
    *,
    cancelled=None,
    clock=time.monotonic,
    identity=process_identity,
):
    """Synchronous bounded MCP server; injected streams/identity support offline tests."""
    cancelled = threading.Event() if cancelled is None else cancelled
    directory = RunDirectory(run_dir)
    handoff = context = None
    initialized = ready = False
    request, negotiated = {}, None
    try:
        config, catalog = _config(directory)
        own_process = identity()
        directory.write_exclusive("process.json", own_process)
        reader = Lines(stdin, cancelled, clock)
        deadline = clock() + SERVER_LIFETIME_S
        _lifecycle(directory, {}, initialized=False, ready=False, reason="started")
        seen_ids = set()
        for _ in range(MAX_MESSAGES):
            raw = reader.next(deadline)
            if raw is None:
                break
            request = _decode(raw)
            _lifecycle(directory, request, initialized=initialized, ready=ready, reason="received")
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)
                or set(request) - {"jsonrpc", "id", "method", "params"}
            ):
                raise Rejected("invalid request envelope")
            method, params = request["method"], request.get("params", {})
            if not isinstance(params, dict):
                raise Rejected("object parameters required")
            if "_meta" in params:
                if not isinstance(params["_meta"], dict):
                    raise Rejected("invalid protocol metadata")
                # Reserved transport metadata is bounded by _decode and opaque.
                # It neither changes tool arguments nor grants capabilities.
                params = {key: value for key, value in params.items() if key != "_meta"}
            if "id" not in request:
                if (
                    method == "notifications/initialized"
                    and initialized
                    and not ready
                    and not params
                ):
                    ready = True
                    _lifecycle(directory, request, initialized=True, ready=True,
                               reason="initialized", protocol=negotiated)
                    continue
                if method == "notifications/cancelled" and handoff is not None:
                    if (
                        set(params) - {"requestId", "reason"}
                        or "requestId" not in params
                        or type(params["requestId"]) is not type(context["request_id"])
                        or params["requestId"] != context["request_id"]
                    ):
                        raise Rejected("foreign cancellation")
                    break
                if initialized:
                    _lifecycle(directory, request, initialized=initialized, ready=ready,
                               reason="ignored_notification", protocol=negotiated)
                    continue  # Notifications never execute tools or receive responses.
                raise Rejected("unexpected notification")
            rpc_id = _rpc_id(request["id"])
            identity_key = (type(rpc_id), rpc_id)
            if identity_key in seen_ids:
                # Never let a ping/list with a reused ID accidentally complete
                # the deliberately unanswered tools/call.
                raise Rejected("duplicate request ID")
            seen_ids.add(identity_key)
            response = {"jsonrpc": "2.0", "id": rpc_id}
            if method == "initialize" and not initialized:
                if (
                    set(params)
                    - {"protocolVersion", "capabilities", "clientInfo"}
                    or not isinstance(params.get("capabilities"), dict)
                    or not isinstance(params.get("clientInfo"), dict)
                ):
                    raise Rejected("unsupported initialize")
                negotiated = _protocol_version(params.get("protocolVersion"))
                response["result"] = {
                    "protocolVersion": negotiated,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ccfleet-local-tools", "version": "0.1.0"},
                }
            elif method == "ping" and initialized and not params:
                response["result"] = {}
            elif method == "tools/list" and ready and not params:
                response["result"] = catalog.tools_list()
            elif method == "tools/call" and ready and handoff is None:
                try:
                    bound = _session(directory, config, catalog)
                    handoff = ToolHandoff(
                        catalog,
                        binding=bound["binding"],
                        session_id=bound["session_id"],
                        server_id=bound["server_id"],
                    )
                    context = {
                        key: bound[key]
                        for key in ("binding", "session_id", "server_id")
                    }
                    if cancelled.is_set():
                        break
                    tool = handoff.begin(
                        {**request, "params": params}, **context, remaining_seconds=CALL_WAIT_S
                    )
                    context["request_id"] = rpc_id
                    if cancelled.is_set():
                        break
                    directory.write_exclusive(
                        "call.json",
                        {
                            "version": 1,
                            "frontend_tool_use": tool.as_tool_use(),
                            "native_rpc_id": rpc_id,
                            "context": bound,
                            "process": own_process,
                        },
                    )
                    deadline = min(deadline, clock() + CALL_WAIT_S)
                    _lifecycle(directory, request, initialized=initialized, ready=ready,
                               reason="tool_handoff_recorded", protocol=negotiated)
                    continue  # Deliberately no response to this tools/call.
                except (OSError, Rejected, ToolBridgeError):
                    if (
                        handoff is not None
                        and context is not None
                        and "request_id" in context
                    ):
                        try:
                            handoff.cancel(**context)
                        except ToolBridgeError:
                            pass
                    handoff = context = None
                    response["error"] = {
                        "code": -32602,
                        "message": "Caller tool handoff rejected",
                    }
                    _lifecycle(directory, request, initialized=initialized, ready=ready,
                               reason="tool_handoff_rejected", protocol=negotiated)
            else:
                response["error"] = {
                    "code": -32601,
                    "message": "Unsupported or duplicate request",
                }
            _send(stdout, response, cancelled=cancelled, clock=clock, deadline=deadline)
            if method == "initialize" and "result" in response:
                initialized = True
            reason = "request_rejected" if "error" in response else "response_sent"
            if (method == "server/discover" and not initialized and not ready
                    and response.get("error", {}).get("code") == -32601):
                # A modern client may try discovery before falling back to
                # legacy initialize. This remains an unsupported, no-op RPC.
                reason = "modern_discovery_fallback"
            if method == "initialize" and "result" in response:
                reason = "initialize_responded"
            elif method == "tools/list" and "result" in response:
                reason = "tools_list_responded"
            _lifecycle(directory, request, initialized=initialized, ready=ready,
                       reason=reason, protocol=negotiated)
        return 0 if handoff is None else 1
    except (OSError, Rejected, ToolBridgeError) as error:
        reason = {"invalid protocol version": "invalid_protocol_version",
                  "invalid protocol metadata": "invalid_protocol_metadata",
                  "invalid bounded JSON": "invalid_json",
                  "object parameters required": "invalid_parameters",
                  "invalid request envelope": "invalid_envelope",
                  "invalid RPC ID": "invalid_request_id",
                  "duplicate request ID": "duplicate_request_id",
                  "unsupported initialize": "invalid_initialize",
                  "unexpected notification": "initialization_required",
                  "foreign cancellation": "foreign_cancellation"}.get(
                      str(error), "protocol_rejected")
        _lifecycle(directory, request, initialized=initialized, ready=ready,
                   reason=reason, protocol=negotiated)
        raise
    finally:
        if handoff is not None and context is not None and "request_id" in context:
            try:
                handoff.cancel(**context)
            except ToolBridgeError:
                pass
        try:
            _lifecycle(directory, request, initialized=initialized, ready=ready,
                       reason="closed", protocol=negotiated)
        finally:
            directory.close()


def _hook_log(directory, name, allowed):
    raw = (
        json.dumps({"tool_name": name, "decision": "approve" if allowed else "block"})
        + "\n"
    ).encode()
    fd = os.open(
        "hook.jsonl",
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | os.O_NOFOLLOW
        | os.O_NONBLOCK
        | os.O_CLOEXEC,
        0o600,
        dir_fd=directory.fd,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size + len(raw) > 16 * 1024
        ):
            raise Rejected("hook log bound")
        if os.write(fd, raw) != len(raw):
            raise Rejected("incomplete hook log")
    finally:
        os.close(fd)


def _hook_input(stdin):
    try:
        fd = stdin.fileno()
    except (AttributeError, io.UnsupportedOperation):
        return stdin.read(MAX_LINE + 1)
    raw = bytearray()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            continue
        part = os.read(fd, min(65536, MAX_LINE + 1 - len(raw)))
        if not part:
            return bytes(raw)
        raw.extend(part)
        if len(raw) > MAX_LINE:
            raise Rejected("hook input bound")
    raise Rejected("hook input deadline")


def hook(run_dir, stdin, stdout):
    """Permit exact declared caller tools and scoped discovery; errors block/exit 2."""
    directory = None
    allowed = False
    try:
        directory = RunDirectory(run_dir)
        config, catalog = _config(directory)
        raw = _hook_input(stdin)
        event = _decode(raw)
        if not isinstance(event, dict):
            raise Rejected("invalid hook event")
        name = event.get("tool_name")
        safe_name = (
            name if isinstance(name, str) and LOG_NAME.fullmatch(name) else "[invalid]"
        )
        allowed = (
            event.get("hook_event_name") == "PreToolUse"
            and (
                (isinstance(event.get("tool_input"), dict) and name in hook_tool_names(catalog))
                or (
                    name == "mcp_list_tools"
                    and isinstance(event.get("tool_input"), dict)
                    and event["tool_input"] == {"server_name": config["mcp_server_name"]}
                )
                or (
                    name == "mcp_call_tool"
                    and _candidate_wrapper(event.get("tool_input"), config, catalog)
                )
            )
        )
        _hook_log(directory, safe_name, allowed)
    except BaseException:  # noqa: BLE001 -- every handled hook failure must deny, including interrupts.
        allowed = False
    finally:
        if directory is not None:
            directory.close()
    try:
        _send(stdout, {"decision": "approve" if allowed else "block"})
    except BaseException:  # noqa: BLE001 -- exit 2 must also deny when stdout fails.
        return 2
    return 0 if allowed else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--hook", action="store_true")
    modes.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    run_dir = args.run_dir
    if run_dir is None and args.hook:
        run_dir = os.environ.get(RUN_DIR_ENV)
    if args.plan or (run_dir is None and not args.hook):
        print(json.dumps({"execute": False, "transport": "stdio", "one_shot_handoff": True,
                          "caller_catalog_only": True, "node_tool_execution": False,
                          "native_model_calls": False}))
        return 0
    if not run_dir:
        if args.hook:
            try:
                _send(sys.stdout.buffer, {"decision": "block"})
            except (OSError, ValueError):
                pass
        return 2
    if args.hook:
        return hook(run_dir, sys.stdin.buffer, sys.stdout.buffer)
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancelled.set())
    try:
        return serve(run_dir, sys.stdin.buffer, sys.stdout.buffer, cancelled=cancelled)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
