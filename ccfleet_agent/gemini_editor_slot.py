"""Interactive Google editor sessions on a personally paired Mac slot."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import queue
import re
import select
import shlex
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if __package__:
    from . import (
        antigravity_slot,
        gemini_preview,
        mac_policy,
        mac_relay_client,
        project_files,
        workspace_io,
        workspace_tools,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import antigravity_slot
    import gemini_preview
    import mac_policy
    import mac_relay_client
    import project_files
    import workspace_io
    import workspace_tools

SOCKET = "gemini-editor.sock"
MAX_TEXT = workspace_tools.LIMIT
ACP_LIMIT = 4 * 1024 * 1024
MCP_NAME = "ccfleetlocal"
MAX_MODELS = 128
CANCEL_GRACE = 12.0
CANCEL_WAKE = object()
CURRENT_TURN = object()
MCP_TOOLS = {"list_project": "list", "make_project_directory": "mkdir", "test_project": "test"}
GUIDE = (
    "You are the user's interactive CCFleet project editor. The project is on the "
    "connecting computer. Use client_view_file, client_edit_file and client_create_file "
    "with absolute paths in the current project. Use ccfleetlocal list_project, "
    "make_project_directory and test_project for local directories and tests. These are lazy "
    "tools: invoke call_mcp_tool with ServerName=ccfleetlocal, ToolName=test_project, Arguments={} "
    "for tests. For list_project or make_project_directory use Arguments with the relative path. "
    "The slot's "
    "filesystem, shell and terminal are outside this project. If a client operation fails "
    "or the user declines it, explain why and wait for the user's next instruction.\n\n"
)


LOGIN_NEEDED = (
    "Open CCFleet Gemini Sign In in the private Mac owner's Applications folder, then reconnect."
)
UNAVAILABLE = "The Gemini editor is unavailable on the private Mac. Reopen it to try again."
BUSY = "Another Gemini editor is using this slot. Close it and reconnect."


class EditorError(ValueError):
    """Fixed user-facing error. Never include a credential, provider payload or host path."""


class ModelUnavailable(EditorError):
    """A rejected selection leaves the current editor session usable."""


def text_value(value, limit=MAX_TEXT):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise ValueError("invalid editor text")
    return value


def relative_path(root, path):
    if not isinstance(path, str) or not path.startswith(str(root) + "/"):
        raise ValueError("outside the client project")
    return project_files._path(path[len(str(root)) + 1 :])


def json_line(stream):
    raw = stream.readline(ACP_LIMIT + 1)
    if not raw:
        raise EOFError()
    if len(raw) > ACP_LIMIT or not raw.endswith(b"\n"):
        raise ValueError("invalid editor record")
    msg = json.loads(raw)
    if not isinstance(msg, dict):
        raise ValueError("invalid editor record")
    return msg


def send_line(stream, msg):
    data = json.dumps(msg, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(data) >= ACP_LIMIT:
        raise ValueError("editor record too large")
    stream.write(data + b"\n")
    stream.flush()


class Device:
    """Serialize agent file callbacks and MCP requests onto the same paired SSH stream."""

    def __init__(self, sock, authorized):
        self.sock = sock
        self.input = sock.makefile("rb")
        self.output = sock.makefile("wb")
        self.socket_lock = threading.Lock()
        self.callback_lock = threading.Lock()
        self.next_id = 0
        self.messages = queue.Queue(maxsize=2)
        self.closed = threading.Event()
        self.authorized = authorized
        self.process = None
        self.process_lock = threading.Lock()
        self.agent = None
        self.turn_key = None
        self.turn_id = None
        self.cancelled_turn = None
        self.cancelled_at = 0.0

    def start(self):
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _read(self):
        last_turn = 0
        try:
            while not self.closed.is_set():
                msg = gemini_preview.receive(self.input)
                tag = msg.get("turn")
                if msg.get("type") == "prompt" and "turn" in msg:
                    if type(tag) is not int or not last_turn < tag <= 2**53 - 1:
                        raise ValueError("invalid turn number")
                    last_turn = tag
                if msg.get("type") == "cancel":
                    if (self.agent is None or set(msg) != {"type", "turn"}
                            or type(tag) is not int or not 0 < tag <= last_turn):
                        raise ValueError("invalid turn cancellation")
                    if tag == last_turn and tag != self.cancelled_turn:
                        self.cancelled_at = time.monotonic()
                        self.cancelled_turn = tag
                        if self.turn_id == tag:
                            self.agent.active.clear()
                        self.agent.cancel_turn(tag)
                    continue
                self.messages.put_nowait(msg)
        except (OSError, EOFError, ValueError, RecursionError, queue.Full):
            self.abort()

    def _watch(self):
        try:
            while not self.closed.wait(1):
                if not self.authorized():
                    break
                if (self.turn_id is not None and self.cancelled_turn == self.turn_id
                        and time.monotonic() - self.cancelled_at >= CANCEL_GRACE):
                    break
        except (OSError, ValueError, KeyError):
            pass
        self.abort()

    def attach(self, proc):
        with self.process_lock:
            self.process = proc
            if self.closed.is_set():
                self._kill()

    def _kill(self):
        if self.process and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)

    def abort(self):
        self.closed.set()
        with self.process_lock:
            self._kill()
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)

    def receive(self, timeout):
        end = time.monotonic() + timeout
        while not self.closed.is_set():
            left = end - time.monotonic()
            if left <= 0:
                raise EditorError("The Gemini editor session timed out. Reopen it to continue.")
            try:
                return self.messages.get(timeout=min(0.5, left))
            except queue.Empty:
                pass
        raise EOFError()

    def send(self, msg):
        if self.closed.is_set():
            raise EOFError()
        with self.socket_lock:
            gemini_preview.send(self.output, msg)

    def begin_turn(self, tag):
        self.turn_key = object()
        self.turn_id = tag
        self.agent.active.set()
        if tag is not None and self.cancelled_turn == tag:
            self.agent.active.clear()

    def finish_turn(self):
        self.agent.active.clear()
        # Drain a previously issued MCP callback before releasing the wire for the next turn.
        with self.callback_lock:
            self.turn_key = self.turn_id = None

    def request(self, op, *, turn=CURRENT_TURN, **fields):
        if turn is CURRENT_TURN:
            turn = self.turn_key
        with self.callback_lock:
            if turn is None or self.turn_key is not turn or not self.agent.active.is_set():
                raise ValueError("no active turn")
            self.next_id += 1
            self.send({"type": "request", "id": self.next_id, "op": op, **fields})
            reply = self.receive(600)
            if (
                reply.get("type") != "response"
                or type(reply.get("id")) is not int
                or reply["id"] != self.next_id
            ):
                raise EditorError("The workspace returned an unexpected editor response.")
            if set(reply) == {"type", "id", "error"}:
                raise ValueError("workspace declined or could not complete this operation")
            if set(reply) != {"type", "id", "result"}:
                raise ValueError("invalid workspace reply")
            if not self.agent.active.is_set():
                raise ValueError("turn cancelled")
            return reply["result"]


def listing_path(path, project):
    """Map directory spellings inside the agent’s virtual project to client-relative names."""
    if not isinstance(path, str) or len(path) > project_files.MAX_PATH_BYTES + len(str(project)):
        raise ValueError("invalid directory")
    requested = Path(path)
    if requested.is_absolute():
        if project is None or not requested.is_relative_to(project):
            raise ValueError("outside the client project")
        requested = requested.relative_to(project)
    relative = requested.as_posix()
    return "." if relative == "." else project_files._path(relative)


def mcp_request(device, msg, project=None, *, turn=CURRENT_TURN):
    """Only the selected MacBook project exposes these three tools."""
    if msg.get("jsonrpc") != "2.0" or "id" not in msg or "method" not in msg:
        return None
    method = msg["method"]
    request = device.request
    if turn is not CURRENT_TURN:
        request = functools.partial(request, turn=turn)
    params = msg.get("params", {})
    reply = {"jsonrpc": "2.0", "id": msg["id"]}
    if not isinstance(params, dict):
        return {**reply, "error": {"code": -32602, "message": "Invalid arguments"}}
    if method == "initialize":
        version = params.get("protocolVersion")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            version = "2025-11-25"
        result = {
            "protocolVersion": version,
            "serverInfo": {"name": MCP_NAME, "version": "1"},
            "capabilities": {"tools": {}},
        }
    elif method == "tools/list":
        result = {"tools": [t for t in workspace_tools.TOOLS if t["name"] in MCP_TOOLS]}
    elif method == "ping":
        result = {}
    elif method in ("resources/list", "resources/templates/list", "prompts/list"):
        key = {
            "resources/list": "resources",
            "resources/templates/list": "resourceTemplates",
            "prompts/list": "prompts",
        }[method]
        result = {key: []}
    elif method == "tools/call":
        try:
            name = params.get("name")
            args = params.get("arguments", {})
            if name not in MCP_TOOLS or not isinstance(args, dict) or args.keys() - {"path"}:
                raise ValueError("unsupported operation")
            if name == "test_project":
                if args:
                    raise ValueError("custom test commands are not supported")
                data = request("test")
                if (
                    not isinstance(data, dict)
                    or set(data) != {"exit", "output"}
                    or type(data["exit"]) is not int
                ):
                    raise ValueError("invalid test response")
                text_value(data["output"])
            elif name == "list_project":
                path = listing_path(args.get("path", "."), project)
                data = request("list", path=path)
                if (
                    not isinstance(data, list)
                    or len(data) > 1500
                    or any(not isinstance(p, str) for p in data)
                ):
                    raise ValueError("invalid directory response")
            else:
                path = project_files._path(args.get("path"))
                data = request("mkdir", path=path)
                if data is not True:
                    raise ValueError("directory unavailable")
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
        except (ValueError, OSError, KeyError, TypeError):
            reason = (
                "Directory listing unavailable. Use . for the project or a readable "
                "project-relative subfolder."
                if name == "list_project"
                else "Client operation declined or unavailable."
            )
            result = {"isError": True, "content": [{"type": "text", "text": reason}]}
    else:
        return {**reply, "error": {"code": -32601, "message": "Method not found"}}
    return {**reply, "result": result}


class ProjectMCP(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class ProjectHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            mac_relay_client.require_peer(self.request, os.getuid())
            while True:
                msg = json_line(self.rfile)
                turn = self.server.device.turn_key
                if (msg.get("method") == "tools/call"
                        and (turn is None or not self.server.turn.is_set())):
                    reply = {
                        "jsonrpc": "2.0",
                        "id": msg.get("id"),
                        "error": {"code": -32600, "message": "No interactive prompt is active"},
                    }
                else:
                    reply = mcp_request(self.server.device, msg, self.server.project, turn=turn)
                if reply is not None:
                    send_line(self.wfile, reply)
        except (EOFError, OSError, ValueError):
            return


GOOGLE_HOSTS = frozenset(
    {
        "cloudcode-pa.googleapis.com",
        "daily-cloudcode-pa.googleapis.com",
        "oauth2.googleapis.com",
        "antigravity-pa.googleapis.com",
        "antigravity-unleash.goog",
        "businessaicode.googleapis.com",
    }
)


class GoogleConnection(socketserver.StreamRequestHandler):
    """Relay opaque Google TLS only; all provider credentials remain inside its signed binary."""

    def handle(self):
        try:
            self.request.settimeout(12)
            first = self.rfile.readline(512).split()
            if (
                len(first) != 3
                or first[0] != b"CONNECT"
                or first[2] not in (b"HTTP/1.0", b"HTTP/1.1")
                or first[1].decode("ascii") not in {host + ":443" for host in GOOGLE_HOSTS}
            ):
                self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            for _ in range(32):
                header = self.rfile.readline(4096)
                if header == b"\r\n":
                    break
                if not header or not header.endswith(b"\n"):
                    return
            else:
                return
            host = first[1].decode("ascii").split(":")[0]
            with socket.create_connection((host, 443), timeout=15) as upstream:
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                upstream.settimeout(30)
                while True:
                    ready, _, _ = select.select([self.request, upstream], [], [], 180)
                    if not ready:
                        return
                    for src in ready:
                        packet = src.recv(65536)
                        if not packet:
                            return
                        (upstream if src is self.request else self.request).sendall(packet)
        except (OSError, ValueError):
            return


class GoogleProxy(socketserver.ThreadingTCPServer):
    daemon_threads = True


def _sandbox(agent, home, session):
    return [
        "/usr/bin/sandbox-exec",
        "-D",
        "HOME_DIR=" + str(home),
        "-D",
        "PROJECT=" + str(session),
        "-D",
        "PYTHON=" + str(Path(sys.executable).resolve()),
        "-D",
        "PYTHON_APP="
        + str(
            Path(sys.executable).resolve().parent.parent
            / "Resources/Python.app/Contents/MacOS/Python"
        ),
        "-D",
        "EXE=" + str(agent),
        "-D",
        "HELPER=" + str(agent.with_name("localharness_external")),
        "-D",
        "LIBRARY=" + str(home / "Library"),
        "-D",
        "KEYCHAINS=" + str(home / "Library/Keychains"),
        "-D",
        "GUARDS=" + str(session / "home/.gemini/config"),
        "-D",
        "PROFILE_HOME=" + str(session / "home"),
        "-D",
        "PROFILE_GEMINI=" + str(session / "home/.gemini"),
        "-D",
        "PREF=" + str(home / "Library/Preferences/com.apple.security.plist"),
        "-p",
        SANDBOX,
        str(agent),
    ]


# Only Google's personal editor agent and the fixed stdio project helper are executable.
# No user's shell profile, project, or unrelated credentials are available to model tools.
SANDBOX = """
(version 1)
(allow default)
(deny file-write*)
(deny file-read* file-write* (subpath "/Users") (subpath (param "HOME_DIR"))
    (subpath "/Volumes") (subpath "/private/tmp"))
