"""Local ACP v1 editor provider over CCFleet's paired Antigravity device transport."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

VERSION = "0.2.18"
MAX_RECORD = 4 * 1024 * 1024
SLOT_BUSY = "Another Gemini editor is using this slot. Close it and reconnect."
BUSY_RETRY_WINDOW = 10.0
BUSY_RETRY_DELAY = 0.4
ALLOW = {"optionId": "allow", "kind": "allow_once", "name": "Allow once"}
DECLINE = {"optionId": "decline", "kind": "reject_once", "name": "Decline"}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def invalid(message):
    return RpcError(-32602, message)


def prompt_text(blocks, limit, workspace):
    if not isinstance(blocks, list) or not blocks:
        raise invalid("Enter a text message.")
    lines = []
    for block in blocks:
        if not isinstance(block, dict):
            raise invalid("Expected text or a local file reference.")
        if block.get("type") == "resource_link" and isinstance(block.get("uri"), str):
            try:
                uri = urlsplit(block["uri"])
                if (uri.scheme != "file" or uri.netloc not in ("", "localhost")
                        or uri.query or uri.fragment):
                    raise ValueError()
                relative = Path(unquote(uri.path, errors="strict")).relative_to(workspace.path)
                name = workspace.files._path(relative.as_posix())
                workspace._guard(name)
            except (ValueError, OSError):
                raise invalid("Choose a file in this workspace.") from None
            lines.append("Referenced project file: " + name)
            continue
        if block.get("type") != "text":
            raise invalid("This Gemini provider accepts text and project file references.")
        text = block.get("text")
        if not isinstance(text, str):
            raise invalid("Invalid text prompt.")
        lines.append(text)
    value = "\n".join(lines)
    if not value.strip() or len(value.encode()) > limit:
        raise invalid("Enter a nonempty message within the Gemini prompt limit.")
    return value


def edit_preview(path, before, after, visible):
    """T3 currently reduces an ACP permission diff to a generic "Changed files" card."""
    def lines(value):
        parts = value.split("\n")
        if parts[-1] == "":
            return parts[:-1]
        return [*parts, "\\ No newline at end of file"]

    diff = difflib.unified_diff(lines(before), lines(after), "a/" + path, "b/" + path, lineterm="")
    # Indented code displays filenames and untrusted contents as literal text, including fences.
    return "Proposed local change\n\n" + "\n".join("    " + visible(line) for line in diff) + "\n\n"


class Session:
    def __init__(self, host, params):
        self.host = host
        self.id = secrets.token_urlsafe(24)
        raw = params.get("cwd")
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise invalid("Choose an existing absolute local workspace folder.")
        self.cancelled = threading.Event()
        private = host.tools.PrivateFolders(host.fleet_home)
        try:
            self.workspace = host.tools.Workspace(
                raw, self.confirm_action, host.test, file_tools=host.files,
                private_folders=private, cancelled=self.unavailable,
            )
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            raise invalid("The local workspace or test runner is unavailable.") from exc
        self.process = None
        self.lock = threading.Lock()
        self.channel = None
        self.current = None
        self.models = []
        self.seen = {}
        self.approved = {}
        self.writing = None
        self.local_tool = None

    def _open_channel(self):
        # Only the launcher chooses this fixed command and environment. ACP mcpServers, _meta,
        # shell arguments and environment from the selected project are never sent to the slot.
        with self.lock:
            if self.cancelled.is_set():
                raise EOFError
            self.process = subprocess.Popen(
                self.host.command, cwd="/", env=self.host.environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            self.channel = self.host.editor.Channel(self.process)
        self.channel.send({"type": "open", "version": 1})
        return self.channel.receive(timeout=420)

    def open(self):
        if self.host.test_folder:
            if self.workspace.test_directory(self.host.test_folder) is None:
                raise invalid("Choose an ordinary local test subfolder.")
            self.workspace.test_relative = self.host.test_folder
        # A short-lived T3 model-discovery session may overlap the first actual thread.
        # Retry only admission explicitly refused as busy, before any prompt can be sent.
        deadline = None
        delay = BUSY_RETRY_DELAY
        while True:
            try:
                ready = self._open_channel()
                break
            except ValueError as exc:
                reason = str(exc)
                if ("CCFleet Gemini Sign In" in reason
                        or reason.startswith("Sign in to the Antigravity")):
                    raise RpcError(-32000, "Open CCFleet Gemini Sign In on your private Mac, "
                                   "then start a new thread.") from None
                if reason != SLOT_BUSY:
                    raise
            self._retire_channel(graceful=False)
            now = time.monotonic()
            if deadline is None:
                deadline = now + BUSY_RETRY_WINDOW
            if now + delay > deadline:
                raise RpcError(-32000, "Another Gemini session is using your slot. "
                               "Close it before opening a new T3 Gemini thread.")
            if self.cancelled.wait(delay):
                raise EOFError
            delay = min(1.5, delay * 2)
        self.current = ready.get("model")
        if ready.get("type") != "ready" or not isinstance(self.current, str):
            raise ValueError("Invalid Gemini session.")
        models = ready.get("models", [self.current])
        if (not isinstance(models, list) or not 1 <= len(models) <= 256
                or any(not isinstance(m, str) or not self.host.editor.MODEL_ID.fullmatch(m)
                       for m in models) or self.current not in models
                or len(set(models)) != len(models)):
            raise ValueError("Invalid model catalog.")
        self.models = models
        return {"sessionId": self.id, "configOptions": self.config(), "models": {
            "currentModelId": self.current, "availableModels": [
                {"modelId": m, "name": m} for m in models
            ],
        }}

    def unavailable(self):
        return self.cancelled.is_set() or (
            self.process is not None and self.process.poll() is not None
        )

    def interrupt(self):
        with self.lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            if self.process and self.process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)

    def _retire_channel(self, *, graceful):
        with self.lock:
            proc, channel = self.process, self.channel
            already_signalled = self.cancelled.is_set()
            self.process = self.channel = None
        if proc is None:
            return
        if proc.poll() is None and not already_signalled:
            if graceful:
                with contextlib.suppress(OSError, ValueError):
                    channel.send({"type": "close"})
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=1)
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        with contextlib.suppress(OSError):
            proc.stdin.close()
        proc.stdout.close()

    def close(self):
        self._retire_channel(graceful=True)
        self.workspace.close()

    def update(self, event, **fields):
        self.host.write({"method": "session/update", "params": {
            "sessionId": self.id, "update": {"sessionUpdate": event, **fields},
        }})

    def text(self, value):
        self.update("agent_message_chunk", content={"type": "text", "text": value})

    def config(self):
        return [{"id": "model", "name": "Model", "category": "model", "type": "select",
                 "currentValue": self.current,
                 "options": [{"name": m, "value": m} for m in self.models]}]

    def select_model(self, model):
        # T3 may send its placeholder before the user selects one of the advertised models.
        if model == "default":
            model = self.current
        if model not in self.models:
            raise invalid("Choose a Gemini model offered by this slot.")
        if model != self.current:
            self.channel.send({"type": "set_model", "model": model})
            reply = self.channel.receive()
            if reply.get("type") != "model" or reply.get("model") not in self.models:
                raise ValueError("Gemini did not confirm the current model.")
            self.current = reply["model"]
            self.update("config_option_update", configOptions=self.config())
            if "error" in reply or self.current != model:
                raise RpcError(-32000, "The Gemini slot declined that selection.")
        return self.config()

    def permission(self, title, kind, location, content):
        if self.unavailable():
            return False
        tool = {"toolCallId": secrets.token_urlsafe(16), "title": title, "kind": kind,
                "status": "pending", "locations": [{"path": str(location)}], "content": content}
        self.update("tool_call", **tool)
        tool_id = tool["toolCallId"]
        if self.host.ask(self, tool):
            return tool_id
        self.finish_tool(tool_id, False)
        return None

    def finish_tool(self, tool_id, success):
        if tool_id and not self.cancelled.is_set():
            self.update("tool_call_update", toolCallId=tool_id,
                        status="completed" if success else "failed")

    def confirm_action(self, action, detail):
        if self.unavailable():
            return False
        if action == "Save file":
            return self.writing == detail
        if action == "Create directory":
            location = self.workspace.path / detail
            kind, title = "edit", f"Create local directory {detail}"
        elif action == "Run local tests":
            location = self.workspace.test_directory()
            if location is None:
                return False
            kind, title = "execute", f"Run local tests: {detail}"
        else:
            return False
        title = self.host.editor.terminal_text(title)
        self.text(title + "\n\n")
        content = [{"type": "content", "content": {"type": "text", "text": title}}]
        self.local_tool = self.permission(title, kind, location, content)
        return bool(self.local_tool)

    def approve_edit(self, path, content):
        previous = self.approved.pop(path, None)
        if previous:
            self.finish_tool(previous[2], False)
        if len(self.approved) >= 32:
            raise ValueError("Too many pending edits.")
        if len(content.encode()) > self.host.tools.LIMIT:
            raise ValueError("File too large.")
        item = self.workspace.read_optional(path)
        expected = item["sha256"] if item else "new"
        if expected != self.seen.get(path, "new"):
            raise ValueError("Read the file again before editing.")
        before = item["text"] if item else ""
        location = self.workspace.path / path
        title = ("Edit " if item else "Create ") + self.host.editor.terminal_text(path)
        self.text(edit_preview(path, before, content, self.host.editor.terminal_text))
        tool_id = self.permission(title, "edit", location, [{
            "type": "diff", "path": str(location), "oldText": before if item else None,
            "newText": content}])
        if not tool_id:
            return False
        self.approved[path] = (hashlib.sha256(content.encode()).digest(), expected, tool_id)
        return True

    def file_action(self, msg):
        if self.unavailable():
            raise EOFError
        op = msg.get("op")
        fields = self.host.editor.REQUEST_FIELDS.get(op) if isinstance(op, str) else None
        if (fields is None or set(msg) != {"type", "id", "op"} | fields
                or any(not isinstance(msg[k], str) for k in fields)):
            raise ValueError("Invalid workspace request.")
        path = msg.get("path")
        if op == "read":
            item = self.workspace.call("read_project", {"path": path})
            if path not in self.seen and len(self.seen) >= 10000:
                self.seen.pop(next(iter(self.seen)))
            self.seen[path] = item["sha256"]
            return item["text"]
        if op == "list":
            return self.workspace.call("list_project", {"path": path})
        if op == "approve":
            return self.approve_edit(path, msg["content"])
        if op == "write":
            grant = self.approved.pop(path, None)
            if not grant:
                raise ValueError("Unapproved edit.")
            if hashlib.sha256(msg["content"].encode()).digest() != grant[0]:
                self.finish_tool(grant[2], False)
                raise ValueError("Unapproved edit.")
            self.writing = path
            success = False
            try:
                saved = self.workspace.call("save_project", {
                    "path": path, "text": msg["content"], "expected": grant[1],
                })
                self.seen[path] = saved["sha256"]
                success = True
            finally:
                self.writing = None
                self.finish_tool(grant[2], success)
            return True
        success = False
        try:
            if op == "mkdir":
                self.workspace.call("make_project_directory", {"path": path})
                result = True
            else:
                if self.workspace.command is None:
                    raise ValueError("No local test runner is configured.")
                result = self.workspace.call("test_project", {})
            success = True
            return result
        finally:
            self.finish_tool(self.local_tool, success)
            self.local_tool = None

    def prompt(self, params):
        prompt = prompt_text(params.get("prompt"), self.host.editor.MAX_PROMPT, self.workspace)
        parts = prompt.strip().split(maxsplit=1)
        if parts[0] == "/help":
            self.text("Use T3’s model picker or /model to see available Gemini models. "
                      "File changes and enabled local tests request permission in T3. "
                      "Use Supervised mode to review each request.")
            return {"stopReason": "end_turn"}
        if parts[0] == "/model":
            if len(parts) == 1:
                self.text("Current model: " + self.current + "\n\n" + "\n".join(
                    f"{i}. {name}" for i, name in enumerate(self.models, 1)
                ) + "\n\nUse T3’s model picker or /model <number or ID> to switch.")
            else:
                requested = parts[1]
                model = next((m for i, m in enumerate(self.models, 1) if requested == str(i)),
                             requested)
                try:
                    self.select_model(model)
                    self.text("Using " + self.current + ".")
                except RpcError as exc:
                    self.text(str(exc))
            return {"stopReason": "end_turn"}
        ids = set()
        self.channel.send({"type": "prompt", "text": prompt})
        try:
            while not self.cancelled.is_set():
                msg = self.channel.receive()
                kind = msg.get("type")
                if kind == "end" and isinstance(msg.get("reason"), str):
                    reason = msg["reason"]
                    return {"stopReason": {
                        "refused": "refusal", "refusal": "refusal", "end_turn": "end_turn",
                        "cancelled": "cancelled", "max_tokens": "max_tokens",
                        "max_turn_requests": "max_turn_requests",
                    }.get(reason, "refusal")}
                if kind == "text" and isinstance(msg.get("text"), str):
                    self.text(msg["text"])
                    continue
                ident = msg.get("id")
                if (kind != "request" or type(ident) is not int or ident <= 0
                        or ident in ids or len(ids) >= 4096):
                    raise ValueError("Unexpected slot record.")
                ids.add(ident)
                response = {"type": "response", "id": ident}
                try:
                    response["result"] = self.file_action(msg)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    response["error"] = "Local operation declined, protected or unavailable."
                if not self.cancelled.is_set():
                    self.channel.send(response)
            return {"stopReason": "cancelled"}
        finally:
            for _, _, tool_id in self.approved.values():
                self.finish_tool(tool_id, False)
            self.approved.clear()


class Bridge:
    """Read editor messages independently of slot work so cancellation interrupts blocked I/O."""

    def __init__(self, command, environment, editor, tools, files, fleet_home, test, test_folder,
                 stdin, stdout):
        self.command = command
        self.environment = environment
        self.editor = editor
        self.tools = tools
        self.files = files
        self.fleet_home = fleet_home
        self.test = test
        self.test_folder = test_folder
        self.stdin = stdin
        self.stdout = stdout
        self.output_lock = threading.Lock()
        self.lock = threading.RLock()
        self.initialized = False
        self.session = None
        self.active = None
        self.worker = None
        self.pending = {}
        self.closed = False

    def write(self, msg):
        raw = json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False, allow_nan=False).encode()
        if len(raw) > MAX_RECORD:
            raise ValueError("ACP response too large.")
        with self.output_lock:
            self.stdout.write(raw + b"\n")
            self.stdout.flush()

    def error(self, ident, code, message):
        self.write({"id": ident, "error": {"code": code, "message": message}})

    def ask(self, session, tool):
        ident = "permission-" + secrets.token_urlsafe(18)
        answer = queue.Queue(maxsize=1)
        with self.lock:
            if self.closed or session.cancelled.is_set():
                return False
            self.pending[ident] = answer
        try:
            self.write({"id": ident, "method": "session/request_permission", "params": {
                "sessionId": session.id, "toolCall": tool, "options": [ALLOW, DECLINE],
            }})
            # The slot's device callback allows ten minutes for a human decision.
            deadline = time.monotonic() + 590
            while not session.unavailable() and time.monotonic() < deadline:
                try:
                    reply = answer.get(timeout=0.25)
                except queue.Empty:
                    continue
                return (not session.unavailable() and isinstance(reply, dict)
                        and reply.get("outcome") == {"outcome": "selected", "optionId": "allow"})
            return False
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def interrupt(self):
        if self.session:
            self.session.interrupt()
        for answer in self.pending.values():
            with contextlib.suppress(queue.Full):
                answer.put_nowait(None)

    def discard(self, session):
        session.close()
        if self.session is session:
            self.session = None

    def perform(self, session, method, params, ident):
        result = None
        error = None
        broken = False
        try:
            if method == "session/new":
                result = session.open()
            elif method == "session/prompt":
                result = session.prompt(params)
            else:
                if method == "session/set_model":
                    session.select_model(params.get("modelId"))
                    result = {}
                else:
                    if params.get("configId") != "model":
                        raise invalid("Unknown session option.")
                    result = {"configOptions": session.select_model(params.get("value"))}
        except RpcError as exc:
            error = exc
        except (OSError, EOFError, ValueError, TypeError, subprocess.SubprocessError):
            broken = True
            error = RpcError(-32000, "The private Gemini connection ended. Open a new T3 thread.")
        finally:
            with self.lock:
                try:
                    if session.cancelled.is_set() and method == "session/prompt":
                        result, error = {"stopReason": "cancelled"}, None
                        if not self.closed:
                            session.text("Cancelled. Open a new T3 thread to reconnect to Gemini.")
                    if broken or session.cancelled.is_set() or self.closed or (
                            error and method == "session/new"):
                        self.discard(session)
                    if not self.closed:
                        if error:
                            self.error(ident, error.code, str(error))
                        else:
                            self.write({"id": ident, "result": result})
                except (OSError, ValueError):
                    if self.session is session:
                        self.discard(session)
                    self.closed = True
                finally:
                    self.active = None

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            self.error(None, -32600, "Invalid JSON-RPC message.")
            return
        method = msg.get("method")
        ident = msg.get("id")
        with self.lock:
            if method is None:
                if isinstance(ident, str) and ident in self.pending:
                    with contextlib.suppress(queue.Full):
                        self.pending[ident].put_nowait(msg.get("result"))
                return
            params = msg.get("params", {})
            if ident is None:
                if (method == "session/cancel" and isinstance(params, dict) and self.session
                        and params.get("sessionId") == self.session.id
                        and self.active == "session/prompt"):
                    self.interrupt()
                return
            if type(ident) not in (str, int) or not isinstance(method, str):
                self.error(None, -32600, "Invalid request.")
                return
            try:
                if not isinstance(params, dict):
                    raise invalid("Expected an object of parameters.")
                if method == "initialize":
                    version = params.get("protocolVersion")
                    if self.initialized or type(version) is not int or version < 1:
                        raise invalid("This editor provider requires a new ACP v1 connection.")
                    self.initialized = True
                    self.write({"id": ident, "result": {
                        "protocolVersion": 1,
                        "agentInfo": {"name": "ccfleet-gemini", "title": "CCFleet Gemini",
                                      "version": VERSION},
                        "agentCapabilities": {"sessionCapabilities": {"close": {}}},
                        "authMethods": [],
                    }})
                    return
                if not self.initialized:
                    raise RpcError(-32000, "Initialize the ACP connection first.")
                if method not in ("session/new", "session/prompt", "session/set_config_option",
                                  "session/set_model", "session/close"):
                    raise RpcError(-32601, "Method not available.")
                if method != "session/new" and (
                        not self.session or params.get("sessionId") != self.session.id):
                    raise invalid("Session unavailable. Start a new T3 thread.")
                if method == "session/close":
                    if self.active:
                        self.interrupt()
                    else:
                        self.discard(self.session)
                    self.write({"id": ident, "result": {}})
                    return
                if self.active:
                    raise RpcError(-32000, "A Gemini request is in progress.")
                if method == "session/new":
                    if self.session is not None:
                        raise RpcError(-32000, "This connection already has an open Gemini thread.")
                    self.session = Session(self, params)
                self.active = method
                self.worker = threading.Thread(
                    target=self.perform, args=(self.session, method, params, ident), daemon=True,
                )
                self.worker.start()
            except RpcError as exc:
                self.error(ident, exc.code, str(exc))

    def serve(self):
        try:
            while not self.closed:
                line = self.stdin.readline(MAX_RECORD + 1)
                if not line:
                    break
                if len(line) > MAX_RECORD or not line.endswith(b"\n"):
                    self.error(None, -32600, "ACP record exceeds the limit or is incomplete.")
                    return 2
                try:
                    msg = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    self.error(None, -32700, "Invalid JSON.")
                    continue
                self.handle(msg)
        except (EOFError, BrokenPipeError):
            pass
        finally:
            with self.lock:
                self.closed = True
                self.interrupt()
                if self.session is not None and not self.active:
                    self.discard(self.session)
            if self.worker and self.worker.ident is not None:
                self.worker.join(timeout=5)
        return 0


def run(command, environment, editor, tools, files, *, fleet_home, test=None, test_folder=None,
        stdin=None, stdout=None):
    """Run the ACP adapter. Only the trusted local launcher supplies pairing and runner options."""
    server = Bridge(command, environment, editor, tools, files, fleet_home, test, test_folder,
                    sys.stdin.buffer if stdin is None else stdin,
                    sys.stdout.buffer if stdout is None else stdout)
    if threading.current_thread() is not threading.main_thread():
        return server.serve()

    def terminate(signum, _frame):
        # T3 can terminate its ACP child with SIGTERM while SSH and the test runner
        # each have a separate process group. Always execute serve()'s teardown.
        raise SystemExit(128 + signum)

    original = signal.signal(signal.SIGTERM, terminate)
    try:
        return server.serve()
    finally:
        signal.signal(signal.SIGTERM, original)
