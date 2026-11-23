"""Local ACP v1 editor provider over CCFleet's paired Antigravity device transport."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import queue
import secrets
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

VERSION = "0.2.24"
# ACP reserves -32000 for sessions which require authentication.
AUTH_REQUIRED = -32000
INTERNAL_ERROR = -32603
MAX_RECORD = 4 * 1024 * 1024
# T3 limits each received ACP text block to 65,536 UTF-16 code units.
TEXT_CHUNK_CHARS = 8192
SLOT_BUSY = "Another Gemini editor is using this slot. Close it and reconnect."
BUSY_RETRY_WINDOW = 10.0
BUSY_RETRY_DELAY = 0.4
CANCEL_GRACE = 15.0  # Allow the slot’s 12-second cancellation deadline to finish first.
ALLOW = {"optionId": "allow", "kind": "allow_once", "name": "Allow once"}
DECLINE = {"optionId": "decline", "kind": "reject_once", "name": "Decline"}


# These strings are part of the paired slot protocol, including older slot releases.
# Unrecognized text may contain host paths, credentials or vendor diagnostics.
OPEN_TIMEOUT = "Gemini startup timed out. Check that the private Mac slot is online."
OPEN_TRANSPORT = ("The connection to the private Mac closed before Gemini was ready. "
                  "Check the slot and this Mac’s pairing on ccfleet.io/account/gemini.")
OPEN_REFUSED = "The private slot could not open Gemini. Check Antigravity on the slot Mac."
BAD_READY = "The slot returned an invalid Gemini startup reply. Check its CCFleet service version."
SLOT_SESSION_TIMEOUT = "The Gemini editor session timed out. Reopen it to continue."
RESUME_MISSING = "The previous Gemini session cannot be resumed."
FRESH_REQUIRED = ("The previous Gemini conversation is unavailable on the slot. "
                  "Send /reconnect to start a fresh Gemini conversation in this T3 thread. "
                  "Gemini will no longer have the previous conversation context.")
TURN_LOST = ("The connection to Gemini was interrupted. The previous request may have partly "
             "run. Check any changes before continuing. CCFleet will not resend that request.")
SLOT_LOGIN_REQUIRED = {
    "Open CCFleet Gemini Sign In in the private Mac owner's Applications folder, then reconnect.",
    "Sign in to the Antigravity editor",
}
OPEN_SLOT_ERRORS = {
    "The Gemini editor is unavailable on the private Mac. Reopen it to try again.":
        "The private Mac could not start its Gemini editor. Check the slot service.",
    "The Gemini slot timed out. Reopen the editor to continue.": OPEN_TIMEOUT,
    SLOT_SESSION_TIMEOUT: OPEN_TIMEOUT,
    "The Google editor agent stopped on the slot.":
        "Antigravity stopped during startup on the private Mac. Check the slot service.",
    "The Google editor could not complete this request.": OPEN_REFUSED,
    "The signed-in Google account has no available Gemini editor model.":
        "Antigravity on the private Mac offered no Gemini models. Check the account on the slot.",
    "The Google editor declined the model change.":
        "Antigravity on the private Mac declined the starting Gemini model.",
    "The installed Google editor agent is incompatible. Update CCFleet on the slot.":
        "The slot’s Antigravity agent is incompatible with CCFleet. Update the slot service.",
    "Update your CCFleet client to open the Gemini editor.":
        "The slot requires a newer CCFleet client. Run ccfleet update on this Mac.",
    "invalid Gemini editor frame size": BAD_READY,
    "invalid Gemini editor message": BAD_READY,
}


# A valid error frame reporting the slot/provider's loss can have a recoverable checkpoint.
# Malformed frames and protocol violations always close the ACP session.
INTERRUPTED_SLOT_ERRORS = {
    SLOT_SESSION_TIMEOUT,
    "The Gemini slot timed out. Reopen the editor to continue.",
    "The Google editor agent stopped on the slot.",
    "The Google editor could not complete this request.",
    "The Gemini editor is unavailable on the private Mac. Reopen it to try again.",
}


BROKER_START_ERRORS = {
    b"ccfleet: broker refused the WebSocket connection (HTTP 401)\n":
        (AUTH_REQUIRED, "CCFleet did not accept this Mac’s Gemini pairing (HTTP 401). "
         "Run ccfleet pair gemini --replace with your current setup code."),
    b"ccfleet: broker refused the WebSocket connection (HTTP 403)\n":
        (INTERNAL_ERROR, "CCFleet refused slot access (HTTP 403). "
         "Check your slot on ccfleet.io/account."),
    b"ccfleet: broker refused the WebSocket connection (HTTP 502)\n":
        (INTERNAL_ERROR, "CCFleet could not reach the private Mac (HTTP 502). "
         "Check the slot connection."),
    b"ccfleet: broker refused the WebSocket connection (HTTP 503)\n":
        (INTERNAL_ERROR, "The private slot connection is unavailable (HTTP 503). "
         "Check the slot connection."),
    b"ccfleet: broker refused the WebSocket connection (HTTP 504)\n":
        (INTERNAL_ERROR, "The private slot connection timed out (HTTP 504). "
         "Check the slot connection."),
}


def collect_broker_status(stream, status):
    """Drain SSH stderr; hand off only complete, fixed local proxy status lines."""
    message = None
    try:
        with stream:
            continuation = False
            for chunk in iter(lambda: stream.readline(1024), b""):
                if not continuation and message is None:
                    message = BROKER_START_ERRORS.get(chunk)
                    if message is not None:
                        status.put_nowait(message)
                continuation = not chunk.endswith(b"\n")
                del chunk
    except OSError:
        pass
    finally:
        if message is None:
            status.put_nowait(None)


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SessionRejected(RpcError):
    """Do not reuse a session after a protocol mismatch or rejection of its access."""


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
        self.cancelled = threading.Event()  # Logical session terminated; never cleared.
        self.turn_cancelled = threading.Event()
        self.cancel_timer = None
        self.can_cancel_turn = False
        self.turn_number = 0
        self.active_turn = None
        self.send_lock = threading.Lock()
        private = host.tools.PrivateFolders(host.fleet_home)
        try:
            self.workspace = host.tools.Workspace(
                raw, self.confirm_action, host.test, file_tools=host.files,
                private_folders=private, cancelled=self.unavailable,
            )
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            raise invalid("The local workspace or test runner is unavailable.") from exc
        self.commands = host.editor.ProjectCommands(
            host.tools, host.files, self.workspace.path, host.fleet_home,
            root_fd=self.workspace.root_fd)
        self.resume_token = None
        self.offered_token = None
        self.reconnecting = False
        self.process = None
        self.lock = threading.Lock()
        self.channel = None
        self.current = None
        self.models = []
        self.seen = {}
        self.approved = {}
        self.writing = None
        self.local_tool = None

    def _open_channel(self, token):
        # Only the launcher chooses this fixed command and environment. ACP mcpServers, _meta,
        # shell arguments and environment from the selected project are never sent to the slot.
        with self.lock:
            if self.cancelled.is_set() or self.reconnecting and self.turn_cancelled.is_set():
                raise EOFError
            try:
                self.process = subprocess.Popen(
                    self.host.command, cwd="/", env=self.host.environment, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                )
            except OSError:
                raise RpcError(INTERNAL_ERROR,
                               "CCFleet could not start the local SSH transport.") from None
            status = queue.Queue(maxsize=1)
            threading.Thread(target=collect_broker_status, args=(self.process.stderr, status),
                             daemon=True).start()
            self.channel = self.host.editor.Channel(self.process)
        try:
            with self.send_lock:
                if token is None:
                    self.channel.send({"type": "open", "version": 1})
                else:
                    self.channel.send({"type": "resume", "version": 1, "resumeToken": token})
            return self.channel.receive(timeout=420)
        except (TimeoutError, subprocess.TimeoutExpired):
            raise RpcError(INTERNAL_ERROR, OPEN_TIMEOUT) from None
        except (EOFError, OSError):
            # SSH and its proxy have separate output streams. Give the collector time to
            # see a diagnostic already in flight, without waiting for SSH to terminate.
            try:
                reason = status.get(timeout=0.25)
            except queue.Empty:
                reason = None
            if reason and (reason[0] == AUTH_REQUIRED or "(HTTP 403)" in reason[1]):
                raise SessionRejected(*reason) from None
            raise RpcError(*(reason or (INTERNAL_ERROR, OPEN_TRANSPORT))) from None

    def open(self):
        if self.host.test_folder:
            if self.workspace.test_directory(self.host.test_folder) is None:
                raise invalid("Choose an ordinary local test subfolder.")
            self.workspace.test_relative = self.host.test_folder
        self.connect()
        return {"sessionId": self.id, "configOptions": self.config(), "models": {
            "currentModelId": self.current, "availableModels": [
                {"modelId": m, "name": m} for m in self.models
            ],
        }}

    def connect(self, token=None):
        previous_model = self.current
        # A short-lived T3 model-discovery session may overlap the first actual thread.
        # Retry only admission explicitly refused as busy, before any prompt can be sent.
        deadline = None
        delay = BUSY_RETRY_DELAY
        while True:
            try:
                ready = self._open_channel(token)
                break
            except ValueError as exc:
                reason = str(exc)
                if token is not None and reason == RESUME_MISSING:
                    self.resume_token = None
                    raise RpcError(INTERNAL_ERROR, FRESH_REQUIRED) from None
                if reason in SLOT_LOGIN_REQUIRED:
                    raise SessionRejected(AUTH_REQUIRED, "Open CCFleet Gemini Sign In on your "
                                          "private Mac, then start a new thread.") from None
                if reason != SLOT_BUSY:
                    message = OPEN_SLOT_ERRORS.get(reason, OPEN_REFUSED)
                    if message == BAD_READY:
                        raise SessionRejected(INTERNAL_ERROR, message) from None
                    raise RpcError(INTERNAL_ERROR, message) from None
            self._retire_channel(graceful=False)
            now = time.monotonic()
            if deadline is None:
                deadline = now + BUSY_RETRY_WINDOW
            if now + delay > deadline:
                raise RpcError(INTERNAL_ERROR, "Another Gemini session is using your slot. "
                               "Close it before retrying.")
            if self.cancelled.wait(delay):
                raise EOFError
            delay = min(1.5, delay * 2)
        try:
            current, models, offered, tagged = self.host.editor.ready_session(ready, token)
        except ValueError:
            raise SessionRejected(INTERNAL_ERROR, BAD_READY) from None
        self.can_cancel_turn = tagged
        self.commands.connected(ready)
        self.offered_token = offered
        models = models or [current]
        if token is not None:
            # A restored conversation already contains a user's work. For new
            # conversations, opt in only upon the first real prompt, never discovery.
            with self.send_lock:
                self.channel.send({"type": "enableResume"})
        self.models = models
        self.current = current
        if previous_model is not None and previous_model != current:
            try:
                if previous_model not in models:
                    raise invalid("The previous Gemini model is unavailable. Select an offered "
                                  "model in T3 before sending your next instruction.")
                self.select_model(previous_model)
            except RpcError:
                self.current = previous_model
                raise

    def reconnect(self, *, allow_fresh):
        if not self.resume_token and not allow_fresh:
            raise RpcError(INTERNAL_ERROR, FRESH_REQUIRED)
        native_context = self.resume_token is not None
        with self.lock:
            self.reconnecting = True
        try:
            try:
                self.connect(self.resume_token)
            finally:
                with self.lock:
                    self.reconnecting = False
            if self.turn_cancelled.is_set():
                raise EOFError
        except (RpcError, ValueError, OSError, EOFError, subprocess.SubprocessError) as exc:
            self._retire_channel(graceful=False)
            if self.turn_cancelled.is_set() and not isinstance(exc, SessionRejected):
                raise RpcError(INTERNAL_ERROR,
                               "Reconnection interrupted before sending a prompt.") from None
            raise
        self.text("Reconnected to the previous Gemini conversation.\n\n" if native_context else
                  "Connected to a fresh Gemini conversation in this thread. Gemini starts "
                  "without the earlier conversation context. Send your next instruction.")

    def detach(self):
        self._retire_channel(graceful=False)
        self.commands.capable = False
        self.offered_token = None
        self.seen.clear()
        self.approved.clear()
        self.writing = self.local_tool = None

    def unavailable(self):
        return (self.cancelled.is_set() or self.turn_cancelled.is_set()
                or self.process is None or self.process.poll() is not None)

    def interrupt(self):
        with self.lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            proc = self.process
            if proc is None or proc.poll() is not None:
                return
            # An intentional close relinquishes the slot's short-lived resume lease.
            # Avoid blocking teardown if another writer already owns a stalled SSH pipe.
            if self.resume_token and self.send_lock.acquire(blocking=False):
                try:
                    with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
                        if select.select([], [proc.stdin], [], 0)[1]:
                            self.channel.send({"type": "close"})
                            proc.wait(timeout=0.5)
                finally:
                    self.send_lock.release()
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)

    def cancel_turn(self):
        with self.lock:
            if self.turn_cancelled.is_set() or self.cancelled.is_set():
                return
            self.turn_cancelled.set()
            if self.reconnecting:
                # No model prompt has been sent on this transport; the native slot can
                # still resume the old conversation using the outstanding lease.
                if self.process is not None and self.process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                return
            if self.can_cancel_turn:
                self.cancel_timer = threading.Timer(CANCEL_GRACE, self._expire_cancel)
                self.cancel_timer.daemon = True
                self.cancel_timer.start()
        if not self.can_cancel_turn:
            self.interrupt()
            return
        # The prompt and its cancel must reach the slot in order, even when T3 steers
        # before the worker has started writing. The watchdog can kill a stuck write.
        with self.send_lock:
            # EOF cleanup can retire a channel before the ACP turn has settled.
            # Snapshot that particular stream so a late Steer cannot target a lost channel.
            with self.lock:
                channel = self.channel
                turn = self.active_turn
            if channel is not None and turn is not None and not self.cancelled.is_set():
                try:
                    channel.send({"type": "cancel", "turn": turn})
                except (OSError, ValueError):
                    # If SSH fails while sending cancel, let the active worker report
                    # the uncertain turn and detach. Explicit session close is separate.
                    with self.lock:
                        proc = self.process if self.channel is channel else None
                        if proc is not None and proc.poll() is None:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(proc.pid, signal.SIGKILL)

    def _expire_cancel(self):
        with self.lock:
            if self.cancel_timer is not threading.current_thread():
                return
            # A silent transport is uncertain too. Only a fresh, authenticated
            # session/resume may establish that the original conversation survived.
            if not self.resume_token:
                self.cancelled.set()
            if self.process is not None and self.process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)

    def finish_turn(self):
        # Called under the bridge lock before accepting another ACP request.
        with self.lock:
            if self.cancel_timer is not None:
                self.cancel_timer.cancel()
                self.cancel_timer = None
            self.active_turn = None
            self.turn_cancelled.clear()

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
        self.resume_token = self.offered_token = None
        self.workspace.close()

    def send_request(self, message):
        try:
            self.channel.send(message)
        except BrokenPipeError:
            # A timed-out slot can send its final frame and close before our next
            # write. Inspect only that frame. Never retry a prompt or a model change.
            try:
                self.channel.receive(timeout=0.25)
            except (OSError, EOFError):
                pass
            raise

    def update(self, event, **fields):
        self.host.write({"method": "session/update", "params": {
            "sessionId": self.id, "update": {"sessionUpdate": event, **fields},
        }})

    def text(self, value):
        # Keep the whole proposed patch reviewable, including astral Unicode.
        for start in range(0, len(value), TEXT_CHUNK_CHARS):
            if self.turn_cancelled.is_set():
                return
            self.update("agent_message_chunk", content={
                "type": "text", "text": value[start:start + TEXT_CHUNK_CHARS],
            })

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
        if model != self.current and self.channel is None:
            self.current = model
            self.update("config_option_update", configOptions=self.config())
        elif model != self.current:
            self.send_request({"type": "set_model", "model": model})
            reply = self.channel.receive()
            if reply.get("type") != "model" or reply.get("model") not in self.models:
                raise ValueError("Gemini did not confirm the current model.")
            self.current = reply["model"]
            self.update("config_option_update", configOptions=self.config())
            if "error" in reply or self.current != model:
                raise RpcError(INTERNAL_ERROR, "The Gemini slot declined that selection.")
        return self.config()

    def permission(self, title, kind, location, content, *, automatic=False):
        if self.unavailable():
            return False
        self.commands.check_mutation()
        if self.commands.no_prompt and not automatic:
            raise self.host.editor.FullAccessUnavailable(
                "This operation is unavailable in full-access mode.")
        if automatic and (self.command_unavailable() or not self.commands.allowed()):
            return False
        tool = {"toolCallId": secrets.token_urlsafe(16), "title": title, "kind": kind,
                "status": "in_progress" if automatic else "pending",
                "locations": [{"path": str(location)}], "content": content}
        self.update("tool_call", **tool)
        tool_id = tool["toolCallId"]
        if automatic or self.host.ask(self, tool):
            return tool_id
        self.finish_tool(tool_id, False)
        return None

    def finish_tool(self, tool_id, success, result=None):
        if tool_id and not self.cancelled.is_set():
            fields = {}
            if result is not None:
                text = self.host.editor.terminal_text(result["output"])
                fields["content"] = [{"type": "content", "content": {
                    "type": "text", "text": f"Exit {result['exit']}\n{text}"}}]
            self.update("tool_call_update", toolCallId=tool_id,
                        status="completed" if success else "failed", **fields)

    def confirm_action(self, action, detail):
        if self.unavailable():
            return False
        if action == "Save file":
            return (self.writing is not None and self.writing[0] == detail
                    and (not self.writing[1]
                         or self.commands.allowed() and not self.command_unavailable()))
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
        self.local_tool = self.permission(title, kind, location, content,
                                          automatic=self.commands.allowed())
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
        automatic = self.commands.allowed()
        tool_id = self.permission(title, "edit", location, [{
            "type": "diff", "path": str(location), "oldText": before if item else None,
            "newText": content}], automatic=automatic)
        if not tool_id:
            return False
        self.approved[path] = (hashlib.sha256(content.encode()).digest(), expected, tool_id,
                               automatic)
        return True

    def command_unavailable(self):
        channel = self.channel
        if self.unavailable() or channel is None:
            return True
        try:
            return channel.disconnected()
        except (OSError, ValueError):
            return True

    def project_command(self, msg):
        label = self.commands.title(msg)
        title = label if len(label) <= 160 else label[:159] + "…"
        content = [{"type": "content", "content": {"type": "text", "text": label}}]
        tool_id = self.permission(title, "execute", self.workspace.path, content, automatic=True)
        if not tool_id:
            raise ValueError("Project command unavailable.")
        result = None
        try:
            result = self.commands.execute(msg, self.command_unavailable)
            return result
        finally:
            self.finish_tool(tool_id, result is not None and result["exit"] == 0, result)

    def file_action(self, msg):
        if self.unavailable():
            raise EOFError
        op = self.host.editor.request_operation(msg)
        if self.commands.grant is not None or op not in ("read", "list"):
            self.commands.check_mutation()
        if op in ("shell", "publish"):
            return self.project_command(msg)
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
            self.writing = (path, grant[3])
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
        if parts[0] == "/workspace":
            # A workspace change is a local management action, never an instruction
            # obtained by concatenating attachments, multiple blocks or multiline text.
            blocks = params["prompt"]
            if (len(blocks) != 1 or set(blocks[0]) != {"type", "text"}
                    or blocks[0]["type"] != "text" or any(c in prompt for c in "\r\n\x00")
                    or params.keys() - {"sessionId", "prompt"}):
                raise invalid("Send /workspace as one plain text message, without attachments.")
            try:
                selected, message = self.commands.select_workspace(
                    self.workspace, parts[1] if len(parts) == 2 else "")
            except self.host.editor.FullAccessUnavailable as exc:
                raise invalid(str(exc)) from None
            except (ValueError, OSError, RuntimeError):
                raise invalid("This local workspace could not be selected.") from None
            if selected is not self.workspace:
                self.seen.clear()
                self.approved.clear()
                self.writing = self.local_tool = None
                self.workspace = selected
                self.commands.retire_stale_input(self)
            self.text(message)
            return {"stopReason": "end_turn"}
        if parts[0] == "/help":
            self.text("Use T3’s model picker or /model to see available Gemini models. "
                      "Use Supervised mode to review changes in the default editor. "
                      "For project commands, run ccfleet gemini --full-access in a local "
                      "terminal. With all-projects access enabled on this Mac, "
                      "use /workspace ~/path/to/project to select a project or /workspace to show "
                      "the active folder. Revoke with ccfleet gemini --revoke-full-access. "
                      "If the private connection "
                      "drops, /reconnect checks it without sending a prompt.")
            return {"stopReason": "end_turn"}
        if (self.resume_token and self.process is not None and self.process.poll() is not None):
            # A previously completed turn has no unconfirmed prompt. This new instruction
            # can open a fresh authenticated transport before it is submitted for the first time.
            self.detach()
        if parts[0] == "/reconnect":
            if len(parts) != 1:
                raise invalid("Send /reconnect by itself. It does not submit a model prompt.")
            if self.process is not None and self.process.poll() is not None:
                self.detach()
            if self.channel is None:
                self.reconnect(allow_fresh=True)
            else:
                self.text("The Gemini connection is already open for this thread.")
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
        self.commands.retire_stale_input(self)
        blocked = self.commands.blocked_workspace()
        if blocked:
            self.text(blocked)
            return {"stopReason": "end_turn"}
        if self.channel is None:
            self.reconnect(allow_fresh=False)
        request = {"type": "prompt", "text": prompt}
        try:
            with self.commands.turn(request):
                result = self.prompt_turn(request)
                if result["stopReason"] != "cancelled":
                    self.commands.switched = False
                return result
        except self.host.editor.FullAccessUnavailable as exc:
            raise invalid(str(exc)) from None

    def prompt_turn(self, request):
        ids = set()
        with self.send_lock:
            if self.turn_cancelled.is_set() or self.cancelled.is_set():
                return {"stopReason": "cancelled"}
            if self.offered_token and self.resume_token is None:
                self.resume_token = self.offered_token
                self.channel.send({"type": "enableResume"})
            if self.can_cancel_turn:
                self.turn_number += 1
                self.active_turn = self.turn_number
                request["turn"] = self.active_turn
            self.send_request(request)
        try:
            while not self.cancelled.is_set():
                msg = self.channel.receive()
                kind = msg.get("type")
                if kind == "end" and isinstance(msg.get("reason"), str):
                    if self.can_cancel_turn and (type(msg.get("turn")) is not int
                                                 or msg["turn"] != self.active_turn):
                        raise ValueError("Unexpected Gemini turn completion.")
                    reason = msg["reason"]
                    return {"stopReason": {
                        "refused": "refusal", "refusal": "refusal", "end_turn": "end_turn",
                        "cancelled": "cancelled", "max_tokens": "max_tokens",
                        "max_turn_requests": "max_turn_requests",
                    }.get(reason, "refusal")}
                if kind == "text" and isinstance(msg.get("text"), str):
                    self.text(msg["text"])
                    continue
                if kind == "progress":
                    if (not self.resume_token or not self.can_cancel_turn
                            or set(msg) != {"type", "turn"} or type(msg["turn"]) is not int
                            or msg["turn"] != self.active_turn):
                        raise ValueError("Unexpected progress frame.")
                    continue
                ident = msg.get("id")
                if (kind != "request" or type(ident) is not int or ident <= 0
                        or ident in ids or len(ids) >= 4096):
                    raise ValueError("Unexpected slot record.")
                ids.add(ident)
                response = {"type": "response", "id": ident}
                try:
                    response["result"] = self.file_action(msg)
                except self.host.editor.FullAccessUnavailable as exc:
                    self.text(str(exc))
                    response["error"] = str(exc)
                except (EOFError, OSError, ValueError, subprocess.TimeoutExpired):
                    response["error"] = "Local operation declined, protected or unavailable."
                # A slot-side callback may still be awaiting this reply after cancel.
                with self.send_lock:
                    if self.turn_cancelled.is_set():
                        response = {"type": "response", "id": ident, "error": "Turn cancelled."}
                    if not self.cancelled.is_set():
                        self.channel.send(response)
            return {"stopReason": "cancelled"}
        finally:
            for _, _, tool_id, _ in self.approved.values():
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
            if self.closed or session.unavailable():
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

    def interrupt(self, *, turn_only=False):
        if self.session:
            if turn_only:
                self.session.cancel_turn()
            else:
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
        link_notice = None
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
        except SessionRejected as exc:
            broken, error = True, exc
        except RpcError as exc:
            error = exc
        except (OSError, EOFError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            recoverable = (isinstance(exc, (OSError, EOFError, subprocess.TimeoutExpired))
                           or isinstance(exc, ValueError) and str(exc) in INTERRUPTED_SLOT_ERRORS)
            if (recoverable and method != "session/new" and not session.cancelled.is_set()
                    and (not session.turn_cancelled.is_set() or session.resume_token)):
                # EOF can arrive after Steer was sent but before its acknowledgement.
                # Keep only the separately authorized resume handle, never replay the turn.
                session.detach()
                status = ("The slot reported that this Gemini session timed out. "
                          if str(exc) == SLOT_SESSION_TIMEOUT else "")
                recovery = ("Send your next instruction here to resume Gemini’s conversation."
                            if session.resume_token else FRESH_REQUIRED)
                summary = (TURN_LOST if method == "session/prompt" else
                           "The Gemini connection was interrupted before confirming the model.")
                link_notice = status + summary + " " + recovery
                error = RpcError(INTERNAL_ERROR, link_notice)
            elif isinstance(exc, ValueError) and str(exc) in SLOT_LOGIN_REQUIRED:
                broken = True
                error = SessionRejected(AUTH_REQUIRED, "Open CCFleet Gemini Sign In on your "
                                        "private Mac, then start a new thread.")
            else:
                broken = True
                error = RpcError(INTERNAL_ERROR, "The private Gemini connection ended. "
                                 "Open a new T3 thread.")
        finally:
            with self.lock:
                try:
                    if session.cancelled.is_set() and method == "session/new":
                        result = None
                        error = RpcError(INTERNAL_ERROR,
                                         "Gemini startup was cancelled by the editor.")
                    if method == "session/prompt":
                        steered = session.turn_cancelled.is_set()
                        session.finish_turn()
                        if steered or session.cancelled.is_set():
                            result, error = {"stopReason": "cancelled"}, None
                            if steered and not self.closed and link_notice:
                                session.text(link_notice)
                            elif (steered and not self.closed
                                  and (broken or session.cancelled.is_set())):
                                if session.can_cancel_turn:
                                    session.text("The slot did not confirm the interruption. "
                                                 "Open a new T3 thread to reconnect.")
                                else:
                                    session.text("This slot needs the steering update to keep the "
                                                 "conversation. Open a new T3 thread.")
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
                    self.interrupt(turn_only=True)
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
                    raise RpcError(INTERNAL_ERROR, "Initialize the ACP connection first.")
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
                    raise RpcError(INTERNAL_ERROR, "A Gemini request is in progress.")
                if method == "session/new":
                    if self.session is not None:
                        raise RpcError(INTERNAL_ERROR,
                                   "This connection already has an open Gemini thread.")
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