(allow file-read* (literal (param "EXE")) (literal (param "HELPER"))
    (literal "/Users") (literal "/private/tmp") (literal (param "HOME_DIR"))
    (literal (param "LIBRARY")) (literal (param "PREF")))
(allow file-read* file-write* (subpath (param "KEYCHAINS")))
(allow file-read* file-write* (subpath (param "PROJECT")) (literal "/dev/null"))
(deny file-write* (subpath (param "GUARDS")) (literal (param "PROJECT"))
    (literal (param "PROFILE_HOME")) (literal (param "PROFILE_GEMINI")))
(deny network*)
(allow network-bind network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*") (remote unix-socket (subpath (param "PROJECT"))))
(deny process-exec)
(allow process-exec (literal (param "EXE")) (literal (param "HELPER"))
    (literal "/usr/bin/security") (literal "/usr/bin/python3")
    (literal (param "PYTHON")) (literal (param "PYTHON_APP")))
"""


class Agent:
    def __init__(self, process, device=None, root=None):
        self.process = process
        self.device = device
        self.root = root
        self.sid = None
        self.model = None
        self.models = []
        self.approvals = {}
        self.active = threading.Event()
        self.messages = queue.Queue(maxsize=64)
        self.seq = 0
        self.stdin_lock = threading.Lock()
        self.prompt_turn = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                self.messages.put(json_line(self.process.stdout))
        except (OSError, EOFError, ValueError, RecursionError):
            self.messages.put(None)

    def _permission(self, params):
        tool = params.get("toolCall", {})
        approved = False
        if self.active.is_set() and isinstance(tool, dict):
            mcp = tool.get("_meta", {}).get("mcp", {})
            if mcp.get("server") == MCP_NAME and mcp.get("tool") in MCP_TOOLS:
                # Local writes and tests are independently approved again on the client.
                approved = True
            elif tool.get("kind") == "edit" and tool.get("title") in (
                "Run client_edit_file?",
                "Run client_create_file?",
            ):
                content = tool.get("content")
                if (
                    isinstance(content, list)
                    and len(content) == 1
                    and content[0].get("type") == "diff"
                ):
                    path = relative_path(self.root, content[0].get("path"))
                    text = text_value(content[0].get("newText"))
                    self.approvals.pop(path, None)
                    try:
                        approved = self.device.request("approve", path=path, content=text) is True
                    except ValueError:
                        if self.active.is_set():
                            raise
                    if approved and self.active.is_set():
                        if len(self.approvals) >= 32:
                            raise ValueError("too many pending edits")
                        self.approvals[path] = hashlib.sha256(text.encode()).digest()
        kind = "allow_once" if approved and self.active.is_set() else "reject_once"
        option = next(
            (
                o.get("optionId")
                for o in params.get("options", [])
                if isinstance(o, dict) and o.get("kind") == kind
            ),
            None,
        )
        return {
            "outcome": {"outcome": "selected", "optionId": option}
            if option
            else {"outcome": "cancelled"}
        }

    def callback(self, method, params):
        if self.sid is None or params.get("sessionId") != self.sid or not self.device:
            raise ValueError("invalid session")
        if method == "session/request_permission":
            return self._permission(params)
        if not self.active.is_set():
            raise ValueError("no active prompt")
        path = relative_path(self.root, params.get("path"))
        if method == "fs/read_text_file":
            text = text_value(self.device.request("read", path=path))
            start = params.get("line", 1)
            limit = params.get("limit")
            if (
                type(start) is not int
                or start < 1
                or limit is not None
                and (type(limit) is not int or limit < 0)
            ):
                raise ValueError("invalid line range")
            if start == 1 and limit is None:
                return {"content": text}
            lines = text.splitlines(keepends=True)
            return {
                "content": "".join(
                    lines[start - 1 : start - 1 + limit if limit is not None else None]
                )
            }
        if method == "fs/write_text_file":
            text = text_value(params.get("content"))
            if self.approvals.pop(path, None) != hashlib.sha256(text.encode()).digest():
                raise ValueError("local change was not approved")
            if self.device.request("write", path=path, content=text) is not True:
                raise ValueError("local write refused")
            return {}
        raise ValueError("unsupported client tool")

    def cancel_turn(self, turn):
        # Only interrupt the exact request already written to Google's input. A cancellation
        # received before the prompt write is handled by rpc() without sending a prompt.
        with self.stdin_lock:
            if self.prompt_turn == turn:
                send_line(self.process.stdin, {
                    "jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.sid}
                })
        with contextlib.suppress(queue.Full):
            self.messages.put_nowait(CANCEL_WAKE)

    def rpc(self, method, params, timeout=90, *, turn=None):
        self.seq += 1
        with self.stdin_lock:
            if turn is not None and self.device.cancelled_turn == turn:
                return {"stopReason": "cancelled"}
            send_line(self.process.stdin, {
                "jsonrpc": "2.0", "id": self.seq, "method": method, "params": params
            })
            self.prompt_turn = turn
        until = time.monotonic() + timeout
        while True:
            if turn is not None and self.device.cancelled_turn == turn:
                until = min(until, self.device.cancelled_at + CANCEL_GRACE)
            try:
                msg = self.messages.get(timeout=max(0.001, until - time.monotonic()))
            except queue.Empty:
                raise EditorError(
                    "The Gemini slot timed out. Reopen the editor to continue."
                ) from None
            if time.monotonic() > until:
                raise EditorError("The Gemini slot timed out. Reopen the editor to continue.")
            if msg is None:
                raise EditorError("The Google editor agent stopped on the slot.")
            if msg is CANCEL_WAKE:
                continue
            if "method" not in msg:
                with self.stdin_lock:
                    self.prompt_turn = None
                if msg.get("id") != self.seq:
                    raise ValueError("unexpected agent response")
                if "error" in msg:
                    error = msg["error"]
                    if not isinstance(error, dict) or type(error.get("code")) is not int:
                        raise ValueError("invalid agent error")
                    if (turn is not None and self.device.cancelled_turn == turn
                            and error["code"] == -32800):
                        return {"stopReason": "cancelled"}
                    if method == "session/new" and error["code"] == -32000:
                        raise EditorError(LOGIN_NEEDED)
                    if method == "session/set_model":
                        raise ModelUnavailable("The Google editor declined the model change.")
                    raise EditorError("The Google editor could not complete this request.")
                if not isinstance(msg.get("result"), dict):
                    raise ValueError("invalid agent response")
                return msg["result"]
            p = msg.get("params", {})
            if "id" not in msg:
                if (
                    self.active.is_set()
                    and msg["method"] == "session/update"
                    and p.get("sessionId") == self.sid
                    and isinstance(p.get("update"), dict)
                ):
                    update = p["update"]
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        content = update.get("content", {})
                        if content.get("type") == "text":
                            self.device.send({"type": "text", "text": text_value(content["text"])})
                continue
            try:
                answer = self.callback(msg["method"], p)
                reply = {"result": answer}
            except (ValueError, KeyError, TypeError):
                reply = {
                    "error": {
                        "code": -32602,
                        "message": "Client operation declined or unavailable.",
                    }
                }
            with self.stdin_lock:
                send_line(self.process.stdin, {"jsonrpc": "2.0", "id": msg["id"], **reply})

    def initialize(self):
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "ccfleet-project-editor", "version": "1"},
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
            },
        )
        if result.get("protocolVersion") != 1 or "oauth-personal" not in [
            m.get("id") for m in result.get("authMethods", [])
        ]:
            raise EditorError(
                "The installed Google editor agent is incompatible. Update CCFleet on the slot."
            )

    def session(self, project, mcp):
        result = self.rpc("session/new", {"cwd": str(project), "mcpServers": mcp})
        self.sid = result["sessionId"]
        if not isinstance(self.sid, str) or not self.sid:
            raise ValueError("invalid session")
        models = result.get("models", {})
        available = models.get("availableModels") if isinstance(models, dict) else None
        if not isinstance(available, list):
            raise ValueError("invalid agent model catalog")
        ids = (entry.get("modelId") for entry in available if isinstance(entry, dict))
        self.models = list(
            dict.fromkeys(
                model
                for model in ids
                if isinstance(model, str) and re.fullmatch(r"gemini-[a-z0-9.-]{1,80}", model)
            )
        )[:MAX_MODELS]
        if not self.models:
            raise EditorError("The signed-in Google account has no available Gemini editor model.")
        preferred = ("gemini-3.8-flash-low", models.get("currentModelId"))
        self.model = None
        self.select_model(next((m for m in preferred if m in self.models), self.models[0]))
        return self.model

    def select_model(self, model):
        if not isinstance(model, str) or model not in self.models:
            raise ModelUnavailable("Choose a Gemini model offered by this slot.")
        if model != self.model:
            self.rpc("session/set_model", {"sessionId": self.sid, "modelId": model})
            self.model = model


def environment(home, scratch):
    # The mini is the only Google client. Never pass an API key, Vertex project,
    # alternate base URL, inherited proxy, browser or executable search path.
    return {
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
    }


def _start_agent(argv, env, project):
    return subprocess.Popen(
        argv,
        cwd=project,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_agent(proc):
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    proc.stdin.close()
    proc.stdout.close()


def runtime(home):
    if __package__:
        from .gemini_editor_distribution import ensure_runtime
    else:
        from gemini_editor_distribution import ensure_runtime
    return ensure_runtime(home)


def require_personal(home):
    # Check the editor's account *type* in settings, never read the OAuth store.
    try:
        settings = home / ".gemini/antigravity-acp/settings.json"
        fd = os.open(settings, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as src:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or info.st_size > 1024 * 1024
            ):
                raise ValueError()
            value = json.load(src)
        if value.get("auth", {}).get("type") == "oauth-personal":
            return
    except (OSError, ValueError, AttributeError):
        pass
    raise EditorError(LOGIN_NEEDED)


# Only this fixed program may be registered as an MCP stdio server by an editor session.
# It needs no access to the slot installation or the owner's source trees.
MCP_STDIO = """import socket,sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
with s.makefile('rb') as src:
    for line in sys.stdin.buffer:
        import json
        if len(line) > 4194304: break
        msg = json.loads(line)
        s.sendall(line)
        if 'id' in msg and 'method' in msg:
            reply = src.readline(4194305)
            if not reply or len(reply) > 4194304: break
            sys.stdout.buffer.write(reply)
            sys.stdout.buffer.flush()
"""


# Google's standalone agent loads these documented global hooks from its disposable
# HOME. A command hook is fixed by the slot; its executable, configuration and code
# are read-only to the Google process. Nothing in the client project is executed here.
TOOL_POLICY = """import json, sys
try:
    tool = json.load(sys.stdin)["toolCall"]
    name = tool["name"]
    allowed = {"client_view_file", "client_create_file", "client_edit_file"}
    mcp = {"list_project", "make_project_directory", "test_project"}
    permitted = name in allowed
    if name == "call_mcp_tool" or name in mcp:
        args = tool.get("args", {})
        actual = args.get("ToolName")
        permitted = (args.get("ServerName") == "ccfleetlocal" and actual in mcp
                     and name in ("call_mcp_tool", actual))
except (ValueError, AttributeError, KeyError, TypeError):
    permitted = False
print(json.dumps({"decision": "allow" if permitted else "deny",
                  "reason": "This session uses the connected computer's editor tools."}))
"""


def session_profile(home, session):
    # Google uses the current macOS Keychain for the existing personal sign-in. Only
    # its standard OS location is linked; no credential is read or copied by CCFleet.
    profile = session / "home"
    library = profile / "Library"
    library.mkdir(mode=0o700, parents=True)
    (library / "Keychains").symlink_to(home / "Library/Keychains", target_is_directory=True)
    cfg = profile / ".gemini/config"
    cfg.mkdir(mode=0o700, parents=True)
    acp = cfg.parent / "antigravity-acp"
    acp.mkdir(mode=0o700)
    (acp / "settings.json").write_text(json.dumps({"auth": {"type": "oauth-personal"}}))
    hook = cfg / "editor-policy.py"
    hook.write_text(TOOL_POLICY)
    command = shlex.join(["/usr/bin/python3", "-I", "-B", str(hook)])
    hooks = {
        "ccfleet-editor": {
            "enabled": True,
            "PreToolUse": [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}],
        }
    }
    (cfg / "hooks.json").write_text(json.dumps(hooks))
    return profile


def interact(agent, device):
    device.agent = agent
    device.send({"type": "ready", "model": agent.model, "models": agent.models,
                 "capabilities": {"cancelTurn": True}})
    while True:
        msg = device.receive(3600)
        if msg == {"type": "close"}:
            return
        if set(msg) == {"type", "model"} and msg["type"] == "set_model":
            try:
                agent.select_model(msg["model"])
            except ModelUnavailable as exc:
                device.send({"type": "model", "model": agent.model, "error": str(exc)})
            else:
                device.send({"type": "model", "model": agent.model})
            continue
        if (msg.get("type") != "prompt"
                or set(msg) not in ({"type", "text"}, {"type", "text", "turn"})):
            raise EditorError("The workspace sent an unexpected message.")
        prompt = text_value(msg["text"], 65536)
        if not prompt.strip():
            raise EditorError("Enter a project request to start a turn.")
        tag = msg.get("turn")
        device.begin_turn(tag)
        try:
            response = agent.rpc(
                "session/prompt",
                {
                    "sessionId": agent.sid,
                    "prompt": [{"type": "text", "text": GUIDE + prompt}],
                },
                timeout=900,
                turn=tag,
            )
        finally:
            device.finish_turn()
            agent.approvals.clear()
        reason = response.get("stopReason")
        if not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,48}", reason):
            raise EditorError("The Gemini editor returned an incomplete turn.")
        end = {"type": "end", "reason": reason}
        if tag is not None:
            end["turn"] = tag
            if device.cancelled_turn == tag:
                end["reason"] = "cancelled"
        device.send(end)


def run_editor(home, device, binary, session):
    profile = session_profile(home, session)
    project = session / "project"
    project.mkdir(mode=0o700)
    path = session / "m.sock"
    mcp = [
        {
            "name": MCP_NAME,
            "command": "/usr/bin/python3",
            "args": ["-I", "-B", "-c", MCP_STDIO, str(path)],
            "env": [],
        }
    ]
    with (
        ProjectMCP(str(path), ProjectHandler) as service,
        GoogleProxy(("127.0.0.1", 0), GoogleConnection) as google,
    ):
        os.chmod(path, 0o600)
        env = environment(profile, session)
        proxy = "http://127.0.0.1:" + str(google.server_address[1])
        env.update(
            HTTPS_PROXY=proxy,
            HTTP_PROXY=proxy,
            https_proxy=proxy,
            http_proxy=proxy,
            NO_PROXY="127.0.0.1,localhost,::1",
            no_proxy="127.0.0.1,localhost,::1",
        )
        proc = _start_agent(_sandbox(binary, home, session), env, project)
        threading.Thread(target=google.serve_forever, daemon=True).start()
        device.attach(proc)
        try:
            agent = Agent(proc, device, project)
            service.device = device
            service.project = project
            service.turn = agent.active
            threading.Thread(target=service.serve_forever, daemon=True).start()
            try:
                agent.initialize()
                agent.session(project, mcp)
                interact(agent, device)
            finally:
                service.shutdown()
        finally:
            stop_agent(proc)
            google.shutdown()


def worker(fd):
    owner = mac_policy.authenticated_owner()
    home = Path(owner.pw_dir)
    with socket.socket(fileno=fd) as sock:
        mac_relay_client.require_peer(sock, owner.pw_uid)
        sock.settimeout(8)
        fingerprint = sock.recv(32, socket.MSG_WAITALL)
        if len(fingerprint) != 32:
            return 2

        def authorized():
            return antigravity_slot.permitted(home, fingerprint)

        if not authorized():
            return 2
        sock.settimeout(None)
        device = Device(sock, authorized)
        device.start()
        try:
            opening = device.receive(15)
            if opening != {"type": "open", "version": 1} or type(opening["version"]) is not int:
                raise EditorError("Update your CCFleet client to open the Gemini editor.")
            require_personal(home)
            binary = runtime(home)
            if not device.closed.is_set() and authorized():
                with tempfile.TemporaryDirectory(
                    dir="/private/tmp", prefix="ccfleet-editor-"
                ) as tmp:
                    run_editor(home, device, binary, Path(tmp))
            return 0
        except (OSError, ValueError, KeyError, subprocess.SubprocessError, EOFError) as exc:
            with contextlib.suppress(OSError, EOFError):
                device.send(
                    {
                        "type": "error",
                        "message": str(exc) if isinstance(exc, EditorError) else UNAVAILABLE,
                    }
                )
            return 2
        finally:
            device.abort()
            device.input.close()
            device.output.close()


def serve():
    os.umask(0o077)
    owner = mac_policy.authenticated_owner()
    root = mac_policy.directory(Path(owner.pw_dir))
    directory = mac_policy.open_directory(root, private=True)
    os.close(directory)
    with (root / "gemini-editor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = root / SOCKET
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(5)
            child = None
            while True:
                conn, _ = server.accept()
                with conn:
                    try:
                        mac_relay_client.require_peer(conn, owner.pw_uid)
                        conn.settimeout(1)
                        role = conn.recv(1)
                        if role == b"P":
                            conn.sendall(b"Y")
                            continue
                        if role != b"S":
                            continue
                        conn.settimeout(None)
                        if child is not None and child.poll() is None:
                            # Read the fingerprint before returning the busy error.
                            conn.settimeout(2)
                            conn.recv(32, socket.MSG_WAITALL)
                            with conn.makefile("wb") as stream:
                                gemini_preview.send(
                                    stream,
                                    {
                                        "type": "error",
                                        "message": BUSY,
                                    },
                                )
                            continue
                        child = subprocess.Popen(
                            [
                                sys.executable,
                                "-I",
                                "-B",
                                str(Path(__file__).resolve()),
                                "worker",
                                str(conn.fileno()),
                            ],
                            pass_fds=(conn.fileno(),),
                        )
                    except (OSError, ValueError):
                        continue


def relay(home, fingerprint):
    if not antigravity_slot.permitted(home, fingerprint):
        return 2
    path = mac_policy.directory(home) / SOCKET
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(8)
        sock.connect(str(path))
        mac_relay_client.require_peer(sock, os.getuid())
        sock.sendall(b"S" + fingerprint)
        sock.settimeout(None)
        workspace_io.pipe_socket(sock)
    return 0


def login():
    # Owner GUI only. The forced SSH dispatcher deliberately has no login operation.
    owner = mac_policy.authenticated_owner()
    home = Path(owner.pw_dir)
    binary = runtime(home)
    with tempfile.TemporaryDirectory(dir="/private/tmp", prefix="ccfleet-editor-login-") as tmp:
        folder = Path(tmp)
        proc = _start_agent([str(binary)], environment(home, folder), folder)
        try:
            agent = Agent(proc)
            agent.initialize()
            try:
                require_personal(home)
                agent.session(folder, [])
                return 0
            except EditorError:
                pass
            agent.rpc("authenticate", {"methodId": "oauth-personal"}, timeout=600)
            require_personal(home)
            agent.session(folder, [])
            return 0
        finally:
            stop_agent(proc)


def main(argv):
    if argv == ["serve"]:
        return serve()
    if argv == ["login"]:
        return login()
    if len(argv) == 2 and argv[0] == "worker":
        return worker(int(argv[1]))
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (EOFError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit(2)
